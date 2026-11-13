"""Testing and verification tools.

The server assembles each command line from enumerated arguments, so callers cannot add
flags. Everything runs in the sandbox without network access.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.mcp_server.context import RISK_META, ServerContext
from app.mcp_server.parsers import (
    empty_totals,
    excerpt,
    overall_status,
    parse_eslint_json,
    parse_jest_json,
    parse_junit_xml,
    parse_ruff_json,
    parse_typecheck_output,
    totals_of,
)
from app.mcp_server.tools.filesystem import READ, run_blocking
from app.mcp_server.tools.terminal import Directory, resolve_directory
from app.repo_intel.detect import build_profile, is_test_file
from app.sandbox.runner import REPORT_PLACEHOLDER, SandboxRequest, SandboxResult
from app.security.paths import normalize_relative
from app.security.redaction import redact_text

EXECUTE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
Targets = Annotated[
    list[Annotated[str, Field(max_length=500)]],
    Field(description="Test files or directories relative to the working directory (pytest node ids allowed)", max_length=50),
]


def validate_targets(sc: ServerContext, workdir: str, targets: list[str]) -> list[str]:
    cleaned: list[str] = []
    for target in targets:
        if not target or target.startswith("-") or "\x00" in target or "\n" in target:
            raise ToolError(f"[invalid_target] Invalid test target: {target[:80]!r}")
        path_part, sep, node = target.partition("::")
        rel = normalize_relative(path_part)
        full = rel if workdir == "." else f"{workdir}/{rel}"
        sc.workspace.resolve(full)
        cleaned.append(rel + (sep + node if sep else ""))
    return cleaned


def _mark_missing(cases: list[dict[str, Any]], targets: list[str], reason: str) -> None:
    """Targets that produced no result are reported as not_executed rather than silently dropped."""
    for target in targets:
        path = target.split("::", 1)[0].rstrip("/")
        if not any((c.get("file") or "").startswith(path) or path in c["node_id"] for c in cases):
            cases.append({"node_id": target, "file": path, "name": target, "outcome": "not_executed", "duration_ms": 0, "message": reason})


def _finish(
    *, tool: str, kind: str, argv: list[str], result: SandboxResult, cases: list[dict[str, Any]], targets: list[str]
) -> dict[str, Any]:
    if result.error:
        raise ToolError(f"[sandbox_error] {result.error}")
    if result.exit_code in (126, 127) or (result.exit_code not in (0, None) and "No module named" in result.stderr and not cases):
        reason = f"{tool} is not available in the execution environment; dependencies may need to be installed"
        _mark_missing(cases, targets or ["(all tests)"], reason)
        return {
            "framework": tool, "kind": kind, "command": argv, "status": "not_executed", "totals": totals_of(cases),
            "cases": cases, "duration_ms": result.duration_ms, "timed_out": False, "sandbox_mode": result.mode,
            "output_excerpt": redact_text(excerpt(result.stdout + "\n" + result.stderr, 3000)), "reason": reason,
        }  # fmt: skip
    if result.timed_out:
        _mark_missing(cases, targets or ["(remaining tests)"], "run timed out before this was executed")
    elif targets:
        _mark_missing(cases, targets, "no result was reported for this target")
    for case in cases:
        if case.get("message"):
            case["message"] = redact_text(case["message"])
    totals = totals_of(cases)
    return {
        "framework": tool,
        "kind": kind,
        "command": argv,
        "status": overall_status(totals, exit_code=result.exit_code, timed_out=result.timed_out),
        "totals": totals,
        "cases": cases,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
        "exit_code": result.exit_code,
        "sandbox_mode": result.mode,
        "output_excerpt": redact_text(excerpt(result.stdout + ("\n" + result.stderr if result.stderr.strip() else ""), 6000)),
    }


def _issues_result(*, tool: str, kind: str, argv: list[str], result: SandboxResult, issues: list[dict[str, Any]]) -> dict[str, Any]:
    if result.error:
        raise ToolError(f"[sandbox_error] {result.error}")
    unavailable = result.exit_code in (126, 127) or ("No module named" in result.stderr and not issues)
    errors = sum(1 for i in issues if i.get("severity") != "warning")
    if result.timed_out:
        status = "timeout"
    elif unavailable:
        status = "not_executed"
    elif result.exit_code == 0 and not errors:
        status = "passed"
    elif issues:
        status = "failed"
    else:
        status = "error"  # the tool itself failed (bad config, crash) without reporting issues
    for issue in issues:
        issue["message"] = redact_text(str(issue.get("message", "")))[:1000]
    return {
        "framework": tool,
        "kind": kind,
        "command": argv,
        "status": status,
        "issues": issues,
        "issue_count": len(issues),
        "error_count": errors,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
        "exit_code": result.exit_code,
        "sandbox_mode": result.mode,
        "output_excerpt": redact_text(excerpt(result.stdout + ("\n" + result.stderr if result.stderr.strip() else ""), 4000)),
        **({"reason": f"{tool} is not available in the execution environment"} if unavailable else {}),
    }


def _node_modules_missing(sc: ServerContext, workdir: str) -> bool:
    """npx --no-install can only run tools that are installed in the package or one of its parents."""
    current = sc.workspace.root / workdir if workdir != "." else sc.workspace.root
    while True:
        if (current / "node_modules").is_dir():
            return False
        if current == sc.workspace.root:
            return True
        current = current.parent


def _not_installed(tool: str, kind: str, argv: list[str], targets: list[str]) -> dict[str, Any]:
    reason = f"{tool} was not run: node_modules is missing, so dependencies have to be installed first (needs approval)"
    cases: list[dict[str, Any]] = []
    if kind in ("unit", "integration"):
        _mark_missing(cases, targets or ["(all tests)"], reason)
    payload: dict[str, Any] = {
        "framework": tool, "kind": kind, "command": argv, "status": "not_executed", "duration_ms": 0,
        "timed_out": False, "exit_code": None, "sandbox_mode": "none", "output_excerpt": "", "reason": reason,
    }  # fmt: skip
    if kind in ("unit", "integration"):
        payload |= {"totals": totals_of(cases), "cases": cases}
    else:
        payload |= {"issues": [], "issue_count": 0, "error_count": 0}
    return payload


def register(server: MCPServer, sc: ServerContext) -> None:
    async def run(argv: list[str], workdir: str, timeout: int, *, report: bool = False) -> SandboxResult:
        return await sc.sandbox.run(SandboxRequest(argv=argv, workdir=workdir, timeout_seconds=timeout, want_report=report))

    @server.tool(annotations=READ, meta={RISK_META: "read"})
    async def test_discover() -> dict[str, Any]:
        """Detect test frameworks, quality tools, npm scripts and test files in the repository."""

        def discover() -> dict[str, Any]:
            ws = sc.workspace
            files = ws.tracked_files()
            profile = build_profile(ws, files)
            tests = [
                f for f in files
                if is_test_file(f)
                and f.rsplit(".", 1)[-1] in {"py", "js", "jsx", "ts", "tsx", "mjs", "cjs"}
                and f.rsplit("/", 1)[-1] not in {"__init__.py", "conftest.py"}
            ]  # fmt: skip
            return {
                "test_frameworks": profile["test_frameworks"],
                "quality_tools": profile["quality_tools"],
                "npm_scripts": {p["directory"]: p["scripts"] for p in profile["node_packages"] if p["scripts"]},
                "test_files": tests[:500],
                "test_file_count": len(tests),
            }

        return await run_blocking(discover)

    @server.tool(annotations=EXECUTE, meta={RISK_META: "execute"})
    async def test_run(
        framework: Literal["pytest", "vitest", "jest", "npm"],
        targets: Targets = [],  # noqa: B006 - pydantic copies defaults
        kind: Literal["unit", "integration"] = "unit",
        directory: Directory = ".",
        timeout_seconds: Annotated[int, Field(ge=10, le=1800)] = 600,
    ) -> dict[str, Any]:
        """Run tests in the sandbox and return per-test outcomes: passed, failed, skipped, error, not_executed."""
        workdir = await run_blocking(resolve_directory, sc, directory)
        requested = await run_blocking(validate_targets, sc, workdir, targets)
        base = sc.workspace.root if workdir == "." else sc.workspace.root / workdir
        # A missing target would make the runner abort before running anything; report it and run the rest.
        missing = [t for t in requested if not (base / t.split("::", 1)[0]).exists()]
        clean = [t for t in requested if t not in missing]
        if requested and not clean:
            cases = [
                {
                    "node_id": t,
                    "file": t.split("::", 1)[0],
                    "name": t,
                    "outcome": "not_executed",
                    "duration_ms": 0,
                    "message": "target does not exist",
                }
                for t in missing
            ]
            return {
                "framework": framework, "kind": kind, "command": [], "status": "not_executed", "totals": totals_of(cases),
                "cases": cases, "duration_ms": 0, "timed_out": False, "exit_code": None, "sandbox_mode": "none",
                "output_excerpt": "", "reason": "none of the requested test targets exist",
            }  # fmt: skip
        root_prefix = str(base)
        if framework == "pytest":
            argv = [
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={REPORT_PLACEHOLDER}",
                "-o",
                "junit_family=xunit1",
                *clean,
            ]
            result = await run(argv, workdir, timeout_seconds, report=True)
            cases = parse_junit_xml(result.report) if result.report else []
        elif framework in ("vitest", "jest"):
            argv = (
                ["npx", "--no-install", "vitest", "run", "--reporter=json", f"--outputFile={REPORT_PLACEHOLDER}", *clean]
                if framework == "vitest"
                else ["npx", "--no-install", "jest", "--json", f"--outputFile={REPORT_PLACEHOLDER}", "--ci", *clean]
            )
            if _node_modules_missing(sc, workdir):
                return _not_installed(framework, kind, [a.replace(REPORT_PLACEHOLDER, "<report>") for a in argv], clean)
            result = await run(argv, workdir, timeout_seconds, report=True)
            cases = parse_jest_json(result.report, root_prefix) if result.report else []
        else:
            if clean:
                raise ToolError("[invalid_target] The npm test script does not accept targets")
            argv = ["npm", "test", "--silent"]
            result = await run(argv, workdir, timeout_seconds)
            # No structured report: the whole script is one result.
            outcome = "passed" if result.exit_code == 0 else "failed"
            cases = [] if result.timed_out or result.exit_code in (126, 127) else [
                {"node_id": "npm test", "file": None, "name": "npm test", "outcome": outcome, "duration_ms": result.duration_ms,
                 "message": None if outcome == "passed" else excerpt(result.stdout + "\n" + result.stderr, 2000)}
            ]  # fmt: skip
        shown = [a.replace(REPORT_PLACEHOLDER, "<report>") for a in argv]
        payload = _finish(tool=framework, kind=kind, argv=shown, result=result, cases=cases, targets=clean)
        for target in missing:
            payload["cases"].append(
                {
                    "node_id": target,
                    "file": target.split("::", 1)[0],
                    "name": target,
                    "outcome": "not_executed",
                    "duration_ms": 0,
                    "message": "target does not exist",
                }
            )
        if missing:
            payload["totals"] = totals_of(payload["cases"])
        return payload

    @server.tool(annotations=EXECUTE, meta={RISK_META: "execute"})
    async def lint_run(
        tool: Literal["ruff", "eslint"],
        targets: Targets = [],  # noqa: B006
        directory: Directory = ".",
        timeout_seconds: Annotated[int, Field(ge=10, le=900)] = 300,
    ) -> dict[str, Any]:
        """Run a linter (never with auto-fix) and return the issues it reports."""
        workdir = await run_blocking(resolve_directory, sc, directory)
        clean = await run_blocking(validate_targets, sc, workdir, targets)
        root_prefix = str(sc.workspace.root if workdir == "." else sc.workspace.root / workdir)
        if tool == "ruff":
            argv = ["python", "-m", "ruff", "check", "--output-format=json", "--no-cache", *(clean or ["."])]
            result = await run(argv, workdir, timeout_seconds)
            issues = parse_ruff_json(result.stdout)
            for issue in issues:
                if issue["file"].startswith(root_prefix + "/"):
                    issue["file"] = issue["file"][len(root_prefix) + 1 :]
        else:
            argv = ["npx", "--no-install", "eslint", "--format", "json", *(clean or ["."])]
            if _node_modules_missing(sc, workdir):
                return _not_installed(tool, "lint", argv, clean)
            result = await run(argv, workdir, timeout_seconds)
            issues = parse_eslint_json(result.stdout, root_prefix)
        return _issues_result(tool=tool, kind="lint", argv=argv, result=result, issues=issues)

    @server.tool(annotations=EXECUTE, meta={RISK_META: "execute"})
    async def typecheck_run(
        tool: Literal["mypy", "vue-tsc", "tsc"],
        directory: Directory = ".",
        timeout_seconds: Annotated[int, Field(ge=10, le=900)] = 600,
    ) -> dict[str, Any]:
        """Run a type checker and return the errors it reports."""
        workdir = await run_blocking(resolve_directory, sc, directory)
        argv = (
            ["python", "-m", "mypy", ".", "--no-color-output", "--show-column-numbers", "--cache-dir=/tmp/asea-mypy-cache"]
            if tool == "mypy"
            else ["npx", "--no-install", tool, "--noEmit", "--pretty", "false"]
        )
        if tool != "mypy" and _node_modules_missing(sc, workdir):
            return _not_installed(tool, "typecheck", argv, [])
        result = await run(argv, workdir, timeout_seconds)
        issues = parse_typecheck_output(result.stdout + "\n" + result.stderr)
        return _issues_result(tool=tool, kind="typecheck", argv=argv, result=result, issues=issues)

    @server.tool(annotations=EXECUTE, meta={RISK_META: "execute"})
    async def build_run(
        tool: Literal["npm"] = "npm",
        directory: Directory = ".",
        timeout_seconds: Annotated[int, Field(ge=10, le=1800)] = 900,
    ) -> dict[str, Any]:
        """Run the project's build script (npm run build) to verify that it still builds."""
        workdir = await run_blocking(resolve_directory, sc, directory)
        argv = ["npm", "run", "build", "--silent"]
        if _node_modules_missing(sc, workdir):
            return _not_installed(tool, "build", argv, [])
        # Builds write output files, so the workspace is mounted writable for this one command.
        result = await sc.sandbox.run(SandboxRequest(argv=argv, workdir=workdir, timeout_seconds=timeout_seconds, writable=True))
        payload = _issues_result(tool=tool, kind="build", argv=argv, result=result, issues=[])
        if payload["status"] == "error":
            payload["status"] = "failed"
        return payload


__all__ = ["empty_totals", "register"]
