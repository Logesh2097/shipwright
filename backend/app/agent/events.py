"""Durable run events (execution history and the source for live progress)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.db.models import AgentEvent, AgentRun, Task
from app.db.session import Database
from app.security.redaction import redact_value


async def emit(db: Database, run_id: uuid.UUID, type_: str, *, node: str | None = None, payload: dict[str, Any] | None = None) -> None:
    async with db.session() as session:
        session.add(AgentEvent(run_id=run_id, type=type_, node=node, payload=redact_value(payload or {}, max_string=4000)))
        await session.execute(update(AgentRun).where(AgentRun.id == run_id).values(heartbeat_at=datetime.now(UTC)))


async def update_run(db: Database, run_id: uuid.UUID, **fields: Any) -> None:
    if not fields:
        return
    async with db.session() as session:
        await session.execute(update(AgentRun).where(AgentRun.id == run_id).values(**fields))


async def update_task(db: Database, task_id: uuid.UUID, **fields: Any) -> None:
    if not fields:
        return
    async with db.session() as session:
        await session.execute(update(Task).where(Task.id == task_id).values(**fields))


async def set_status(db: Database, run_id: uuid.UUID, task_id: uuid.UUID, status: str, **run_fields: Any) -> None:
    """Keep the run and its task in step."""
    async with db.session() as session:
        await session.execute(update(AgentRun).where(AgentRun.id == run_id).values(status=status, **run_fields))
        await session.execute(update(Task).where(Task.id == task_id).values(status=status))
