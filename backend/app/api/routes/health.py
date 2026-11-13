"""Liveness and readiness."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.db.session import get_database
from app.runtime import get_runtime

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, Any]:
    runtime = get_runtime()
    checks: dict[str, Any] = {}
    try:
        async with get_database().engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            # The users table only exists once migrations have run.
            await conn.execute(text("SELECT 1 FROM users LIMIT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    checks["llm"] = "configured" if runtime.settings.llm_configured else "not_configured"
    checks["sandbox"] = runtime.sandbox_status()
    checks["mcp_sessions"] = runtime.mcp.session_count() if runtime.started else 0
    checks["agent_runner"] = "ok" if runtime.started else "not_started"
    ok = checks["database"] == "ok" and runtime.started
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "unavailable", "checks": checks}
