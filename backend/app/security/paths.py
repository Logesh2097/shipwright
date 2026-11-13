"""Filesystem confinement shared by the backend and the MCP server."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath

IGNORED_DIRS = frozenset(
    {
        ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".ruff_cache",
        ".pytest_cache", ".tox", ".nox", "dist", "build", ".next", ".nuxt", ".output", "coverage",
        ".coverage", "htmlcov", ".idea", ".vscode", ".gradle", "target", ".terraform", ".cache",
        "site-packages", ".eggs",
    }
)  # fmt: skip

SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_dsa*",
    "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials",
    "credentials.*", "*.credentials", "secrets.*", "*.secret", "*.secrets", "service-account*.json",
    "*.tfstate", "*.tfstate.*", ".htpasswd", "*.kdbx",
)  # fmt: skip
# Templates that document variable names without holding values.
_SECRET_EXEMPT = (".env.example", ".env.sample", ".env.template", ".env.dist", "*.pub")


class PathNotAllowed(ValueError):
    """Raised when a path is outside the workspace or names a protected file."""

    def __init__(self, message: str, *, reason: str = "path_not_allowed") -> None:
        super().__init__(message)
        self.reason = reason


def is_secret_path(rel_path: str) -> bool:
    name = PurePosixPath(rel_path.replace("\\", "/")).name.lower()
    if any(fnmatch.fnmatchcase(name, pat) for pat in _SECRET_EXEMPT):
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_FILE_PATTERNS)


def is_ignored_path(rel_path: str) -> bool:
    return any(part in IGNORED_DIRS for part in PurePosixPath(rel_path).parts)


def normalize_relative(rel_path: str) -> str:
    """Validate and normalise a repository-relative path without touching the filesystem."""
    if not isinstance(rel_path, str) or rel_path == "":
        raise PathNotAllowed("Path must be a non-empty string")
    if "\x00" in rel_path:
        raise PathNotAllowed("Path contains a NUL byte")
    candidate = rel_path.replace("\\", "/")
    if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        raise PathNotAllowed("Absolute paths are not allowed; use a path relative to the repository root")
    if candidate.startswith("~"):
        raise PathNotAllowed("Home-relative paths are not allowed")
    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PathNotAllowed("Parent directory segments ('..') are not allowed")
        parts.append(part)
    return "/".join(parts) if parts else "."


def resolve_inside(
    root: Path,
    rel_path: str,
    *,
    allow_git_dir: bool = False,
    allow_secret: bool = False,
) -> Path:
    """Return the real path for ``rel_path`` under ``root`` or raise ``PathNotAllowed``.

    Symlinks are resolved before the containment check, so a link pointing outside the
    workspace is rejected even though its own location is inside.
    """
    normalized = normalize_relative(rel_path)
    real_root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(real_root / normalized))
    if target != real_root and real_root not in target.parents:
        raise PathNotAllowed("Path resolves outside the repository", reason="path_escape")
    rel_real = target.relative_to(real_root).as_posix() if target != real_root else "."
    for checked in {normalized, rel_real}:
        parts = PurePosixPath(checked).parts
        if not allow_git_dir and ".git" in parts:
            raise PathNotAllowed("The .git directory is not accessible", reason="git_internal")
        if not allow_secret and is_secret_path(checked):
            raise PathNotAllowed("This file is treated as a secret and cannot be accessed", reason="secret_file")
    return target


def is_within(root: Path, candidate: Path) -> bool:
    real_root = Path(os.path.realpath(root))
    real = Path(os.path.realpath(candidate))
    return real == real_root or real_root in real.parents


def looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    text_chars = bytes({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x7F)) | set(range(0x80, 0x100)))
    nontext = sample.translate(None, text_chars)
    return len(nontext) / len(sample) > 0.30
