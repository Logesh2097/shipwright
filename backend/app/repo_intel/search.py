"""Symbol search, relevant-file retrieval, route listing and repository overview."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CodeSymbol, RepoFile, Repository
from app.repo_intel.detect import CODE_LANGUAGES
from app.repo_intel.workspace import Workspace

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "should",
        "so",
        "that",
        "the",
        "their",
        "there",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "add",
        "adding",
        "bug",
        "bugs",
        "change",
        "code",
        "create",
        "error",
        "explain",
        "file",
        "files",
        "find",
        "fix",
        "function",
        "functions",
        "generate",
        "identify",
        "implement",
        "issue",
        "make",
        "module",
        "need",
        "new",
        "problem",
        "refactor",
        "remove",
        "show",
        "tests",
        "test",
        "unit",
        "update",
        "use",
        "using",
        "work",
        "works",
        "existing",
        "specific",
        "investigate",
        "analyze",
        "suggest",
        "failing",
        "write",
    ]
)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d{3,}")


def split_identifier(token: str) -> list[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", token).replace("_", " ").replace("-", " ").split()
    return [p.lower() for p in parts if len(p) > 1]


def query_terms(query: str, extra: list[str] | None = None) -> list[str]:
    """Distinct, lower-cased search terms: whole identifiers and their parts, minus stop words."""
    terms: list[str] = []
    for raw in [*(_TOKEN_RE.findall(query)), *(extra or [])]:
        whole = raw.lower()
        for candidate in [whole, *split_identifier(raw)]:
            if len(candidate) > 2 and candidate not in _STOPWORDS and candidate not in terms:
                terms.append(candidate)
    return terms[:24]


def _stem(term: str) -> str:
    for suffix in ("ation", "ing", "ers", "er", "ed", "es", "s"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            return term[: -len(suffix)]
    return term


async def search_symbols(
    session: AsyncSession, repository_id: uuid.UUID, query: str, *, kind: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    terms = query_terms(query) or [query.strip().lower()]
    terms = [t for t in terms if t][:8]
    if not terms:
        return []
    stmt = (
        select(CodeSymbol, RepoFile.path)
        .join(RepoFile, RepoFile.id == CodeSymbol.file_id)
        .where(CodeSymbol.repository_id == repository_id)
        .where(or_(*[func.lower(CodeSymbol.name).contains(t, autoescape=True) for t in terms]))
    )
    if kind:
        stmt = stmt.where(CodeSymbol.kind == kind)
    else:
        stmt = stmt.where(CodeSymbol.kind != "import")
    rows = (await session.execute(stmt.limit(2000))).all()
    exact = query.strip().lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for symbol, path in rows:
        name = symbol.name.lower()
        score = sum(2.0 if t == name else 1.0 for t in terms if t in name)
        if name == exact:
            score += 5
        scored.append((score, symbol_to_dict(symbol, path)))
    scored.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["start_line"]))
    return [item for _, item in scored[:limit]]


def symbol_to_dict(symbol: CodeSymbol, path: str) -> dict[str, Any]:
    return {
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind,
        "path": path,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "signature": symbol.signature,
        "extra": symbol.extra,
    }


async def list_routes(session: AsyncSession, repository_id: uuid.UUID, limit: int = 500) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(CodeSymbol, RepoFile.path)
            .join(RepoFile, RepoFile.id == CodeSymbol.file_id)
            .where(CodeSymbol.repository_id == repository_id, CodeSymbol.kind == "route")
            .order_by(RepoFile.path, CodeSymbol.start_line)
            .limit(limit)
        )
    ).all()
    return [
        {
            "method": s.extra.get("http_method"),
            "path": s.extra.get("http_path"),
            "handler": s.extra.get("handler") or s.qualified_name,
            "framework": s.extra.get("framework"),
            "file": path,
            "line": s.start_line,
        }
        for s, path in rows
    ]


async def relevant_files(
    session: AsyncSession,
    repository: Repository,
    query: str,
    *,
    extra_terms: list[str] | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Rank files for a free-text request using paths, symbol names and content matches."""
    terms = query_terms(query, extra_terms)
    if not terms:
        return []
    stems = {t: _stem(t) for t in terms}
    files = list(await session.scalars(select(RepoFile).where(RepoFile.repository_id == repository.id, RepoFile.is_binary.is_(False))))
    if not files:
        return []
    by_id = {f.id: f for f in files}
    scores: dict[uuid.UUID, float] = defaultdict(float)
    reasons: dict[uuid.UUID, list[str]] = defaultdict(list)

    # 1. Path matches
    for f in files:
        path_tokens = set(split_identifier(re.sub(r"[/.]", " ", f.path)))
        hits = [t for t in terms if t in path_tokens or any(stems[t] in pt for pt in path_tokens)]
        if hits:
            scores[f.id] += 3.0 * len(hits)
            reasons[f.id].append(f"path matches: {', '.join(hits[:5])}")

    # 2. Symbol-name matches
    rows = (
        await session.execute(
            select(CodeSymbol.file_id, CodeSymbol.name, CodeSymbol.kind)
            .where(CodeSymbol.repository_id == repository.id, CodeSymbol.kind != "import")
            .where(or_(*[func.lower(CodeSymbol.name).contains(stems[t], autoescape=True) for t in terms]))
            .limit(5000)
        )
    ).all()
    symbol_hits: dict[uuid.UUID, set[str]] = defaultdict(set)
    # Each term counts once per file (its best match), so a file with many similarly named
    # symbols, typically a test module, does not outrank the file that defines the thing.
    best: dict[tuple[uuid.UUID, str], float] = {}
    for file_id, name, kind in rows:
        lowered = name.lower()
        parts = set(split_identifier(name)) | {lowered}
        for t in terms:
            if t in parts:
                value = 5.0 if kind == "route" else 4.0
            elif stems[t] in lowered:
                value = 1.5
            else:
                continue
            symbol_hits[file_id].add(name)
            best[(file_id, t)] = max(best.get((file_id, t), 0.0), value)
    for (file_id, _term), value in best.items():
        scores[file_id] += value
    for file_id, names in symbol_hits.items():
        reasons[file_id].append(f"symbols: {', '.join(sorted(names)[:5])}")

    # 3. Content matches (git grep, one pass for all terms)
    ws = Workspace(Path(repository.local_path))
    counts = await asyncio.to_thread(ws.grep_counts, sorted(set(stems.values())))
    by_path = {f.path: f for f in files}
    for path, count in counts.items():
        row = by_path.get(path)
        if row is not None:
            scores[row.id] += 1.5 * math.log1p(count)
            reasons[row.id].append(f"{count} matching lines")

    ranked: list[dict[str, Any]] = []
    for file_id, score in scores.items():
        row = by_id.get(file_id)
        if row is None:
            continue
        if row.language not in CODE_LANGUAGES:
            score *= 0.5
        if row.is_test and not any(t in {"test", "tests", "pytest", "vitest", "spec"} for t in terms):
            score *= 0.6
        if row.line_count > 2000:
            score *= 0.8
        ranked.append(
            {
                "path": row.path,
                "score": round(score, 2),
                "language": row.language,
                "lines": row.line_count,
                "is_test": row.is_test,
                "reasons": reasons[file_id][:3],
            }
        )
    ranked.sort(key=lambda r: (-r["score"], r["path"]))
    return ranked[:limit]


def _tree_outline(paths: list[str], max_dirs: int = 40) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for path in paths:
        parts = PurePosixPath(path).parts
        for depth in (1, 2):
            if len(parts) > depth:
                counts["/".join(parts[:depth]) + "/"] += 1
        if len(parts) == 1:
            counts["(root files)"] += 1
    ordered = sorted(counts.items(), key=lambda kv: (kv[0].count("/"), -kv[1], kv[0]))
    top = sorted(ordered[:max_dirs], key=lambda kv: kv[0])
    return [f"{'  ' * (name.count('/') - 1 if name.endswith('/') else 0)}{name} ({n} files)" for name, n in top]


async def build_overview(session: AsyncSession, repository: Repository, max_chars: int = 6000) -> str:
    """A bounded, human-readable description of the repository for prompts and the UI."""
    profile = repository.profile or {}
    paths = list(
        await session.scalars(select(RepoFile.path).where(RepoFile.repository_id == repository.id).order_by(RepoFile.path).limit(20000))
    )
    lines = [
        f"Repository: {repository.name}",
        f"Kind: {profile.get('kind', 'unknown')}; default branch: {repository.default_branch or 'unknown'}",
        "Languages (files): " + (", ".join(f"{k} {v}" for k, v in list(profile.get("languages", {}).items())[:10]) or "none detected"),
        "Frameworks and tools: " + (", ".join(profile.get("frameworks", [])) or "none detected"),
        f"Files: {profile.get('file_count', len(paths))} ({profile.get('test_file_count', 0)} test files)",
    ]
    if tests := profile.get("test_frameworks"):
        lines.append("Test frameworks: " + ", ".join(f"{t['framework']} in {t['directory']}" for t in tests))
    if tools := profile.get("quality_tools"):
        lines.append("Quality tools: " + ", ".join(f"{t['tool']} ({t['kind']}) in {t['directory']}" for t in tools))
    if entries := profile.get("entry_points"):
        lines.append("Entry points: " + ", ".join(entries[:12]))
    for pkg in profile.get("node_packages", [])[:4]:
        if pkg.get("scripts"):
            lines.append(f"npm scripts in {pkg['directory']}: " + ", ".join(sorted(pkg["scripts"])[:15]))
    lines.append("")
    lines.append("Layout:")
    lines.extend(_tree_outline(paths))
    routes = await list_routes(session, repository.id, limit=40)
    if routes:
        lines.append("")
        lines.append("HTTP routes (first 40):")
        lines.extend(f"  {r['method']} {r['path']} -> {r['handler']} ({r['file']}:{r['line']})" for r in routes)
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... [overview truncated]"
