"""Builds the MCP server from the selected toolsets."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from app.mcp_server.context import ALL_TOOLSETS, ServerContext
from app.mcp_server.tools import filesystem, git, github, terminal, testing

_REGISTRARS = {
    "fs": filesystem.register,
    "git": git.register,
    "terminal": terminal.register,
    "testing": testing.register,
    "github": github.register,
}

INSTRUCTIONS = (
    "Development tools for one repository. Paths are relative to the repository root. "
    "Read tools are always available. Tools that modify files, run sensitive commands, write to Git "
    "or to GitHub only work with an approval token issued after a human decision."
)


def build_server(sc: ServerContext, **kwargs: object) -> MCPServer:
    unknown = set(sc.toolsets) - set(ALL_TOOLSETS)
    if unknown:
        raise ValueError(f"Unknown toolsets: {', '.join(sorted(unknown))}")
    needs_workspace = set(sc.toolsets) & {"fs", "git", "terminal", "testing"}
    if needs_workspace and sc.workspace_root is None:
        raise ValueError("ASEA_WORKSPACE_ROOT is required for the fs, git, terminal and testing toolsets")
    if sc.workspace_root is not None and not sc.workspace_root.is_dir():
        raise ValueError("ASEA_WORKSPACE_ROOT is not a directory")
    name = "asea-github" if sc.toolsets == ("github",) else "asea-workspace"
    server = MCPServer(name, version="0.1.0", instructions=INSTRUCTIONS, log_level="WARNING", **kwargs)  # type: ignore[arg-type]
    for toolset in sc.toolsets:
        _REGISTRARS[toolset](server, sc)
    return server
