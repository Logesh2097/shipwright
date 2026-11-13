"""Filesystem tools. All paths are relative to the workspace root fixed at server start."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp_server.context import RISK_META, ServerContext, tool_error

RelPath = Annotated[str, Field(description="Path relative to the repository root", min_length=1, max_length=1000)]
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


async def run_blocking[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        raise tool_error(exc) from exc


def register(server: MCPServer, sc: ServerContext) -> None:
    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def fs_list_directory(
        path: Annotated[str, Field(description="Directory relative to the repository root", max_length=1000)] = ".",
        max_entries: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """List a directory. Ignored directories (.git, node_modules, virtualenvs) and secret files are omitted."""
        return await run_blocking(sc.workspace.list_directory, path, max_entries)

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def fs_read_file(
        path: RelPath,
        start_line: Annotated[int, Field(ge=1)] = 1,
        max_lines: Annotated[int, Field(ge=1, le=2000)] = 400,
    ) -> dict[str, Any]:
        """Read a text file with line numbers context. Secret-looking values are redacted. Returns the file's sha256."""
        return await run_blocking(sc.workspace.read_file, path, start_line, max_lines)

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def fs_find_files(
        pattern: Annotated[str, Field(description="Glob such as 'src/**/*.py' or a filename fragment", min_length=1, max_length=300)],
        max_results: Annotated[int, Field(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        """Find files by glob pattern or name fragment among tracked and unignored files."""
        return await run_blocking(sc.workspace.find_files, pattern, max_results)

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def fs_search_text(
        query: Annotated[str, Field(min_length=1, max_length=300)],
        regex: bool = False,
        path_glob: Annotated[str | None, Field(description="Optional filter such as '*.py' or 'src/**/*.ts'", max_length=300)] = None,
        max_results: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """Case-insensitive text search across the repository. Returns path, line number and line text."""
        return await run_blocking(sc.workspace.search_text, query, regex=regex, path_glob=path_glob, max_results=max_results)

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def fs_file_info(path: RelPath) -> dict[str, Any]:
        """Size, sha256, line count and binary flag of a file."""
        return await run_blocking(sc.workspace.file_info, path)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
        meta={RISK_META: "write"},
    )
    async def fs_write_file(
        path: RelPath,
        content: Annotated[str, Field(max_length=2_000_000)],
        expected_sha256: Annotated[
            str,
            Field(description="sha256 the file must currently have, or 'absent' to create a new file", pattern=r"^([0-9a-f]{64}|absent)$"),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        """Write a file atomically. Requires a human approval. Fails with [stale] if the file changed."""
        sc.require_approval(ctx, "fs_write_file", {"path": path, "content": content, "expected_sha256": expected_sha256})
        return await run_blocking(sc.workspace.write_file, path, content, expected_sha256)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
        meta={RISK_META: "sensitive"},
    )
    async def fs_delete_file(
        path: RelPath,
        expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Delete a file. Requires a human approval. Fails with [stale] if the file changed."""
        sc.require_approval(ctx, "fs_delete_file", {"path": path, "expected_sha256": expected_sha256})
        return await run_blocking(sc.workspace.delete_file, path, expected_sha256)
