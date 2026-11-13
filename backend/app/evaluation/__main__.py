"""Command line: ``python -m app.evaluation``.

Examples:
  python -m app.evaluation --auto-approve                      # real model, all cases
  python -m app.evaluation --auto-approve --cases fix-discount-bug explain-pricing
  python -m app.evaluation --auto-approve --scripted            # harness self-check, no model

``--scripted`` replaces the model with canned decisions for the cases that have a script.
It validates the harness and the workflow plumbing; its numbers are not a measure of
model quality and are labelled as scripted in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.llm import ScriptedChatModel
from app.config import get_settings
from app.db.session import get_database
from app.evaluation.dataset import CASES, EvalCase, get_cases
from app.evaluation.harness import CaseResult, run_evaluation
from app.evaluation.scripted import SCENARIOS
from app.logs import configure_logging
from app.runtime import Runtime


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging("WARNING")
    if not settings.allowed_roots:
        print("REPOS_ALLOWED_ROOTS must be set: evaluation repositories are created inside the first allowed root.", file=sys.stderr)
        return 2
    cases = get_cases(args.cases)
    if args.scripted:
        cases = [c for c in cases if c.id in SCENARIOS]
        if not cases:
            print("None of the selected cases has a script.", file=sys.stderr)
            return 2
    elif not settings.llm_configured:
        print(
            "No language model is configured (LLM_PROVIDER, its API key, LLM_MODEL). Configure one, or use --scripted for a harness self-check.",
            file=sys.stderr,
        )
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="asea-eval-", dir=settings.allowed_roots[0]))
    runtime = Runtime(settings, get_database(), checkpointer=InMemorySaver())
    await runtime.startup()

    def model_for(case: EvalCase) -> ScriptedChatModel | None:
        return ScriptedChatModel(responder=SCENARIOS[case.id]()) if args.scripted else None

    def show(result: CaseResult) -> None:
        mark = "PASS" if result.success else "FAIL"
        print(
            f"[{mark}] {result.case_id:28s} status={result.status:10s} type={'ok' if result.task_type_correct else 'x':2s} "
            f"files={result.file_recall:.2f} tools={result.tool_recall:.2f} applied={result.changes_applied!s:5s} "
            f"correct={result.code_correct!s:5s} denied={result.denied_tool_calls} tokens={result.input_tokens + result.output_tokens} "
            f"{result.duration_seconds:.1f}s" + (f"  error={result.error[:120]}" if result.error else "")
        )

    try:
        report = await run_evaluation(
            runtime, cases, auto_approve=args.auto_approve, workdir=workdir,
            model_label="scripted" if args.scripted else str(settings.llm_model), scripted=args.scripted,
            set_model=model_for, on_result=show,
        )  # fmt: skip
    finally:
        await runtime.shutdown()
        await get_database().dispose()
        workdir.rmdir() if workdir.exists() and not any(workdir.iterdir()) else None

    print("\nMetrics" + (" (SCRIPTED MODEL - validates the harness, not model quality)" if args.scripted else ""))
    for key, value in report.metrics.items():
        print(f"  {key:40s} {value}")
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\nReport written to {output}")
    return 0 if all(c.success for c in report.cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", nargs="*", help=f"case ids (default: all). Available: {', '.join(c.id for c in CASES)}")
    parser.add_argument(
        "--auto-approve", action="store_true", help="approve the agent's proposals on the throwaway evaluation repositories"
    )
    parser.add_argument("--scripted", action="store_true", help="use canned model decisions (harness self-check)")
    parser.add_argument("--output", help="write the full JSON report to this file")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
