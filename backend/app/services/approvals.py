"""Approval requests, decisions, and execution of approved operations.

Every sensitive operation is represented by an approval row describing exactly what will
run. A decision can be made once, by a developer with access to the project. Execution
happens only after the decision is committed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import payload_hash
from app.db.models import AgentRun, Approval, Project, Repository, User
from app.errors import AppError, Conflict, NotFound
from app.logs import get_logger
from app.mcp_client.gateway import CallContext
from app.mcp_client.policy import APPROVAL_ACTION_LABELS
from app.repo_intel.indexer import index_repository
from app.security.commands import classify_command
from app.services import changes
from app.services.access import audit, effective_role, require_repository

if TYPE_CHECKING:
    from app.runtime import Runtime

log = get_logger(__name__)


async def create_approval(
    session: AsyncSession,
    *,
    expiry_hours: int,
    repository_id: uuid.UUID,
    action_type: str,
    payload: dict[str, Any],
    title: str | None = None,
    requested_by: User | None = None,
    task_id: uuid.UUID | None = None,
    change_set_id: uuid.UUID | None = None,
) -> Approval:
    if action_type not in APPROVAL_ACTION_LABELS:
        raise AppError(f"Unknown approval type '{action_type}'")
    approval = Approval(
        repository_id=repository_id,
        task_id=task_id,
        change_set_id=change_set_id,
        action_type=action_type,
        title=(title or APPROVAL_ACTION_LABELS[action_type])[:300],
        payload=payload,
        payload_hash=payload_hash(payload),
        requested_by_user_id=requested_by.id if requested_by else None,
        expires_at=datetime.now(UTC) + timedelta(hours=expiry_hours),
    )
    session.add(approval)
    await session.flush()
    await audit(
        session, "approval.request", actor=requested_by, actor_type="user" if requested_by else "agent",
        resource_type="approval", resource_id=approval.id, details={"action_type": action_type, "payload": payload},
    )  # fmt: skip
    return approval


def command_approval_type(argv: list[str]) -> str:
    """Classify a command request; denied commands cannot even be requested."""
    decision = classify_command(argv)
    if decision.level == "denied":
        raise AppError(f"This command is not allowed: {decision.reason}", code="command_denied", status_code=400)
    if decision.level == "safe":
        raise AppError("This command does not need approval; run it from the Tests screen instead", code="approval_not_needed")
    return decision.category if decision.category in ("install_dependencies", "run_migration") else "sensitive_command"


async def decide_approval(
    runtime: Runtime,
    session: AsyncSession,
    approval_id: uuid.UUID,
    user: User,
    *,
    approve: bool,
    comment: str | None,
    confirm_deletions: bool = False,
    ip: str | None = None,
) -> Approval:
    """Record a decision (exactly once) and trigger whatever follows from it."""
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFound("Approval not found")
    try:
        await require_repository(session, user, approval.repository_id, "developer")
    except NotFound:
        raise NotFound("Approval not found") from None
    # Lock the row so two simultaneous decisions cannot both succeed.
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update().execution_options(populate_existing=True)
    )
    assert approval is not None
    if approval.status != "pending":
        raise Conflict(f"This request was already {approval.status}", code="approval_already_decided")
    now = datetime.now(UTC)
    if approval.expires_at is not None and approval.expires_at < now:
        approval.status = "expired"
        await session.commit()
        if approval.run_id is not None:
            await _notify_run(runtime, approval, "expired", None)
        raise Conflict("This request has expired", code="approval_expired")
    if payload_hash(approval.payload) != approval.payload_hash:
        approval.status = "failed"
        approval.result = {"error": "payload was modified after the request was created"}
        await audit(session, "security.approval_payload_tampered", actor=user, resource_type="approval", resource_id=approval.id, ip=ip)
        await session.commit()
        raise Conflict("This request is no longer valid", code="approval_invalid")

    change_set = None
    if approval.change_set_id is not None:
        change_set = await changes.get_change_set(session, approval.change_set_id, lock=True)
    if approve and approval.action_type == "apply_changes":
        if change_set is None or change_set.status != "proposed":
            raise Conflict("The change set is no longer awaiting review", code="stale_change_set")
        deletes = [f.path for f in change_set.files if f.change_type == "delete"]
        if deletes and not confirm_deletions:
            raise AppError(
                "This change deletes files. Confirm the deletions explicitly to approve it.",
                code="deletion_confirmation_required", status_code=409, details={"deletes": deletes},
            )  # fmt: skip

    approval.status = "approved" if approve else "rejected"
    approval.decided_by = user.id
    approval.decided_at = now
    approval.comment = comment
    if change_set is not None and approval.action_type == "apply_changes":
        change_set.status = "approved" if approve else "rejected"
        change_set.decided_at = now
    await audit(
        session, "approval.approve" if approve else "approval.reject", actor=user, resource_type="approval",
        resource_id=approval.id, details={"action_type": approval.action_type, "comment": comment}, ip=ip,
    )  # fmt: skip
    await session.commit()

    decision = "approved" if approve else "rejected"
    if approval.run_id is not None and await _notify_run(runtime, approval, decision, comment):
        return approval
    if approve:
        runtime.spawn(execute_approval(runtime, approval.id, user.id), name=f"approval-{approval.id}")
    return approval


async def _notify_run(runtime: Runtime, approval: Approval, decision: str, comment: str | None) -> bool:
    """Resume the agent run that is waiting on this approval. Returns False if no run is waiting."""
    assert approval.run_id is not None
    async with runtime.db.session() as session:
        run = await session.get(AgentRun, approval.run_id)
        waiting = run is not None and run.status == "awaiting_approval"
    if not waiting:
        return False
    await runtime.runner.resume_with_decision(approval.run_id, {"approval_id": str(approval.id), "decision": decision, "comment": comment})
    return True


async def execute_approval(runtime: Runtime, approval_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Carry out an approved, user-requested operation and record the outcome."""
    db, gateway = runtime.db, runtime.gateway
    async with db.session() as session:
        approval = await session.get(Approval, approval_id)
        user = await session.get(User, user_id)
        if approval is None or user is None or approval.status != "approved":
            return
        repository = await session.get(Repository, approval.repository_id)
        if repository is None:
            approval.status, approval.result = "failed", {"error": "repository no longer exists"}
            return
        project = await session.get(Project, repository.project_id)
        from app.db.models import ProjectMember

        membership = await session.get(ProjectMember, (repository.project_id, user.id))
        role = effective_role(user, membership.role if membership else None) or "viewer"
        ctx = CallContext(
            repository=repository, role=role, user_id=user.id, actor="user", approval=approval,
            auto_run_tests=bool((project.settings or {}).get("auto_run_tests", True)) if project else True,
        )  # fmt: skip
        payload = approval.payload
        action = approval.action_type
        result: dict[str, Any]
        ok = False
        try:
            if action == "apply_changes":
                change_set = await changes.get_change_set(session, uuid.UUID(payload["change_set_id"]))
                if change_set is None or change_set.status != "approved":
                    result = {"error": "change set is not in an approved state"}
                else:
                    result = await changes.apply_change_set(session, gateway, repository, change_set, ctx)
                    ok = result["status"] == "applied"
            else:
                tool, arguments = _tool_call_for(action, payload, user, repository)
                outcome = await gateway.call(tool, arguments, ctx)
                ok = outcome.ok and not (isinstance(outcome.data, dict) and outcome.data.get("exit_code") not in (0, None))
                result = (
                    outcome.data if outcome.ok and isinstance(outcome.data, dict)
                    else {"error_code": outcome.error_code, "error": outcome.error_message}
                )  # fmt: skip
        except Exception as exc:
            log.error("approval_execution_failed", approval_id=str(approval_id), exc_info=exc)
            result = {"error": f"{type(exc).__name__}: {exc}"[:500]}
        approval.status = "executed" if ok else "failed"
        approval.result = result
        await audit(
            session, f"operation.{action}", actor=user, resource_type="approval", resource_id=approval.id,
            details={"ok": ok, "result": result},
        )  # fmt: skip
        repository_id = repository.id
    if ok and action in ("apply_changes", "install_dependencies"):
        await index_repository(db, runtime.settings, repository_id)


def _tool_call_for(action: str, payload: dict[str, Any], user: User, repository: Repository) -> tuple[str, dict[str, Any]]:
    if action == "git_create_branch":
        return "git_create_branch", {"name": payload["name"], "checkout": True}
    if action == "git_commit":
        return "git_commit", {
            "paths": payload["paths"], "message": payload["message"],
            "author_name": user.full_name or user.email.split("@")[0], "author_email": user.email,
        }  # fmt: skip
    if action == "git_push":
        return "github_push_branch", {"branch": payload["branch"]}
    if action == "github_create_pr":
        return "github_create_pull_request", {
            "full_name": payload["full_name"], "head": payload["head"], "base": payload["base"],
            "title": payload["title"], "body": payload.get("body", ""), "draft": bool(payload.get("draft", False)),
        }  # fmt: skip
    if action in ("install_dependencies", "run_migration", "sensitive_command"):
        return "terminal_run_sensitive", {
            "argv": payload["argv"], "timeout_seconds": int(payload.get("timeout_seconds", 900)),
            "directory": payload.get("directory", "."),
        }  # fmt: skip
    raise AppError(f"Approval type '{action}' cannot be executed directly")
