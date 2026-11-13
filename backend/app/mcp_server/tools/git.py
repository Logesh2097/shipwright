"""Git tools. Read tools are unrestricted; write tools need an approval token.

There is deliberately no tool for reset, clean, checkout of files, rebase, merge, force
push or configuration changes.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp_server.context import RISK_META, ServerContext
from app.mcp_server.tools.filesystem import READ, run_blocking
from app.security.gitsafe import GitResult, is_safe_ref, is_valid_branch_name, run_git
from app.security.paths import is_secret_path, normalize_relative
from app.security.redaction import redact_text

MAX_DIFF_CHARS = 200_000
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def _cap(text: str, limit: int = MAX_DIFF_CHARS) -> tuple[str, bool]:
    redacted = redact_text(text)
    return (redacted[:limit], True) if len(redacted) > limit else (redacted, False)


def parse_status(output: str) -> dict[str, Any]:
    branch = None
    tracking = None
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if line.startswith("## "):
            head = line[3:]
            if head.startswith("No commits yet on "):
                branch = head.removeprefix("No commits yet on ")
            else:
                branch, _, tracking_part = head.partition("...")
                tracking = tracking_part or None
            continue
        if len(line) < 4:
            continue
        index, worktree, path = line[0], line[1], line[3:]
        if " -> " in path and index in "RC":
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        state = (
            "untracked" if index == "?" else
            "conflicted" if "U" in (index, worktree) or (index == worktree and index in "AD") else
            "staged" if worktree == " " else
            "modified" if index == " " else "staged+modified"
        )  # fmt: skip
        entries.append({"path": path, "index": index, "worktree": worktree, "state": state})
    return {"branch": branch, "tracking": tracking, "clean": not entries, "entries": entries}


def register(server: MCPServer, sc: ServerContext) -> None:
    def git(args: list[str], **kwargs: Any) -> GitResult:
        return run_git(sc.workspace.root, args, **kwargs)

    def checked_path(path: str) -> str:
        normalized = normalize_relative(path)
        sc.workspace.resolve(normalized)
        return normalized

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def git_status() -> dict[str, Any]:
        """Current branch and the state of changed, staged and untracked files."""
        result = await run_blocking(git, ["status", "--porcelain=v1", "--branch", "--untracked-files=all"], check=True)
        return parse_status(result.stdout)

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def git_diff(
        path: Annotated[str | None, Field(max_length=1000)] = None,
        staged: bool = False,
        ref: Annotated[str | None, Field(description="Compare the working tree against this commit or branch", max_length=200)] = None,
    ) -> dict[str, Any]:
        """Unified diff of uncommitted changes (or against a ref)."""
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if staged:
            args.append("--cached")
        if ref:
            if not is_safe_ref(ref):
                raise ToolError("[invalid_ref] Invalid ref")
            args.append(ref)
        args.append("--")
        if path:
            args.append(await run_blocking(checked_path, path))
        result = await run_blocking(git, args, check=True)
        diff, truncated = _cap(result.stdout)
        return {"diff": diff, "truncated": truncated, "empty": not result.stdout.strip()}

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def git_log(
        max_count: Annotated[int, Field(ge=1, le=200)] = 20,
        path: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> dict[str, Any]:
        """Recent commits, optionally limited to one path."""
        args = ["log", f"-n{max_count}", "--pretty=format:%H%x1f%an%x1f%aI%x1f%s%x1e", "--"]
        if path:
            args.append(await run_blocking(checked_path, path))
        result = await run_blocking(git, args)
        if not result.ok:
            if "does not have any commits yet" in result.stderr:
                return {"commits": []}
            raise ToolError(f"[git_error] {result.stderr.strip()[:300]}")
        commits = []
        for record in result.stdout.split("\x1e"):
            fields = record.strip().split("\x1f")
            if len(fields) == 4:
                commits.append({"sha": fields[0], "author": fields[1], "date": fields[2], "subject": redact_text(fields[3])})
        return {"commits": commits}

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def git_show(
        ref: Annotated[str, Field(min_length=1, max_length=200)],
        path: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> dict[str, Any]:
        """Show a commit (message, stat and patch), optionally limited to one path."""
        if not is_safe_ref(ref):
            raise ToolError("[invalid_ref] Invalid ref")
        args = ["show", "--no-ext-diff", "--no-textconv", "--no-color", "--stat", "--patch", ref, "--"]
        if path:
            args.append(await run_blocking(checked_path, path))
        result = await run_blocking(git, args, check=True)
        output, truncated = _cap(result.stdout)
        return {"ref": ref, "output": output, "truncated": truncated}

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def git_branches() -> dict[str, Any]:
        """Local and remote-tracking branches."""
        result = await run_blocking(
            git, ["branch", "-a", "--format=%(refname:short)%1f%(objectname:short)%1f%(HEAD)%1f%(upstream:short)"], check=True
        )
        branches = []
        current = None
        for line in result.stdout.splitlines():
            fields = line.split("\x1f")
            if len(fields) != 4 or fields[0].endswith("/HEAD"):
                continue
            is_current = fields[2] == "*"
            if is_current:
                current = fields[0]
            branches.append(
                {"name": fields[0], "commit": fields[1], "current": is_current, "upstream": fields[3] or None,
                 "remote": fields[0].startswith("origin/")}
            )  # fmt: skip
        return {"current": current, "branches": branches}

    @server.tool(annotations=WRITE, meta={RISK_META: "sensitive"})
    async def git_create_branch(
        name: Annotated[str, Field(min_length=1, max_length=200)],
        checkout: bool,
        ctx: Context,
    ) -> dict[str, Any]:
        """Create a branch from the current HEAD (and switch to it). Requires a human approval."""
        sc.require_approval(ctx, "git_create_branch", {"name": name, "checkout": checkout})
        if not is_valid_branch_name(name):
            raise ToolError("[invalid_branch] Invalid branch name")
        exists = await run_blocking(git, ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"])
        if exists.ok:
            raise ToolError(f"[branch_exists] Branch '{name}' already exists")
        # "switch -c" carries uncommitted changes over; it never discards work.
        args = ["switch", "-c", name] if checkout else ["branch", name]
        result = await run_blocking(git, args)
        if not result.ok:
            raise ToolError(f"[git_error] {result.stderr.strip()[:300]}")
        return {"branch": name, "checked_out": checkout}

    @server.tool(annotations=WRITE, meta={RISK_META: "sensitive"})
    async def git_commit(
        paths: Annotated[list[str], Field(min_length=1, max_length=200)],
        message: Annotated[str, Field(min_length=3, max_length=5000)],
        author_name: Annotated[str, Field(min_length=1, max_length=200)],
        author_email: Annotated[str, Field(min_length=3, max_length=320)],
        ctx: Context,
    ) -> dict[str, Any]:
        """Stage exactly the listed paths and commit them. Requires a human approval. Hooks are not run."""
        sc.require_approval(
            ctx, "git_commit",
            {"paths": paths, "message": message, "author_name": author_name, "author_email": author_email},
        )  # fmt: skip
        normalized: list[str] = []
        for path in paths:
            rel = await run_blocking(checked_path, path)
            if rel == "." or is_secret_path(rel):
                raise ToolError(f"[path_not_allowed] Cannot commit '{path}'")
            normalized.append(rel)
        identity = {
            "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
        }  # fmt: skip
        added = await run_blocking(git, ["add", "--", *normalized])
        if not added.ok:
            raise ToolError(f"[git_error] {added.stderr.strip()[:300]}")
        committed = await run_blocking(git, ["commit", "-m", message, "--only", "--", *normalized], extra_env=identity)
        if not committed.ok:
            detail = (committed.stderr.strip() or committed.stdout.strip())[:300]
            code = (
                "nothing_to_commit"
                if "nothing to commit" in (committed.stdout + committed.stderr) or "no changes added" in committed.stdout
                else "git_error"
            )
            raise ToolError(f"[{code}] {detail}")
        sha = (await run_blocking(git, ["rev-parse", "HEAD"], check=True)).stdout.strip()
        branch = (await run_blocking(git, ["symbolic-ref", "--short", "-q", "HEAD"])).stdout.strip() or None
        return {"commit": sha, "branch": branch, "paths": normalized}
