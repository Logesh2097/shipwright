# API documentation

Base path: `/api/v1`. JSON everywhere. Interactive OpenAPI docs are served at
`/api/docs` and the schema at `/api/openapi.json`.

Authentication: `Authorization: Bearer <access token>` on everything except
`/health/*`, `/auth/register`, `/auth/login` and `/auth/refresh`.

## Errors

```json
{ "error": { "code": "not_found", "message": "Repository not found", "details": {}, "request_id": "..." } }
```

| Status | Codes |
|---|---|
| 400 | `bad_request`, `invalid_repository`, `path_not_allowed` |
| 401 | `unauthenticated` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict`, `duplicate_task`, `stale_change_set`, `approval_already_decided` |
| 422 | `validation_error` |
| 429 | `rate_limited` |
| 502 | `upstream_error` (GitHub, LLM) |
| 503 | `service_unavailable`, `llm_not_configured`, `mcp_unavailable` |

List endpoints accept `limit` (default 50, max 200) and `offset`, and return
`{ "items": [...], "total": n }`.

## Health

| Method | Path | Description |
|---|---|---|
| GET | `/health/live` | Process is up |
| GET | `/health/ready` | Database reachable and migrated; reports LLM, sandbox and MCP status |

## Auth and users

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/auth/register` | public | Create an account. While no administrator exists, the account becomes the administrator |
| POST | `/auth/login` | public | Returns access and refresh tokens |
| POST | `/auth/refresh` | public | Exchange a refresh token |
| GET | `/auth/me` | any | Current user |
| GET | `/users` | admin | List users |
| PATCH | `/users/{id}` | admin | Change role or active flag |

## Projects

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/projects` | member | Projects visible to the caller |
| POST | `/projects` | developer | Create (caller becomes owner) |
| GET | `/projects/{id}` | member | Detail with the caller's role |
| PATCH | `/projects/{id}` | owner | Name, description, settings |
| DELETE | `/projects/{id}` | owner | Delete project and its records (never repository files) |
| GET | `/projects/{id}/members` | member | List members |
| PUT | `/projects/{id}/members` | owner | Add or change a member by email |
| DELETE | `/projects/{id}/members/{user_id}` | owner | Remove a member |

## Repositories

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/projects/{id}/repositories` | member | List |
| POST | `/projects/{id}/repositories` | owner | Register a local Git repository and start indexing |
| GET | `/repositories/{id}` | member | Detail and profile |
| DELETE | `/repositories/{id}` | owner | Unregister (files untouched) |
| POST | `/repositories/{id}/index` | developer | Re-index |
| GET | `/repositories/{id}/tree?path=` | member | Directory listing |
| GET | `/repositories/{id}/file?path=&start_line=&max_lines=` | member | File content (redacted) |
| GET | `/repositories/{id}/files?q=` | member | Indexed files |
| GET | `/repositories/{id}/symbols?q=&kind=` | member | Symbol search |
| GET | `/repositories/{id}/search?q=&regex=` | member | Text search |
| GET | `/repositories/{id}/relevant-files?q=` | member | Ranked files for a query |
| GET | `/repositories/{id}/routes` | member | Discovered HTTP routes |
| GET | `/repositories/{id}/context` | member | Generated repository overview |
| GET | `/repositories/{id}/unused-code` | member | Unused imports and functions |
| GET | `/repositories/{id}/git/status` | member | Working tree status |
| GET | `/repositories/{id}/git/diff?path=&staged=` | member | Diff |
| GET | `/repositories/{id}/git/log` | member | Recent commits |
| GET | `/repositories/{id}/git/branches` | member | Branches |

## Tasks and agent runs

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/tasks` | developer | Submit a request; starts an agent run. 409 `duplicate_task` if an identical task is active |
| GET | `/tasks?project_id=&repository_id=&status=` | member | List |
| GET | `/tasks/{id}` | member | Detail with latest run |
| POST | `/tasks/{id}/cancel` | developer | Cancel |
| POST | `/tasks/{id}/resume` | developer | Resume an interrupted or failed run from its checkpoint |
| GET | `/tasks/{id}/runs` | member | Runs of a task |
| GET | `/runs/{id}` | member | Run detail: classification, analysis, plan, report, usage |
| GET | `/runs/{id}/events?after=` | member | Events after a cursor |
| GET | `/runs/{id}/stream?after=` | member | Server-sent events |

## Change sets and approvals

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/change-sets?task_id=&repository_id=&status=` | member | List |
| GET | `/change-sets/{id}` | member | Files and diffs |
| GET | `/change-sets/{id}/staleness` | member | Files that changed since the proposal was made |
| POST | `/change-sets/{id}/revert` | developer | Revert applied changes where the file is still as the agent left it |
| GET | `/approvals?status=&repository_id=&project_id=` | member | List |
| GET | `/approvals/{id}` | member | Detail with payload |
| POST | `/approvals/{id}/approve` | developer | Body: `comment`, `confirm_deletions` |
| POST | `/approvals/{id}/reject` | developer | Body: `comment` |

## Tests and verification

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/repositories/{id}/tests/discover` | member | Frameworks, test files, scripts |
| POST | `/repositories/{id}/test-runs?directory=` | developer | Body: `kind`, `tool`, `targets`. Runs synchronously in the sandbox and returns the result |
| GET | `/test-runs?repository_id=&task_id=` | member | List |
| GET | `/test-runs/{id}` | member | Totals and per-test results |

## Git and GitHub write operations

Each of these creates a pending approval and returns it. Nothing runs until it is
approved.

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/repositories/{id}/git/branch` | developer | Body: `name` |
| POST | `/repositories/{id}/git/commit` | developer | Body: `paths`, `message` |
| POST | `/repositories/{id}/git/push` | developer | Body: `branch` (not the default branch) |
| POST | `/repositories/{id}/github/pull-requests` | developer | Body: `head`, `base`, `title`, `body`, `draft` |
| POST | `/repositories/{id}/commands?directory=` | developer | Body: `argv`. Sensitive command request (install or migration); denied commands are refused with 400 |

## GitHub (read)

| Method | Path | Description |
|---|---|---|
| GET | `/github/status` | Whether a token is stored, its hint and login |
| PUT | `/github/credential` | Store a fine-grained token (validated against the API) |
| DELETE | `/github/credential` | Remove it |
| GET | `/github/repositories` | Repositories visible to the token |
| GET | `/github/repositories/{owner}/{repo}` | Metadata |
| GET | `/github/repositories/{owner}/{repo}/issues` | Issues |
| GET | `/github/repositories/{owner}/{repo}/issues/{number}` | One issue with its body |
| GET | `/github/repositories/{owner}/{repo}/pulls` | Pull requests |
| GET | `/github/repositories/{owner}/{repo}/pulls/{number}` | One pull request with its description |
| GET | `/github/repositories/{owner}/{repo}/branches` | Branches |
| GET | `/github/repositories/{owner}/{repo}/commits` | Commits |

## MCP, history and settings

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/mcp/servers` | member | Live MCP sessions and their state |
| GET | `/mcp/tools` | member | Tool catalogue with schemas and risk levels |
| GET | `/tool-executions?repository_id=&run_id=&status=` | member | Tool call log |
| GET | `/audit-logs?action=&resource_type=` | admin | Audit trail |
| GET | `/dashboard/summary` | any | Counts computed from the database |
| GET | `/settings/system` | any | Sandbox mode, LLM configured or not, policy table, limits |
