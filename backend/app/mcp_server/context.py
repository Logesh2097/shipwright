"""Per-process configuration of the MCP server and helpers shared by its tools."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from app.repo_intel.workspace import Workspace, WorkspaceError
from app.sandbox.runner import Sandbox, SandboxConfig, create_sandbox
from app.security.approval_tokens import META_KEY, ApprovalTokenError, verify_approval_token
from app.security.gitsafe import GitError
from app.security.paths import PathNotAllowed

RISK_META = "asea/risk"
ALL_TOOLSETS = ("fs", "git", "terminal", "testing", "github")


@dataclass
class ServerContext:
    workspace_root: Path | None = None
    toolsets: tuple[str, ...] = ("fs", "git", "terminal", "testing")
    approval_secret: str = ""
    sandbox_config: SandboxConfig = field(default_factory=SandboxConfig)
    max_file_bytes: int = 1_000_000
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport
    github_push_url: str | None = None  # tests point this at a local bare repository
    _sandbox: Sandbox | None = None

    @classmethod
    def from_env(cls, toolsets: tuple[str, ...]) -> ServerContext:
        root = os.environ.get("ASEA_WORKSPACE_ROOT")
        return cls(
            workspace_root=Path(root) if root else None,
            toolsets=toolsets,
            approval_secret=os.environ.get("ASEA_APPROVAL_SECRET", ""),
            sandbox_config=SandboxConfig.from_env(),
            max_file_bytes=int(os.environ.get("ASEA_MAX_FILE_BYTES", "1000000")),
            github_token=os.environ.get("ASEA_GITHUB_TOKEN") or None,
            github_api_url=os.environ.get("ASEA_GITHUB_API_URL", "https://api.github.com"),
        )

    @property
    def workspace(self) -> Workspace:
        if self.workspace_root is None:
            raise ToolError("[no_workspace] This server was started without a workspace")
        return Workspace(self.workspace_root, max_file_bytes=self.max_file_bytes)

    @property
    def root_str(self) -> str:
        return str(self.workspace.root) if self.workspace_root is not None else ""

    @property
    def sandbox(self) -> Sandbox:
        if self._sandbox is None:
            try:
                self._sandbox = create_sandbox(self.workspace.root, self.sandbox_config)
            except RuntimeError as exc:
                raise ToolError(f"[sandbox_unavailable] {exc}") from exc
        return self._sandbox

    def require_approval(self, mcp_ctx: Context, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Verify the approval token carried in the request's ``_meta``; raise ``ToolError`` if absent or wrong."""
        meta = getattr(mcp_ctx.request_context, "meta", None) or {}
        token = meta.get(META_KEY) if isinstance(meta, dict) else getattr(meta, META_KEY, None)
        try:
            return verify_approval_token(self.approval_secret, token, tool=tool, arguments=arguments, workspace=self.root_str)
        except ApprovalTokenError as exc:
            raise ToolError(f"[approval_required] {exc}") from exc


def tool_error(exc: Exception) -> ToolError:
    """Map internal exceptions to a ToolError whose message starts with a stable ``[code]``."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, PathNotAllowed):
        return ToolError(f"[{exc.reason}] {exc}")
    if isinstance(exc, WorkspaceError):
        return ToolError(f"[{exc.code}] {exc}")
    if isinstance(exc, GitError):
        return ToolError(f"[git_error] {exc}")
    return ToolError(f"[internal_error] {type(exc).__name__}")
