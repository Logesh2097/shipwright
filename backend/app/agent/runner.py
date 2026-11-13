"""Run lifecycle: start, pause for approval, resume, cancel, recover after a restart."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import select, update

from app.agent.events import emit, set_status
from app.agent.graph import AgentDeps, AgentState, build_graph
from app.db.models import AgentRun, Approval, ChangeSet, Task
from app.errors import AppError, Conflict
from app.logs import get_logger

log = get_logger(__name__)


class AgentRunner:
    def __init__(self, deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]) -> None:
        self.deps = deps
        self.db = deps.db
        self.graph = build_graph(deps).compile(checkpointer=checkpointer)
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._cancelled: set[uuid.UUID] = set()
        self._shutting_down = False

    # --- public API --------------------------------------------------------------------

    def is_active(self, run_id: uuid.UUID) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def start(self, run_id: uuid.UUID) -> None:
        async with self.db.session() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise AppError("Run not found", status_code=404, code="not_found")
            task = await session.get(Task, run.task_id)
            assert task is not None
            initial: AgentState = {
                "run_id": str(run.id),
                "task_id": str(task.id),
                "repository_id": str(task.repository_id),
                "user_id": str(task.created_by),
                "request": task.request_text,
                "repair_attempts": 0,
            }
        self._spawn(run_id, initial, "run_started")

    async def resume_with_decision(self, run_id: uuid.UUID, decision: dict[str, Any]) -> None:
        """Continue a run that is waiting at the approval interrupt."""
        await self._mark_running(run_id)
        self._spawn(run_id, Command(resume=decision), "run_resumed")

    async def resume_from_checkpoint(self, run_id: uuid.UUID) -> None:
        """Continue an interrupted or failed run from its last checkpoint (the failed step runs again)."""
        state = await self.graph.aget_state(self._config(run_id))
        if not state.next and not state.values:
            await self.start(run_id)
            return
        if not state.next:
            raise Conflict("This run already finished; create a new task instead", code="run_finished")
        if state.interrupts:
            # Waiting for a human decision: nothing to execute until it arrives.
            async with self.db.session() as session:
                run = await session.get(AgentRun, run_id)
                assert run is not None
                task_id = run.task_id
            await set_status(self.db, run_id, task_id, "awaiting_approval", error=None)
            return
        await self._mark_running(run_id)
        self._spawn(run_id, None, "run_resumed")

    async def cancel(self, run_id: uuid.UUID) -> None:
        self._cancelled.add(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        else:
            await self._finish_cancelled(run_id)

    async def recover_orphans(self) -> int:
        """Runs left 'running' or 'queued' by a previous process are marked interrupted."""
        async with self.db.session() as session:
            runs = list(await session.scalars(select(AgentRun).where(AgentRun.status.in_(("running", "queued")))))
            for run in runs:
                run.status = "interrupted"
                run.error = "The server restarted while this run was in progress. Resume it to continue from the last checkpoint."
                await session.execute(update(Task).where(Task.id == run.task_id).values(status="interrupted"))
        for run in runs:
            await emit(self.db, run.id, "run_interrupted", payload={"reason": "server restart"})
        if runs:
            log.warning("orphaned_runs_marked_interrupted", count=len(runs))
        return len(runs)

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait(self, run_id: uuid.UUID, timeout: float = 60.0) -> None:
        """Wait for the current execution segment of a run to end (used by tests and the harness)."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    # --- internals ---------------------------------------------------------------------

    async def _mark_running(self, run_id: uuid.UUID) -> None:
        """Reflect the new state before the background task starts, so callers never read a stale status."""
        if self.is_active(run_id):
            raise Conflict("This run is already executing", code="run_active")
        await set_status(self.db, run_id, await self._task_id(run_id), "running", error=None, finished_at=None)

    def _config(self, run_id: uuid.UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 60}

    def _spawn(self, run_id: uuid.UUID, graph_input: Any, event: str) -> None:
        if self.is_active(run_id):
            raise Conflict("This run is already executing", code="run_active")
        self._cancelled.discard(run_id)
        task = asyncio.create_task(self._execute(run_id, graph_input, event), name=f"agent-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda t, rid=run_id: self._tasks.pop(rid, None) if self._tasks.get(rid) is t else None)  # type: ignore[misc]

    async def _task_id(self, run_id: uuid.UUID) -> uuid.UUID:
        async with self.db.session() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                raise RuntimeError("run disappeared")
            return run.task_id

    async def _execute(self, run_id: uuid.UUID, graph_input: Any, event: str) -> None:
        db = self.db
        task_id = await self._task_id(run_id)
        try:
            async with db.session() as session:
                run = await session.get(AgentRun, run_id)
                assert run is not None
                run.status = "running"
                run.error = None
                run.started_at = run.started_at or datetime.now(UTC)
                run.finished_at = None
                await session.execute(update(Task).where(Task.id == task_id).values(status="running"))
            await emit(db, run_id, event)

            interrupted = False
            async for chunk in self.graph.astream(graph_input, self._config(run_id), stream_mode="updates"):
                if "__interrupt__" in chunk:
                    interrupted = True
                    continue
                for update_ in chunk.values():
                    if isinstance(update_, dict) and update_.get("usage"):
                        await self._add_usage(run_id, update_["usage"])

            if interrupted:
                await set_status(db, run_id, task_id, "awaiting_approval", current_node="await_approval")
                await emit(db, run_id, "run_paused", payload={"reason": "awaiting approval"})
            else:
                await set_status(db, run_id, task_id, "completed", finished_at=datetime.now(UTC), current_node=None)
                await self._drop_checkpoints(run_id)
                await emit(db, run_id, "run_finished", payload={"status": "completed"})
        except asyncio.CancelledError:
            if run_id in self._cancelled:
                await asyncio.shield(self._finish_cancelled(run_id))
            else:
                # Process shutdown: leave the run resumable.
                await asyncio.shield(
                    set_status(db, run_id, task_id, "interrupted", error="The server stopped while this run was in progress.")
                )
            raise
        except AppError as exc:
            await self._fail(run_id, task_id, exc.code, exc.message)
        except Exception as exc:
            log.error("agent_run_failed", run_id=str(run_id), exc_info=exc)
            await self._fail(run_id, task_id, "agent_error", f"{type(exc).__name__}: {exc}")

    async def _fail(self, run_id: uuid.UUID, task_id: uuid.UUID, code: str, message: str) -> None:
        error = f"[{code}] {message}"[:4000]
        await set_status(self.db, run_id, task_id, "failed", error=error, finished_at=datetime.now(UTC))
        await emit(self.db, run_id, "run_finished", payload={"status": "failed", "error": error})

    async def _drop_checkpoints(self, run_id: uuid.UUID) -> None:
        """A finished or cancelled run can never resume, so its checkpoints are removed."""
        checkpointer = self.graph.checkpointer
        try:
            if isinstance(checkpointer, BaseCheckpointSaver):
                await checkpointer.adelete_thread(str(run_id))
        except Exception as exc:  # cleanup only; the run's outcome is already recorded
            log.warning("checkpoint_cleanup_failed", run_id=str(run_id), error=str(exc)[:200])

    async def _finish_cancelled(self, run_id: uuid.UUID) -> None:
        task_id = await self._task_id(run_id)
        await self._drop_checkpoints(run_id)
        async with self.db.session() as session:
            await session.execute(
                update(Approval).where(Approval.run_id == run_id, Approval.status == "pending").values(status="cancelled")
            )
            await session.execute(
                update(ChangeSet).where(ChangeSet.run_id == run_id, ChangeSet.status.in_(("draft", "proposed"))).values(status="rejected")
            )
        await set_status(self.db, run_id, task_id, "cancelled", finished_at=datetime.now(UTC))
        await emit(self.db, run_id, "run_finished", payload={"status": "cancelled"})

    async def _add_usage(self, run_id: uuid.UUID, usage: dict[str, int]) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    llm_calls=AgentRun.llm_calls + usage.get("llm_calls", 0),
                    input_tokens=AgentRun.input_tokens + usage.get("input_tokens", 0),
                    output_tokens=AgentRun.output_tokens + usage.get("output_tokens", 0),
                )
            )
