"""Evaluation harness.

Runs each case against a fresh copy of the sample repository using the real workflow
(graph, gateway, MCP tools, sandbox, approvals) and measures the outcome.

Approvals: the harness can approve the agent's proposals itself so that cases run
unattended. This happens only inside this harness, only for throwaway repositories it
created, only with ``auto_approve=True`` (``--auto-approve`` on the command line), and the
decision is recorded under a dedicated evaluation account. The product itself never
approves anything on a developer's behalf.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from sqlalchemy import delete, func, select

from app.db.models import (
    AgentEvent,
    AgentRun,
    Approval,
    AuditLog,
    ChangeSet,
    Project,
    ProjectMember,
    Repository,
    Task,
    TestRun,
    ToolExecution,
    User,
)
from app.evaluation.dataset import EvalCase
from app.evaluation.fixtures import create_sample_repository
from app.repo_intel.indexer import index_repository, validate_repository_path
from app.runtime import Runtime
from app.sandbox.runner import SandboxRequest, create_sandbox
from app.security.auth import hash_password
from app.services.approvals import decide_approval

EVAL_EMAIL = "evaluation-runner@asea.local"
TERMINAL = ("completed", "failed", "cancelled", "interrupted")


@dataclass
class CaseResult:
    case_id: str
    status: str
    completed: bool
    task_type: str | None
    task_type_correct: bool
    files_identified: list[str]
    file_recall: float
    tools_used: list[str]
    tool_recall: float
    changes_proposed: bool
    changes_applied: bool
    changed_files: list[str]
    unnecessary_modifications: list[str]
    verification_status: str
    tests: dict[str, int]
    code_correct: bool | None  # None when the case has nothing to verify
    report_mentions: float
    denied_tool_calls: int
    security_events: int
    approvals_requested: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    error: str | None = None
    success: bool = False


@dataclass
class EvalReport:
    started_at: str
    model: str
    scripted: bool
    auto_approve: bool
    sandbox_mode: str
    cases: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git_changed(path: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, check=False
    ).stdout
    return sorted(line[3:].strip().strip('"') for line in out.splitlines() if line.strip())


async def _ensure_eval_user(runtime: Runtime) -> uuid.UUID:
    async with runtime.db.session() as session:
        user = await session.scalar(select(User).where(User.email == EVAL_EMAIL))
        if user is None:
            # The password is random and discarded: nobody can log in as this account.
            user = User(email=EVAL_EMAIL, full_name="Evaluation runner", password_hash=hash_password(uuid.uuid4().hex), role="developer")
            session.add(user)
            await session.flush()
        return user.id


async def _wait(runtime: Runtime, run_id: uuid.UUID, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with runtime.db.session() as session:
            run = await session.get(AgentRun, run_id)
            status = run.status if run else "failed"
        if status in TERMINAL or status == "awaiting_approval":
            if not runtime.runner.is_active(run_id):
                return status
        await asyncio.sleep(0.2)
    await runtime.runner.cancel(run_id)
    return "timeout"


async def run_case(runtime: Runtime, case: EvalCase, *, auto_approve: bool, workdir: Path, timeout: float = 900.0) -> CaseResult:
    started = time.monotonic()
    repo_dir = Path(tempfile.mkdtemp(prefix=f"eval-{case.id}-", dir=workdir))
    repo_dir.rmdir()
    create_sample_repository(repo_dir)
    info = validate_repository_path(runtime.settings, str(repo_dir))
    user_id = await _ensure_eval_user(runtime)
    async with runtime.db.session() as session:
        user = await session.get(User, user_id)
        assert user is not None
        project = Project(name=f"evaluation-{case.id}-{uuid.uuid4().hex[:8]}", owner_id=user_id, settings={"auto_run_tests": True})
        project.members.append(ProjectMember(user_id=user_id, role="owner"))
        session.add(project)
        await session.flush()
        repository = Repository(
            project_id=project.id, name=repo_dir.name, local_path=info.path, default_branch=info.default_branch, status="indexing"
        )
        session.add(repository)
        await session.flush()
        project_id, repository_id = project.id, repository.id
    await index_repository(runtime.db, runtime.settings, repository_id)

    error: str | None = None
    status = "failed"
    run_id = uuid.uuid4()
    try:
        async with runtime.db.session() as session:
            task = Task(
                project_id=project_id, repository_id=repository_id, created_by=user_id, title=case.request[:300],
                request_text=case.request, dedupe_key=uuid.uuid4().hex, status="queued",
            )  # fmt: skip
            session.add(task)
            await session.flush()
            run = AgentRun(id=run_id, task_id=task.id, thread_id=str(run_id), status="queued")
            session.add(run)
        await runtime.runner.start(run_id)
        for _ in range(runtime.settings.agent_max_repair_attempts + 2):
            status = await _wait(runtime, run_id, timeout)
            if status != "awaiting_approval":
                break
            async with runtime.db.session() as session:
                approval = await session.scalar(select(Approval).where(Approval.run_id == run_id, Approval.status == "pending"))
                user = await session.get(User, user_id)
                assert user is not None and approval is not None
                await decide_approval(
                    runtime, session, approval.id, user, approve=auto_approve,
                    comment="evaluation harness (auto-approve)" if auto_approve else "evaluation harness (no auto-approve)",
                    confirm_deletions=False,
                )  # fmt: skip
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]

    result = await _measure(runtime, case, repo_dir, run_id, repository_id, status, error, time.monotonic() - started)

    # Clean up: evaluation data and the throwaway repository are removed.
    await runtime.mcp.close_matching(repository_id=str(repository_id))
    async with runtime.db.session() as session:
        await session.execute(delete(AuditLog).where(AuditLog.details["repository_id"].astext == str(repository_id)))
        project_row = await session.get(Project, project_id)
        if project_row is not None:
            await session.delete(project_row)
    shutil.rmtree(repo_dir, ignore_errors=True)
    return result


async def _measure(
    runtime: Runtime,
    case: EvalCase,
    repo_dir: Path,
    run_id: uuid.UUID,
    repository_id: uuid.UUID,
    status: str,
    error: str | None,
    duration: float,
) -> CaseResult:
    async with runtime.db.session() as session:
        run = await session.get(AgentRun, run_id)
        events = list(await session.scalars(select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.id)))
        executions = list(await session.scalars(select(ToolExecution).where(ToolExecution.run_id == run_id)))
        change_sets = list(await session.scalars(select(ChangeSet).where(ChangeSet.run_id == run_id)))
        test_runs = list(await session.scalars(select(TestRun).where(TestRun.run_id == run_id).order_by(TestRun.created_at)))
        approvals = await session.scalar(select(func.count()).select_from(Approval).where(Approval.run_id == run_id)) or 0
        security = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action.startswith("security."), AuditLog.details["run_id"].astext == str(run_id))
        ) or 0  # fmt: skip

    identified: set[str] = set()
    tools_used: list[str] = []
    for event in events:
        payload = event.payload or {}
        if event.type == "tool_call":
            tools_used.append(payload.get("tool", ""))
            path = (payload.get("arguments") or {}).get("path")
            if payload.get("ok") and isinstance(path, str):
                identified.add(path)
        if event.type == "node_finished":
            identified.update(payload.get("relevant_files") or [])
            identified.update(payload.get("files_to_change") or [])
    expected_files = set(case.expected_files)
    file_recall = len(expected_files & identified) / len(expected_files) if expected_files else 1.0
    expected_tools = set(case.expected_tools)
    tool_recall = len(expected_tools & set(tools_used)) / len(expected_tools) if expected_tools else 1.0

    changed = _git_changed(repo_dir)
    unnecessary = [p for p in changed if not any(p == allowed or p.startswith(allowed) for allowed in case.allowed_changes)]
    applied = any(cs.status == "applied" for cs in change_sets)

    tests = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "not_executed": 0}
    verification_status = "not_executed"
    unit_runs = [t for t in test_runs if t.kind in ("unit", "integration")]
    if unit_runs:
        last = unit_runs[-1]
        tests = {
            "passed": last.passed,
            "failed": last.failed,
            "skipped": last.skipped,
            "error": last.errors,
            "not_executed": last.not_executed,
        }
        verification_status = last.status

    code_correct: bool | None = None
    if case.verify_targets:
        # Independent check, outside the agent: run the case's tests against the final working tree.
        sandbox = create_sandbox(repo_dir, runtime.mcp.sandbox_config())
        outcome = await sandbox.run(
            SandboxRequest(argv=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *case.verify_targets], timeout_seconds=300)
        )
        code_correct = outcome.exit_code == 0

    report = (run.final_report or "") if run else ""
    mentions = (
        sum(1 for m in case.report_must_mention if m.lower() in report.lower()) / len(case.report_must_mention)
        if case.report_must_mention else 1.0
    )  # fmt: skip
    task_type = (run.classification or {}).get("task_type") if run and run.classification else None
    completed = status == "completed"
    success = (
        completed
        and file_recall >= 0.5
        and mentions >= 0.5
        and not unnecessary
        and (applied if case.expects_changes else not changed)
        and (code_correct is not False)
    )
    return CaseResult(
        case_id=case.id,
        status=status,
        completed=completed,
        task_type=task_type,
        task_type_correct=task_type == case.expected_task_type,
        files_identified=sorted(identified),
        file_recall=round(file_recall, 3),
        tools_used=sorted(set(tools_used)),
        tool_recall=round(tool_recall, 3),
        changes_proposed=bool(change_sets),
        changes_applied=applied,
        changed_files=changed,
        unnecessary_modifications=unnecessary,
        verification_status=verification_status,
        tests=tests,
        code_correct=code_correct,
        report_mentions=round(mentions, 3),
        denied_tool_calls=sum(1 for e in executions if e.status == "denied"),
        security_events=int(security),
        approvals_requested=int(approvals),
        llm_calls=run.llm_calls if run else 0,
        input_tokens=run.input_tokens if run else 0,
        output_tokens=run.output_tokens if run else 0,
        duration_seconds=round(duration, 2),
        error=error or (run.error if run else None),
        success=success,
    )


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    n = len(results) or 1
    verifiable = [r for r in results if r.code_correct is not None]
    with_tests = [r for r in results if sum(r.tests.values()) > 0]
    executed_tests = sum(r.tests["passed"] + r.tests["failed"] + r.tests["error"] for r in with_tests)
    return {
        "cases": len(results),
        "task_completion_rate": round(sum(r.completed for r in results) / n, 3),
        "task_success_rate": round(sum(r.success for r in results) / n, 3),
        "task_classification_accuracy": round(sum(r.task_type_correct for r in results) / n, 3),
        "repository_understanding_accuracy": round(sum(r.report_mentions for r in results) / n, 3),
        "correct_file_identification": round(sum(r.file_recall for r in results) / n, 3),
        "tool_selection_accuracy": round(sum(r.tool_recall for r in results) / n, 3),
        "code_correctness": round(sum(bool(r.code_correct) for r in verifiable) / len(verifiable), 3) if verifiable else None,
        "test_pass_rate": round(sum(r.tests["passed"] for r in with_tests) / executed_tests, 3) if executed_tests else None,
        "cases_with_unnecessary_modifications": sum(1 for r in results if r.unnecessary_modifications),
        "unauthorized_action_attempts": sum(r.denied_tool_calls for r in results),
        "security_events": sum(r.security_events for r in results),
        "total_input_tokens": sum(r.input_tokens for r in results),
        "total_output_tokens": sum(r.output_tokens for r in results),
        "total_llm_calls": sum(r.llm_calls for r in results),
        "mean_execution_seconds": round(sum(r.duration_seconds for r in results) / n, 2),
    }


async def run_evaluation(
    runtime: Runtime,
    cases: list[EvalCase],
    *,
    auto_approve: bool,
    workdir: Path,
    model_label: str,
    scripted: bool,
    set_model: Callable[[EvalCase], BaseChatModel | None] | None = None,
    on_result: Callable[[CaseResult], None] | None = None,
) -> EvalReport:
    report = EvalReport(
        started_at=datetime.now(UTC).isoformat(),
        model=model_label,
        scripted=scripted,
        auto_approve=auto_approve,
        sandbox_mode=runtime.sandbox_status()["mode"],
    )
    for case in cases:
        if set_model is not None:
            model = set_model(case)
            runtime._llm_factory = (lambda m=model: m) if model is not None else None
        result = await run_case(runtime, case, auto_approve=auto_approve, workdir=workdir)
        report.cases.append(result)
        if on_result is not None:
            on_result(result)
    report.metrics = aggregate(report.cases)
    return report
