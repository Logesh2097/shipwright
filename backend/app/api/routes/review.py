"""Change sets, approvals, test runs, and requests for sensitive operations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.agent.graph import payload_hash
from app.api.deps import CurrentUser, PageDep, SessionDep, SettingsDep, client_ip
from app.db.models import Approval, ChangeSet, FileChange, Repository, TestRun
from app.errors import AppError, Conflict, NotFound
from app.mcp_client.gateway import context_for_user
from app.runtime import get_runtime
from app.schemas.api import (
    ApprovalDecision,
    ApprovalOut,
    ChangeSetOut,
    ChangeSetSummary,
    CommandRequest,
    GitBranchRequest,
    GitCommitRequest,
    GitPushRequest,
    ListResponse,
    PullRequestCreate,
    TestRunCreate,
    TestRunDetail,
    TestRunOut,
)
from app.security.gitsafe import is_valid_branch_name
from app.security.paths import PathNotAllowed, is_secret_path, normalize_relative
from app.services import changes, verification
from app.services.access import audit, require_repository, visible_project_ids
from app.services.approvals import command_approval_type, create_approval, decide_approval

router = APIRouter()


async def _visible_repository_ids(session: SessionDep, user: CurrentUser) -> Any:
    visible = await visible_project_ids(session, user)
    if visible is None:
        return None
    return select(Repository.id).where(Repository.project_id.in_(visible))


# --- change sets -----------------------------------------------------------------------


@router.get("/change-sets", response_model=ListResponse[ChangeSetSummary], tags=["changes"])
async def list_change_sets(
    user: CurrentUser, session: SessionDep, page: PageDep,
    task_id: uuid.UUID | None = None, repository_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(max_length=30)] = None,
) -> ListResponse[ChangeSetSummary]:  # fmt: skip
    query = select(ChangeSet).where(ChangeSet.status != "draft")
    repo_ids = await _visible_repository_ids(session, user)
    if repo_ids is not None:
        query = query.where(ChangeSet.repository_id.in_(repo_ids))
    if task_id:
        query = query.where(ChangeSet.task_id == task_id)
    if repository_id:
        query = query.where(ChangeSet.repository_id == repository_id)
    if status:
        query = query.where(ChangeSet.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await session.scalars(query.order_by(ChangeSet.created_at.desc()).limit(page.limit).offset(page.offset)))
    counts = dict(
        (
            await session.execute(
                select(FileChange.change_set_id, func.count()).where(FileChange.change_set_id.in_([r.id for r in rows])).group_by(FileChange.change_set_id)
            )
        ).all()
    ) if rows else {}  # fmt: skip
    items = []
    for row in rows:
        item = ChangeSetSummary.model_validate(row)
        item.file_count = counts.get(row.id, 0)
        items.append(item)
    return ListResponse(items=items, total=total)


async def _require_change_set(
    session: SessionDep, user: CurrentUser, change_set_id: uuid.UUID, min_role: str = "viewer"
) -> tuple[ChangeSet, Repository, str]:
    change_set = await changes.get_change_set(session, change_set_id)
    if change_set is None:
        raise NotFound("Change set not found")
    try:
        repository, role = await require_repository(session, user, change_set.repository_id, min_role)
    except NotFound:
        raise NotFound("Change set not found") from None
    return change_set, repository, role


@router.get("/change-sets/{change_set_id}", response_model=ChangeSetOut, tags=["changes"])
async def get_change_set(change_set_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ChangeSet:
    change_set, _, _ = await _require_change_set(session, user, change_set_id)
    return change_set


@router.get("/change-sets/{change_set_id}/staleness", tags=["changes"])
async def change_set_staleness(change_set_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """Whether the files still match what the proposal was based on (checked before approving)."""
    from pathlib import Path

    from app.repo_intel.workspace import Workspace

    change_set, repository, _ = await _require_change_set(session, user, change_set_id)
    if change_set.status not in ("proposed", "approved"):
        return {"applicable": False, "stale_files": []}
    ws = Workspace(Path(repository.local_path), max_file_bytes=settings.max_file_bytes)
    return {"applicable": True, "stale_files": changes.stale_files(ws, change_set)}


@router.post("/change-sets/{change_set_id}/revert", response_model=ChangeSetOut, tags=["changes"])
async def revert_change_set(change_set_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request) -> ChangeSet:
    """Restore files written by the agent. Files edited since then are left alone and reported."""
    change_set, repository, role = await _require_change_set(session, user, change_set_id, "developer")
    if change_set.status not in ("applied", "partially_reverted"):
        raise Conflict(f"Only applied change sets can be reverted (status: {change_set.status})", code="not_applied")
    payload = {"change_set_id": str(change_set.id), "files": [f.path for f in change_set.files if f.status == "applied"]}
    # The developer's explicit request is the approval; it is recorded like any other.
    approval = Approval(
        repository_id=repository.id, task_id=change_set.task_id, change_set_id=change_set.id,
        action_type="revert_changes", title=f"Revert: {change_set.title}"[:300], payload=payload,
        payload_hash=payload_hash(payload), status="approved", requested_by_user_id=user.id, decided_by=user.id,
        decided_at=datetime.now(UTC),
    )  # fmt: skip
    session.add(approval)
    await session.flush()
    await session.commit()
    runtime = get_runtime()
    ctx = await context_for_user(session, user, repository, role, approval=approval)
    result = await changes.revert_change_set(session, runtime.gateway, repository, change_set, ctx)
    approval.status = "executed" if result["reverted"] and not result["conflicts"] else "failed" if not result["reverted"] else "executed"
    approval.result = result
    await audit(session, "change_set.revert", actor=user, resource_type="change_set", resource_id=change_set.id,
                details=result, ip=client_ip(request))  # fmt: skip
    await session.commit()
    from app.repo_intel.indexer import index_repository

    runtime.spawn(index_repository(runtime.db, runtime.settings, repository.id), name=f"index-{repository.id}")
    refreshed = await changes.get_change_set(session, change_set.id)
    assert refreshed is not None
    return refreshed


# --- approvals -------------------------------------------------------------------------


@router.get("/approvals", response_model=ListResponse[ApprovalOut], tags=["approvals"])
async def list_approvals(
    user: CurrentUser, session: SessionDep, page: PageDep,
    status: Annotated[str | None, Query(max_length=20)] = None,
    repository_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
) -> ListResponse[ApprovalOut]:  # fmt: skip
    query = select(Approval)
    repo_ids = await _visible_repository_ids(session, user)
    if repo_ids is not None:
        query = query.where(Approval.repository_id.in_(repo_ids))
    if status:
        query = query.where(Approval.status == status)
    if repository_id:
        query = query.where(Approval.repository_id == repository_id)
    if task_id:
        query = query.where(Approval.task_id == task_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(query.order_by(Approval.created_at.desc()).limit(page.limit).offset(page.offset))
    return ListResponse(items=[ApprovalOut.model_validate(a) for a in rows], total=total)


@router.get("/approvals/{approval_id}", response_model=ApprovalOut, tags=["approvals"])
async def get_approval(approval_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFound("Approval not found")
    try:
        await require_repository(session, user, approval.repository_id)
    except NotFound:
        raise NotFound("Approval not found") from None
    return approval


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut, tags=["approvals"])
async def approve(approval_id: uuid.UUID, body: ApprovalDecision, user: CurrentUser, session: SessionDep, request: Request) -> Approval:
    return await decide_approval(
        get_runtime(), session, approval_id, user, approve=True, comment=body.comment,
        confirm_deletions=body.confirm_deletions, ip=client_ip(request),
    )  # fmt: skip


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut, tags=["approvals"])
async def reject(approval_id: uuid.UUID, body: ApprovalDecision, user: CurrentUser, session: SessionDep, request: Request) -> Approval:
    return await decide_approval(get_runtime(), session, approval_id, user, approve=False, comment=body.comment, ip=client_ip(request))


# --- requests for sensitive operations (each creates a pending approval) ---------------


async def _request(
    session: SessionDep, settings: SettingsDep, user: CurrentUser, repository: Repository, action: str, payload: dict[str, Any], title: str
) -> Approval:
    approval = await create_approval(
        session, expiry_hours=settings.approval_expiry_hours, repository_id=repository.id, action_type=action,
        payload=payload, title=title, requested_by=user,
    )  # fmt: skip
    await session.commit()
    return approval


@router.post("/repositories/{repository_id}/git/branch", response_model=ApprovalOut, status_code=201, tags=["git"])
async def request_branch(
    repository_id: uuid.UUID, body: GitBranchRequest, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> Approval:
    repository, _ = await require_repository(session, user, repository_id, "developer")
    if not is_valid_branch_name(body.name):
        raise AppError("Invalid branch name", code="invalid_branch")
    return await _request(session, settings, user, repository, "git_create_branch", {"name": body.name}, f"Create branch '{body.name}'")


@router.post("/repositories/{repository_id}/git/commit", response_model=ApprovalOut, status_code=201, tags=["git"])
async def request_commit(
    repository_id: uuid.UUID, body: GitCommitRequest, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> Approval:
    repository, _ = await require_repository(session, user, repository_id, "developer")
    paths: list[str] = []
    for path in body.paths:
        try:
            normalized = normalize_relative(path)
        except PathNotAllowed as exc:
            raise AppError(str(exc), code="path_not_allowed") from exc
        if normalized == "." or is_secret_path(normalized) or ".git" in normalized.split("/"):
            raise AppError(f"'{path}' cannot be committed", code="path_not_allowed")
        paths.append(normalized)
    payload = {"paths": sorted(set(paths)), "message": body.message.strip()}
    return await _request(session, settings, user, repository, "git_commit", payload, f"Commit {len(payload['paths'])} file(s)")


@router.post("/repositories/{repository_id}/git/push", response_model=ApprovalOut, status_code=201, tags=["git"])
async def request_push(
    repository_id: uuid.UUID, body: GitPushRequest, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> Approval:
    repository, _ = await require_repository(session, user, repository_id, "developer")
    if not repository.github_full_name:
        raise AppError("This repository has no GitHub remote named 'origin'", code="not_a_github_remote", status_code=409)
    if not is_valid_branch_name(body.branch):
        raise AppError("Invalid branch name", code="invalid_branch")
    if repository.default_branch and body.branch == repository.default_branch:
        raise AppError("Pushing directly to the default branch is not allowed", code="default_branch_protected", status_code=409)
    payload = {"branch": body.branch, "repository": repository.github_full_name}
    return await _request(
        session, settings, user, repository, "git_push", payload, f"Push '{body.branch}' to {repository.github_full_name}"
    )


@router.post("/repositories/{repository_id}/github/pull-requests", response_model=ApprovalOut, status_code=201, tags=["github"])
async def request_pull_request(
    repository_id: uuid.UUID, body: PullRequestCreate, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> Approval:
    repository, _ = await require_repository(session, user, repository_id, "developer")
    if not repository.github_full_name:
        raise AppError("This repository has no GitHub remote named 'origin'", code="not_a_github_remote", status_code=409)
    base = body.base or repository.default_branch
    if not base:
        raise AppError("A base branch is required", code="base_required")
    if body.head == base:
        raise AppError("Head and base branches must differ", code="invalid_pull_request")
    payload = {
        "full_name": repository.github_full_name, "head": body.head, "base": base, "title": body.title.strip(),
        "body": body.body, "draft": body.draft,
    }  # fmt: skip
    return await _request(session, settings, user, repository, "github_create_pr", payload, f"Open pull request: {body.title.strip()}")


@router.post("/repositories/{repository_id}/commands", response_model=ApprovalOut, status_code=201, tags=["terminal"])
async def request_command(
    repository_id: uuid.UUID, body: CommandRequest, user: CurrentUser, session: SessionDep, settings: SettingsDep,
    directory: Annotated[str, Query(max_length=1000)] = ".",
) -> Approval:  # fmt: skip
    repository, _ = await require_repository(session, user, repository_id, "developer")
    action = command_approval_type(body.argv)
    try:
        directory = normalize_relative(directory)
    except PathNotAllowed as exc:
        raise AppError(str(exc), code="path_not_allowed") from exc
    payload = {"argv": body.argv, "directory": directory, "timeout_seconds": 900}
    return await _request(session, settings, user, repository, action, payload, f"Run: {' '.join(body.argv)}"[:300])


# --- tests -----------------------------------------------------------------------------


@router.get("/repositories/{repository_id}/tests/discover", tags=["tests"])
async def discover_tests(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Any:
    repository, role = await require_repository(session, user, repository_id)
    ctx = await context_for_user(session, user, repository, role)
    result = await get_runtime().gateway.call("test_discover", {}, ctx)
    if not result.ok:
        raise AppError(
            result.error_message or "Discovery failed",
            code=result.error_code or "tool_error",
            status_code=503 if result.error_code == "mcp_unavailable" else 400,
        )
    return result.data


@router.post("/repositories/{repository_id}/test-runs", response_model=TestRunDetail, status_code=201, tags=["tests"])
async def create_test_run(
    repository_id: uuid.UUID, body: TestRunCreate, user: CurrentUser, session: SessionDep,
    directory: Annotated[str, Query(max_length=1000)] = ".",
) -> TestRun:  # fmt: skip
    """Run a check in the sandbox and store the result. The developer's request is the authorisation."""
    repository, role = await require_repository(session, user, repository_id, "developer")
    kinds = {
        "pytest": ("unit", "integration"), "vitest": ("unit", "integration"), "jest": ("unit", "integration"),
        "npm": ("unit", "integration", "build"), "ruff": ("lint",), "eslint": ("lint",),
        "mypy": ("typecheck",), "vue-tsc": ("typecheck",), "tsc": ("typecheck",),
    }  # fmt: skip
    if body.kind not in kinds[body.tool]:
        raise AppError(f"'{body.tool}' cannot be used for a {body.kind} run", code="invalid_combination")
    ctx = await context_for_user(session, user, repository, role)
    # A developer explicitly asking for this run is sufficient even when automatic runs are disabled.
    ctx.auto_run_tests = True
    run = await verification.run_check(
        get_runtime().gateway, ctx, tool=body.tool, kind=body.kind, targets=body.targets, directory=directory
    )
    return await _load_test_run(session, run.id)


async def _load_test_run(session: SessionDep, test_run_id: uuid.UUID) -> TestRun:
    run = await session.scalar(select(TestRun).where(TestRun.id == test_run_id).options(selectinload(TestRun.cases)))
    if run is None:
        raise NotFound("Test run not found")
    return run


@router.get("/test-runs", response_model=ListResponse[TestRunOut], tags=["tests"])
async def list_test_runs(
    user: CurrentUser, session: SessionDep, page: PageDep,
    repository_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
) -> ListResponse[TestRunOut]:  # fmt: skip
    query = select(TestRun)
    repo_ids = await _visible_repository_ids(session, user)
    if repo_ids is not None:
        query = query.where(TestRun.repository_id.in_(repo_ids))
    if repository_id:
        query = query.where(TestRun.repository_id == repository_id)
    if task_id:
        query = query.where(TestRun.task_id == task_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await session.scalars(query.order_by(TestRun.created_at.desc()).limit(page.limit).offset(page.offset))
    return ListResponse(items=[TestRunOut.model_validate(r) for r in rows], total=total)


@router.get("/test-runs/{test_run_id}", response_model=TestRunDetail, tags=["tests"])
async def get_test_run(test_run_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> TestRun:
    run = await _load_test_run(session, test_run_id)
    try:
        await require_repository(session, user, run.repository_id)
    except NotFound:
        raise NotFound("Test run not found") from None
    return run
