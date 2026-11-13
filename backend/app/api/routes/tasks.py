"""Development tasks, agent runs and live progress."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, PageDep, SessionDep, client_ip
from app.db.models import ACTIVE_TASK_STATUSES, AgentEvent, AgentRun, Task
from app.db.session import get_database
from app.errors import AppError, Conflict, NotFound
from app.runtime import get_runtime
from app.schemas.api import EventOut, ListResponse, RunOut, TaskCreate, TaskOut
from app.services.access import audit, require_project, require_repository, visible_project_ids

router = APIRouter(tags=["tasks"])
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")


def dedupe_key(repository_id: uuid.UUID, request_text: str) -> str:
    normalized = re.sub(r"\s+", " ", request_text.strip().lower())
    return hashlib.sha256(f"{repository_id}:{normalized}".encode()).hexdigest()


async def _latest_run(session: SessionDep, task_id: uuid.UUID) -> AgentRun | None:
    return await session.scalar(select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.created_at.desc()).limit(1))


async def _task_out(session: SessionDep, task: Task) -> TaskOut:
    out = TaskOut.model_validate(task)
    run = await _latest_run(session, task.id)
    out.latest_run = RunOut.model_validate(run) if run else None
    return out


async def _require_task(session: SessionDep, user: CurrentUser, task_id: uuid.UUID, min_role: str = "viewer") -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("Task not found")
    try:
        await require_project(session, user, task.project_id, min_role)
    except NotFound:
        raise NotFound("Task not found") from None
    return task


async def _require_run(session: SessionDep, user: CurrentUser, run_id: uuid.UUID) -> AgentRun:
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise NotFound("Run not found")
    await _require_task(session, user, run.task_id)
    return run


@router.post("/tasks", response_model=TaskOut, status_code=201)
async def create_task(body: TaskCreate, user: CurrentUser, session: SessionDep, request: Request) -> TaskOut:
    repository, _ = await require_repository(session, user, body.repository_id, "developer")
    if repository.status == "error":
        raise AppError("The repository could not be indexed; re-index it first", code="repository_not_ready", status_code=409)
    key = dedupe_key(repository.id, body.request)
    existing = await session.scalar(
        select(Task).where(Task.repository_id == repository.id, Task.dedupe_key == key, Task.status.in_(ACTIVE_TASK_STATUSES))
    )
    if existing is not None:
        raise Conflict(
            "An identical task is already in progress for this repository", code="duplicate_task",
            details={"task_id": str(existing.id), "status": existing.status},
        )  # fmt: skip
    title = (body.title or body.request.strip().splitlines()[0])[:300]
    task = Task(
        project_id=repository.project_id, repository_id=repository.id, created_by=user.id, title=title,
        request_text=body.request.strip(), dedupe_key=key, status="queued",
    )  # fmt: skip
    session.add(task)
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with an identical request; the partial unique index is the final guard.
        await session.rollback()
        raise Conflict("An identical task is already in progress for this repository", code="duplicate_task") from None
    run = AgentRun(task_id=task.id, thread_id=str(uuid.uuid4()), status="queued")
    session.add(run)
    await session.flush()
    run.thread_id = str(run.id)
    await audit(session, "task.create", actor=user, resource_type="task", resource_id=task.id,
                details={"repository_id": str(repository.id)}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    await get_runtime().runner.start(run.id)
    return await _task_out(session, task)


@router.get("/tasks", response_model=ListResponse[TaskOut])
async def list_tasks(
    user: CurrentUser, session: SessionDep, page: PageDep,
    project_id: uuid.UUID | None = None, repository_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(max_length=30)] = None,
) -> ListResponse[TaskOut]:  # fmt: skip
    query = select(Task)
    visible = await visible_project_ids(session, user)
    if visible is not None:
        query = query.where(Task.project_id.in_(visible))
    if project_id:
        query = query.where(Task.project_id == project_id)
    if repository_id:
        query = query.where(Task.repository_id == repository_id)
    if status:
        query = query.where(Task.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(query.order_by(Task.created_at.desc()).limit(page.limit).offset(page.offset))
    return ListResponse(items=[await _task_out(session, t) for t in rows], total=total)


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> TaskOut:
    return await _task_out(session, await _require_task(session, user, task_id))


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request) -> TaskOut:
    task = await _require_task(session, user, task_id, "developer")
    if task.status not in ACTIVE_TASK_STATUSES:
        raise Conflict(f"The task is already {task.status}", code="task_not_active")
    run = await _latest_run(session, task.id)
    await audit(session, "task.cancel", actor=user, resource_type="task", resource_id=task.id, ip=client_ip(request))
    await session.commit()
    if run is not None:
        await get_runtime().runner.cancel(run.id)
    await session.refresh(task)
    return await _task_out(session, task)


@router.post("/tasks/{task_id}/resume", response_model=TaskOut)
async def resume_task(task_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request) -> TaskOut:
    """Continue an interrupted or failed run from its last checkpoint."""
    task = await _require_task(session, user, task_id, "developer")
    run = await _latest_run(session, task.id)
    if run is None or run.status not in ("interrupted", "failed"):
        raise Conflict("Only interrupted or failed tasks can be resumed", code="task_not_resumable")
    duplicate = await session.scalar(
        select(Task.id).where(
            Task.repository_id == task.repository_id, Task.dedupe_key == task.dedupe_key,
            Task.status.in_(ACTIVE_TASK_STATUSES), Task.id != task.id,
        )
    )  # fmt: skip
    if duplicate:
        raise Conflict("An identical task is already in progress", code="duplicate_task", details={"task_id": str(duplicate)})
    await audit(session, "task.resume", actor=user, resource_type="task", resource_id=task.id, ip=client_ip(request))
    await session.commit()
    await get_runtime().runner.resume_from_checkpoint(run.id)
    await session.refresh(task)
    return await _task_out(session, task)


@router.get("/tasks/{task_id}/runs", response_model=list[RunOut])
async def list_runs(task_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> list[AgentRun]:
    await _require_task(session, user, task_id)
    rows = await session.scalars(select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.created_at.desc()))
    return list(rows)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> AgentRun:
    return await _require_run(session, user, run_id)


@router.get("/runs/{run_id}/events", response_model=list[EventOut])
async def run_events(
    run_id: uuid.UUID, user: CurrentUser, session: SessionDep,
    after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[AgentEvent]:  # fmt: skip
    await _require_run(session, user, run_id)
    rows = await session.scalars(
        select(AgentEvent).where(AgentEvent.run_id == run_id, AgentEvent.id > after).order_by(AgentEvent.id).limit(limit)
    )
    return list(rows)


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/runs/{run_id}/stream")
async def run_stream(
    run_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request, after: Annotated[int, Query(ge=0)] = 0
) -> StreamingResponse:
    """Server-sent events for a run. Backed by the events table, so clients can reconnect with ``after``."""
    await _require_run(session, user, run_id)
    database = get_database()

    async def generate() -> AsyncIterator[str]:
        cursor = after
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            async with database.session() as s:
                events = list(
                    await s.scalars(
                        select(AgentEvent).where(AgentEvent.run_id == run_id, AgentEvent.id > cursor).order_by(AgentEvent.id).limit(200)
                    )
                )
                run = await s.get(AgentRun, run_id)
            for event in events:
                cursor = event.id
                yield _sse("agent_event", EventOut.model_validate(event).model_dump(mode="json"), event.id)
            if run is None:
                return
            if not events:
                idle += 1
                settled = run.status in TERMINAL_RUN_STATUSES or run.status in ("awaiting_approval", "interrupted")
                if settled and idle >= 2:
                    # Nothing more will happen until a human acts; tell the client and stop. The extra
                    # pass makes sure events written just after the status change are delivered first.
                    yield _sse("status", {"status": run.status, "current_node": run.current_node})
                    return
                if idle % 15 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.3 if settled else 1.0)
            else:
                idle = 0

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
