# MCP servers and tools

Built with the official MCP Python SDK 2.x (`mcp.server.mcpserver.MCPServer`,
`mcp.Client`). One server program, `python -m app.mcp_server`, exposes tool groups
selected by `--toolsets`.

| Session kind | Toolsets | Spawned | Environment |
|---|---|---|---|
| `workspace` | `fs`, `git`, `terminal`, `testing` | one per repository, on first use | `ASEA_WORKSPACE_ROOT`, sandbox settings, approval-token key |
| `github` | `github` | one per user and repository pair when needed | the user's decrypted GitHub token, optional workspace root (for push), approval-token key |

The backend connects over stdio. The SDK gives the child a scrubbed environment, so
backend secrets such as the OpenAI key or database URL are not inherited. The workspace
root is fixed when the process starts; no tool accepts a root or an absolute path.

For external MCP clients the same server can run with
`--transport streamable-http --host 127.0.0.1 --port 8765`, protected by a bearer token
(`ASEA_MCP_HTTP_TOKEN`). Write tools still require approval tokens, which only the
backend can mint, so an external client is effectively read-and-execute only.

## Risk levels and policy

Each tool declares a risk level in its MCP `_meta` (`asea/risk`). The Tool Gateway is
the authority; the server enforces the same rule independently.

| Risk | Meaning | Gateway behaviour |
|---|---|---|
| `read` | Reads repository or GitHub data | Allowed for project members |
| `execute` | Runs an allow-listed command in the sandbox without network | Allowed for developers; needs approval when the project disables `auto_run_tests` |
| `write` | Modifies files in the workspace | Requires an approved approval record; gateway mints a token |
| `sensitive` | Deletes files, installs dependencies, runs migrations, Git writes, push, PR | Same as `write`, and never reachable from an LLM tool loop |

### Approval tokens

For `write` and `sensitive` calls the gateway attaches
`_meta["asea/approval-token"]`: an HMAC-SHA256 over the tool name, a digest of the exact
arguments, the workspace root, the approval id and an expiry (default 120 s). The server
recomputes the digest from the arguments it actually received and refuses the call on
any mismatch. The model never sees a token and cannot produce one.

## Tools

All paths are relative to the workspace root. All outputs are JSON text and are
size-limited. Secret-looking values are redacted by the server and again by the gateway.

### Filesystem (`fs`)

| Tool | Risk | Arguments | Result |
|---|---|---|---|
| `fs_list_directory` | read | `path="."`, `max_entries=200` | entries with type and size; ignored and secret files omitted |
| `fs_read_file` | read | `path`, `start_line=1`, `max_lines=400` | numbered lines, total line count, sha256 |
| `fs_find_files` | read | `pattern` (glob), `max_results=200` | matching paths |
| `fs_search_text` | read | `query`, `regex=false`, `path_glob=None`, `max_results=100` | path, line number, line text |
| `fs_file_info` | read | `path` | size, sha256, line count, binary flag |
| `fs_write_file` | write | `path`, `content`, `expected_sha256` (hash, or `"absent"` for a new file) | new sha256; fails with `stale` if the file differs from `expected_sha256` |
| `fs_delete_file` | sensitive | `path`, `expected_sha256` | fails with `stale` on mismatch |

Refused in every case: paths that escape the root after symlink resolution, `.git/`
internals, secret files (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc`,
`credentials*`, `*.p12`, `*.pfx`, `.netrc`), binary files for text tools, and files over
the size limit.

### Git (`git`)

Git runs with hooks, fsmonitor, external diff, textconv, pagers and prompts disabled so
a hostile repository configuration cannot execute code.

| Tool | Risk | Arguments |
|---|---|---|
| `git_status` | read | none |
| `git_diff` | read | `path=None`, `staged=false`, `ref=None` |
| `git_log` | read | `max_count=20`, `path=None` |
| `git_show` | read | `ref`, `path=None` |
| `git_branches` | read | none |
| `git_create_branch` | sensitive | `name`, `checkout=true` |
| `git_commit` | sensitive | `paths` (explicit list), `message` |

`git_commit` stages only the listed paths. There is no tool for reset, clean, checkout
of files, rebase, merge, force push, or config changes.

### Terminal (`terminal`)

| Tool | Risk | Arguments |
|---|---|---|
| `terminal_run` | execute | `argv` (list, no shell), `timeout_seconds=120`, `directory="."` |
| `terminal_run_sensitive` | sensitive | `argv`, `timeout_seconds`, `directory` (all required, because the approval token covers the exact arguments) |

`argv` is matched against the command policy (`app/security/commands.py`): an allowlist
of programs and subcommands, with a separate list that is only valid for
`terminal_run_sensitive`. Everything else is denied. The category of a sensitive command
(`install_dependencies`, `run_migration`) is derived from the command itself, not
supplied by the caller. Commands run in the sandbox; only dependency installation gets
network access and a writable mount.

### Testing (`testing`)

| Tool | Risk | Arguments | Result |
|---|---|---|---|
| `test_discover` | read | none | frameworks found, test files, available npm scripts |
| `test_run` | execute | `framework` (`pytest`, `vitest`, `jest`, `npm`), `targets=[]`, `kind` (`unit`, `integration`), `directory="."`, `timeout_seconds=600` | status, counters, per-test outcomes, output excerpt |
| `lint_run` | execute | `tool` (`ruff`, `eslint`), `targets=[]`, `directory="."` | status, issue list |
| `typecheck_run` | execute | `tool` (`mypy`, `vue-tsc`, `tsc`), `directory="."` | status, issue list |
| `build_run` | execute | `tool` (`npm`), `directory="."` | status, output excerpt |

The server builds the command line itself from these enumerated arguments; the caller
cannot inject flags. Results use five outcomes: `passed`, `failed`, `skipped`, `error`,
`not_executed`. A requested target that does not exist, a run that timed out, and a tool
whose dependencies are not installed (no `node_modules`) are all reported as
`not_executed`, never as passed.

`build_run` is the one execute-level tool that writes to the working tree (build output
such as `dist/`). The agent's verification step does not call it; it runs only when a
developer starts a build from the Tests screen.

### GitHub (`github`)

| Tool | Risk | Arguments |
|---|---|---|
| `github_get_authenticated_user` | read | none |
| `github_list_repositories` | read | `page=1`, `per_page=30` |
| `github_get_repository` | read | `full_name` |
| `github_list_issues` | read | `full_name`, `state="open"`, `page=1`, `per_page=30` |
| `github_get_issue` | read | `full_name`, `number` |
| `github_list_pull_requests` | read | `full_name`, `state="open"`, `page=1` |
| `github_get_pull_request` | read | `full_name`, `number` |
| `github_list_branches` | read | `full_name` |
| `github_list_commits` | read | `full_name`, `branch=None`, `per_page=20` |
| `github_push_branch` | sensitive | `branch` |
| `github_create_pull_request` | sensitive | `full_name`, `head`, `base`, `title`, `body`, `draft=false` |

`github_push_branch` refuses the repository's default branch and never forces. There is
no merge tool. Rate-limit responses are returned as a structured `rate_limited` error
with the reset time.

`github_push_branch` reads the default branch from GitHub's API before pushing and
refuses if that lookup fails. The token is given to Git through per-process environment
configuration (`http.extraheader`); it is not written to `.git/config`, not placed in
the URL, and removed from any output.

## Internal (non-MCP) tools

These are served by the backend through the same gateway (server name `internal`) and
are logged the same way. They read the PostgreSQL index or write proposal rows only.

| Tool | Risk | Purpose |
|---|---|---|
| `code_repo_overview` | read | languages, frameworks, entry points, top-level layout |
| `code_search_symbols` | read | functions, classes, methods, routes, components by name |
| `code_relevant_files` | read | ranked files for a free-text query |
| `code_list_routes` | read | discovered HTTP routes |
| `code_find_unused` | read | unused imports and unreferenced top-level functions (Python) |
| `propose_edit` | read | exact-match replacement in a file, recorded as a proposal |
| `propose_create_file` | read | new file proposal |
| `propose_delete_file` | read | deletion proposal (flagged for explicit confirmation) |

The `propose_*` tools are `read` risk because they change nothing in the repository.

## Client behaviour

- **Discovery**: on connect the manager lists tools and caches schemas and risk levels.
- **Validation**: arguments are validated against the tool's JSON schema by the server;
  the gateway additionally rejects unknown tools and tools outside the caller's allowed
  set.
- **Timeouts**: every call has a read timeout (default 60 s; longer for test and
  terminal tools).
- **Disconnection**: a dead session is discarded and restarted once. Read-only calls are
  retried automatically; write calls are not, and report `mcp_unavailable`.
- **Logging**: every call, including denied ones, becomes a `tool_executions` row.
