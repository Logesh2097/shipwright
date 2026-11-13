"""MCP status, tool catalogue, execution logs, audit trail, dashboard and system settings."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, PageDep, SessionDep, SettingsDep
from app.db.models import (
    AgentRun,
    Approval,
    AuditLog,
    ChangeSet,
    Project,
    Repository,
    Task,
    TestRun,
    ToolExecution,
)
from app.mcp_client.gateway import INTERNAL_TOOLS
from app.mcp_client.policy import APPROVAL_ACTION_LABELS, RISK_DESCRIPTIONS, TOOL_CATALOG
from app.runtime import get_runtime
from app.schemas.api import AuditLogOut, ListResponse, ToolExecutionOut
from app.security.commands import classify_command
from app.services.access import visible_project_ids

router = APIRouter()


@router.get("/mcp/servers", tags=["mcp"])
async def mcp_servers(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    """Live MCP sessions. Non-administrators only see sessions for their own repositories."""
    runtime = get_runtime()
    sessions = runtime.mcp.describe()
    visible = await visible_project_ids(session, user)
    if visible is not None:
        repo_ids = {str(r) for r in await session.scalars(select(Repository.id).where(Repository.project_id.in_(visible)))}
        sessions = [
            s for s in sessions
            if (s["repository_id"] in repo_ids) or (s["kind"] == "github" and s["user_id"] == str(user.id))
        ]  # fmt: skip
    names = {
        str(r.id): r.name
        for r in await session.scalars(select(Repository).where(Repository.id.in_([uuid.UUID(s["repository_id"]) for s in sessions if s["repository_id"]])))
    } if sessions else {}  # fmt: skip
    for item in sessions:
        item["repository_name"] = names.get(item["repository_id"] or "")
    return {
        "sessions": sessions,
        "restarts": runtime.mcp.restarts,
        "sandbox": runtime.sandbox_status(),
        "definitions": [
            {"kind": "workspace", "toolsets": ["fs", "git", "terminal", "testing"], "transport": "stdio",
             "scope": "one process per repository, started on first use"},
            {"kind": "github", "toolsets": ["github"], "transport": "stdio",
             "scope": "one process per user and repository, holds that user's token"},
        ],
    }  # fmt: skip


@router.get("/mcp/tools", tags=["mcp"])
async def mcp_tools(_: CurrentUser) -> dict[str, Any]:
    """Tool catalogue with risk levels. Schemas come from live discovery when a session exists."""
    runtime = get_runtime()
    discovered: dict[str, Any] = {}
    for mcp_session in runtime.mcp._sessions.values():
        for name, info in mcp_session.tools.items():
            discovered.setdefault(name, {"description": info.description, "input_schema": info.input_schema, "declared_risk": info.risk})
    tools = []
    for spec in TOOL_CATALOG.values():
        entry: dict[str, Any] = {
            "name": spec.name, "server": spec.server, "risk": spec.risk,
            "requires_approval": spec.risk in ("write", "sensitive"), "description": None, "input_schema": None,
        }  # fmt: skip
        if spec.server == "internal":
            model, description = INTERNAL_TOOLS[spec.name]
            entry |= {"description": description, "input_schema": model.model_json_schema()}
        elif spec.name in discovered:
            entry |= {k: discovered[spec.name][k] for k in ("description", "input_schema")}
        tools.append(entry)
    return {"tools": tools, "risk_levels": RISK_DESCRIPTIONS}


@router.get("/tool-executions", response_model=ListResponse[ToolExecutionOut], tags=["mcp"])
async def tool_executions(
    user: CurrentUser, session: SessionDep, page: PageDep,
    repository_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    tool: Annotated[str | None, Query(max_length=80)] = None,
) -> ListResponse[ToolExecutionOut]:  # fmt: skip
    query = select(ToolExecution)
    visible = await visible_project_ids(session, user)
    if visible is not None:
        repos = select(Repository.id).where(Repository.project_id.in_(visible))
        query = query.where(
            ToolExecution.repository_id.in_(repos) | ((ToolExecution.repository_id.is_(None)) & (ToolExecution.user_id == user.id))
        )
    if repository_id:
        query = query.where(ToolExecution.repository_id == repository_id)
    if run_id:
        query = query.where(ToolExecution.run_id == run_id)
    if status:
        query = query.where(ToolExecution.status == status)
    if tool:
        query = query.where(ToolExecution.tool_name == tool)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(query.order_by(ToolExecution.created_at.desc()).limit(page.limit).offset(page.offset))
    return ListResponse(items=[ToolExecutionOut.model_validate(r) for r in rows], total=total)


@router.get("/audit-logs", response_model=ListResponse[AuditLogOut], tags=["audit"])
async def audit_logs(
    _: AdminUser, session: SessionDep, page: PageDep,
    action: Annotated[str | None, Query(max_length=80, description="Exact action or prefix such as 'security.'")] = None,
    resource_type: Annotated[str | None, Query(max_length=40)] = None,
) -> ListResponse[AuditLogOut]:  # fmt: skip
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action.startswith(action, autoescape=True))
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(query.order_by(AuditLog.created_at.desc()).limit(page.limit).offset(page.offset))
    return ListResponse(items=[AuditLogOut.model_validate(r) for r in rows], total=total)


@router.get("/dashboard/summary", tags=["dashboard"])
async def dashboard(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    """Counts computed from the database for what the caller can see. Nothing is estimated."""
    visible = await visible_project_ids(session, user)

    def scoped(model: Any, column: Any) -> Any:
        query = select(func.count()).select_from(model)
        return query if visible is None else query.where(column.in_(visible))

    repos = select(Repository.id) if visible is None else select(Repository.id).where(Repository.project_id.in_(visible))

    async def grouped(model: Any, group_col: Any, scope_col: Any, scope: Any) -> dict[str, int]:
        query = select(group_col, func.count()).group_by(group_col)
        if visible is not None:
            query = query.where(scope_col.in_(scope))
        return {str(k): v for k, v in (await session.execute(query)).all()}

    task_ids = select(Task.id) if visible is None else select(Task.id).where(Task.project_id.in_(visible))
    usage = (
        await session.execute(
            select(
                func.coalesce(func.sum(AgentRun.llm_calls), 0),
                func.coalesce(func.sum(AgentRun.input_tokens), 0),
                func.coalesce(func.sum(AgentRun.output_tokens), 0),
            ).where(AgentRun.task_id.in_(task_ids))
        )
    ).one()
    recent = await session.scalars(
        (select(Task) if visible is None else select(Task).where(Task.project_id.in_(visible))).order_by(Task.created_at.desc()).limit(8)
    )
    runtime = get_runtime()
    return {
        "projects": await session.scalar(scoped(Project, Project.id)) or 0,
        "repositories": await session.scalar(scoped(Repository, Repository.project_id)) or 0,
        "tasks_by_status": await grouped(Task, Task.status, Task.project_id, visible),
        "tasks_by_type": await grouped(Task, Task.task_type, Task.project_id, visible),
        "pending_approvals": await session.scalar(
            select(func.count()).select_from(Approval).where(Approval.status == "pending", Approval.repository_id.in_(repos))
        ) or 0,
        "change_sets_by_status": await grouped(ChangeSet, ChangeSet.status, ChangeSet.repository_id, repos),
        "test_runs_by_status": await grouped(TestRun, TestRun.status, TestRun.repository_id, repos),
        "tool_calls_by_status": await grouped(ToolExecution, ToolExecution.status, ToolExecution.repository_id, repos),
        "llm_usage": {"calls": int(usage[0]), "input_tokens": int(usage[1]), "output_tokens": int(usage[2])},
        "recent_tasks": [
            {"id": str(t.id), "title": t.title, "status": t.status, "task_type": t.task_type, "created_at": t.created_at.isoformat()}
            for t in recent
        ],
        "system": {
            "llm_configured": runtime.settings.llm_configured,
            "sandbox": runtime.sandbox_status(),
            "active_runs": runtime.runner.active_count() if runtime.started else 0,
            "mcp_sessions": runtime.mcp.session_count(),
        },
    }  # fmt: skip


@router.get("/settings/system", tags=["settings"])
async def system_settings(_: CurrentUser, settings: SettingsDep) -> dict[str, Any]:
    """Effective configuration that is safe to show. Secrets are never included."""
    runtime = get_runtime()
    examples = [
        ["pytest", "-q"], ["npm", "run", "build"], ["ruff", "check", "."], ["pip", "install", "-r", "requirements.txt"],
        ["npm", "ci"], ["alembic", "upgrade", "head"], ["ruff", "check", "--fix", "."], ["curl", "https://example.com"],
        ["rm", "-rf", "."], ["python", "-c", "print(1)"],
    ]  # fmt: skip
    return {
        "environment": settings.environment,
        "llm": {"configured": settings.llm_configured, "model": settings.llm_model, "provider": settings.llm_provider},
        "sandbox": runtime.sandbox_status() | {
            "image": settings.sandbox_image, "memory_mb": settings.sandbox_memory_mb, "cpus": settings.sandbox_cpus,
            "pids_limit": settings.sandbox_pids_limit,
        },
        "repositories": {"allowed_roots": [str(p) for p in settings.allowed_roots], "max_file_bytes": settings.max_file_bytes},
        "agent": {
            "max_tool_steps": settings.agent_max_tool_steps, "max_repair_attempts": settings.agent_max_repair_attempts,
        },
        "approvals": {
            "expiry_hours": settings.approval_expiry_hours, "token_ttl_seconds": settings.approval_token_ttl_seconds,
            "actions": APPROVAL_ACTION_LABELS,
        },
        "registration_open": settings.allow_registration,
        "risk_levels": RISK_DESCRIPTIONS,
        "command_policy_examples": [
            {"command": " ".join(argv), "level": (d := classify_command(argv)).level, "reason": d.reason} for argv in examples
        ],
        "roles": {
            "viewer": "Read project data",
            "developer": "Create tasks, run checks, approve or reject requests",
            "owner": "Developer rights plus repository registration, membership and project settings",
            "admin": "Owner rights in every project plus user management and the audit log",
        },
    }  # fmt: skip
