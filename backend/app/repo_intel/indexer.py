"""Repository registration checks and incremental indexing into PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.config import Settings
from app.db.models import CodeSymbol, RepoFile, Repository
from app.db.session import Database
from app.errors import AppError
from app.logs import get_logger
from app.repo_intel.detect import SYMBOL_LANGUAGES, build_profile, detect_language, is_test_file
from app.repo_intel.symbols import Symbol, extract_symbols
from app.repo_intel.workspace import Workspace, WorkspaceError, sha256_bytes
from app.security.gitsafe import (
    GitError,
    inspect_git_config,
    parse_github_full_name,
    run_git,
    strip_url_credentials,
)
from app.security.paths import is_within, looks_binary

log = get_logger(__name__)


class InvalidRepository(AppError):
    code = "invalid_repository"
    status_code = 400


@dataclass
class RepositoryInfo:
    path: str
    name: str
    default_branch: str | None
    remote_url: str | None
    github_full_name: str | None
    head_commit: str | None


def validate_repository_path(settings: Settings, raw_path: str) -> RepositoryInfo:
    """Check that ``raw_path`` is a Git work tree inside an allowed root and safe to operate on."""
    if "\x00" in raw_path:
        raise InvalidRepository("Invalid path")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise InvalidRepository("Repository path must be absolute")
    real = Path(os.path.realpath(candidate))
    roots = settings.allowed_roots
    if not roots:
        raise InvalidRepository("No repository roots are configured. Set REPOS_ALLOWED_ROOTS on the server.", code="roots_not_configured")
    if not any(is_within(root, real) and real != Path(os.path.realpath(root)) for root in roots):
        raise InvalidRepository(
            "Repository must be located inside one of the allowed directories", code="path_not_allowed",
            details={"allowed_roots": [str(r) for r in roots]},
        )  # fmt: skip
    if not real.is_dir():
        raise InvalidRepository("Directory does not exist")
    if not (real / ".git").exists():
        raise InvalidRepository("Not the root of a Git repository (no .git found)", code="not_a_git_repository")
    risky = inspect_git_config(real)
    if risky:
        raise InvalidRepository(
            "The repository's .git/config contains settings that can execute programs. Remove them before registering.",
            code="unsafe_git_config",
            details={"settings": risky},
        )
    try:
        top = run_git(real, ["rev-parse", "--show-toplevel"], check=True).stdout.strip()
    except GitError as exc:
        raise InvalidRepository(f"Git could not read this repository: {exc}", code="not_a_git_repository") from exc
    if Path(os.path.realpath(top)) != real:
        raise InvalidRepository("Path must be the top level of the repository", code="not_repository_root")

    head = run_git(real, ["rev-parse", "HEAD"])
    branch = run_git(real, ["symbolic-ref", "--short", "-q", "HEAD"])
    remote = run_git(real, ["remote", "get-url", "origin"])
    remote_head = run_git(real, ["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"])
    default_branch = (
        remote_head.stdout.strip().removeprefix("origin/") if remote_head.ok and remote_head.stdout.strip()
        else branch.stdout.strip() or None
    )  # fmt: skip
    remote_url = strip_url_credentials(remote.stdout) if remote.ok and remote.stdout.strip() else None
    return RepositoryInfo(
        path=str(real),
        name=real.name,
        default_branch=default_branch,
        remote_url=remote_url,
        github_full_name=parse_github_full_name(remote_url),
        head_commit=head.stdout.strip() if head.ok else None,
    )


@dataclass
class _Scanned:
    path: str
    language: str | None
    size: int
    lines: int
    content_hash: str
    is_test: bool
    is_binary: bool
    symbols: list[Symbol] = field(default_factory=list)
    symbols_extracted: bool = False


def _scan(root: str, max_file_bytes: int, max_files: int, known: dict[str, str]) -> tuple[list[_Scanned], dict[str, Any], str | None]:
    """Blocking scan; run in a worker thread."""
    ws = Workspace(Path(root), max_file_bytes=max_file_bytes)
    files = ws.tracked_files(limit=max_files)
    scanned: list[_Scanned] = []
    for rel in files:
        try:
            path = ws.resolve(rel)
            if not path.is_file():
                continue
            size = path.stat().st_size
        except (ValueError, OSError):
            continue
        language = detect_language(rel)
        if size > max_file_bytes:
            scanned.append(_Scanned(rel, language, size, 0, "", is_test_file(rel), False))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        binary = looks_binary(data[:8192])
        digest = sha256_bytes(data)
        item = _Scanned(rel, language, size, 0 if binary else data.count(b"\n") + 1, digest, is_test_file(rel), binary)
        if not binary and language in SYMBOL_LANGUAGES and known.get(rel) != digest:
            item.symbols = extract_symbols(rel, language, data.decode("utf-8", errors="replace"))
            item.symbols_extracted = True
        scanned.append(item)
    profile = build_profile(ws, [s.path for s in scanned])
    profile["truncated"] = len(files) >= max_files
    head = run_git(root, ["rev-parse", "HEAD"])
    return scanned, profile, head.stdout.strip() if head.ok else None


async def index_repository(db: Database, settings: Settings, repository_id: uuid.UUID) -> dict[str, Any]:
    """Scan the working tree and bring ``repo_files`` and ``code_symbols`` up to date."""
    async with db.session() as session:
        repository = await session.get(Repository, repository_id)
        if repository is None:
            return {"status": "missing"}
        repository.status = "indexing"
        repository.index_error = None
        root = repository.local_path
        existing = {f.path: f for f in await session.scalars(select(RepoFile).where(RepoFile.repository_id == repository_id))}
    known = {path: f.content_hash for path, f in existing.items()}

    try:
        scanned, profile, head = await asyncio.to_thread(_scan, root, settings.max_file_bytes, settings.index_max_files, known)
    except (WorkspaceError, OSError, GitError) as exc:
        log.warning("index_failed", repository_id=str(repository_id), error=str(exc))
        async with db.session() as session:
            repository = await session.get(Repository, repository_id)
            if repository is not None:
                repository.status = "error"
                repository.index_error = str(exc)[:2000]
        return {"status": "error", "error": str(exc)}

    stats = {"files": len(scanned), "added": 0, "updated": 0, "removed": 0, "symbols": 0}
    async with db.session() as session:
        current = {f.path: f for f in await session.scalars(select(RepoFile).where(RepoFile.repository_id == repository_id))}
        seen: set[str] = set()
        for item in scanned:
            seen.add(item.path)
            row = current.get(item.path)
            if row is None:
                row = RepoFile(repository_id=repository_id, path=item.path)
                session.add(row)
                stats["added"] += 1
            elif row.content_hash != item.content_hash:
                stats["updated"] += 1
            row.language = item.language
            row.size_bytes = item.size
            row.line_count = item.lines
            row.content_hash = item.content_hash
            row.is_test = item.is_test
            row.is_binary = item.is_binary
            row.indexed_at = datetime.now(UTC)
            if item.symbols_extracted:
                await session.flush()
                await session.execute(delete(CodeSymbol).where(CodeSymbol.file_id == row.id))
                session.add_all(
                    CodeSymbol(
                        repository_id=repository_id,
                        file_id=row.id,
                        name=s.name[:300],
                        qualified_name=s.qualified_name[:600],
                        kind=s.kind,
                        start_line=s.start_line,
                        end_line=s.end_line,
                        signature=s.signature,
                        extra=s.extra,
                    )
                    for s in item.symbols
                )
                stats["symbols"] += len(item.symbols)
        removed = [row.id for path, row in current.items() if path not in seen]
        if removed:
            await session.execute(delete(RepoFile).where(RepoFile.id.in_(removed)))
            stats["removed"] = len(removed)
        repository = await session.get(Repository, repository_id)
        if repository is not None:
            repository.status = "ready"
            repository.profile = profile
            repository.head_commit = head
            repository.last_indexed_at = datetime.now(UTC)
    log.info("index_complete", repository_id=str(repository_id), **stats)
    return {"status": "ready", **stats}
