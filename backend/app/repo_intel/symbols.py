"""Symbol extraction.

Python uses the standard ``ast`` module. JavaScript, TypeScript and Vue use regular
expressions: good enough to find declarations, routes and components, but not a parser.
Nested or unusually formatted declarations can be missed; text search covers those.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket", "route", "api_route"}


@dataclass
class Symbol:
    name: str
    kind: str  # function | class | method | route | component | import | variable
    start_line: int
    end_line: int
    qualified_name: str = ""
    signature: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def extract_symbols(path: str, language: str | None, text: str) -> list[Symbol]:
    if language == "python":
        return _python_symbols(text)
    if language in {"javascript", "typescript"}:
        return _js_symbols(text, path)
    if language == "vue":
        return _vue_symbols(text, path)
    return []


# --- Python ----------------------------------------------------------------------------


def _unparse(node: ast.AST | None, limit: int = 200) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)[:limit]
    except Exception:
        return ""


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {_unparse(node.returns, 80)}" if node.returns else ""
    return f"{prefix} {node.name}({_unparse(node.args, 300)}){returns}"


def _decorator_name(dec: ast.expr) -> str:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return _unparse(target, 120)


def _route_from_decorator(dec: ast.expr) -> dict[str, Any] | None:
    """Recognise @app.get("/x"), @router.post("/x"), @app.route("/x", methods=[...])."""
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    method = dec.func.attr.lower()
    if method not in HTTP_METHODS:
        return None
    path: str | None = None
    if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
        path = dec.args[0].value
    for kw in dec.keywords:
        if kw.arg == "path" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            path = kw.value.value
    if path is None:
        return None
    methods = [method.upper()]
    if method in {"route", "api_route"}:
        methods = ["GET"]
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List | ast.Tuple):
                found = [e.value.upper() for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                methods = found or methods
    return {"http_methods": methods, "http_path": path, "router": _unparse(dec.func.value, 60)}


def _python_symbols(text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    symbols: list[Symbol] = []

    def visit_function(node: ast.FunctionDef | ast.AsyncFunctionDef, parent: str | None) -> None:
        decorators = [_decorator_name(d) for d in node.decorator_list]
        qualified = f"{parent}.{node.name}" if parent else node.name
        end = node.end_lineno or node.lineno
        symbols.append(
            Symbol(
                name=node.name,
                kind="method" if parent else "function",
                start_line=node.lineno,
                end_line=end,
                qualified_name=qualified,
                signature=_py_signature(node),
                extra={"decorators": decorators[:8], "docstring": (ast.get_docstring(node) or "")[:200]},
            )
        )
        for dec in node.decorator_list:
            if route := _route_from_decorator(dec):
                for method in route["http_methods"]:
                    symbols.append(
                        Symbol(
                            name=f"{method} {route['http_path']}",
                            kind="route",
                            start_line=node.lineno,
                            end_line=end,
                            qualified_name=qualified,
                            signature=_py_signature(node),
                            extra={
                                "http_method": method,
                                "http_path": route["http_path"],
                                "handler": qualified,
                                "router": route["router"],
                                "framework": "python",
                            },
                        )
                    )

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            visit_function(node, None)
        elif isinstance(node, ast.ClassDef):
            bases = [_unparse(b, 60) for b in node.bases]
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    qualified_name=node.name,
                    signature=f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}",
                    extra={"bases": bases, "docstring": (ast.get_docstring(node) or "")[:200]},
                )
            )
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit_function(child, node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            module = node.module or "" if isinstance(node, ast.ImportFrom) else ""
            level = node.level if isinstance(node, ast.ImportFrom) else 0
            for alias in node.names:
                target = f"{'.' * level}{module}.{alias.name}" if isinstance(node, ast.ImportFrom) else alias.name
                symbols.append(
                    Symbol(
                        name=alias.asname or alias.name.split(".")[0],
                        kind="import",
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        qualified_name=target.strip("."),
                        extra={"module": f"{'.' * level}{module}" if isinstance(node, ast.ImportFrom) else alias.name},
                    )
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for assigned in targets:
                if isinstance(assigned, ast.Name) and (assigned.id.isupper() or _is_router_call(node.value)):
                    symbols.append(
                        Symbol(
                            name=assigned.id,
                            kind="variable",
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            qualified_name=assigned.id,
                            signature=_unparse(node, 160),
                        )
                    )
    return symbols


def _is_router_call(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Call) and _unparse(value.func, 40).split(".")[-1] in {"APIRouter", "FastAPI", "Flask", "Blueprint"}


# --- JavaScript / TypeScript -----------------------------------------------------------

_JS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "function",
        re.compile(
            r"^[ \t]*(?P<export>export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>{]*>)?\s*\(",
            re.M,
        ),
    ),
    (
        "function",
        re.compile(
            r"^[ \t]*(?P<export>export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s*)?"
            r"(?:function\b|(?:<[^>\n]*>\s*)?\([^)\n]*\)\s*(?::[^=\n]+?)?=>|[A-Za-z_$][\w$]*\s*=>)",
            re.M,
        ),
    ),
    (
        "class",
        re.compile(r"^[ \t]*(?P<export>export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)", re.M),
    ),
    (
        "class",
        re.compile(r"^[ \t]*(?P<export>export\s+)?(?:interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)", re.M),
    ),
    (
        "variable",
        re.compile(
            r"^(?P<export>export\s+)(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?!(?:async\s*)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>))",
            re.M,
        ),
    ),
]
_JS_ROUTE_RE = re.compile(
    r"\b(?P<obj>app|router|server|api|[A-Za-z_$][\w$]*[Rr]outer)\s*\.\s*(?P<method>get|post|put|patch|delete|all|use)\s*\(\s*"
    r"(?P<q>['\"`])(?P<path>/[^'\"`\n]*)(?P=q)"
)
_JS_IMPORT_RE = re.compile(r"^[ \t]*import\s+(?:type\s+)?(?P<clause>[^'\";]+?)\s+from\s+['\"](?P<module>[^'\"]+)['\"]", re.M)
_JS_REQUIRE_RE = re.compile(
    r"^[ \t]*(?:const|let|var)\s+(?P<clause>\{[^}]+\}|[A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\)",
    re.M,
)
_VUE_ROUTE_RE = re.compile(r"\bpath\s*:\s*(?P<q>['\"`])(?P<path>/[^'\"`\n]*)(?P=q)")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _block_end(text: str, start_index: int, start_line: int) -> int:
    """Find the line of the brace that closes the first block opened after ``start_index``."""
    open_index = text.find("{", start_index)
    newline = text.find("\n", start_index)
    if open_index == -1 or (newline != -1 and open_index - start_index > 400):
        return start_line
    depth = 0
    in_string: str | None = None
    i = open_index
    limit = min(len(text), open_index + 60_000)
    while i < limit:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        elif ch in "'\"`":
            in_string = ch
        elif ch == "/" and text[i : i + 2] == "//":
            nl = text.find("\n", i)
            i = nl if nl != -1 else limit
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _line_of(text, i)
        i += 1
    return start_line


def _import_names(clause: str) -> list[str]:
    names: list[str] = []
    clause = clause.strip()
    braces = re.search(r"\{([^}]*)\}", clause)
    if braces:
        for part in braces.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^type\s+", "", part)
            names.append(re.split(r"\s+as\s+|\s*:\s*", part)[-1].strip())
        clause = clause.replace(braces.group(0), "")
    for part in clause.split(","):
        part = part.strip()
        if not part:
            continue
        star = re.match(r"\*\s+as\s+([A-Za-z_$][\w$]*)", part)
        names.append(star.group(1) if star else part)
    return [n for n in names if re.fullmatch(r"[A-Za-z_$][\w$]*", n)]


def _js_symbols(text: str, path: str, line_offset: int = 0) -> list[Symbol]:
    symbols: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for kind, pattern in _JS_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group("name")
            line = _line_of(text, match.start("name"))
            if (name, line) in seen:
                continue
            seen.add((name, line))
            end = _block_end(text, match.end(), line) if kind != "variable" else line
            signature = text[match.start() : text.find("\n", match.start()) if "\n" in text[match.start() :] else len(text)]
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    start_line=line + line_offset,
                    end_line=max(end, line) + line_offset,
                    qualified_name=name,
                    signature=signature.strip()[:200],
                    extra={"exported": bool(match.group("export"))},
                )
            )
    for match in _JS_ROUTE_RE.finditer(text):
        method = match.group("method").upper()
        if method == "USE":
            continue
        line = _line_of(text, match.start())
        symbols.append(
            Symbol(
                name=f"{method} {match.group('path')}",
                kind="route",
                start_line=line + line_offset,
                end_line=line + line_offset,
                qualified_name=f"{match.group('obj')}.{match.group('method')}",
                signature=text[match.start() : match.end()][:200],
                extra={"http_method": method, "http_path": match.group("path"), "framework": "node", "router": match.group("obj")},
            )
        )
    if re.search(r"\b(createRouter|RouteRecordRaw|routes\s*[:=])", text):
        for match in _VUE_ROUTE_RE.finditer(text):
            line = _line_of(text, match.start())
            symbols.append(
                Symbol(
                    name=f"PAGE {match.group('path')}",
                    kind="route",
                    start_line=line + line_offset,
                    end_line=line + line_offset,
                    qualified_name="router",
                    signature=text[match.start() : match.end()][:200],
                    extra={"http_method": "PAGE", "http_path": match.group("path"), "framework": "vue-router"},
                )
            )
    for pattern in (_JS_IMPORT_RE, _JS_REQUIRE_RE):
        for match in pattern.finditer(text):
            line = _line_of(text, match.start())
            for name in _import_names(match.group("clause")):
                symbols.append(
                    Symbol(
                        name=name,
                        kind="import",
                        start_line=line + line_offset,
                        end_line=line + line_offset,
                        qualified_name=f"{match.group('module')}.{name}",
                        extra={"module": match.group("module")},
                    )
                )
    return symbols


_VUE_SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)


def _vue_symbols(text: str, path: str) -> list[Symbol]:
    name = path.rsplit("/", 1)[-1].removesuffix(".vue")
    total_lines = text.count("\n") + 1
    symbols = [
        Symbol(
            name=name,
            kind="component",
            start_line=1,
            end_line=total_lines,
            qualified_name=name,
            signature=f"<{name}> single-file component",
            extra={"script_setup": bool(re.search(r"<script\b[^>]*\bsetup\b", text, re.I))},
        )
    ]
    for match in _VUE_SCRIPT_RE.finditer(text):
        offset = _line_of(text, match.start("body")) - 1
        symbols.extend(_js_symbols(match.group("body"), path, line_offset=offset))
    props = re.search(r"defineProps\s*<\s*\{(?P<body>[^}]*)\}", text, re.S)
    if props:
        symbols[0].extra["props"] = re.findall(r"^\s*([A-Za-z_$][\w$]*)\??\s*:", props.group("body"), re.M)[:30]
    emits = re.search(r"defineEmits\s*(?:<[^>]*>)?\s*\(\s*\[(?P<body>[^\]]*)\]", text, re.S)
    if emits:
        symbols[0].extra["emits"] = re.findall(r"['\"]([^'\"]+)['\"]", emits.group("body"))[:30]
    return symbols
