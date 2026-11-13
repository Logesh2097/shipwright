"""Project-scoped authorisation. Every data access goes through these helpers."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Project, ProjectMember, Repository, User
from app.errors import Forbidden, NotFound
from app.security.redaction import redact_value

ROLE_RANK = {"viewer": 1, "developer": 2, "owner": 3}


def effective_role(user: User, membership_role: str | None) -> str | None:
    """Combine the global role with the project membership role."""
    if user.role == "admin":
        return "owner"
    if membership_role is None:
        return None
    if user.role == "viewer":
        return "viewer"
    return membership_role


async def project_role(session: AsyncSession, user: User, project_id: uuid.UUID) -> str | None:
    membership = await session.get(ProjectMember, (project_id, user.id))
    return effective_role(user, membership.role if membership else None)


async def require_project(session: AsyncSession, user: User, project_id: uuid.UUID, min_role: str = "viewer") -> tuple[Project, str]:
    project = await session.get(Project, project_id)
    role = await project_role(session, user, project_id) if project else None
    if project is None or role is None:
        # Same answer for "does not exist" and "not yours", so existence is not leaked.
        raise NotFound("Project not found")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise Forbidden(f"This action requires the '{min_role}' role in the project")
    return project, role


async def require_repository(
    session: AsyncSession, user: User, repository_id: uuid.UUID, min_role: str = "viewer"
) -> tuple[Repository, str]:
    repository = await session.get(Repository, repository_id)
    if repository is None:
        raise NotFound("Repository not found")
    try:
        _, role = await require_project(session, user, repository.project_id, min_role)
    except NotFound:
        raise NotFound("Repository not found") from None
    return repository, role


async def visible_project_ids(session: AsyncSession, user: User) -> list[uuid.UUID] | None:
    """Project ids the user may read; ``None`` means all (administrators)."""
    if user.role == "admin":
        return None
    rows = await session.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id))
    return list(rows)


async def audit(
    session: AsyncSession,
    action: str,
    *,
    actor: User | None = None,
    actor_type: str | None = None,
    resource_type: str | None = None,
    resource_id: Any = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_user_id=actor.id if actor else None,
            actor_type=actor_type or ("user" if actor else "system"),
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=redact_value(details or {}, max_string=2000),
            ip_address=ip,
        )
    )
