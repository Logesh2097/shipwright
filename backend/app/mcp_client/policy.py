"""Tool catalogue and permission policy. The backend is the authority; the MCP server
declares the same risk levels and enforces approval tokens independently."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Risk = Literal["read", "execute", "write", "sensitive"]
Server = Literal["workspace", "github", "internal"]

RISK_DESCRIPTIONS: dict[str, str] = {
    "read": "Reads repository or GitHub data. Allowed for every project member.",
    "execute": "Runs an allow-listed command in the sandbox without network. Allowed for developers; "
    "needs approval when the project disables automatic test runs.",
    "write": "Modifies files in the repository. Requires an approved request; never callable by the model.",
    "sensitive": "Deletes files, installs dependencies, runs migrations, writes to Git or GitHub. "
    "Requires an approved request; never callable by the model.",
}
MIN_ROLE_FOR_RISK: dict[str, str] = {"read": "viewer", "execute": "developer", "write": "developer", "sensitive": "developer"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    server: Server
    risk: Risk
    long_running: bool = False


def _specs(server: Server, risk: Risk, *names: str, long_running: bool = False) -> list[ToolSpec]:
    return [ToolSpec(n, server, risk, long_running) for n in names]


_ALL: list[ToolSpec] = [
    *_specs("workspace", "read", "fs_list_directory", "fs_read_file", "fs_find_files", "fs_search_text", "fs_file_info"),
    *_specs("workspace", "write", "fs_write_file"),
    *_specs("workspace", "sensitive", "fs_delete_file"),
    *_specs("workspace", "read", "git_status", "git_diff", "git_log", "git_show", "git_branches"),
    *_specs("workspace", "sensitive", "git_create_branch", "git_commit"),
    *_specs("workspace", "execute", "terminal_run", long_running=True),
    *_specs("workspace", "sensitive", "terminal_run_sensitive", long_running=True),
    *_specs("workspace", "read", "test_discover"),
    *_specs("workspace", "execute", "test_run", "lint_run", "typecheck_run", "build_run", long_running=True),
    *_specs(
        "github", "read",
        "github_get_authenticated_user", "github_list_repositories", "github_get_repository", "github_list_issues",
        "github_get_issue", "github_list_pull_requests", "github_get_pull_request", "github_list_branches",
        "github_list_commits",
    ),
    *_specs("github", "sensitive", "github_push_branch", "github_create_pull_request", long_running=True),
    *_specs(
        "internal", "read",
        "code_repo_overview", "code_search_symbols", "code_relevant_files", "code_list_routes", "code_find_unused",
        "propose_edit", "propose_create_file", "propose_delete_file",
    ),
]  # fmt: skip

TOOL_CATALOG: dict[str, ToolSpec] = {spec.name: spec for spec in _ALL}

# Which tools an approved request of a given type authorises.
APPROVAL_TOOLS: dict[str, frozenset[str]] = {
    "apply_changes": frozenset({"fs_write_file", "fs_delete_file"}),
    "revert_changes": frozenset({"fs_write_file", "fs_delete_file"}),
    "install_dependencies": frozenset({"terminal_run_sensitive"}),
    "run_migration": frozenset({"terminal_run_sensitive"}),
    "sensitive_command": frozenset({"terminal_run_sensitive"}),
    "run_tests": frozenset({"test_run", "lint_run", "typecheck_run", "build_run", "terminal_run"}),
    "git_create_branch": frozenset({"git_create_branch"}),
    "git_commit": frozenset({"git_commit"}),
    "git_push": frozenset({"github_push_branch"}),
    "github_create_pr": frozenset({"github_create_pull_request"}),
}

APPROVAL_ACTION_LABELS: dict[str, str] = {
    "apply_changes": "Apply code changes",
    "revert_changes": "Revert applied changes",
    "install_dependencies": "Install dependencies",
    "run_migration": "Run a database migration",
    "sensitive_command": "Run a sensitive command",
    "run_tests": "Run tests and checks",
    "git_create_branch": "Create a Git branch",
    "git_commit": "Commit files",
    "git_push": "Push a branch to GitHub",
    "github_create_pr": "Open a pull request",
}

READ_TOOLS = frozenset(s.name for s in _ALL if s.risk == "read" and s.server == "workspace")
CODE_INTEL_TOOLS = frozenset({"code_repo_overview", "code_search_symbols", "code_relevant_files", "code_list_routes", "code_find_unused"})
PROPOSE_TOOLS = frozenset({"propose_edit", "propose_create_file", "propose_delete_file"})
VERIFY_TOOLS = frozenset({"test_discover", "test_run", "lint_run", "typecheck_run"})


def policy_table() -> list[dict[str, str]]:
    return [
        {"tool": s.name, "server": s.server, "risk": s.risk, "requires_approval": str(s.risk in ("write", "sensitive")).lower()}
        for s in _ALL
    ]
