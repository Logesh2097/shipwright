"""Language, framework, dependency and test-framework detection."""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from app.repo_intel.workspace import Workspace, WorkspaceError

LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "typescript", ".vue": "vue", ".html": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown",
    ".sql": "sql", ".sh": "shell", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".svelte": "svelte",
}  # fmt: skip
LANGUAGE_BY_NAME = {"Dockerfile": "dockerfile", "Makefile": "makefile"}
CODE_LANGUAGES = {
    "python", "javascript", "typescript", "vue", "go", "rust", "java", "kotlin", "ruby", "php",
    "csharp", "c", "cpp", "svelte",
}  # fmt: skip
SYMBOL_LANGUAGES = {"python", "javascript", "typescript", "vue"}

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$"),
    re.compile(r"(^|/)e2e/"),
)

PY_FRAMEWORKS = {
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask", "starlette": "Starlette",
    "sqlalchemy": "SQLAlchemy", "alembic": "Alembic", "pydantic": "Pydantic", "celery": "Celery",
    "langchain": "LangChain", "langgraph": "LangGraph", "pytest": "pytest", "mcp": "MCP SDK",
}  # fmt: skip
JS_FRAMEWORKS = {
    "vue": "Vue", "react": "React", "next": "Next.js", "nuxt": "Nuxt", "express": "Express",
    "@nestjs/core": "NestJS", "fastify": "Fastify", "koa": "Koa", "vite": "Vite", "pinia": "Pinia",
    "vue-router": "Vue Router", "svelte": "Svelte", "@angular/core": "Angular", "tailwindcss": "Tailwind CSS",
    "typescript": "TypeScript", "vitest": "Vitest", "jest": "Jest", "eslint": "ESLint", "prisma": "Prisma",
}  # fmt: skip
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def detect_language(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.name in LANGUAGE_BY_NAME or p.name.startswith("Dockerfile"):
        return LANGUAGE_BY_NAME.get(p.name, "dockerfile")
    return LANGUAGE_BY_EXT.get(p.suffix.lower())


def is_test_file(path: str) -> bool:
    return any(p.search(path) for p in _TEST_PATTERNS)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read(ws: Workspace, path: str) -> str | None:
    try:
        return ws.read_text_raw(path)
    except (WorkspaceError, ValueError):
        return None


def _python_dependencies(ws: Workspace, files: list[str]) -> tuple[set[str], list[str]]:
    deps: set[str] = set()
    manifests: list[str] = []
    for path in files:
        name = PurePosixPath(path).name
        depth = path.count("/")
        if depth > 3:
            continue
        if name == "pyproject.toml" and (text := _read(ws, path)):
            manifests.append(path)
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            project = data.get("project", {})
            raw: list[str] = list(project.get("dependencies", []) or [])
            for group in (project.get("optional-dependencies", {}) or {}).values():
                raw += list(group or [])
            for group in (data.get("dependency-groups", {}) or {}).values():
                raw += [g for g in (group or []) if isinstance(g, str)]
            poetry = data.get("tool", {}).get("poetry", {})
            raw += list((poetry.get("dependencies", {}) or {}).keys())
            raw += list((poetry.get("dev-dependencies", {}) or {}).keys())
            for group in (poetry.get("group", {}) or {}).values():
                raw += list((group.get("dependencies", {}) or {}).keys())
            for item in raw:
                if match := _REQ_NAME_RE.match(str(item)):
                    deps.add(_norm(match.group(1)))
            if "pytest" in data.get("tool", {}):
                deps.add("pytest")
        elif re.fullmatch(r"requirements[\w.-]*\.txt", name) and (text := _read(ws, path)):
            manifests.append(path)
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-") and (match := _REQ_NAME_RE.match(line)):
                    deps.add(_norm(match.group(1)))
        elif name in {"setup.py", "setup.cfg", "Pipfile"}:
            manifests.append(path)
        elif name in {"pytest.ini", "tox.ini"}:
            deps.add("pytest")
    return deps, manifests


def _node_packages(ws: Workspace, files: list[str]) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for path in files:
        if PurePosixPath(path).name != "package.json" or path.count("/") > 3:
            continue
        text = _read(ws, path)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        directory = str(PurePosixPath(path).parent)
        packages.append(
            {
                "path": path,
                "directory": "." if directory in ("", ".") else directory,
                "name": data.get("name"),
                "dependencies": sorted(deps),
                "scripts": {k: str(v)[:200] for k, v in (data.get("scripts") or {}).items()},
            }
        )
    return packages


def build_profile(ws: Workspace, files: list[str]) -> dict[str, Any]:
    languages: Counter[str] = Counter()
    for path in files:
        if lang := detect_language(path):
            languages[lang] += 1

    py_deps, py_manifests = _python_dependencies(ws, files)
    node_packages = _node_packages(ws, files)
    js_deps = {d for pkg in node_packages for d in pkg["dependencies"]}

    frameworks = sorted(
        {label for dep, label in PY_FRAMEWORKS.items() if dep in py_deps}
        | {label for dep, label in JS_FRAMEWORKS.items() if dep in js_deps}
    )

    file_set = set(files)
    names = {PurePosixPath(f).name for f in files}
    package_managers = sorted(
        {
            manager
            for lockfile, manager in {
                "package-lock.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "uv.lock": "uv",
                "poetry.lock": "poetry", "Pipfile.lock": "pipenv",
            }.items()
            if lockfile in names
        }
        | ({"pip"} if any(PurePosixPath(m).name.startswith("requirements") for m in py_manifests) else set())
    )  # fmt: skip

    test_frameworks: list[dict[str, Any]] = []
    has_py_tests = any(is_test_file(f) and f.endswith(".py") for f in files)
    if "pytest" in py_deps or has_py_tests:
        test_frameworks.append({"framework": "pytest", "directory": _python_root(py_manifests)})
    for pkg in node_packages:
        for tool in ("vitest", "jest"):
            if tool in pkg["dependencies"]:
                test_frameworks.append({"framework": tool, "directory": pkg["directory"]})
        if "test" in pkg["scripts"] and not any(t in pkg["dependencies"] for t in ("vitest", "jest")):
            test_frameworks.append({"framework": "npm", "directory": pkg["directory"]})

    quality_tools: list[dict[str, Any]] = []
    if "ruff" in py_deps or "ruff.toml" in names or ".ruff.toml" in names:
        quality_tools.append({"tool": "ruff", "kind": "lint", "directory": _python_root(py_manifests)})
    if "mypy" in py_deps or "mypy.ini" in names:
        quality_tools.append({"tool": "mypy", "kind": "typecheck", "directory": _python_root(py_manifests)})
    for pkg in node_packages:
        if "eslint" in pkg["dependencies"]:
            quality_tools.append({"tool": "eslint", "kind": "lint", "directory": pkg["directory"]})
        if "vue-tsc" in pkg["dependencies"]:
            quality_tools.append({"tool": "vue-tsc", "kind": "typecheck", "directory": pkg["directory"]})
        elif "typescript" in pkg["dependencies"]:
            quality_tools.append({"tool": "tsc", "kind": "typecheck", "directory": pkg["directory"]})
        if "build" in pkg["scripts"]:
            quality_tools.append({"tool": "npm", "kind": "build", "directory": pkg["directory"]})

    entry_candidates = (
        "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "app/main.py", "src/main.py",
        "src/main.ts", "src/main.js", "src/index.ts", "src/index.js", "index.js", "server.js", "src/App.vue",
    )  # fmt: skip
    entry_points = sorted(f for f in file_set if any(f == c or f.endswith("/" + c) for c in entry_candidates) and f.count("/") <= 3)

    has_backend = bool({"FastAPI", "Django", "Flask", "Express", "NestJS", "Fastify", "Koa"} & set(frameworks))
    has_frontend = bool({"Vue", "React", "Next.js", "Nuxt", "Svelte", "Angular"} & set(frameworks))
    kind = (
        "fullstack" if has_backend and has_frontend
        else "backend" if has_backend
        else "frontend" if has_frontend
        else "library"
    )  # fmt: skip

    code_files = sum(n for lang, n in languages.items() if lang in CODE_LANGUAGES)
    return {
        "kind": kind,
        "languages": dict(languages.most_common()),
        "primary_language": next((lang for lang, _ in languages.most_common() if lang in CODE_LANGUAGES), None),
        "frameworks": frameworks,
        "python_dependencies": sorted(py_deps)[:300],
        "node_packages": [{**p, "dependencies": p["dependencies"][:300]} for p in node_packages[:20]],
        "manifests": sorted(py_manifests + [p["path"] for p in node_packages]),
        "package_managers": package_managers,
        "test_frameworks": test_frameworks,
        "quality_tools": quality_tools,
        "entry_points": entry_points[:30],
        "file_count": len(files),
        "code_file_count": code_files,
        "test_file_count": sum(1 for f in files if is_test_file(f)),
    }


def _python_root(manifests: list[str]) -> str:
    """Directory of the shallowest Python manifest (where pytest/ruff/mypy should run)."""
    if not manifests:
        return "."
    best = min(manifests, key=lambda m: (m.count("/"), m))
    parent = str(PurePosixPath(best).parent)
    return "." if parent in ("", ".") else parent
