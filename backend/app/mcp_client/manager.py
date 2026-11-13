"""Lifecycle of MCP client sessions.

One stdio server process per repository (``workspace``) and one per user/repository pair
for GitHub tools. Each session is owned by a dedicated task because the SDK's client is a
context manager that must be entered and exited from the same task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer

from app.config import Settings
from app.logs import get_logger
from app.sandbox.runner import SandboxConfig

log = get_logger(__name__)
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ERROR_RE = re.compile(r"^Error executing tool [\w-]+: \[(?P<code>[\w-]+)\]\s*(?P<message>.*)$", re.S)


class McpUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SessionKey:
    kind: str  # "workspace" | "github"
    repository_id: str | None = None
    user_id: str | None = None

    def label(self) -> str:
        return ":".join(p for p in (self.kind, self.repository_id, self.user_id) if p)


@dataclass
class SessionSpec:
    key: SessionKey
    workspace_root: str | None = None
    github_token: str | None = None


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: str | None


@dataclass
class ToolCallResult:
    ok: bool
    data: Any = None
    text: str = ""
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    notices: list[str] = field(default_factory=list)

    @classmethod
    def failure(cls, code: str, message: str, **kwargs: Any) -> ToolCallResult:
        return cls(ok=False, error_code=code, error_message=message, text=f"[{code}] {message}", **kwargs)


ServerFactory = Callable[[SessionSpec], MCPServer]


class McpSession:
    def __init__(self, spec: SessionSpec, target: Any, timeout: float) -> None:
        self.spec = spec
        self._target = target
        self._timeout = timeout
        self.client: Client | None = None
        self.tools: dict[str, ToolInfo] = {}
        self.started_at = time.time()
        self.last_used = time.monotonic()
        self.calls = 0
        self.errors = 0
        self.alive = False
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._error: BaseException | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._own(), name=f"mcp-session-{self.spec.key.label()}")
        await self._ready.wait()
        if self._error is not None:
            raise McpUnavailable(f"MCP server failed to start: {self._error}") from self._error

    async def _own(self) -> None:
        try:
            async with Client(self._target, read_timeout_seconds=self._timeout) as client:
                listing = await client.list_tools()
                for tool in listing.tools:
                    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
                    meta = tool.meta or {}
                    self.tools[tool.name] = ToolInfo(tool.name, tool.description or "", dict(schema), meta.get("asea/risk"))
                self.client = client
                self.alive = True
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            self._error = exc
            if not isinstance(exc, asyncio.CancelledError):
                log.warning("mcp_session_ended", session=self.spec.key.label(), error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            self.alive = False
            self.client = None
            self._ready.set()

    async def call(self, tool: str, arguments: dict[str, Any], *, meta: dict[str, Any] | None, timeout: float) -> ToolCallResult:
        if not self.alive or self.client is None:
            raise McpUnavailable("MCP session is not connected")
        self.last_used = time.monotonic()
        self.calls += 1
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self.client.call_tool(tool, arguments, read_timeout_seconds=timeout, meta=meta),  # type: ignore[arg-type]
                timeout=timeout + 5,
            )
        except TimeoutError:
            self.errors += 1
            return ToolCallResult.failure(
                "timeout", f"{tool} did not finish within {timeout:.0f} seconds",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )  # fmt: skip
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors += 1
            message = str(exc)
            # Request-level timeouts and protocol errors leave the session usable; transport failures do not.
            if "timed out" in message.lower():
                return ToolCallResult.failure(
                    "timeout", f"{tool} did not finish within {timeout:.0f} seconds",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )  # fmt: skip
            self.alive = False
            raise McpUnavailable(f"MCP call failed: {type(exc).__name__}: {message}"[:300]) from exc
        duration = int((time.perf_counter() - started) * 1000)
        text = "\n".join(getattr(block, "text", "") for block in result.content if getattr(block, "type", "") == "text")
        is_error = bool(getattr(result, "is_error", None) or getattr(result, "isError", None))
        if is_error:
            self.errors += 1
            match = _ERROR_RE.match(text.strip())
            if match:
                return ToolCallResult.failure(match.group("code"), match.group("message").strip(), duration_ms=duration)
            code = "invalid_arguments" if "validation error" in text else "tool_error"
            return ToolCallResult.failure(code, text.strip()[:2000], duration_ms=duration)
        data: Any = None
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(text) if text else None
        return ToolCallResult(ok=True, data=data, text=text, duration_ms=duration)

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with contextlib.suppress(BaseException):
                    await self._task
            except Exception:  # noqa: S110 - the owner task already logged why the session ended
                pass

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.spec.key.kind,
            "repository_id": self.spec.key.repository_id,
            "user_id": self.spec.key.user_id,
            "transport": "stdio" if isinstance(self._target, StdioServerParameters) else "in-process",
            "alive": self.alive,
            "tool_count": len(self.tools),
            "calls": self.calls,
            "errors": self.errors,
            "started_at": self.started_at,
            "idle_seconds": int(time.monotonic() - self.last_used),
        }


class McpSessionManager:
    def __init__(self, settings: Settings, *, server_factory: ServerFactory | None = None, idle_seconds: float = 900.0) -> None:
        self.settings = settings
        self._server_factory = server_factory
        self._sessions: dict[SessionKey, McpSession] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}
        self._idle_seconds = idle_seconds
        self._reaper: asyncio.Task[None] | None = None
        self.restarts = 0

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle(), name="mcp-idle-reaper")

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for key, session in list(self._sessions.items()):
                if not session.alive or now - session.last_used > self._idle_seconds:
                    await self.close(key)

    def sandbox_config(self) -> SandboxConfig:
        s = self.settings
        return SandboxConfig(
            mode=s.sandbox_mode, image=s.sandbox_image, memory_mb=s.sandbox_memory_mb, cpus=s.sandbox_cpus,
            pids_limit=s.sandbox_pids_limit, path_map=s.sandbox_path_map,
        )  # fmt: skip

    def _target(self, spec: SessionSpec) -> Any:
        if self._server_factory is not None:
            return self._server_factory(spec)
        toolsets = "github" if spec.key.kind == "github" else "fs,git,terminal,testing"
        env = {
            "ASEA_APPROVAL_SECRET": self.settings.approval_token_secret.get_secret_value(),
            "ASEA_MAX_FILE_BYTES": str(self.settings.max_file_bytes),
            "ASEA_GITHUB_API_URL": self.settings.github_api_url,
            "LOG_LEVEL": "WARNING",
            **self.sandbox_config().to_env(),
        }
        if spec.workspace_root:
            env["ASEA_WORKSPACE_ROOT"] = spec.workspace_root
        if spec.github_token:
            env["ASEA_GITHUB_TOKEN"] = spec.github_token
        # The SDK starts the child with a minimal inherited environment plus these values, so
        # backend secrets (database URL, JWT key, LLM key) never reach the tool server.
        return StdioServerParameters(
            command=sys.executable, args=["-m", "app.mcp_server", "--toolsets", toolsets], env=env, cwd=str(_BACKEND_ROOT)
        )

    async def get(self, spec: SessionSpec) -> McpSession:
        lock = self._locks.setdefault(spec.key, asyncio.Lock())
        async with lock:
            session = self._sessions.get(spec.key)
            if session is not None and session.alive:
                if session.spec.workspace_root == spec.workspace_root and session.spec.github_token == spec.github_token:
                    return session
                await session.close()  # configuration changed (e.g. a new token): start over
            elif session is not None:
                await session.close()
                self.restarts += 1
                log.info("mcp_session_restart", session=spec.key.label())
            session = McpSession(spec, self._target(spec), self.settings.mcp_long_call_timeout_seconds)
            await session.start()
            self._sessions[spec.key] = session
            return session

    async def call(
        self,
        spec: SessionSpec,
        tool: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
        timeout: float | None = None,
        retry_safe: bool = False,
    ) -> ToolCallResult:
        """Call a tool, restarting a dead session once. Only ``retry_safe`` (read-only) calls are repeated."""
        timeout = timeout or self.settings.mcp_call_timeout_seconds
        attempts = 2 if retry_safe else 1
        last_error = "MCP server unavailable"
        for attempt in range(attempts):
            try:
                session = await self.get(spec)
                return await session.call(tool, arguments, meta=meta, timeout=timeout)
            except McpUnavailable as exc:
                last_error = str(exc)
                log.warning("mcp_call_unavailable", tool=tool, attempt=attempt + 1, error=last_error)
                await self.close(spec.key)
        return ToolCallResult.failure("mcp_unavailable", last_error)

    async def close(self, key: SessionKey) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    async def close_matching(self, *, user_id: str | None = None, repository_id: str | None = None) -> None:
        for key in list(self._sessions):
            if (user_id and key.user_id == user_id) or (repository_id and key.repository_id == repository_id):
                await self.close(key)

    async def close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(BaseException):
                await self._reaper
            self._reaper = None
        for key in list(self._sessions):
            await self.close(key)

    # --- introspection -----------------------------------------------------------------

    def session_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.alive)

    def describe(self) -> list[dict[str, Any]]:
        return [s.describe() for s in self._sessions.values()]
