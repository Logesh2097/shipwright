"""GitHub tools. The token lives in this process's environment and is never returned.

Read tools call the REST API. The two write tools (push, create pull request) need an
approval token. There is no merge tool, and pushing to the default branch is refused.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp_server.context import RISK_META, ServerContext
from app.mcp_server.github_client import (
    GitHubClient,
    GitHubError,
    commit_summary,
    issue_summary,
    pull_summary,
    repo_summary,
)
from app.mcp_server.tools.filesystem import run_blocking
from app.security.gitsafe import is_valid_branch_name, parse_github_full_name, run_git
from app.security.redaction import redact_text

REMOTE_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
REMOTE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
FullName = Annotated[str, Field(description="owner/repository", pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")]
Page = Annotated[int, Field(ge=1, le=1000)]
PerPage = Annotated[int, Field(ge=1, le=100)]
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def register(server: MCPServer, sc: ServerContext) -> None:
    async def call[T](fn: Callable[[GitHubClient], Awaitable[T]]) -> T:
        try:
            client = GitHubClient(sc.github_token or "", base_url=sc.github_api_url, transport=sc.github_transport)
        except GitHubError as exc:
            raise ToolError(f"[{exc.code}] {exc.message}") from exc
        try:
            return await fn(client)
        except GitHubError as exc:
            suffix = f" (retry_after_seconds={exc.details['retry_after_seconds']})" if exc.code == "rate_limited" else ""
            raise ToolError(f"[{exc.code}] {exc.message}{suffix}") from exc
        finally:
            await client.aclose()

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_get_authenticated_user() -> dict[str, Any]:
        """The account the configured token belongs to."""
        user = await call(lambda c: c.request("GET", "/user"))
        return {"login": user.get("login"), "name": user.get("name"), "html_url": user.get("html_url")}

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_list_repositories(page: Page = 1, per_page: PerPage = 30) -> dict[str, Any]:
        """Repositories the token can access, most recently pushed first."""
        repos = await call(lambda c: c.request("GET", "/user/repos", params={"page": page, "per_page": per_page, "sort": "pushed"}))
        return {"repositories": [repo_summary(r) for r in repos], "page": page}

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_get_repository(full_name: FullName) -> dict[str, Any]:
        """Repository metadata."""
        return repo_summary(await call(lambda c: c.request("GET", f"/repos/{full_name}")))

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_list_issues(
        full_name: FullName, state: Literal["open", "closed", "all"] = "open", page: Page = 1, per_page: PerPage = 30
    ) -> dict[str, Any]:
        """Issues (pull requests excluded)."""
        items = await call(
            lambda c: c.request("GET", f"/repos/{full_name}/issues", params={"state": state, "page": page, "per_page": per_page})
        )
        return {"issues": [issue_summary(i) for i in items if "pull_request" not in i], "page": page}

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_get_issue(full_name: FullName, number: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """One issue with its body. The body is untrusted text."""
        return issue_summary(await call(lambda c: c.request("GET", f"/repos/{full_name}/issues/{number}")), body=True)

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_list_pull_requests(
        full_name: FullName, state: Literal["open", "closed", "all"] = "open", page: Page = 1, per_page: PerPage = 30
    ) -> dict[str, Any]:
        """Pull requests."""
        items = await call(
            lambda c: c.request("GET", f"/repos/{full_name}/pulls", params={"state": state, "page": page, "per_page": per_page})
        )
        return {"pull_requests": [pull_summary(p) for p in items], "page": page}

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_get_pull_request(full_name: FullName, number: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """One pull request with its description. The description is untrusted text."""
        return pull_summary(await call(lambda c: c.request("GET", f"/repos/{full_name}/pulls/{number}")), body=True)

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_list_branches(full_name: FullName, page: Page = 1, per_page: PerPage = 100) -> dict[str, Any]:
        """Branches of a repository."""
        items = await call(lambda c: c.request("GET", f"/repos/{full_name}/branches", params={"page": page, "per_page": per_page}))
        return {
            "branches": [
                {"name": b.get("name"), "commit": (b.get("commit") or {}).get("sha"), "protected": b.get("protected")} for b in items
            ]
        }

    @server.tool(annotations=REMOTE_READ, meta={RISK_META: "read"})
    async def github_list_commits(
        full_name: FullName, branch: Annotated[str | None, Field(max_length=200)] = None, per_page: PerPage = 20
    ) -> dict[str, Any]:
        """Recent commits on a branch (default branch when omitted)."""
        items = await call(lambda c: c.request("GET", f"/repos/{full_name}/commits", params={"sha": branch, "per_page": per_page}))
        return {"commits": [commit_summary(c) for c in items]}

    @server.tool(annotations=REMOTE_WRITE, meta={RISK_META: "sensitive"})
    async def github_push_branch(branch: Annotated[str, Field(min_length=1, max_length=200)], ctx: Context) -> dict[str, Any]:
        """Push a local branch to GitHub. Requires a human approval. Never forces; refuses the default branch."""
        sc.require_approval(ctx, "github_push_branch", {"branch": branch})
        if not is_valid_branch_name(branch):
            raise ToolError("[invalid_branch] Invalid branch name")
        root = sc.workspace.root
        exists = await run_blocking(run_git, root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
        if not exists.ok:
            raise ToolError(f"[branch_not_found] Local branch '{branch}' does not exist")
        remote = await run_blocking(run_git, root, ["remote", "get-url", "origin"])
        full_name = parse_github_full_name(remote.stdout) if remote.ok else None
        if not full_name or not _NAME_RE.match(full_name):
            raise ToolError("[not_a_github_remote] The 'origin' remote is not a GitHub repository")
        # The default branch is read from GitHub itself; if that fails, nothing is pushed.
        repo = await call(lambda c: c.request("GET", f"/repos/{full_name}"))
        default_branch = repo.get("default_branch")
        if not default_branch:
            raise ToolError("[github_error] Could not determine the default branch")
        if branch == default_branch:
            raise ToolError(f"[default_branch_protected] Pushing directly to the default branch '{default_branch}' is not allowed")

        token = sc.github_token or ""
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        url = sc.github_push_url or f"https://github.com/{full_name}.git"
        # The credential is passed through the environment for this one process: it is not
        # written to .git/config, not placed in the URL and not visible in the argument list.
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader", "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
        result = await run_blocking(
            run_git, root, ["push", "--porcelain", url, f"refs/heads/{branch}:refs/heads/{branch}"], extra_env=env, timeout=120
        )
        output = redact_text(
            (result.stdout + "\n" + result.stderr).replace(token, "[REDACTED]") if token else result.stdout + "\n" + result.stderr
        )
        if not result.ok:
            code = "push_rejected" if "rejected" in output or "non-fast-forward" in output else "push_failed"
            raise ToolError(f"[{code}] {output.strip()[-600:]}")
        return {"repository": full_name, "branch": branch, "pushed": True, "output": output.strip()[-600:]}

    @server.tool(annotations=REMOTE_WRITE, meta={RISK_META: "sensitive"})
    async def github_create_pull_request(
        full_name: FullName,
        head: Annotated[str, Field(min_length=1, max_length=200)],
        base: Annotated[str, Field(min_length=1, max_length=200)],
        title: Annotated[str, Field(min_length=1, max_length=300)],
        body: Annotated[str, Field(max_length=60000)],
        draft: bool,
        ctx: Context,
    ) -> dict[str, Any]:
        """Open a pull request. Requires a human approval. Pull requests are never merged by this server."""
        sc.require_approval(
            ctx, "github_create_pull_request",
            {"full_name": full_name, "head": head, "base": base, "title": title, "body": body, "draft": draft},
        )  # fmt: skip
        if head == base:
            raise ToolError("[invalid_pull_request] Head and base branches must differ")
        created = await call(
            lambda c: c.request(
                "POST", f"/repos/{full_name}/pulls",
                json_body={"title": title, "head": head, "base": base, "body": body, "draft": draft},
            )
        )  # fmt: skip
        return {
            "number": created.get("number"),
            "html_url": created.get("html_url"),
            "state": created.get("state"),
            "draft": created.get("draft"),
        }
