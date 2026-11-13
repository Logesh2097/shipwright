"""Hardened invocation of the git CLI against untrusted repositories.

A repository's own configuration can make ordinary read commands execute programs
(fsmonitor, external diff, textconv, clean/smudge filters, pagers, ssh commands). Every
call here neutralises those, ignores user and system configuration, and never prompts.
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_BASE_CONFIG = [
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "core.sshCommand=false",
    "core.askPass=",
    "core.editor=true",
    "diff.external=",
    "credential.helper=",
    "protocol.ext.allow=never",
    "protocol.file.allow=user",
    "advice.detachedHead=false",
    "gc.auto=0",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
]

_DANGEROUS_SECTION_RE = re.compile(r"^(filter|include|includeif|alias|merge|difftool|mergetool|pager|uploadpack|receive)\b", re.I)
_DANGEROUS_KEYS = {
    ("core", "fsmonitor"), ("core", "sshcommand"), ("core", "gitproxy"), ("core", "hookspath"),
    ("core", "pager"), ("core", "editor"), ("core", "askpass"), ("core", "worktree"),
    ("diff", "external"), ("credential", "helper"), ("gpg", "program"), ("sequence", "editor"),
}  # fmt: skip


class GitError(Exception):
    def __init__(self, message: str, *, returncode: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": tempfile.gettempdir(),
        "LC_ALL": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ASKPASS": "true",
        "SSH_ASKPASS": "true",
        "GIT_SSH_COMMAND": "false",
    }
    if extra:
        env.update(extra)
    return env


def run_git(
    root: Path | str,
    args: list[str],
    *,
    timeout: float = 30.0,
    check: bool = False,
    extra_env: dict[str, str] | None = None,
    input_text: str | None = None,
    max_output: int = 2_000_000,
) -> GitResult:
    root = str(root)
    command = ["git", "-C", root]
    for item in [*_BASE_CONFIG, f"safe.directory={root}"]:
        command += ["-c", item]
    command += args
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            env=_env(extra_env),
            input=input_text.encode() if input_text is not None else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0] if args else ''} timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    stdout = proc.stdout[:max_output].decode("utf-8", errors="replace")
    stderr = proc.stderr[:20_000].decode("utf-8", errors="replace")
    result = GitResult(proc.returncode, stdout, stderr)
    if check and not result.ok:
        raise GitError(stderr.strip() or f"git {args[0]} failed", returncode=proc.returncode, stderr=stderr)
    return result


def inspect_git_config(root: Path | str) -> list[str]:
    """Return settings in the repository's own config that could execute programs."""
    config_path = Path(root) / ".git" / "config"
    if not config_path.is_file():
        return []
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        # git config allows leading whitespace before keys; configparser treats that as continuation.
        text = "\n".join(line.strip() for line in config_path.read_text(errors="replace").splitlines())
        parser.read_string(text)
    except configparser.Error:
        return ["unparseable .git/config"]
    findings: list[str] = []
    for section in parser.sections():
        base = section.split()[0].lower() if section.split() else section.lower()
        if _DANGEROUS_SECTION_RE.match(base):
            findings.append(f"[{section}]")
            continue
        for key in parser.options(section):
            lowered = key.lower()
            if (base, lowered) in _DANGEROUS_KEYS:
                findings.append(f"{base}.{lowered}")
            if base == "diff" and lowered in {"textconv", "command"}:
                findings.append(f"[{section}] {lowered}")
            if base == "url" and lowered in {"insteadof", "pushinsteadof"}:
                findings.append(f"[{section}] {lowered}")
    return sorted(set(findings))


def strip_url_credentials(url: str) -> str:
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]+@", r"\1", url.strip(), flags=re.I)


def parse_github_full_name(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9][A-Za-z0-9._/-]{0,199}(?<![./])(?<!\.lock)$")


def is_valid_branch_name(name: str) -> bool:
    return bool(_BRANCH_RE.match(name)) and name not in {"HEAD"}


_REF_RE = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}$")


def is_safe_ref(ref: str) -> bool:
    """Refs passed to read commands: no leading dash (option injection), no ranges with odd characters."""
    return bool(_REF_RE.match(ref)) and ".." not in ref.replace("...", "").replace("..", "", 1)
