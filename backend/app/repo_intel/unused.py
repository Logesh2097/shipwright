"""Static detection of unused imports and unreferenced top-level definitions.

Python analysis uses ``ast`` and is reliable for imports. "Unreferenced definition" means
the name does not appear anywhere else in the repository's text; dynamic use (getattr,
framework discovery, public library API) cannot be seen, so those findings are marked
``medium`` or ``low`` confidence and should be reviewed before removal.

JavaScript/TypeScript/Vue: unused imports only, by identifier occurrence.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.repo_intel.detect import detect_language, is_test_file
from app.repo_intel.symbols import _JS_IMPORT_RE, _VUE_SCRIPT_RE, _import_names
from app.repo_intel.workspace import Workspace, WorkspaceError

_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_FRAMEWORK_DECORATOR_HINTS = (
    "route", "get", "post", "put", "patch", "delete", "fixture", "command", "task", "on_event",
    "validator", "property", "setter", "listens_for", "exception_handler", "middleware", "tool",
    "websocket", "hookimpl", "register", "receiver", "callback", "overload",
)  # fmt: skip


def _python_file(path: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Return (unused imports, top-level definitions, names used in this file)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], [], set(_IDENT_RE.findall(text))

    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    # Names referenced inside string annotations or __all__.
    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 200:
            used.update(_IDENT_RE.findall(node.value))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, ast.List | ast.Tuple):
                exported.update(e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str))

    is_init = path.endswith("__init__.py")
    unused_imports: list[dict[str, Any]] = []
    lines = text.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        source_line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if "noqa" in source_line:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name.split(".")[0]
            # "import x as x" / "from m import x as x" is the explicit re-export idiom.
            if alias.asname and alias.asname == alias.name.split(".")[-1]:
                continue
            if bound in used or bound in exported or is_init or bound.startswith("_"):
                continue
            unused_imports.append(
                {
                    "path": path,
                    "line": node.lineno,
                    "name": bound,
                    "statement": source_line.strip()[:160],
                    "confidence": "high",
                }
            )

    definitions: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            decorators = [ast.unparse(d).lower() for d in node.decorator_list]
            definitions.append(
                {
                    "path": path,
                    "line": node.lineno,
                    "name": node.name,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "decorated": bool(decorators),
                    "framework_hook": any(h in d for d in decorators for h in _FRAMEWORK_DECORATOR_HINTS),
                    "exported": node.name in exported,
                }
            )
    return unused_imports, definitions, used


def _js_unused_imports(path: str, text: str, language: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    scripts = [text]
    template = ""
    if language == "vue":
        scripts = [m.group("body") for m in _VUE_SCRIPT_RE.finditer(text)]
        template = _VUE_SCRIPT_RE.sub("", text)
    for script in scripts:
        counts = Counter(_IDENT_RE.findall(script))
        # Vue templates reference components in PascalCase or kebab-case.
        template_idents = set(_IDENT_RE.findall(template))
        template_kebab = set(re.findall(r"</?([a-z][a-z0-9]*(?:-[a-z0-9]+)+)", template))
        for match in _JS_IMPORT_RE.finditer(script):
            statement = script[match.start() : match.end()]
            line = text.count("\n", 0, text.find(statement)) + 1 if statement in text else 1
            for name in _import_names(match.group("clause")):
                kebab = re.sub(r"(?<!^)([A-Z])", r"-\1", name).lower()
                if counts[name] > 1 or name in template_idents or kebab in template_kebab:
                    continue
                findings.append(
                    {
                        "path": path,
                        "line": line,
                        "name": name,
                        "statement": statement.strip()[:160],
                        "confidence": "medium" if "type" in statement.split(name)[0][-12:] else "high",
                    }
                )
    return findings


def find_unused_code(ws: Workspace, files: list[str], *, max_files: int = 3000) -> dict[str, Any]:
    """Analyse ``files`` (repository-relative paths). Blocking; run in a worker thread."""
    unused_imports: list[dict[str, Any]] = []
    definitions: list[dict[str, Any]] = []
    usage: Counter[str] = Counter()
    own_usage: dict[str, set[str]] = {}
    analysed = 0
    skipped = 0

    for path in files:
        language = detect_language(path)
        if language not in {"python", "javascript", "typescript", "vue", "html", "yaml", "toml", "json", "markdown"}:
            continue
        if analysed >= max_files:
            skipped += 1
            continue
        try:
            text = ws.read_text_raw(path)
        except (WorkspaceError, ValueError):
            continue
        analysed += 1
        if language == "python":
            imports, defs, used = _python_file(path, text)
            unused_imports.extend(imports)
            definitions.extend(defs)
            own_usage[path] = used
            usage.update(used)
        elif language in {"javascript", "typescript", "vue"}:
            unused_imports.extend(_js_unused_imports(path, text, language))
            usage.update(set(_IDENT_RE.findall(text)))
        else:
            # Config and docs can reference entry points by name (e.g. "module:function").
            usage.update(set(_IDENT_RE.findall(text)))

    by_name = Counter(d["name"] for d in definitions)
    unused_definitions: list[dict[str, Any]] = []
    for definition in definitions:
        name = definition["name"]
        if name.startswith("__") or name in {"main", "app", "lifespan", "setup", "teardown"}:
            continue
        if is_test_file(definition["path"]) or name.startswith(("test_", "Test")):
            continue
        if definition["framework_hook"] or definition["exported"]:
            continue
        # A name counts as referenced if any file mentions it beyond its own definition.
        references = usage[name]
        if name in own_usage.get(definition["path"], set()):
            continue
        if references > 0:
            continue
        confidence = "medium"
        if definition["decorated"] or by_name[name] > 1 or (not name.startswith("_") and Path(definition["path"]).name == "__init__.py"):
            confidence = "low"
        unused_definitions.append({k: definition[k] for k in ("path", "line", "name", "kind")} | {"confidence": confidence})

    unused_imports.sort(key=lambda f: (f["path"], f["line"]))
    unused_definitions.sort(key=lambda f: (f["path"], f["line"]))
    return {
        "unused_imports": unused_imports[:500],
        "unused_definitions": unused_definitions[:500],
        "files_analysed": analysed,
        "files_skipped": skipped,
        "notes": [
            "Unused imports are reliable for Python; JS/TS/Vue results are identifier based.",
            "Unreferenced definitions may still be used dynamically or as public API; review before removing.",
        ],
    }
