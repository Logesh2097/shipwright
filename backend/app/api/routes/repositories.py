"""Repository registration, exploration and code intelligence."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import func, select

from app.api.deps import CurrentUser, SessionDep, SettingsDep, client_ip
from app.db.models import RepoFile, Repository
from app.errors import AppError, Conflict, NotFound, ServiceUnavailable
from app.mcp_client.gateway import context_for_user
from app.repo_intel import search
from app.repo_intel.indexer import index_repository, validate_repository_path
from app.repo_intel.unused import find_unused_code
from app.repo_intel.workspace import Workspace, WorkspaceError
from app.runtime import get_runtime
from app.schemas.api import RepositoryCreate, RepositoryOut
from app.security.paths import PathNotAllowed
from app.services.access import audit, require_project, require_repository

router = APIRouter(tags=["repositories"])


def _workspace(repository: Repository, max_bytes: int) -> Workspace:
    if not Path(repository.local_path).is_dir():
        raise AppError("The repository directory no longer exists on the server", code="repository_missing", status_code=409)
    return Workspace(Path(repository.local_path), max_file_bytes=max_bytes)


async def _blocking(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except PathNotAllowed as exc:
        raise AppError(str(exc), code="path_not_allowed", status_code=400, details={"reason": exc.reason}) from exc
    except WorkspaceError as exc:
        status = 404 if exc.code == "not_found" else 400
        raise AppError(str(exc), code=exc.code, status_code=status) from exc


@router.get("/projects/{project_id}/repositories", response_model=list[RepositoryOut])
async def list_repositories(project_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> list[Repository]:
    await require_project(session, user, project_id)
    rows = await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.created_at))
    return list(rows)


@router.post("/projects/{project_id}/repositories", response_model=RepositoryOut, status_code=201)
async def register_repository(
    project_id: uuid.UUID, body: RepositoryCreate, user: CurrentUser, session: SessionDep, settings: SettingsDep, request: Request
) -> Repository:
    await require_project(session, user, project_id, "owner")
    info = await asyncio.to_thread(validate_repository_path, settings, body.local_path)
    exists = await session.scalar(select(Repository.id).where(Repository.project_id == project_id, Repository.local_path == info.path))
    if exists:
        raise Conflict("This repository is already registered in the project", code="repository_exists")
    repository = Repository(
        project_id=project_id,
        name=(body.name or info.name).strip()[:200],
        local_path=info.path,
        default_branch=info.default_branch,
        remote_url=info.remote_url,
        github_full_name=info.github_full_name,
        head_commit=info.head_commit,
        status="indexing",
    )
    session.add(repository)
    await session.flush()
    await audit(session, "repository.register", actor=user, resource_type="repository", resource_id=repository.id,
                details={"path": info.path}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    runtime = get_runtime()
    runtime.spawn(index_repository(runtime.db, settings, repository.id), name=f"index-{repository.id}")
    return repository


@router.get("/repositories/{repository_id}", response_model=RepositoryOut)
async def get_repository(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Repository:
    repository, _ = await require_repository(session, user, repository_id)
    return repository


@router.delete("/repositories/{repository_id}", status_code=204)
async def delete_repository(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request) -> Response:
    """Removes the registration and its records. Files on disk are never touched."""
    repository, _ = await require_repository(session, user, repository_id, "owner")
    await audit(session, "repository.unregister", actor=user, resource_type="repository", resource_id=repository.id,
                details={"path": repository.local_path}, ip=client_ip(request))  # fmt: skip
    await session.delete(repository)
    await session.commit()
    await get_runtime().mcp.close_matching(repository_id=str(repository_id))
    return Response(status_code=204)


@router.post("/repositories/{repository_id}/index", response_model=RepositoryOut, status_code=202)
async def reindex(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep) -> Repository:
    repository, _ = await require_repository(session, user, repository_id, "developer")
    if repository.status == "indexing":
        raise Conflict("Indexing is already in progress", code="indexing_in_progress")
    repository.status = "indexing"
    await session.commit()
    runtime = get_runtime()
    runtime.spawn(index_repository(runtime.db, settings, repository.id), name=f"index-{repository.id}")
    return repository


@router.get("/repositories/{repository_id}/tree")
async def tree(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep,
    path: Annotated[str, Query(max_length=1000)] = ".",
) -> dict[str, Any]:  # fmt: skip
    repository, _ = await require_repository(session, user, repository_id)
    return await _blocking(_workspace(repository, settings.max_file_bytes).list_directory, path, 500)


@router.get("/repositories/{repository_id}/file")
async def read_file(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep,
    path: Annotated[str, Query(min_length=1, max_length=1000)],
    start_line: Annotated[int, Query(ge=1)] = 1,
    max_lines: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> dict[str, Any]:  # fmt: skip
    repository, _ = await require_repository(session, user, repository_id)
    return await _blocking(_workspace(repository, settings.max_file_bytes).read_file, path, start_line, max_lines)


@router.get("/repositories/{repository_id}/files")
async def list_files(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:  # fmt: skip
    await require_repository(session, user, repository_id)
    stmt = select(RepoFile).where(RepoFile.repository_id == repository_id)
    if q:
        stmt = stmt.where(func.lower(RepoFile.path).contains(q.lower(), autoescape=True))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.scalars(stmt.order_by(RepoFile.path).limit(limit))
    return {
        "total": total,
        "items": [
            {"path": f.path, "language": f.language, "lines": f.line_count, "size": f.size_bytes, "is_test": f.is_test} for f in rows
        ],
    }


@router.get("/repositories/{repository_id}/symbols")
async def symbols(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: Annotated[str | None, Query(pattern="^(function|class|method|route|component|variable|import)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:  # fmt: skip
    await require_repository(session, user, repository_id)
    return {"items": await search.search_symbols(session, repository_id, q, kind=kind, limit=limit)}


@router.get("/repositories/{repository_id}/search")
async def search_text(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep,
    q: Annotated[str, Query(min_length=1, max_length=300)],
    regex: bool = False,
    path_glob: Annotated[str | None, Query(max_length=300)] = None,
) -> dict[str, Any]:  # fmt: skip
    repository, _ = await require_repository(session, user, repository_id)
    ws = _workspace(repository, settings.max_file_bytes)
    return await _blocking(ws.search_text, q, regex=regex, path_glob=path_glob, max_results=200)


@router.get("/repositories/{repository_id}/relevant-files")
async def relevant(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
) -> dict[str, Any]:  # fmt: skip
    repository, _ = await require_repository(session, user, repository_id)
    return {"items": await search.relevant_files(session, repository, q, limit=20)}


@router.get("/repositories/{repository_id}/routes")
async def routes(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    await require_repository(session, user, repository_id)
    return {"items": await search.list_routes(session, repository_id)}


@router.get("/repositories/{repository_id}/context")
async def context(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    repository, _ = await require_repository(session, user, repository_id)
    return {"overview": await search.build_overview(session, repository), "profile": repository.profile}


@router.get("/repositories/{repository_id}/unused-code")
async def unused_code(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    repository, _ = await require_repository(session, user, repository_id)
    paths = list(await session.scalars(select(RepoFile.path).where(RepoFile.repository_id == repository_id)))
    return await _blocking(find_unused_code, _workspace(repository, settings.max_file_bytes), paths)


async def _git_tool(session: SessionDep, user: CurrentUser, repository_id: uuid.UUID, tool: str, arguments: dict[str, Any]) -> Any:
    repository, role = await require_repository(session, user, repository_id)
    ctx = await context_for_user(session, user, repository, role)
    result = await get_runtime().gateway.call(tool, arguments, ctx)
    if not result.ok:
        if result.error_code == "mcp_unavailable":
            raise ServiceUnavailable(result.error_message or "MCP server unavailable", code="mcp_unavailable")
        if result.error_code == "not_found":
            raise NotFound(result.error_message or "Not found")
        raise AppError(result.error_message or "Tool call failed", code=result.error_code or "tool_error")
    return result.data


@router.get("/repositories/{repository_id}/git/status")
async def git_status(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Any:
    return await _git_tool(session, user, repository_id, "git_status", {})


@router.get("/repositories/{repository_id}/git/diff")
async def git_diff(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep,
    path: Annotated[str | None, Query(max_length=1000)] = None, staged: bool = False,
) -> Any:  # fmt: skip
    return await _git_tool(session, user, repository_id, "git_diff", {"path": path, "staged": staged})


@router.get("/repositories/{repository_id}/git/log")
async def git_log(
    repository_id: uuid.UUID, user: CurrentUser, session: SessionDep, max_count: Annotated[int, Query(ge=1, le=200)] = 30
) -> Any:
    return await _git_tool(session, user, repository_id, "git_log", {"max_count": max_count})


@router.get("/repositories/{repository_id}/git/branches")
async def git_branches(repository_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> Any:
    return await _git_tool(session, user, repository_id, "git_branches", {})
