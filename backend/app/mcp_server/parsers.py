"""Parsers that turn tool output into structured results.

Every test outcome is one of: passed, failed, skipped, error, not_executed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

OUTCOMES = ("passed", "failed", "skipped", "error", "not_executed")
MAX_CASES = 2000
MAX_MESSAGE = 4000
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def excerpt(text: str, limit: int = 6000) -> str:
    """Keep the head and (mostly) the tail of long output: failures are summarised at the end."""
    text = strip_ansi(text).strip()
    if len(text) <= limit:
        return text
    head = limit // 4
    return f"{text[:head]}\n... [{len(text) - limit} characters omitted] ...\n{text[-(limit - head) :]}"


def empty_totals() -> dict[str, int]:
    return dict.fromkeys(OUTCOMES, 0)


def totals_of(cases: list[dict[str, Any]]) -> dict[str, int]:
    totals = empty_totals()
    for case in cases:
        totals[case["outcome"]] = totals.get(case["outcome"], 0) + 1
    return totals


def overall_status(totals: dict[str, int], *, exit_code: int | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if totals["failed"]:
        return "failed"
    if totals["error"]:
        return "error"
    executed = totals["passed"] + totals["skipped"]
    if exit_code not in (0, None) and not executed:
        return "error"
    if exit_code not in (0, None):
        return "failed"
    if executed == 0:
        return "not_executed"
    return "passed"


# --- pytest (JUnit XML) ----------------------------------------------------------------


def parse_junit_xml(xml_text: str) -> list[dict[str, Any]]:
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return []
    cases: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        file_attr = case.get("file")
        outcome = "passed"
        message = None
        for child in case:
            tag = child.tag.lower()
            if tag in {"failure", "error", "skipped"}:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                text = (child.get("message") or "") + ("\n" + child.text if child.text else "")
                message = strip_ansi(text.strip())[:MAX_MESSAGE] or None
                break
        file: str | None
        if file_attr:
            file = file_attr
        else:
            # pytest classnames look like "tests.test_api" or "tests.test_api.TestThing"
            parts = classname.split(".")
            module_parts = [p for p in parts if not p[:1].isupper()] or parts
            file = "/".join(module_parts) + ".py" if classname else None
        class_parts = [p for p in classname.split(".") if p[:1].isupper()]
        node_id = "::".join([p for p in [file, *class_parts, name] if p])
        try:
            duration_ms = int(float(case.get("time", "0")) * 1000)
        except ValueError:
            duration_ms = 0
        cases.append({"node_id": node_id, "file": file, "name": name, "outcome": outcome, "duration_ms": duration_ms, "message": message})
        if len(cases) >= MAX_CASES:
            break
    return cases


# --- vitest / jest (JSON reporter; same shape) -----------------------------------------


def parse_jest_json(text: str, root_prefix: str = "") -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    cases: list[dict[str, Any]] = []
    status_map = {
        "passed": "passed",
        "failed": "failed",
        "pending": "skipped",
        "skipped": "skipped",
        "todo": "skipped",
        "disabled": "skipped",
    }
    for suite in data.get("testResults", []) or []:
        file = str(suite.get("name", ""))
        for prefix in (root_prefix.rstrip("/") + "/", "/workspace/"):
            if prefix != "/" and file.startswith(prefix):
                file = file[len(prefix) :]
                break
        assertions = suite.get("assertionResults", []) or []
        if not assertions and suite.get("status") == "failed":
            cases.append(
                {"node_id": file, "file": file, "name": "(suite failed to run)", "outcome": "error", "duration_ms": 0,
                 "message": strip_ansi(str(suite.get("message", "")))[:MAX_MESSAGE] or None}
            )  # fmt: skip
        for assertion in assertions:
            name = assertion.get("fullName") or assertion.get("title") or ""
            messages = assertion.get("failureMessages") or []
            cases.append(
                {
                    "node_id": f"{file} > {name}",
                    "file": file,
                    "name": name,
                    "outcome": status_map.get(str(assertion.get("status")), "error"),
                    "duration_ms": int(assertion.get("duration") or 0),
                    "message": strip_ansi("\n".join(map(str, messages)))[:MAX_MESSAGE] or None,
                }
            )
            if len(cases) >= MAX_CASES:
                return cases
    return cases


# --- linters and type checkers ---------------------------------------------------------


def parse_ruff_json(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    issues = []
    for item in data if isinstance(data, list) else []:
        location = item.get("location") or {}
        filename = str(item.get("filename", ""))
        for prefix in ("/workspace/",):
            if filename.startswith(prefix):
                filename = filename[len(prefix) :]
        issues.append(
            {"file": filename, "line": location.get("row"), "column": location.get("column"),
             "code": item.get("code"), "message": item.get("message", ""), "severity": "error"}
        )  # fmt: skip
    return issues[:1000]


def parse_eslint_json(text: str, root_prefix: str = "") -> list[dict[str, Any]]:
    start = text.find("[")
    try:
        data = json.loads(text[start:]) if start != -1 else []
    except json.JSONDecodeError:
        return []
    issues = []
    for file_result in data if isinstance(data, list) else []:
        file = str(file_result.get("filePath", ""))
        for prefix in (root_prefix.rstrip("/") + "/", "/workspace/"):
            if prefix != "/" and file.startswith(prefix):
                file = file[len(prefix) :]
                break
        for message in file_result.get("messages", []) or []:
            issues.append(
                {"file": file, "line": message.get("line"), "column": message.get("column"),
                 "code": message.get("ruleId"), "message": message.get("message", ""),
                 "severity": "error" if message.get("severity") == 2 else "warning"}
            )  # fmt: skip
    return issues[:1000]


_MYPY_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<col>\d+))?: (?P<sev>error|warning|note): (?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$"
)
_TSC_RE = re.compile(r"^(?P<file>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) (?P<code>TS\d+): (?P<msg>.*)$")
_TSC_PRETTY_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+) - (?P<sev>error|warning) (?P<code>TS\d+): (?P<msg>.*)$")


def parse_typecheck_output(text: str) -> list[dict[str, Any]]:
    issues = []
    for raw in strip_ansi(text).splitlines():
        line = raw.strip()
        match = _MYPY_RE.match(line) or _TSC_RE.match(line) or _TSC_PRETTY_RE.match(line)
        if not match or match.group("sev") == "note":
            continue
        issues.append(
            {"file": match.group("file").strip(), "line": int(match.group("line")),
             "column": int(match.group("col")) if match.group("col") else None,
             "code": match.group("code"), "message": match.group("msg").strip(), "severity": match.group("sev")}
        )  # fmt: skip
    return issues[:1000]
