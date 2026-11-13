"""Terminal tools: allow-listed commands, executed in the sandbox."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp_server.context import RISK_META, ServerContext
from app.mcp_server.parsers import excerpt
from app.mcp_server.tools.filesystem import run_blocking
from app.sandbox.runner import SandboxRequest, SandboxResult
from app.security.commands import classify_command
from app.security.redaction import redact_text

Argv = Annotated[
    list[Annotated[str, Field(max_length=2000)]],
    Field(description="Command as an argument list, e.g. ['pytest', '-q', 'tests/test_api.py']. No shell.", min_length=1, max_length=64),
]
Directory = Annotated[str, Field(description="Working directory relative to the repository root", max_length=1000)]


def resolve_directory(sc: ServerContext, directory: str) -> str:
    path = sc.workspace.resolve(directory)
    if not path.is_dir():
        raise ToolError(f"[not_a_directory] Not a directory: {directory}")
    return sc.workspace.rel(path)


def result_payload(result: SandboxResult, argv: list[str], limit: int = 8000) -> dict[str, Any]:
    if result.error:
        raise ToolError(f"[sandbox_error] {result.error}")
    return {
        "command": argv,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "sandbox_mode": result.mode,
        "stdout": redact_text(excerpt(result.stdout, limit)),
        "stderr": redact_text(excerpt(result.stderr, limit // 2)),
        "truncated": result.truncated,
    }


def register(server: MCPServer, sc: ServerContext) -> None:
    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        meta={RISK_META: "execute"},
    )
    async def terminal_run(
        argv: Argv,
        timeout_seconds: Annotated[int, Field(ge=1, le=900)] = 120,
        directory: Directory = ".",
    ) -> dict[str, Any]:
        """Run an allow-listed development command (tests, linters, type checkers) in the sandbox without network.

        Commands that install dependencies or run migrations are refused here; they need approval.
        """
        decision = classify_command(argv)
        if decision.level == "denied":
            raise ToolError(f"[command_denied] {decision.reason}")
        if decision.level == "sensitive":
            raise ToolError(
                f"[approval_required] '{' '.join(argv[:3])}' is a sensitive command ({decision.category}); a developer has to approve it"
            )
        workdir = await run_blocking(resolve_directory, sc, directory)
        result = await sc.sandbox.run(
            SandboxRequest(argv=argv, workdir=workdir, timeout_seconds=timeout_seconds, network=False, writable=False)
        )
        return result_payload(result, argv)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        meta={RISK_META: "sensitive"},
    )
    async def terminal_run_sensitive(
        argv: Argv,
        timeout_seconds: Annotated[int, Field(ge=1, le=1800)],
        directory: Directory,
        ctx: Context,
    ) -> dict[str, Any]:
        """Run a sensitive command (dependency installation, migration). Requires a human approval."""
        sc.require_approval(ctx, "terminal_run_sensitive", {"argv": argv, "timeout_seconds": timeout_seconds, "directory": directory})
        decision = classify_command(argv)
        if decision.level == "denied":
            raise ToolError(f"[command_denied] {decision.reason}")
        workdir = await run_blocking(resolve_directory, sc, directory)
        result = await sc.sandbox.run(
            SandboxRequest(
                argv=argv,
                workdir=workdir,
                timeout_seconds=timeout_seconds,
                network=decision.needs_network,
                writable=True,
                prepare_python_env=decision.category == "install_dependencies",
            )
        )
        return {**result_payload(result, argv), "category": decision.category or "other"}
