"""Test selection, execution through the Tool Gateway, and persistence of results."""

from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CodeSymbol, RepoFile, Repository, TestCaseResult, TestRun
from app.db.session import Database
from app.repo_intel.detect import detect_language

if TYPE_CHECKING:
    from app.mcp_client.gateway import CallContext, ToolGateway

TOOL_TO_MCP = {
    "pytest": "test_run", "vitest": "test_run", "jest": "test_run",
    "ruff": "lint_run", "eslint": "lint_run",
    "mypy": "typecheck_run", "vue-tsc": "typecheck_run", "tsc": "typecheck_run",
}  # fmt: skip
_JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue")


def _in_directory(path: str, directory: str) -> bool:
    return directory in (".", "") or path == directory or path.startswith(directory.rstrip("/") + "/")


def _relative_to(path: str, directory: str) -> str:
    return path if directory in (".", "") else path[len(directory.rstrip("/")) + 1 :]


async def select_tests(
    session: AsyncSession, repository: Repository, changed_paths: list[str], framework: str, directory: str
) -> dict[str, Any]:
    """Pick the test files most likely to exercise ``changed_paths``.

    Strategy: changed test files themselves; test files named after a changed module; test
    files that import a changed module. Falls back to the whole suite when nothing matches.
    Returned targets are relative to ``directory``.
    """
    is_python = framework == "pytest"
    rows = await session.scalars(select(RepoFile).where(RepoFile.repository_id == repository.id, RepoFile.is_test.is_(True)))
    test_files = [
        f for f in rows
        if _in_directory(f.path, directory)
        and (f.path.endswith(".py") if is_python else f.path.endswith(_JS_EXTS))
        and PurePosixPath(f.path).name not in {"__init__.py", "conftest.py"}
    ]  # fmt: skip
    by_path = {f.path: f for f in test_files}
    selected: dict[str, str] = {}

    relevant_changes = [
        p for p in changed_paths
        if _in_directory(p, directory) and (detect_language(p) == "python") == is_python and detect_language(p) is not None
    ]  # fmt: skip
    if not relevant_changes:
        return {"targets": [], "reason": "no changed files belong to this test suite", "run": False, "selection": {}}

    stems: dict[str, str] = {}
    for path in relevant_changes:
        if path in by_path:
            selected[path] = "changed test file"
            continue
        stem = PurePosixPath(path).stem
        if stem in {"__init__", "index", "main"}:
            stem = PurePosixPath(path).parent.name or stem
        stems[stem] = path

    for test in test_files:
        name = PurePosixPath(test.path).name
        for stem, source in stems.items():
            pattern = rf"(^|[_.\-/]){re.escape(stem)}([_.\-]|$)"
            if re.search(pattern, re.sub(r"^(test_)", "", name).rsplit(".", 1)[0]) or re.search(pattern, name):
                selected.setdefault(test.path, f"named after {source}")

    if stems and test_files:
        imports = (
            await session.execute(
                select(RepoFile.path, CodeSymbol.name, CodeSymbol.qualified_name)
                .join(RepoFile, RepoFile.id == CodeSymbol.file_id)
                .where(CodeSymbol.repository_id == repository.id, CodeSymbol.kind == "import", RepoFile.is_test.is_(True))
            )
        ).all()
        for test_path, _name, qualified in imports:
            if test_path not in by_path:
                continue
            parts = set(re.split(r"[./]", qualified))
            for stem, source in stems.items():
                if stem in parts:
                    selected.setdefault(test_path, f"imports {source}")

    if not selected:
        return {
            "targets": [], "reason": "no test file could be matched to the changes; running the whole suite",
            "run": bool(test_files), "selection": {},
        }  # fmt: skip
    targets = sorted(_relative_to(p, directory) for p in selected)
    return {
        "targets": targets[:40],
        "reason": f"{len(targets)} test file(s) related to the changed files",
        "run": True,
        "selection": {_relative_to(p, directory): why for p, why in selected.items()},
    }


async def record_test_run(
    db: Database,
    *,
    repository_id: uuid.UUID,
    payload: dict[str, Any],
    kind: str,
    targets: list[str],
    task_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> TestRun:
    """Persist a result returned by a testing tool. Lint/typecheck issues are stored as failed cases."""
    cases = list(payload.get("cases") or [])
    totals = dict(payload.get("totals") or {})
    if "issues" in payload:
        issues = payload.get("issues") or []
        cases = [
            {
                "node_id": f"{i.get('file')}:{i.get('line')}" + (f" {i.get('code')}" if i.get("code") else ""),
                "file": i.get("file"),
                "name": str(i.get("code") or "issue"),
                "outcome": "failed" if i.get("severity") != "warning" else "skipped",
                "duration_ms": 0,
                "message": i.get("message"),
            }
            for i in issues[:500]
        ]
        totals = {"passed": 0, "failed": payload.get("error_count", 0), "skipped": 0, "error": 0, "not_executed": 0}
    async with db.session() as session:
        run = TestRun(
            repository_id=repository_id,
            task_id=task_id,
            run_id=run_id,
            triggered_by=user_id,
            kind=kind,
            framework=str(payload.get("framework", "unknown")),
            command=list(payload.get("command") or []),
            targets=targets,
            status=str(payload.get("status", "error")),
            passed=int(totals.get("passed", 0)),
            failed=int(totals.get("failed", 0)),
            skipped=int(totals.get("skipped", 0)),
            errors=int(totals.get("error", 0)),
            not_executed=int(totals.get("not_executed", 0)),
            duration_ms=int(payload.get("duration_ms") or 0),
            output_excerpt=str(payload.get("reason") or "")
            + ("\n" if payload.get("reason") else "")
            + str(payload.get("output_excerpt") or ""),
        )
        run.cases = [
            TestCaseResult(
                node_id=str(c.get("node_id", ""))[:2000],
                file=c.get("file"),
                name=str(c.get("name", ""))[:1000],
                outcome=str(c.get("outcome", "error")),
                duration_ms=int(c.get("duration_ms") or 0),
                message=(str(c["message"])[:8000] if c.get("message") else None),
            )
            for c in cases[:2000]
        ]
        session.add(run)
        await session.flush()
        return run


async def run_check(
    gateway: ToolGateway,
    ctx: CallContext,
    *,
    tool: str,
    kind: str,
    targets: list[str],
    directory: str,
    task_id: uuid.UUID | None = None,
) -> TestRun:
    """Run one check through the gateway and store the outcome (including when it could not run)."""
    assert ctx.repository is not None
    arguments: dict[str, Any]
    if kind == "build":
        mcp_tool, arguments = "build_run", {"tool": "npm", "directory": directory}
    elif tool == "npm":
        mcp_tool, arguments = "test_run", {"framework": "npm", "kind": kind, "directory": directory}
    else:
        mcp_tool = TOOL_TO_MCP[tool]
        arguments = {"directory": directory}
        if mcp_tool == "test_run":
            arguments |= {"framework": tool, "targets": targets, "kind": kind}
        elif mcp_tool == "lint_run":
            arguments |= {"tool": tool, "targets": targets}
        else:
            arguments |= {"tool": tool}
    result = await gateway.call(mcp_tool, arguments, ctx)
    if result.ok and isinstance(result.data, dict):
        payload = result.data
    else:
        # The check did not run at all (denied, sandbox unavailable, MCP down): record that honestly.
        payload = {
            "framework": tool,
            "command": [],
            "status": "not_executed",
            "totals": {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "not_executed": len(targets) or 1},
            "cases": [
                {"node_id": t, "file": t, "name": t, "outcome": "not_executed", "duration_ms": 0, "message": result.error_message}
                for t in (targets or ["(all)"])
            ],
            "reason": f"Not executed: [{result.error_code}] {result.error_message}",
            "duration_ms": result.duration_ms,
        }
    return await record_test_run(
        gateway.db,
        repository_id=ctx.repository.id,
        payload=payload,
        kind=kind,
        targets=targets,
        task_id=task_id,
        run_id=ctx.run_id,
        user_id=ctx.user_id if ctx.actor == "user" else None,
    )


def summarize_run(run: TestRun, max_failures: int = 8) -> dict[str, Any]:
    failures = [
        {"node_id": c.node_id, "outcome": c.outcome, "message": (c.message or "")[:1500]}
        for c in run.cases
        if c.outcome in ("failed", "error")
    ]
    return {
        "test_run_id": str(run.id),
        "kind": run.kind,
        "framework": run.framework,
        "status": run.status,
        "targets": run.targets,
        "totals": {
            "passed": run.passed,
            "failed": run.failed,
            "skipped": run.skipped,
            "error": run.errors,
            "not_executed": run.not_executed,
        },
        "failures": failures[:max_failures],
        "failure_count": len(failures),
        "note": run.output_excerpt.split("\n", 1)[0][:300] if run.status == "not_executed" else "",
    }
