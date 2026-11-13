"""Tool Gateway: the single path from the agent or the API to any tool.

For every call it checks the caller's permissions, requires an approved request for
write and sensitive tools, attaches a short-lived approval token, forwards the call,
redacts the result and records it. The decision never depends on what the model says.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.config import Settings
from app.db.models import Approval, AuditLog, ChangeSet, RepoFile, Repository, ToolExecution, User, UserCredential
from app.db.session import Database
from app.logs import get_logger
from app.mcp_client.manager import McpSessionManager, SessionKey, SessionSpec, ToolCallResult
from app.mcp_client.policy import APPROVAL_TOOLS, MIN_ROLE_FOR_RISK, TOOL_CATALOG, ToolSpec
from app.repo_intel import search
from app.repo_intel.unused import find_unused_code
from app.repo_intel.workspace import Workspace
from app.security.approval_tokens import META_KEY, mint_approval_token
from app.security.auth import decrypt_secret
from app.security.injection import scan_for_injection, wrap_untrusted
from app.security.redaction import redact_text, redact_value
from app.services import changes
from app.services.access import ROLE_RANK

log = get_logger(__name__)


@dataclass
class CallContext:
    repository: Repository | None
    role: str  # effective project role of the human on whose behalf this runs
    user_id: uuid.UUID | None = None
    actor: str = "user"  # "user" | "agent"
    run_id: uuid.UUID | None = None
    allowed_tools: frozenset[str] | None = None  # tools bound to the current agent node
    approval: Approval | None = None
    change_set_id: uuid.UUID | None = None
    auto_run_tests: bool = True
    notices: list[str] = field(default_factory=list)


# --- internal tool argument models (also the schemas shown to the model) ---------------


class _NoArgs(BaseModel):
    pass


class _SymbolSearch(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="Name or part of a name")
    kind: str | None = Field(default=None, description="function, class, method, route, component or variable")
    limit: int = Field(default=30, ge=1, le=100)


class _RelevantFiles(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="What you are looking for, in plain words")
    limit: int = Field(default=15, ge=1, le=40)


class _ProposeEdit(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    old_text: str = Field(min_length=1, max_length=200_000, description="Exact text to replace; must occur exactly once")
    new_text: str = Field(max_length=400_000, description="Replacement text")


class _ProposeCreate(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=800_000)


class _ProposeDelete(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    reason: str = Field(default="", max_length=500)


INTERNAL_TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "code_repo_overview": (_NoArgs, "Overview of the repository: languages, frameworks, layout, entry points and HTTP routes."),
    "code_search_symbols": (_SymbolSearch, "Find functions, classes, methods, routes and components by name in the code index."),
    "code_relevant_files": (_RelevantFiles, "Rank the files most relevant to a plain-language description."),
    "code_list_routes": (_NoArgs, "List the HTTP routes and frontend pages found in the repository."),
    "code_find_unused": (_NoArgs, "Static analysis: unused imports and top-level definitions that nothing references."),
    "propose_edit": (
        _ProposeEdit,
        "Propose replacing an exact piece of text in an existing file. Nothing is written until a developer approves.",
    ),
    "propose_create_file": (_ProposeCreate, "Propose a new file. Nothing is written until a developer approves."),
    "propose_delete_file": (_ProposeDelete, "Propose deleting a file. Needs explicit confirmation from the developer."),
}


class ToolGateway:
    def __init__(self, db: Database, settings: Settings, mcp: McpSessionManager) -> None:
        self.db = db
        self.settings = settings
        self.mcp = mcp

    # --- tool definitions for the model ------------------------------------------------

    async def tool_definitions(self, repository: Repository, names: frozenset[str] | set[str]) -> list[dict[str, Any]]:
        """OpenAI-style function definitions for the given tools (read/execute/proposal tools only)."""
        definitions: list[dict[str, Any]] = []
        workspace_tools = {}
        if any(TOOL_CATALOG[n].server == "workspace" for n in names if n in TOOL_CATALOG):
            session = await self.mcp.get(self._spec(TOOL_CATALOG["fs_read_file"], repository, None))
            workspace_tools = session.tools
        for name in sorted(names):
            spec = TOOL_CATALOG.get(name)
            if spec is None or spec.risk in ("write", "sensitive") or spec.server == "github":
                continue
            if spec.server == "internal":
                model, description = INTERNAL_TOOLS[name]
                schema = model.model_json_schema()
            else:
                info = workspace_tools.get(name)
                if info is None:
                    continue
                description, schema = info.description, info.input_schema
            schema = {k: v for k, v in schema.items() if k != "title"}
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            definitions.append({"type": "function", "function": {"name": name, "description": description[:1000], "parameters": schema}})
        return definitions

    # --- the call path -----------------------------------------------------------------

    async def call(self, tool: str, arguments: dict[str, Any], ctx: CallContext) -> ToolCallResult:
        started = time.perf_counter()
        spec = TOOL_CATALOG.get(tool)
        denial = self._authorize(tool, spec, ctx)
        if denial is not None:
            result = ToolCallResult.failure(*denial)
            await self._record(tool, spec, arguments, ctx, result, status="denied", security=True)
            return result
        assert spec is not None
        if not isinstance(arguments, dict):
            result = ToolCallResult.failure("invalid_arguments", "Tool arguments must be an object")
            await self._record(tool, spec, {}, ctx, result, status="error")
            return result

        try:
            if spec.server == "internal":
                result = await self._call_internal(tool, arguments, ctx)
            else:
                result = await self._call_mcp(spec, arguments, ctx)
        except Exception as exc:
            log.error("tool_call_crashed", tool=tool, exc_info=exc)
            result = ToolCallResult.failure("internal_error", f"{type(exc).__name__} while running {tool}")
        result.duration_ms = result.duration_ms or int((time.perf_counter() - started) * 1000)

        # Everything a tool returns is untrusted: redact, then look for injection attempts.
        result.text = redact_text(result.text)
        if result.data is not None:
            result.data = redact_value(result.data)
        if result.error_message:
            result.error_message = redact_text(result.error_message)
        if result.ok and spec.risk in ("read", "execute"):
            findings = scan_for_injection(result.text)
            if findings:
                notice = f"{tool}: content resembling instructions to the agent ({', '.join(findings)})"
                result.notices.append(notice)
                ctx.notices.append(notice)
        status = "success" if result.ok else ("timeout" if result.error_code == "timeout" else "error")
        security = result.error_code in {
            "path_escape", "path_not_allowed", "secret_file", "git_internal", "command_denied", "approval_required",
        }  # fmt: skip
        await self._record(tool, spec, arguments, ctx, result, status=status, security=security or bool(result.notices))
        return result

    def _authorize(self, tool: str, spec: ToolSpec | None, ctx: CallContext) -> tuple[str, str] | None:
        if spec is None:
            return "unknown_tool", f"'{tool}' is not a known tool"
        if ctx.allowed_tools is not None and tool not in ctx.allowed_tools:
            return "tool_not_allowed", f"'{tool}' is not available in this step"
        if ROLE_RANK.get(ctx.role, 0) < ROLE_RANK[MIN_ROLE_FOR_RISK[spec.risk]]:
            return "forbidden", f"The '{MIN_ROLE_FOR_RISK[spec.risk]}' role is required to use {tool}"
        if spec.server != "github" and ctx.repository is None:
            return "no_repository", f"{tool} needs a repository"
        needs_approval = spec.risk in ("write", "sensitive") or (spec.risk == "execute" and not ctx.auto_run_tests)
        if not needs_approval:
            return None
        approval = ctx.approval
        if approval is None:
            return "approval_required", f"{tool} requires an approved request from a developer"
        if approval.status != "approved":
            return "approval_required", f"The approval for {tool} is '{approval.status}', not approved"
        if tool not in APPROVAL_TOOLS.get(approval.action_type, frozenset()):
            return "approval_required", f"An approval of type '{approval.action_type}' does not cover {tool}"
        if ctx.repository is not None and approval.repository_id != ctx.repository.id:
            return "approval_required", "The approval belongs to a different repository"
        return None

    def _spec(self, spec: ToolSpec, repository: Repository | None, token: str | None, user_id: uuid.UUID | None = None) -> SessionSpec:
        if spec.server == "github":
            return SessionSpec(
                SessionKey("github", str(repository.id) if repository else None, str(user_id) if user_id else None),
                workspace_root=repository.local_path if repository else None,
                github_token=token,
            )
        assert repository is not None
        return SessionSpec(SessionKey("workspace", str(repository.id)), workspace_root=repository.local_path)

    async def _github_token(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        async with self.db.session() as session:
            credential = await session.scalar(
                select(UserCredential).where(UserCredential.user_id == user_id, UserCredential.provider == "github")
            )
            return decrypt_secret(self.settings, credential.encrypted_secret) if credential else None

    async def _call_mcp(self, spec: ToolSpec, arguments: dict[str, Any], ctx: CallContext) -> ToolCallResult:
        token = None
        if spec.server == "github":
            token = await self._github_token(ctx.user_id)
            if not token:
                return ToolCallResult.failure("github_not_configured", "No GitHub token is stored for this account")
        session_spec = self._spec(spec, ctx.repository, token, ctx.user_id)
        meta = None
        if spec.risk in ("write", "sensitive"):
            assert ctx.approval is not None
            workspace = str(Workspace(Path(session_spec.workspace_root)).root) if session_spec.workspace_root else ""
            meta = {
                META_KEY: mint_approval_token(
                    self.settings.approval_token_secret.get_secret_value(),
                    tool=spec.name,
                    arguments=arguments,
                    workspace=workspace,
                    approval_id=str(ctx.approval.id),
                    ttl_seconds=self.settings.approval_token_ttl_seconds,
                )
            }
        timeout = self.settings.mcp_long_call_timeout_seconds if spec.long_running else self.settings.mcp_call_timeout_seconds
        if spec.long_running and isinstance(arguments.get("timeout_seconds"), int):
            timeout = min(timeout, arguments["timeout_seconds"] + 60)
        return await self.mcp.call(session_spec, spec.name, arguments, meta=meta, timeout=timeout, retry_safe=spec.risk == "read")

    async def _call_internal(self, tool: str, arguments: dict[str, Any], ctx: CallContext) -> ToolCallResult:
        model, _ = INTERNAL_TOOLS[tool]
        try:
            args = model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return ToolCallResult.failure("invalid_arguments", problems)
        repository = ctx.repository
        assert repository is not None
        ws = Workspace(Path(repository.local_path), max_file_bytes=self.settings.max_file_bytes)
        async with self.db.session() as session:
            data: Any
            if tool == "code_repo_overview":
                data = {"overview": await search.build_overview(session, repository)}
            elif tool == "code_search_symbols":
                assert isinstance(args, _SymbolSearch)
                data = {"symbols": await search.search_symbols(session, repository.id, args.query, kind=args.kind, limit=args.limit)}
            elif tool == "code_relevant_files":
                assert isinstance(args, _RelevantFiles)
                data = {"files": await search.relevant_files(session, repository, args.query, limit=args.limit)}
            elif tool == "code_list_routes":
                data = {"routes": await search.list_routes(session, repository.id)}
            elif tool == "code_find_unused":
                import asyncio

                paths = list(await session.scalars(select(RepoFile.path).where(RepoFile.repository_id == repository.id)))
                data = await asyncio.to_thread(find_unused_code, ws, paths)
            else:
                if ctx.change_set_id is None:
                    return ToolCallResult.failure("not_available", "Changes cannot be proposed in this step")
                change_set = await session.get(ChangeSet, ctx.change_set_id)
                if change_set is None or change_set.repository_id != repository.id:
                    return ToolCallResult.failure("not_available", "Change set not found")
                try:
                    if isinstance(args, _ProposeEdit):
                        data = await changes.propose_edit(session, change_set, ws, args.path, args.old_text, args.new_text)
                    elif isinstance(args, _ProposeCreate):
                        data = await changes.propose_create_file(session, change_set, ws, args.path, args.content)
                    else:
                        assert isinstance(args, _ProposeDelete)
                        data = await changes.propose_delete_file(session, change_set, ws, args.path)
                except changes.ProposalError as exc:
                    return ToolCallResult.failure(exc.code, exc.message)
        return ToolCallResult(ok=True, data=data, text=json.dumps(data, indent=1, default=str))

    # --- logging -----------------------------------------------------------------------

    async def _record(
        self, tool: str, spec: ToolSpec | None, arguments: dict[str, Any], ctx: CallContext, result: ToolCallResult,
        *, status: str, security: bool = False,
    ) -> None:  # fmt: skip
        try:
            safe_args = redact_value(_shrink(arguments), max_string=2000)
            async with self.db.session() as session:
                session.add(
                    ToolExecution(
                        run_id=ctx.run_id,
                        repository_id=ctx.repository.id if ctx.repository else None,
                        user_id=ctx.user_id,
                        approval_id=ctx.approval.id if ctx.approval else None,
                        server=spec.server if spec else "unknown",
                        tool_name=tool[:80],
                        risk_level=spec.risk if spec else "unknown",
                        arguments=safe_args,
                        status=status,
                        result_preview=result.text[:4000] if result.ok else None,
                        error=None if result.ok else f"[{result.error_code}] {result.error_message}"[:2000],
                        duration_ms=result.duration_ms,
                    )
                )
                if security:
                    session.add(
                        AuditLog(
                            actor_user_id=ctx.user_id,
                            actor_type=ctx.actor,
                            action=f"security.{result.error_code}" if not result.ok else "security.injection_suspected",
                            resource_type="tool",
                            resource_id=tool[:80],
                            details={
                                "arguments": safe_args,
                                "message": (result.error_message or "; ".join(result.notices))[:1000],
                                "run_id": str(ctx.run_id) if ctx.run_id else None,
                                "repository_id": str(ctx.repository.id) if ctx.repository else None,
                            },
                        )
                    )
        except Exception as exc:  # logging must never break the call path
            log.error("tool_execution_log_failed", tool=tool, exc_info=exc)


def _shrink(arguments: dict[str, Any]) -> dict[str, Any]:
    """Large string arguments (file contents) are stored as a size and hash-free preview."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 500:
            out[key] = f"{value[:200]}... [{len(value)} characters]"
        else:
            out[key] = value
    return out


def format_for_model(result: ToolCallResult, max_chars: int) -> str:
    """Tool output as the model sees it: bounded, and delimited as untrusted data."""
    if not result.ok:
        return f"ERROR [{result.error_code}]: {result.error_message}"
    text = result.text
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated {len(text) - max_chars} characters; narrow the request to see more]"
    return wrap_untrusted(text)


async def context_for_user(session: Any, user: User, repository: Repository | None, role: str, **kwargs: Any) -> CallContext:
    auto = True
    if repository is not None:
        from app.db.models import Project

        project = await session.get(Project, repository.project_id)
        auto = bool((project.settings or {}).get("auto_run_tests", True)) if project else True
    return CallContext(repository=repository, role=role, user_id=user.id, actor="user", auto_run_tests=auto, **kwargs)
