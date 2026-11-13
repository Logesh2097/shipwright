"""ORM models. See docs/database-design.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

ACTIVE_TASK_STATUSES = ("queued", "running", "awaiting_approval", "interrupted")
TERMINAL_TASK_STATUSES = ("completed", "failed", "cancelled")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def _fk(target: str, *, ondelete: str = "CASCADE", nullable: bool = False, index: bool = True) -> Any:
    return mapped_column(Uuid, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="developer")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class UserCredential(Base):
    __tablename__ = "user_credentials"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    hint: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    owner_id: Mapped[uuid.UUID] = _fk("users.id", ondelete="RESTRICT")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    members: Mapped[list[ProjectMember]] = relationship(back_populates="project", cascade="all, delete-orphan", passive_deletes=True)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="developer")
    created_at: Mapped[datetime] = _created()

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "local_path"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _fk("projects.id")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    local_path: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    remote_url: Mapped[str | None] = mapped_column(Text)
    github_full_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="registered")
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    head_commit: Mapped[str | None] = mapped_column(String(64))
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class RepoFile(Base):
    __tablename__ = "repo_files"
    __table_args__ = (UniqueConstraint("repository_id", "path"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_binary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    indexed_at: Mapped[datetime] = _created()


class CodeSymbol(Base):
    __tablename__ = "code_symbols"
    __table_args__ = (Index("ix_code_symbols_repo_name", "repository_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id", index=False)
    file_id: Mapped[uuid.UUID] = _fk("repo_files.id")
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    qualified_name: Mapped[str] = mapped_column(String(600), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    file: Mapped[RepoFile] = relationship()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index(
            "uq_tasks_active_dedupe",
            "repository_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'awaiting_approval', 'interrupted')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = _fk("projects.id")
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id")
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="SET NULL", nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = _fk("tasks.id")
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued", index=True)
    current_node: Mapped[str | None] = mapped_column(String(60))
    classification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    analysis: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    final_report: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (Index("ix_agent_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = _fk("agent_runs.id", index=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    node: Mapped[str | None] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class ChangeSet(Base):
    __tablename__ = "change_sets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID | None] = _fk("agent_runs.id", ondelete="SET NULL", nullable=True)
    task_id: Mapped[uuid.UUID | None] = _fk("tasks.id", ondelete="SET NULL", nullable=True)
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id")
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)
    base_commit: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    files: Mapped[list[FileChange]] = relationship(
        back_populates="change_set",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="FileChange.path",
    )


class FileChange(Base):
    __tablename__ = "file_changes"
    __table_args__ = (UniqueConstraint("change_set_id", "path"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    change_set_id: Mapped[uuid.UUID] = _fk("change_sets.id")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    change_type: Mapped[str] = mapped_column(String(10), nullable=False)
    base_hash: Mapped[str | None] = mapped_column(String(64))
    original_content: Mapped[str | None] = mapped_column(Text)
    new_content: Mapped[str | None] = mapped_column(Text)
    new_hash: Mapped[str | None] = mapped_column(String(64))
    diff: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text)

    change_set: Mapped[ChangeSet] = relationship(back_populates="files")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id")
    task_id: Mapped[uuid.UUID | None] = _fk("tasks.id", ondelete="SET NULL", nullable=True)
    run_id: Mapped[uuid.UUID | None] = _fk("agent_runs.id", ondelete="SET NULL", nullable=True)
    change_set_id: Mapped[uuid.UUID | None] = _fk("change_sets.id", ondelete="SET NULL", nullable=True)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="SET NULL", nullable=True, index=False)
    decided_by: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="SET NULL", nullable=True, index=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (Index("ix_tool_executions_repo_created", "repository_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID | None] = _fk("agent_runs.id", ondelete="SET NULL", nullable=True)
    repository_id: Mapped[uuid.UUID | None] = _fk("repositories.id", ondelete="CASCADE", nullable=True, index=False)
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="SET NULL", nullable=True)
    approval_id: Mapped[uuid.UUID | None] = _fk("approvals.id", ondelete="SET NULL", nullable=True, index=False)
    server: Mapped[str] = mapped_column(String(30), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result_preview: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()


class TestRun(Base):
    __tablename__ = "test_runs"
    __test__ = False  # not a pytest class

    id: Mapped[uuid.UUID] = _uuid_pk()
    repository_id: Mapped[uuid.UUID] = _fk("repositories.id")
    task_id: Mapped[uuid.UUID | None] = _fk("tasks.id", ondelete="SET NULL", nullable=True)
    run_id: Mapped[uuid.UUID | None] = _fk("agent_runs.id", ondelete="SET NULL", nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="SET NULL", nullable=True, index=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    framework: Mapped[str] = mapped_column(String(30), nullable=False)
    command: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    targets: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _created()

    cases: Mapped[list[TestCaseResult]] = relationship(back_populates="test_run", cascade="all, delete-orphan", passive_deletes=True)


class TestCaseResult(Base):
    __tablename__ = "test_case_results"
    __test__ = False

    id: Mapped[uuid.UUID] = _uuid_pk()
    test_run_id: Mapped[uuid.UUID] = _fk("test_runs.id")
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    file: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)

    test_run: Mapped[TestRun] = relationship(back_populates="cases")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_user_id: Mapped[uuid.UUID | None] = _fk("users.id", ondelete="SET NULL", nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_id: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
