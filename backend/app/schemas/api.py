"""Request and response models for the REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ListResponse[T](BaseModel):
    items: list[T]
    total: int


# --- auth / users ----------------------------------------------------------------------


def _validate_email(value: str) -> str:
    value = value.strip().lower()
    local, _, domain = value.partition("@")
    if not local or "." not in domain or " " in value or len(value) > 320:
        raise ValueError("invalid email address")
    return value


class RegisterRequest(BaseModel):
    email: str
    full_name: str = Field(default="", max_length=200)
    password: str = Field(min_length=10, max_length=200)

    _email = field_validator("email")(_validate_email)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(max_length=200)

    _email = field_validator("email")(_validate_email)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(ORM):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class UserUpdate(BaseModel):
    role: Literal["admin", "developer", "viewer"] | None = None
    is_active: bool | None = None


# --- projects --------------------------------------------------------------------------


class ProjectSettings(BaseModel):
    auto_run_tests: bool = True


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    settings: ProjectSettings | None = None


class ProjectOut(ORM):
    id: uuid.UUID
    name: str
    description: str
    owner_id: uuid.UUID
    settings: dict[str, Any]
    created_at: datetime
    my_role: str | None = None
    repository_count: int = 0


class MemberUpsert(BaseModel):
    email: str
    role: Literal["owner", "developer", "viewer"] = "developer"

    _email = field_validator("email")(_validate_email)


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str


# --- repositories ----------------------------------------------------------------------


class RepositoryCreate(BaseModel):
    local_path: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, max_length=200)


class RepositoryOut(ORM):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    local_path: str
    default_branch: str | None
    remote_url: str | None
    github_full_name: str | None
    status: str
    profile: dict[str, Any]
    head_commit: str | None
    last_indexed_at: datetime | None
    index_error: str | None
    created_at: datetime


# --- tasks / runs ----------------------------------------------------------------------


class TaskCreate(BaseModel):
    repository_id: uuid.UUID
    request: str = Field(min_length=3, max_length=8000)
    title: str | None = Field(default=None, max_length=300)


class RunOut(ORM):
    id: uuid.UUID
    task_id: uuid.UUID
    status: str
    current_node: str | None
    classification: dict[str, Any] | None
    analysis: str | None
    plan: dict[str, Any] | None
    final_report: str | None
    error: str | None
    llm_calls: int
    input_tokens: int
    output_tokens: int
    repair_attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class TaskOut(ORM):
    id: uuid.UUID
    project_id: uuid.UUID
    repository_id: uuid.UUID
    created_by: uuid.UUID | None
    title: str
    request_text: str
    task_type: str | None
    status: str
    result_summary: str | None
    created_at: datetime
    updated_at: datetime
    latest_run: RunOut | None = None


class EventOut(ORM):
    id: int
    run_id: uuid.UUID
    type: str
    node: str | None
    payload: dict[str, Any]
    created_at: datetime


# --- change sets / approvals -----------------------------------------------------------


class FileChangeOut(ORM):
    id: uuid.UUID
    path: str
    change_type: str
    base_hash: str | None
    new_hash: str | None
    diff: str
    status: str
    error: str | None


class ChangeSetOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID | None
    task_id: uuid.UUID | None
    repository_id: uuid.UUID
    title: str
    summary: str
    status: str
    base_commit: str | None
    created_at: datetime
    decided_at: datetime | None
    applied_at: datetime | None
    files: list[FileChangeOut] = []


class ChangeSetSummary(ORM):
    id: uuid.UUID
    run_id: uuid.UUID | None
    task_id: uuid.UUID | None
    repository_id: uuid.UUID
    title: str
    summary: str
    status: str
    created_at: datetime
    applied_at: datetime | None
    file_count: int = 0


class ApprovalOut(ORM):
    id: uuid.UUID
    repository_id: uuid.UUID
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    change_set_id: uuid.UUID | None
    action_type: str
    title: str
    payload: dict[str, Any]
    status: str
    requested_by_user_id: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    comment: str | None
    result: dict[str, Any] | None
    expires_at: datetime | None
    created_at: datetime


class ApprovalDecision(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)
    confirm_deletions: bool = False


# --- tests -----------------------------------------------------------------------------


class TestRunCreate(BaseModel):
    __test__ = False
    kind: Literal["unit", "integration", "lint", "typecheck", "build"] = "unit"
    tool: Literal["pytest", "vitest", "jest", "npm", "ruff", "eslint", "mypy", "vue-tsc", "tsc"]
    targets: list[str] = Field(default_factory=list, max_length=50)


class TestCaseOut(ORM):
    __test__ = False
    id: uuid.UUID
    node_id: str
    file: str | None
    name: str
    outcome: str
    duration_ms: int
    message: str | None


class TestRunOut(ORM):
    __test__ = False
    id: uuid.UUID
    repository_id: uuid.UUID
    task_id: uuid.UUID | None
    run_id: uuid.UUID | None
    kind: str
    framework: str
    command: list[Any]
    targets: list[Any]
    status: str
    passed: int
    failed: int
    skipped: int
    errors: int
    not_executed: int
    duration_ms: int
    output_excerpt: str
    created_at: datetime


class TestRunDetail(TestRunOut):
    cases: list[TestCaseOut] = []


# --- git / github ----------------------------------------------------------------------


class GitBranchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class GitCommitRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)
    message: str = Field(min_length=3, max_length=5000)


class GitPushRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class PullRequestCreate(BaseModel):
    head: str = Field(min_length=1, max_length=200)
    base: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=60000)
    draft: bool = False


class CommandRequest(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=64)


class GitHubCredentialIn(BaseModel):
    token: str = Field(min_length=20, max_length=400)


class GitHubStatus(BaseModel):
    configured: bool
    hint: str | None = None
    login: str | None = None
    updated_at: datetime | None = None


# --- logs ------------------------------------------------------------------------------


class ToolExecutionOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID | None
    repository_id: uuid.UUID | None
    user_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    server: str
    tool_name: str
    risk_level: str
    arguments: dict[str, Any]
    status: str
    result_preview: str | None
    error: str | None
    duration_ms: int
    created_at: datetime


class AuditLogOut(ORM):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_type: str
    action: str
    resource_type: str | None
    resource_id: str | None
    details: dict[str, Any]
    ip_address: str | None
    created_at: datetime
