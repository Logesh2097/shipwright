"""Command policy for the terminal tools.

Commands are argument vectors (never shell strings). Each is classified as:

* ``safe``      - may run in the sandbox without network, no approval needed
* ``sensitive`` - needs an explicit human approval (dependency installs, migrations)
* ``denied``    - never runs

Anything not recognised is denied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Level = Literal["safe", "sensitive", "denied"]

MAX_ARGS = 64
MAX_ARG_LENGTH = 2000

_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][\w:.-]{0,63}$")
_WRITE_FLAGS = {"--fix", "--fix-only", "--unsafe-fixes", "--write", "-w", "--fix-dry-run"}
_PY_SAFE_MODULES = {"pytest", "unittest", "ruff", "mypy", "compileall", "pyflakes"}
_NPX_SAFE = {"vitest", "jest", "eslint", "tsc", "vue-tsc", "prettier"}
_PYTHONS = {"python", "python3"}


@dataclass(frozen=True)
class CommandDecision:
    level: Level
    reason: str
    category: str = ""  # install_dependencies | run_migration | "" for safe commands
    needs_network: bool = False

    @property
    def allowed_without_approval(self) -> bool:
        return self.level == "safe"


def _deny(reason: str) -> CommandDecision:
    return CommandDecision("denied", reason)


def _safe(reason: str) -> CommandDecision:
    return CommandDecision("safe", reason)


def _sensitive(category: str, reason: str, *, network: bool = False) -> CommandDecision:
    return CommandDecision("sensitive", reason, category=category, needs_network=network)


def classify_command(argv: list[str]) -> CommandDecision:
    if not isinstance(argv, list) or not argv:
        return _deny("empty command")
    if len(argv) > MAX_ARGS:
        return _deny("too many arguments")
    for arg in argv:
        if not isinstance(arg, str):
            return _deny("arguments must be strings")
        if "\x00" in arg or "\n" in arg or "\r" in arg:
            return _deny("control characters in argument")
        if len(arg) > MAX_ARG_LENGTH:
            return _deny("argument too long")

    program, rest = argv[0], argv[1:]
    if "/" in program or "\\" in program:
        return _deny("program must be a bare command name, not a path")

    if program in _PYTHONS:
        return _classify_python(rest)
    if program == "pytest":
        return _classify_pytest(rest)
    if program == "ruff":
        return _classify_ruff(rest)
    if program == "mypy":
        return _safe("type check")
    if program == "pip" or program == "pip3":
        return _classify_pip(rest)
    if program == "uv":
        return _classify_uv(rest)
    if program == "poetry":
        if rest[:1] == ["install"]:
            return _sensitive("install_dependencies", "poetry install", network=True)
        return _deny("only 'poetry install' is supported")
    if program in {"npm", "pnpm", "yarn"}:
        return _classify_node_pm(program, rest)
    if program == "npx":
        return _classify_npx(rest)
    if program == "node":
        if rest in (["--version"], ["-v"]):
            return _safe("version query")
        return _deny("running arbitrary node scripts is not allowed")
    if program in {"vitest", "jest", "eslint", "tsc", "vue-tsc"}:
        return _classify_js_tool(program, rest)
    if program == "alembic":
        return _classify_alembic(rest)
    if program == "flask" and rest[:2] in (["db", "upgrade"], ["db", "downgrade"]):
        return _sensitive("run_migration", "flask database migration")
    return _deny(f"'{program}' is not an allowed command")


def _classify_python(rest: list[str]) -> CommandDecision:
    if rest in (["--version"], ["-V"]):
        return _safe("version query")
    if len(rest) >= 2 and rest[0] == "-m":
        module, margs = rest[1], rest[2:]
        if module == "pytest":
            return _classify_pytest(margs)
        if module == "ruff":
            return _classify_ruff(margs)
        if module in _PY_SAFE_MODULES:
            return _safe(f"python -m {module}")
        if module == "pip":
            return _classify_pip(margs)
        if module == "alembic":
            return _classify_alembic(margs)
        return _deny(f"python -m {module} is not allowed")
    if rest[:2] == ["manage.py", "migrate"]:
        return _sensitive("run_migration", "Django migration")
    if rest[:2] == ["manage.py", "test"]:
        return _safe("Django tests")
    if rest[:1] == ["-c"]:
        return _deny("inline Python (-c) is not allowed")
    return _deny("running arbitrary Python scripts is not allowed")


def _classify_pytest(rest: list[str]) -> CommandDecision:
    path_options = ("--basetemp", "--rootdir", "--confcutdir", "-c", "--junitxml", "--junit-xml", "--override-ini", "-o")
    index = 0
    while index < len(rest):
        arg = rest[index]
        # -p <plugin> imports code by module name; only disabling plugins (-p no:name) is allowed.
        if arg == "-p":
            if index + 1 >= len(rest) or not rest[index + 1].startswith("no:"):
                return _deny("loading pytest plugins by name is not allowed")
            index += 2
            continue
        if arg.startswith("-p") and not arg.startswith("--") and not arg[2:].startswith("no:"):
            return _deny("loading pytest plugins by name is not allowed")
        name, sep, value = arg.partition("=")
        if name in path_options:
            if not sep:
                value = rest[index + 1] if index + 1 < len(rest) else ""
                index += 1
            if name in ("-o", "--override-ini"):
                return _deny("overriding pytest configuration is not allowed")
            if value.startswith(("/", "~")) or ".." in value.split("/"):
                return _deny("pytest path option points outside the repository")
        index += 1
    return _safe("pytest")


def _classify_ruff(rest: list[str]) -> CommandDecision:
    if not rest:
        return _deny("ruff needs a subcommand")
    if any(a in _WRITE_FLAGS for a in rest):
        return _deny("ruff --fix would modify files; propose a change instead")
    if rest[0] == "check":
        return _safe("ruff check")
    if rest[0] == "format":
        if "--check" in rest or "--diff" in rest:
            return _safe("ruff format --check")
        return _deny("ruff format would modify files; use --check")
    if rest[0] in {"--version", "version"}:
        return _safe("version query")
    return _deny(f"ruff {rest[0]} is not allowed")


def _classify_pip(rest: list[str]) -> CommandDecision:
    if not rest:
        return _deny("pip needs a subcommand")
    if rest[0] in {"list", "freeze", "show", "check", "--version"}:
        return _safe(f"pip {rest[0]}")
    if rest[0] == "install":
        for arg in rest[1:]:
            if arg.startswith(("--target", "--prefix", "--root", "-t")):
                return _deny("pip install location overrides are not allowed")
        return _sensitive("install_dependencies", "pip install", network=True)
    return _deny(f"pip {rest[0]} is not allowed")


def _classify_uv(rest: list[str]) -> CommandDecision:
    if rest[:1] == ["sync"] or rest[:2] == ["pip", "install"]:
        return _sensitive("install_dependencies", "uv dependency installation", network=True)
    if rest[:2] in (["pip", "list"], ["pip", "freeze"]) or rest == ["--version"]:
        return _safe("uv query")
    if rest[:2] == ["run", "pytest"]:
        return _classify_pytest(rest[2:])
    return _deny("this uv subcommand is not allowed")


def _classify_node_pm(program: str, rest: list[str]) -> CommandDecision:
    if not rest:
        return _deny(f"{program} needs a subcommand")
    sub = rest[0]
    if sub in {"--version", "-v"}:
        return _safe("version query")
    if sub in {"ls", "list", "outdated"}:
        return _safe(f"{program} {sub}")
    if sub in {"test", "t"}:
        return _safe(f"{program} test")
    if sub in {"run", "run-script"}:
        if len(rest) < 2 or not _SCRIPT_NAME_RE.match(rest[1]):
            return _deny("invalid script name")
        if any(a in _WRITE_FLAGS for a in rest[2:]):
            return _deny("write flags are not allowed; propose a change instead")
        return _safe(f"{program} run {rest[1]}")
    if sub in {"install", "i", "ci", "add"}:
        if sub == "add" or (sub in {"install", "i"} and any(not a.startswith("-") for a in rest[1:])):
            return _sensitive("install_dependencies", f"{program} adds packages", network=True)
        return _sensitive("install_dependencies", f"{program} {sub}", network=True)
    return _deny(f"{program} {sub} is not allowed")


def _classify_npx(rest: list[str]) -> CommandDecision:
    args = [a for a in rest if a not in {"--no-install", "--no"}]
    if not args:
        return _deny("npx needs a tool")
    if len(args) == len(rest):
        return _deny("npx must be called with --no-install so nothing is downloaded")
    if args[0] == "prisma" and args[1:3] in (["migrate", "deploy"], ["migrate", "dev"]):
        return _sensitive("run_migration", "Prisma migration")
    if args[0] not in _NPX_SAFE:
        return _deny(f"npx {args[0]} is not allowed")
    return _classify_js_tool(args[0], args[1:])


def _classify_js_tool(tool: str, rest: list[str]) -> CommandDecision:
    if any(a in _WRITE_FLAGS for a in rest):
        return _deny(f"{tool} write flags are not allowed; propose a change instead")
    if tool == "prettier" and "--check" not in rest:
        return _deny("prettier is only allowed with --check")
    if tool == "vitest" and not any(a in {"run", "--run", "list"} for a in rest):
        return _deny("vitest must be run with 'run' (watch mode never exits)")
    return _safe(tool)


def _classify_alembic(rest: list[str]) -> CommandDecision:
    if rest[:1] in (["upgrade"], ["downgrade"], ["stamp"]):
        return _sensitive("run_migration", f"alembic {rest[0]}")
    if rest[:1] in (["current"], ["history"], ["heads"], ["check"], ["branches"]):
        # These connect to the database configured by the repository; keep them gated.
        return _sensitive("run_migration", f"alembic {rest[0]} (connects to the database)")
    return _deny("this alembic subcommand is not allowed")
