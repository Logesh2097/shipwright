"""Phase 9: the evaluation harness measures real outcomes (scripted model)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from app.agent.llm import ScriptedChatModel
from app.db.models import Project, Repository
from app.evaluation.dataset import CASES, get_cases
from app.evaluation.harness import aggregate, run_evaluation
from app.evaluation.scripted import SCENARIOS
from app.runtime import Runtime
from tests.conftest import _REPOS_ROOT


def test_dataset_is_well_formed() -> None:
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids)) >= 8
    assert set(SCENARIOS) <= set(ids)
    for case in CASES:
        assert case.request and case.expected_files
        assert case.expects_changes or not case.allowed_changes or case.allowed_changes
        if case.expects_changes:
            assert case.allowed_changes, case.id


async def test_harness_measures_scripted_cases(runtime: Runtime) -> None:
    workdir = _REPOS_ROOT / "eval-work"
    workdir.mkdir(exist_ok=True)
    cases = get_cases(["explain-pricing", "fix-discount-bug", "find-unused-code", "prompt-injection-readme"])
    report = await run_evaluation(
        runtime, cases, auto_approve=True, workdir=workdir, model_label="scripted", scripted=True,
        set_model=lambda case: ScriptedChatModel(responder=SCENARIOS[case.id]()),
    )  # fmt: skip
    by_id = {r.case_id: r for r in report.cases}

    fix = by_id["fix-discount-bug"]
    assert fix.success and fix.completed and fix.task_type_correct and fix.changes_applied
    assert fix.changed_files == ["app/pricing.py"] and fix.unnecessary_modifications == []
    assert fix.code_correct is True and fix.verification_status == "passed" and fix.tests["passed"] == 4
    assert fix.approvals_requested == 1 and fix.file_recall == 1.0 and fix.tool_recall == 1.0

    explain = by_id["explain-pricing"]
    assert explain.success and not explain.changes_proposed and explain.changed_files == [] and explain.code_correct is None
    assert by_id["find-unused-code"].report_mentions == 1.0

    hostile = by_id["prompt-injection-readme"]
    assert hostile.completed and hostile.changed_files == [] and hostile.denied_tool_calls == 6 and hostile.security_events >= 6
    assert hostile.approvals_requested == 0

    metrics = report.metrics
    assert metrics["task_completion_rate"] == 1.0 and metrics["code_correctness"] == 1.0
    assert metrics["unauthorized_action_attempts"] == 6 and metrics["cases_with_unnecessary_modifications"] == 0
    assert metrics["total_input_tokens"] > 0 and metrics["mean_execution_seconds"] > 0
    assert report.scripted is True and report.sandbox_mode == "local"

    # The harness cleans up after itself.
    async with runtime.db.session() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 0
        assert await session.scalar(select(func.count()).select_from(Repository)) == 0
    assert not any(Path(workdir).iterdir())


async def test_without_auto_approve_nothing_is_applied(runtime: Runtime) -> None:
    workdir = _REPOS_ROOT / "eval-work-2"
    workdir.mkdir(exist_ok=True)
    report = await run_evaluation(
        runtime, get_cases(["fix-discount-bug"]), auto_approve=False, workdir=workdir, model_label="scripted", scripted=True,
        set_model=lambda case: ScriptedChatModel(responder=SCENARIOS[case.id]()),
    )  # fmt: skip
    result = report.cases[0]
    assert result.completed and result.changes_proposed and not result.changes_applied
    assert result.changed_files == [] and result.code_correct is False and result.success is False
    assert aggregate(report.cases)["task_success_rate"] == 0.0
