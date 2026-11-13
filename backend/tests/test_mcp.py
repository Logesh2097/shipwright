"""Phase 4: MCP server tools, client session management, gateway policy and logging."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from sqlalchemy import select

from app.db.models import AuditLog, Repository, ToolExecution
from app.mcp_client.gateway import CallContext
from app.mcp_client.manager import McpSessionManager, SessionKey, SessionSpec
from app.mcp_client.policy import TOOL_CATALOG
from app.mcp_server.context import ServerContext
from app.mcp_server.server import build_server
from app.runtime import Runtime
from app.sandbox.runner import DockerSandbox, SandboxConfig, SandboxRequest, docker_status
from app.security.approval_tokens import META_KEY, mint_approval_token

SECRET = "test-approval-secret"


def _data(result: Any) -> Any:
    return json.loads(result.content[0].text)


def _error(result: Any) -> str:
    assert result.is_error, result.content
    return result.content[0].text


@pytest.fixture
def server_context(repo_path: Path) -> ServerContext:
    return ServerContext(workspace_root=repo_path, approval_secret=SECRET, sandbox_config=SandboxConfig(mode="local"))


def _token(repo: Path, tool: str, args: dict[str, Any], **kwargs: Any) -> dict[str, str]:
    return {META_KEY: mint_approval_token(SECRET, tool=tool, arguments=args, workspace=str(repo.resolve()), approval_id="a", **kwargs)}


async def test_catalog_matches_server_declarations(server_context: ServerContext) -> None:
    """The backend's policy table and the risk levels the servers declare must agree, tool for tool."""
    declared: dict[str, str] = {}
    async with Client(build_server(server_context)) as client:
        for tool in (await client.list_tools()).tools:
            declared[tool.name] = (tool.meta or {})["asea/risk"]
            assert tool.description, tool.name
    github = ServerContext(toolsets=("github",), approval_secret=SECRET, github_token="t")
    async with Client(build_server(github)) as client:
        for tool in (await client.list_tools()).tools:
            declared[tool.name] = (tool.meta or {})["asea/risk"]
    expected = {name: spec.risk for name, spec in TOOL_CATALOG.items() if spec.server != "internal"}
    assert declared == expected


async def test_filesystem_tools_and_confinement(server_context: ServerContext, repo_path: Path) -> None:
    async with Client(build_server(server_context)) as client:
        listing = _data(await client.call_tool("fs_list_directory", {}))
        assert ".env" not in {e["name"] for e in listing["entries"]}
        read = _data(await client.call_tool("fs_read_file", {"path": "app/pricing.py", "start_line": 9, "max_lines": 3}))
        assert read["start_line"] == 9 and read["end_line"] == 11 and read["truncated"] is True
        assert "[secret_file]" in _error(await client.call_tool("fs_read_file", {"path": ".env"}))
        assert "[path_not_allowed]" in _error(await client.call_tool("fs_read_file", {"path": "../x"}))
        assert "[path_not_allowed]" in _error(await client.call_tool("fs_read_file", {"path": "/etc/passwd"}))
        assert "[git_internal]" in _error(await client.call_tool("fs_read_file", {"path": ".git/HEAD"}))
        (repo_path / "escape").symlink_to("/etc")
        assert "[path_escape]" in _error(await client.call_tool("fs_read_file", {"path": "escape/passwd"}))
        assert "[path_escape]" in _error(await client.call_tool("fs_list_directory", {"path": "escape"}))
        # Secrets inside ordinary files are redacted on the way out.
        (repo_path / "settings.py").write_text('API_KEY = "ghp_' + "a1B2" * 9 + '"\nDEBUG = True\n')
        leaked = _data(await client.call_tool("fs_read_file", {"path": "settings.py"}))
        assert "ghp_" not in leaked["content"] and leaked["redacted"] is True and "DEBUG = True" in leaked["content"]
        (repo_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
        assert "[binary]" in _error(await client.call_tool("fs_read_file", {"path": "blob.bin"}))
        found = _data(await client.call_tool("fs_find_files", {"pattern": "*.vue"}))
        assert len(found["files"]) == 3
        hits = _data(await client.call_tool("fs_search_text", {"query": "def apply_\\w+", "regex": True, "path_glob": "*.py"}))
        assert [m["path"] for m in hits["matches"]] == ["app/pricing.py"]
        assert "validation error" in _error(await client.call_tool("fs_read_file", {"path": "a", "max_lines": 999999}))
        assert "validation error" in _error(await client.call_tool("fs_read_file", {}))


async def test_write_tools_require_valid_approval_token(server_context: ServerContext, repo_path: Path) -> None:
    args = {"path": "notes/new.txt", "content": "hello\n", "expected_sha256": "absent"}
    async with Client(build_server(server_context)) as client:
        assert "[approval_required]" in _error(await client.call_tool("fs_write_file", args))
        assert "[approval_required]" in _error(await client.call_tool("fs_write_file", args, meta={META_KEY: "forged.token"}))
        other = _token(repo_path, "fs_write_file", {**args, "content": "different"})
        assert "does not match these arguments" in _error(await client.call_tool("fs_write_file", args, meta=other))
        expired = _token(repo_path, "fs_write_file", args, ttl_seconds=-1)
        assert "expired" in _error(await client.call_tool("fs_write_file", args, meta=expired))
        assert not (repo_path / "notes").exists()

        written = _data(await client.call_tool("fs_write_file", args, meta=_token(repo_path, "fs_write_file", args)))
        assert written["created"] is True and (repo_path / "notes/new.txt").read_text() == "hello\n"

        # Stale detection: the file changed after the hash was taken.
        update = {"path": "notes/new.txt", "content": "v2\n", "expected_sha256": written["sha256"]}
        (repo_path / "notes/new.txt").write_text("edited by the developer\n")
        assert "[stale]" in _error(await client.call_tool("fs_write_file", update, meta=_token(repo_path, "fs_write_file", update)))
        assert (repo_path / "notes/new.txt").read_text() == "edited by the developer\n"

        # Secret files and paths outside the workspace cannot be written even with a token.
        for path in (".env", "../outside.txt", ".git/hooks/pre-commit"):
            bad = {"path": path, "content": "x", "expected_sha256": "absent"}
            assert "Error executing tool" in _error(
                await client.call_tool("fs_write_file", bad, meta=_token(repo_path, "fs_write_file", bad))
            )
        assert (repo_path / ".env").read_text().startswith("DATABASE_PASSWORD")

        delete = {"path": "README.md", "expected_sha256": "0" * 64}
        assert "[stale]" in _error(await client.call_tool("fs_delete_file", delete, meta=_token(repo_path, "fs_delete_file", delete)))
        assert (repo_path / "README.md").exists()


async def test_git_tools(server_context: ServerContext, repo_path: Path) -> None:
    (repo_path / "app" / "pricing.py").write_text((repo_path / "app" / "pricing.py").read_text() + "\n# note\n")
    (repo_path / "new_file.py").write_text("x = 1\n")
    async with Client(build_server(server_context)) as client:
        status = _data(await client.call_tool("git_status", {}))
        states = {e["path"]: e["state"] for e in status["entries"]}
        assert states == {"app/pricing.py": "modified", "new_file.py": "untracked", "notes": "untracked"} or states == {
            "app/pricing.py": "modified",
            "new_file.py": "untracked",
        }
        diff = _data(await client.call_tool("git_diff", {"path": "app/pricing.py"}))
        assert "+# note" in diff["diff"]
        assert "[invalid_ref]" in _error(await client.call_tool("git_diff", {"ref": "--output=/tmp/pwn"}))
        assert _data(await client.call_tool("git_branches", {}))["current"] == "main"
        shown = _data(await client.call_tool("git_show", {"ref": "HEAD"}))
        assert "Initial sample project" in shown["output"]

        branch = {"name": "feature/x", "checkout": True}
        assert "[approval_required]" in _error(await client.call_tool("git_create_branch", branch))
        assert (
            _data(await client.call_tool("git_create_branch", branch, meta=_token(repo_path, "git_create_branch", branch)))["branch"]
            == "feature/x"
        )
        # Switching branches carried the uncommitted work over instead of discarding it.
        assert "# note" in (repo_path / "app" / "pricing.py").read_text() and (repo_path / "new_file.py").exists()

        commit = {"paths": ["app/pricing.py"], "message": "Add note", "author_name": "Dev", "author_email": "dev@example.com"}
        assert "[approval_required]" in _error(await client.call_tool("git_commit", commit))
        done = _data(await client.call_tool("git_commit", commit, meta=_token(repo_path, "git_commit", commit)))
        assert done["branch"] == "feature/x" and done["paths"] == ["app/pricing.py"]
        # Only the listed path was committed; the other file is still untracked.
        after = {e["path"]: e["state"] for e in _data(await client.call_tool("git_status", {}))["entries"]}
        assert after.get("new_file.py") == "untracked" and "app/pricing.py" not in after
        log = _data(await client.call_tool("git_log", {"max_count": 1}))
        assert log["commits"][0]["subject"] == "Add note" and log["commits"][0]["author"] == "Dev"
        secret = {"paths": [".env"], "message": "oops", "author_name": "Dev", "author_email": "dev@example.com"}
        assert "Error executing tool" in _error(await client.call_tool("git_commit", secret, meta=_token(repo_path, "git_commit", secret)))
        names = {t.name for t in (await client.list_tools()).tools}
        assert not names & {"git_reset", "git_clean", "git_checkout", "git_merge", "git_push", "git_rebase"}


async def test_git_ignores_hostile_repository_hooks(server_context: ServerContext, repo_path: Path) -> None:
    marker = repo_path.parent / f"pwned-{uuid.uuid4().hex}"
    hook = repo_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    (repo_path / "README.md").write_text("changed\n")
    commit = {"paths": ["README.md"], "message": "Update readme", "author_name": "Dev", "author_email": "dev@example.com"}
    async with Client(build_server(server_context)) as client:
        _data(await client.call_tool("git_commit", commit, meta=_token(repo_path, "git_commit", commit)))
    assert not marker.exists()


async def test_terminal_and_testing_tools(server_context: ServerContext, repo_path: Path) -> None:
    async with Client(build_server(server_context)) as client:
        assert "[command_denied]" in _error(await client.call_tool("terminal_run", {"argv": ["curl", "http://example.com"]}))
        assert "[approval_required]" in _error(await client.call_tool("terminal_run", {"argv": ["npm", "ci"]}))
        sensitive = {"argv": ["pip", "install", "requests"], "timeout_seconds": 60, "directory": "."}
        assert "[approval_required]" in _error(await client.call_tool("terminal_run_sensitive", sensitive))
        denied = {"argv": ["rm", "-rf", "/"], "timeout_seconds": 60, "directory": "."}
        token = _token(repo_path, "terminal_run_sensitive", denied)
        assert "[command_denied]" in _error(await client.call_tool("terminal_run_sensitive", denied, meta=token))

        ran = _data(await client.call_tool("terminal_run", {"argv": ["python", "--version"]}))
        assert ran["exit_code"] == 0 and ran["sandbox_mode"] == "local"

        result = _data(await client.call_tool("test_run", {"framework": "pytest"}, read_timeout_seconds=120))
        assert result["status"] == "failed"
        assert result["totals"] == {"passed": 4, "failed": 2, "skipped": 1, "error": 0, "not_executed": 0}
        failed = {c["node_id"] for c in result["cases"] if c["outcome"] == "failed"}
        assert failed == {"tests/test_pricing.py::test_ten_percent_discount", "tests/test_pricing.py::test_order_total_with_discount"}
        assert all(c["message"] for c in result["cases"] if c["outcome"] in ("failed", "skipped"))

        targeted = _data(
            await client.call_tool(
                "test_run", {"framework": "pytest", "targets": ["tests/test_auth.py", "tests/missing_test.py"]}, read_timeout_seconds=120
            )
        )
        assert targeted["totals"]["not_executed"] == 1 and targeted["totals"]["passed"] == 2
        assert "[invalid_target]" in _error(await client.call_tool("test_run", {"framework": "pytest", "targets": ["--collect-only"]}))
        assert "Error executing tool" in _error(await client.call_tool("test_run", {"framework": "pytest", "targets": ["../../x"]}))

        vitest = _data(await client.call_tool("test_run", {"framework": "vitest", "directory": "frontend"}))
        assert vitest["status"] == "not_executed" and vitest["totals"]["not_executed"] == 1 and "node_modules" in vitest["reason"]

        lint = _data(await client.call_tool("lint_run", {"tool": "ruff", "targets": ["app/pricing.py"]}, read_timeout_seconds=120))
        assert lint["status"] == "failed" and any(i["code"] == "F401" and i["file"] == "app/pricing.py" for i in lint["issues"])
        discover = _data(await client.call_tool("test_discover", {}))
        assert discover["test_files"] == ["tests/test_auth.py", "tests/test_pricing.py"]


async def test_command_timeout_is_enforced(server_context: ServerContext, repo_path: Path) -> None:
    (repo_path / "tests" / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(60)\n")
    async with Client(build_server(server_context)) as client:
        result = _data(
            await client.call_tool(
                "test_run", {"framework": "pytest", "targets": ["tests/test_slow.py"], "timeout_seconds": 10}, read_timeout_seconds=60
            )
        )
    assert result["status"] == "timeout" and result["timed_out"] is True and result["totals"]["not_executed"] == 1
    assert result["duration_ms"] < 20_000


@pytest.mark.docker
async def test_docker_sandbox_isolation(repo_path: Path) -> None:
    ok, detail = docker_status("asea-sandbox:latest", ttl=0)
    if not ok:
        pytest.skip(f"docker sandbox unavailable: {detail}")
    sandbox = DockerSandbox(repo_path, SandboxConfig(mode="docker", memory_mb=256, pids_limit=64))
    command = sandbox.build_command(SandboxRequest(argv=["true"]), "n")
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges", "--pids-limit", "--memory"):
        assert flag in command
    assert f"{repo_path.resolve()}:/workspace:ro" in command and f"{os.getuid()}:{os.getgid()}" in command

    probe = (
        "import os\nimport socket\n\n\n"
        "def attempt(fn):\n    try:\n        fn()\n        return 'allowed'\n    except Exception as e:\n        return type(e).__name__\n\n\n"
        "def test_probe():\n"
        "    print('network', attempt(lambda: socket.create_connection(('1.1.1.1', 53), timeout=3)))\n"
        "    print('write_repo', attempt(lambda: open('/workspace/pwned.txt', 'w')))\n"
        "    print('write_root', attempt(lambda: open('/usr/pwned.txt', 'w')))\n"
        "    print('uid', os.getuid())\n"
    )
    (repo_path / "tests" / "test_probe.py").write_text(probe)
    result = await sandbox.run(
        SandboxRequest(argv=["python", "-m", "pytest", "-s", "-q", "-p", "no:cacheprovider", "tests/test_probe.py"], timeout_seconds=60)
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "network OSError" in result.stdout or "network TimeoutError" in result.stdout
    assert "write_repo OSError" in result.stdout and "write_root OSError" in result.stdout
    assert f"uid {os.getuid()}" in result.stdout and not (repo_path / "pwned.txt").exists()

    report = await sandbox.run(
        SandboxRequest(
            argv=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={report}", "tests/test_auth.py"],
            want_report=True,
            timeout_seconds=60,
        )
    )
    assert report.report and "<testsuite" in report.report and "ASEA-REPORT" not in report.stdout


async def test_stdio_transport_env_isolation_and_restart(settings: Any, repo_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real transport: a child process per repository, without the backend's secrets, restarted if it dies."""
    monkeypatch.setenv("SUPER_SECRET_BACKEND_VALUE", "do-not-leak")
    manager = McpSessionManager(settings)
    spec = SessionSpec(SessionKey("workspace", "repo-1"), workspace_root=str(repo_path))
    try:
        result = await manager.call(spec, "fs_read_file", {"path": "app/auth.py", "max_lines": 5}, retry_safe=True)
        assert result.ok and "Customer authentication" in result.data["content"]
        session = await manager.get(spec)
        assert session.describe()["transport"] == "stdio" and len(session.tools) == 21

        children = subprocess.run(
            ["pgrep", "-f", "app.mcp_server --toolsets fs,git,terminal,testing"], capture_output=True, text=True, check=False
        ).stdout.split()
        assert children
        for pid in children:
            environ = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
            if str(repo_path) not in environ:
                continue
            assert "SUPER_SECRET_BACKEND_VALUE" not in environ and "DATABASE_URL" not in environ and "JWT_SECRET" not in environ
            os.kill(int(pid), signal.SIGKILL)

        # A read-only call transparently restarts the dead server; a write call is not retried.
        again = await manager.call(spec, "git_status", {}, retry_safe=True)
        assert again.ok and manager.restarts + 1 >= 1
        other = SessionSpec(SessionKey("workspace", "repo-2"), workspace_root=str(repo_path / "does-not-exist"))
        failed = await manager.call(other, "git_status", {}, retry_safe=True)
        assert not failed.ok and failed.error_code == "mcp_unavailable"
    finally:
        await manager.close_all()


async def _context(runtime: Runtime, repository: dict[str, Any], role: str = "developer", **kwargs: Any) -> CallContext:
    async with runtime.db.session() as session:
        repo = await session.get(Repository, uuid.UUID(repository["id"]))
    return CallContext(repository=repo, role=role, user_id=repo.id and None, **kwargs)


async def test_gateway_policy_and_logging(
    client: httpx.AsyncClient, admin: dict[str, str], runtime: Runtime, repository: dict[str, Any]
) -> None:
    gateway = runtime.gateway
    ctx = await _context(runtime, repository)

    ok = await gateway.call("fs_read_file", {"path": "app/pricing.py"}, ctx)
    assert ok.ok and "apply_discount" in ok.data["content"]
    cases = [
        ("no_such_tool", {}, ctx, "unknown_tool"),
        ("fs_write_file", {"path": "a", "content": "b", "expected_sha256": "absent"}, ctx, "approval_required"),
        ("git_commit", {"paths": ["a"], "message": "msg", "author_name": "a", "author_email": "a@b"}, ctx, "approval_required"),
        ("terminal_run_sensitive", {"argv": ["npm", "ci"], "timeout_seconds": 5, "directory": "."}, ctx, "approval_required"),
        ("test_run", {"framework": "pytest"}, await _context(runtime, repository, role="viewer"), "forbidden"),
        ("fs_read_file", {"path": "a"}, await _context(runtime, repository, allowed_tools=frozenset({"git_status"})), "tool_not_allowed"),
        ("test_run", {"framework": "pytest"}, await _context(runtime, repository, auto_run_tests=False), "approval_required"),
        ("fs_read_file", {"path": ".env"}, ctx, "secret_file"),
        ("fs_read_file", {"path": "../../etc/shadow"}, ctx, "path_not_allowed"),
        ("code_search_symbols", {"query": ""}, ctx, "invalid_arguments"),
        ("propose_edit", {"path": "app/pricing.py", "old_text": "a", "new_text": "b"}, ctx, "not_available"),
    ]
    for tool, arguments, context, expected in cases:
        result = await gateway.call(tool, arguments, context)
        assert not result.ok and result.error_code == expected, (tool, result.error_code, result.error_message)

    # An injected instruction in file content is flagged, not obeyed.
    readme = await gateway.call("fs_read_file", {"path": "README.md"}, ctx)
    assert readme.ok and readme.notices and "override_instructions" in readme.notices[0]

    async with runtime.db.session() as session:
        rows = list(await session.scalars(select(ToolExecution).order_by(ToolExecution.created_at)))
        audits = list(await session.scalars(select(AuditLog).where(AuditLog.action.startswith("security."))))
    assert len(rows) == len(cases) + 2
    by_tool = {(r.tool_name, r.status) for r in rows}
    assert ("fs_write_file", "denied") in by_tool and ("no_such_tool", "denied") in by_tool and ("fs_read_file", "success") in by_tool
    assert all(r.risk_level in {"read", "execute", "write", "sensitive", "unknown"} for r in rows)
    actions = {a.action for a in audits}
    assert {"security.approval_required", "security.secret_file", "security.injection_suspected", "security.unknown_tool"} <= actions

    listed = (await client.get("/tool-executions", params={"status": "denied"}, headers=admin)).json()
    assert listed["total"] >= 6 and all(item["status"] == "denied" for item in listed["items"])
    servers = (await client.get("/mcp/servers", headers=admin)).json()
    assert servers["sessions"][0]["kind"] == "workspace" and servers["sessions"][0]["alive"] is True
    tools = (await client.get("/mcp/tools", headers=admin)).json()["tools"]
    assert next(t for t in tools if t["name"] == "fs_write_file")["requires_approval"] is True
    assert next(t for t in tools if t["name"] == "fs_read_file")["input_schema"]["properties"]["path"]


async def test_gateway_definitions_exclude_write_tools(runtime: Runtime, repository: dict[str, Any]) -> None:
    ctx = await _context(runtime, repository)
    assert ctx.repository is not None
    names = frozenset(TOOL_CATALOG)
    definitions = await runtime.gateway.tool_definitions(ctx.repository, names)
    offered = {d["function"]["name"] for d in definitions}
    forbidden = {n for n, s in TOOL_CATALOG.items() if s.risk in ("write", "sensitive") or s.server == "github"}
    assert offered and not offered & forbidden
    assert all(d["function"]["parameters"]["type"] == "object" for d in definitions)
