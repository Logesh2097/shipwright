"""Projects and membership."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from app.api.deps import CurrentUser, PageDep, SessionDep, client_ip
from app.db.models import Project, ProjectMember, Repository, User
from app.errors import AppError, Conflict, Forbidden, NotFound
from app.schemas.api import ListResponse, MemberOut, MemberUpsert, ProjectCreate, ProjectOut, ProjectUpdate
from app.services.access import audit, effective_role, require_project, visible_project_ids

router = APIRouter(tags=["projects"])


async def _project_out(session: SessionDep, project: Project, role: str | None) -> ProjectOut:
    count = await session.scalar(select(func.count()).select_from(Repository).where(Repository.project_id == project.id))
    out = ProjectOut.model_validate(project)
    out.my_role = role
    out.repository_count = count or 0
    return out


@router.get("/projects", response_model=ListResponse[ProjectOut])
async def list_projects(user: CurrentUser, session: SessionDep, page: PageDep) -> ListResponse[ProjectOut]:
    visible = await visible_project_ids(session, user)
    query = select(Project)
    if visible is not None:
        query = query.where(Project.id.in_(visible))
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    projects = await session.scalars(query.order_by(Project.created_at.desc()).limit(page.limit).offset(page.offset))
    memberships = {m.project_id: m.role for m in await session.scalars(select(ProjectMember).where(ProjectMember.user_id == user.id))}
    items = [await _project_out(session, p, effective_role(user, memberships.get(p.id))) for p in projects]
    return ListResponse(items=items, total=total)


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, user: CurrentUser, session: SessionDep, request: Request) -> ProjectOut:
    if user.role == "viewer":
        raise Forbidden("Viewers cannot create projects")
    name = body.name.strip()
    exists = await session.scalar(select(Project.id).where(Project.owner_id == user.id, Project.name == name))
    if exists:
        raise Conflict("You already have a project with this name", code="project_exists")
    project = Project(name=name, description=body.description, owner_id=user.id, settings={"auto_run_tests": True})
    project.members.append(ProjectMember(user_id=user.id, role="owner"))
    session.add(project)
    await session.flush()
    await audit(session, "project.create", actor=user, resource_type="project", resource_id=project.id,
                details={"name": name}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    return await _project_out(session, project, "owner")


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> ProjectOut:
    project, role = await require_project(session, user, project_id)
    return await _project_out(session, project, role)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, user: CurrentUser, session: SessionDep, request: Request
) -> ProjectOut:
    project, role = await require_project(session, user, project_id, "owner")
    if body.name is not None:
        project.name = body.name.strip()
    if body.description is not None:
        project.description = body.description
    if body.settings is not None:
        project.settings = {**project.settings, **body.settings.model_dump()}
    await audit(session, "project.update", actor=user, resource_type="project", resource_id=project.id,
                details=body.model_dump(exclude_none=True), ip=client_ip(request))  # fmt: skip
    await session.commit()
    return await _project_out(session, project, role)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request) -> Response:
    project, _ = await require_project(session, user, project_id, "owner")
    await audit(session, "project.delete", actor=user, resource_type="project", resource_id=project.id,
                details={"name": project.name}, ip=client_ip(request))  # fmt: skip
    await session.delete(project)
    await session.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/members", response_model=list[MemberOut])
async def list_members(project_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> list[MemberOut]:
    await require_project(session, user, project_id)
    rows = await session.execute(
        select(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.email)
    )
    return [MemberOut(user_id=u.id, email=u.email, full_name=u.full_name, role=m.role) for m, u in rows]


@router.put("/projects/{project_id}/members", response_model=MemberOut)
async def upsert_member(project_id: uuid.UUID, body: MemberUpsert, user: CurrentUser, session: SessionDep, request: Request) -> MemberOut:
    project, _ = await require_project(session, user, project_id, "owner")
    target = await session.scalar(select(User).where(User.email == body.email))
    if target is None:
        raise NotFound("No account with this email exists", code="user_not_found")
    membership = await session.get(ProjectMember, (project.id, target.id))
    if membership is None:
        membership = ProjectMember(project_id=project.id, user_id=target.id, role=body.role)
        session.add(membership)
    else:
        if membership.role == "owner" and body.role != "owner":
            await _ensure_other_owner(session, project.id, target.id)
        membership.role = body.role
    await audit(session, "project.member_upsert", actor=user, resource_type="project", resource_id=project.id,
                details={"user_id": str(target.id), "role": body.role}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    return MemberOut(user_id=target.id, email=target.email, full_name=target.full_name, role=membership.role)


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
async def remove_member(project_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: SessionDep, request: Request) -> Response:
    project, _ = await require_project(session, user, project_id, "owner")
    membership = await session.get(ProjectMember, (project.id, user_id))
    if membership is None:
        raise NotFound("Member not found")
    if membership.role == "owner":
        await _ensure_other_owner(session, project.id, user_id)
    await session.delete(membership)
    await audit(session, "project.member_remove", actor=user, resource_type="project", resource_id=project.id,
                details={"user_id": str(user_id)}, ip=client_ip(request))  # fmt: skip
    await session.commit()
    return Response(status_code=204)


async def _ensure_other_owner(session: SessionDep, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    others = await session.scalar(
        select(func.count()).select_from(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.role == "owner", ProjectMember.user_id != user_id
        )
    )  # fmt: skip
    if not others:
        raise AppError("A project must keep at least one owner", code="last_owner", status_code=409)
