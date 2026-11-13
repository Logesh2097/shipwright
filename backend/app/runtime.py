"""Process-wide services: MCP sessions, the Tool Gateway, the agent runner and background tasks."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agent.graph import AgentDeps
from app.agent.llm import build_chat_model
from app.agent.runner import AgentRunner
from app.config import Settings, get_settings
from app.db.session import Database, get_database
from app.logs import get_logger
from app.mcp_client.gateway import ToolGateway
from app.mcp_client.manager import McpSessionManager, ServerFactory
from app.sandbox.runner import resolve_mode

log = get_logger(__name__)


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        *,
        llm_factory: Callable[[], BaseChatModel] | None = None,
        mcp_server_factory: ServerFactory | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or get_database()
        self.mcp = McpSessionManager(self.settings, server_factory=mcp_server_factory)
        self.gateway = ToolGateway(self.db, self.settings, self.mcp)
        self._llm_factory = llm_factory
        self._checkpointer = checkpointer
        self._pool: Any = None
        self._runner: AgentRunner | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self.started = False

    def llm(self) -> BaseChatModel:
        if self._llm_factory is not None:
            return self._llm_factory()
        return build_chat_model(self.settings)

    @property
    def runner(self) -> AgentRunner:
        if self._runner is None:
            raise RuntimeError("Runtime has not been started")
        return self._runner

    def sandbox_status(self) -> dict[str, str]:
        mode, detail = resolve_mode(self.mcp.sandbox_config())
        return {"mode": mode, "detail": detail, "configured": self.settings.sandbox_mode}

    async def startup(self) -> None:
        if self.started:
            return
        checkpointer = self._checkpointer
        if checkpointer is None:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            self._pool = AsyncConnectionPool(
                conninfo=self.db.psycopg_dsn,
                min_size=1,
                max_size=8,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            await self._pool.open()
            saver = AsyncPostgresSaver(self._pool)
            await saver.setup()
            checkpointer = saver
        deps = AgentDeps(db=self.db, settings=self.settings, gateway=self.gateway, llm_factory=self.llm)
        self._runner = AgentRunner(deps, checkpointer)
        self.mcp.start()
        try:
            await self._runner.recover_orphans()
        except Exception as exc:  # the schema may not be migrated yet; readiness reports that
            log.warning("orphan_recovery_skipped", error=str(exc)[:200])
        self.started = True
        mode, detail = resolve_mode(self.mcp.sandbox_config())
        log.info("runtime_started", sandbox_mode=mode, sandbox_detail=detail, llm_configured=self.settings.llm_configured)

    async def shutdown(self) -> None:
        if self._runner is not None:
            await self._runner.shutdown()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        await self.mcp.close_all()
        if self._pool is not None:
            with contextlib.suppress(Exception):
                await self._pool.close()
        self.started = False

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Run a coroutine in the background, keeping a reference and logging failures."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background_task_failed", task=name, exc_info=t.exception())

        task.add_done_callback(_done)
        return task


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime
