"""Confined, read-mostly access to one repository working tree.

Used by the backend (explorer API, indexer) and by the MCP server's filesystem tools, so
both apply identical path, size, binary and secret-file rules.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.security.gitsafe import GitError, run_git
from app.security.paths import (
    IGNORED_DIRS,
    PathNotAllowed,
    is_ignored_path,
    is_secret_path,
    looks_binary,
    normalize_relative,
    resolve_inside,
)
from app.security.redaction import redact_text

ABSENT = "absent"


class WorkspaceError(Exception):
    def __init__(self, message: str, *, code: str = "workspace_error") -> None:
        super().__init__(message)
        self.code = code


class StaleFileError(WorkspaceError):
    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(
            f"{path} has changed since the proposal was created (expected {expected[:12]}, found {actual[:12]})",
            code="stale",
        )
        self.path, self.expected, self.actual = path, expected, actual


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


@dataclass
class Workspace:
    root: Path
    max_file_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        self.root = Path(os.path.realpath(self.root))

    # --- paths -------------------------------------------------------------------------

    def resolve(self, rel_path: str, **kwargs: Any) -> Path:
        return resolve_inside(self.root, rel_path, **kwargs)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else "."

    # --- listing -----------------------------------------------------------------------

    def list_directory(self, rel_path: str = ".", max_entries: int = 200) -> dict[str, Any]:
        directory = self.resolve(rel_path)
        if not directory.is_dir():
            raise WorkspaceError(f"Not a directory: {rel_path}", code="not_a_directory")
        entries: list[dict[str, Any]] = []
        hidden = 0
        with os.scandir(directory) as it:
            for entry in sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())):
                rel = self.rel(Path(entry.path))
                is_dir = entry.is_dir(follow_symlinks=False)
                if entry.name == ".git" or (is_dir and entry.name in IGNORED_DIRS) or is_secret_path(rel):
                    hidden += 1
                    continue
                if entry.is_symlink():
                    try:
                        self.resolve(rel)
                    except PathNotAllowed:
                        hidden += 1
                        continue
                item: dict[str, Any] = {"name": entry.name, "path": rel, "type": "dir" if is_dir else "file"}
                if not is_dir:
                    try:
                        item["size"] = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        item["size"] = 0
                entries.append(item)
        truncated = len(entries) > max_entries
        return {
            "path": self.rel(directory),
            "entries": entries[:max_entries],
            "truncated": truncated,
            "hidden_entries": hidden,
        }

    def tracked_files(self, limit: int = 20_000) -> list[str]:
        """Files git knows about plus untracked files that are not ignored."""
        try:
            result = run_git(self.root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], check=True)
        except GitError as exc:
            raise WorkspaceError(f"Could not list repository files: {exc}", code="git_error") from exc
        files: list[str] = []
        seen: set[str] = set()
        for name in result.stdout.split("\x00"):
            if not name or name in seen or is_ignored_path(name) or is_secret_path(name):
                continue
            seen.add(name)
            files.append(name)
            if len(files) >= limit:
                break
        return sorted(files)

    def find_files(self, pattern: str, max_results: int = 200) -> dict[str, Any]:
        if "\x00" in pattern or pattern.startswith(("/", "~")) or ".." in pattern.split("/"):
            raise PathNotAllowed("Invalid glob pattern")
        pat = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
        matches = [
            f for f in self.tracked_files()
            if fnmatch.fnmatchcase(f, pat) or fnmatch.fnmatchcase(f.rsplit("/", 1)[-1], pat)
        ]  # fmt: skip
        return {"pattern": pattern, "files": matches[:max_results], "truncated": len(matches) > max_results}

    # --- reading -----------------------------------------------------------------------

    def _read_bytes(self, rel_path: str) -> tuple[Path, bytes]:
        path = self.resolve(rel_path)
        if not path.exists():
            raise WorkspaceError(f"File not found: {rel_path}", code="not_found")
        if not path.is_file():
            raise WorkspaceError(f"Not a regular file: {rel_path}", code="not_a_file")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise WorkspaceError(f"{rel_path} is {size} bytes, above the {self.max_file_bytes} byte limit", code="too_large")
        return path, path.read_bytes()

    def read_text_raw(self, rel_path: str) -> str:
        """Exact file text (no redaction). For hashing, diffing and indexing; not for prompts."""
        _, data = self._read_bytes(rel_path)
        if looks_binary(data[:8192]):
            raise WorkspaceError(f"{rel_path} is a binary file", code="binary")
        return data.decode("utf-8", errors="replace")

    def file_hash(self, rel_path: str) -> str:
        """sha256 of the file, or ``ABSENT`` when it does not exist."""
        path = self.resolve(rel_path)
        if not path.exists():
            return ABSENT
        if not path.is_file():
            raise WorkspaceError(f"Not a regular file: {rel_path}", code="not_a_file")
        return sha256_bytes(path.read_bytes())

    def file_info(self, rel_path: str) -> dict[str, Any]:
        path, data = self._read_bytes(rel_path)
        binary = looks_binary(data[:8192])
        return {
            "path": self.rel(path),
            "size": len(data),
            "sha256": sha256_bytes(data),
            "binary": binary,
            "line_count": 0 if binary else data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
        }

    def read_file(self, rel_path: str, start_line: int = 1, max_lines: int = 400) -> dict[str, Any]:
        """Numbered, redacted excerpt suitable for showing to a model or a user."""
        path, data = self._read_bytes(rel_path)
        if looks_binary(data[:8192]):
            raise WorkspaceError(f"{rel_path} is a binary file", code="binary")
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, start_line)
        count = max(1, min(max_lines, 2000))
        chunk = lines[start - 1 : start - 1 + count]
        redacted = redact_text("\n".join(chunk))
        return {
            "path": self.rel(path),
            "sha256": sha256_bytes(data),
            "total_lines": len(lines),
            "start_line": start,
            "end_line": start + len(chunk) - 1 if chunk else start - 1,
            "truncated": start - 1 + count < len(lines),
            "redacted": redacted != "\n".join(chunk),
            "content": redacted,
        }

    # --- search ------------------------------------------------------------------------

    def search_text(self, query: str, *, regex: bool = False, path_glob: str | None = None, max_results: int = 100) -> dict[str, Any]:
        if not query or "\x00" in query or "\n" in query:
            raise WorkspaceError("Query must be a single non-empty line", code="invalid_query")
        if len(query) > 300:
            raise WorkspaceError("Query is too long", code="invalid_query")
        if regex:
            try:
                re.compile(query)
            except re.error as exc:
                raise WorkspaceError(f"Invalid regular expression: {exc}", code="invalid_query") from exc
        args = ["grep", "-n", "-I", "--untracked", "--no-color", "--full-name", "-i", "-z"]
        args += ["-E" if regex else "-F", "-e", query, "--"]
        if path_glob:
            if path_glob.startswith(("/", "~", ":", "-")) or ".." in path_glob.split("/") or "\x00" in path_glob:
                raise PathNotAllowed("Invalid path filter")
            args.append(f":(glob){path_glob}" if "/" in path_glob else f":(glob)**/{path_glob}")
        else:
            args.append(".")
        try:
            result = run_git(self.root, args, timeout=20)
        except GitError as exc:
            raise WorkspaceError(str(exc), code="git_error") from exc
        if result.returncode not in (0, 1):
            raise WorkspaceError(result.stderr.strip() or "search failed", code="git_error")
        matches: list[dict[str, Any]] = []
        total = 0
        for line in result.stdout.splitlines():
            # With -z the format is: path NUL line-number NUL text
            parts = line.split("\x00", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            path, lineno, text = parts
            if is_ignored_path(path) or is_secret_path(path):
                continue
            total += 1
            if len(matches) < max_results:
                matches.append({"path": path, "line": int(lineno), "text": redact_text(text.strip())[:300]})
        return {"query": query, "matches": matches, "total_matches": total, "truncated": total > len(matches)}

    def grep_counts(self, terms: list[str], timeout: float = 20) -> dict[str, int]:
        """Number of matching lines per file for any of ``terms`` (case-insensitive, literal)."""
        terms = [t for t in terms if t and "\n" not in t and "\x00" not in t][:20]
        if not terms:
            return {}
        args = ["grep", "-c", "-I", "--untracked", "--full-name", "-i", "-z", "-F"]
        for term in terms:
            args += ["-e", term]
        args += ["--", "."]
        try:
            result = run_git(self.root, args, timeout=timeout)
        except GitError:
            return {}
        counts: dict[str, int] = {}
        for line in result.stdout.splitlines():
            path, _, count = line.partition("\x00")
            if count.isdigit() and not is_ignored_path(path) and not is_secret_path(path):
                counts[path] = int(count)
        return counts

    # --- writing (only reachable through approved MCP tool calls) ----------------------

    def write_file(self, rel_path: str, content: str, expected_sha256: str) -> dict[str, Any]:
        """Atomically write ``content`` if the file still matches ``expected_sha256``.

        ``expected_sha256`` is the hash the caller last saw, or ``"absent"`` for a new file.
        """
        normalized = normalize_relative(rel_path)
        path = self.resolve(normalized)
        actual = self.file_hash(normalized)
        if actual != expected_sha256:
            raise StaleFileError(normalized, expected_sha256, actual)
        if path.is_symlink():
            raise WorkspaceError("Refusing to write through a symbolic link", code="symlink")
        data = content.encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise WorkspaceError("Content exceeds the file size limit", code="too_large")
        parent = path.parent
        if not parent.exists():
            # Re-validate the parent chain before creating directories.
            self.resolve(self.rel(parent) if parent != self.root else ".")
            parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        fd, tmp_name = tempfile.mkstemp(prefix=".asea-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            # Last check before the rename narrows the race with concurrent editors.
            if self.file_hash(normalized) != expected_sha256:
                raise StaleFileError(normalized, expected_sha256, self.file_hash(normalized))
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return {"path": normalized, "sha256": sha256_bytes(data), "bytes": len(data), "created": actual == ABSENT}

    def delete_file(self, rel_path: str, expected_sha256: str) -> dict[str, Any]:
        normalized = normalize_relative(rel_path)
        path = self.resolve(normalized)
        actual = self.file_hash(normalized)
        if actual == ABSENT:
            raise WorkspaceError(f"File not found: {normalized}", code="not_found")
        if actual != expected_sha256:
            raise StaleFileError(normalized, expected_sha256, actual)
        path.unlink()
        return {"path": normalized, "deleted": True}
