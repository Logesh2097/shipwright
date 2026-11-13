# Progress

Record of what was built, phase by phase, so the work can be resumed or audited.
Last updated: 2026-11-13.

## Summary

| # | Phase | Status |
|---|---|---|
| A/B | Analysis and implementation plan | complete |
| 1 | Foundation | complete |
| 2 | Backend and database | complete |
| 3 | Repository understanding | complete |
| 4 | MCP server and tools | complete |
| 5 | LangGraph agent | complete; run live on Gemini (`gemini-3.5-flash-lite`) |
| 6 | Controlled editing and approval | complete |
| 7 | Testing and verification | complete (Node tool parsers not run against a repository with installed dependencies) |
| 8 | GitHub integration | complete (mocked API and local bare remote only; no live GitHub call; fine-grained tokens, not GitHub Apps) |
| 9 | Security, reliability, evaluation | complete; one real-model evaluation run: 7 of 9 cases succeeded |
| 10 | Frontend, Docker, deployment | complete (Compose verified locally; Google Cloud layout documented, not deployed) |

Final verification state:

| Check | Result |
|---|---|
| Backend `pytest` | 143 passed |
| Backend `ruff check`, `ruff format --check`, `mypy app` | clean (73 source files) |
| Frontend `vitest` | 30 passed |
| Frontend `eslint`, `vue-tsc --noEmit`, `vite build` | clean |
| `alembic check` | no drift |
| Browser walk-through (headless Chrome) | 33 of 33 checks passed |
| Docker Compose stack | healthy; repository registered and sandboxed pytest run from inside the backend container |
| Evaluation harness, scripted self-check | 4 of 4 cases |
| Evaluation with Gemini (`gemini-3.5-flash-lite`), nine cases, one run | 8 completed, 7 succeeded; details in `docs/testing.md` |

**Needs your involvement**

1. The model is configured: `LLM_PROVIDER=gemini`, `gemini-3.5-flash-lite`, with the key
   from the BI project. That key is on Gemini's free tier (15 requests per minute), so
   runs are paced and slow; a paid tier or a stronger model would improve both speed
   and the quality of changes.
2. Store a fine-grained GitHub token under Settings to use the GitHub features against
   the real API; this path has only been tested against mocks.
3. Review `.env` before any shared deployment (it was generated locally with random
   secrets and is not committed).
4. Nothing has been staged or committed. `git status` shows the whole project as
   untracked files in a freshly initialised repository.

## Stage A/B — analysis and plan

- Found an empty project directory (one zero-byte `docs` file, replaced by the `docs/`
  directory) and no Git repository (`git init` was run; nothing staged or committed).
- Host has Python 3.10 and Node 12; current SQLAlchemy, LangChain and Vite need newer
  runtimes, so the backend uses a uv-managed Python 3.12 and the frontend Node 24.
- Verified current package versions. Prototyped the MCP 2.x API (`MCPServer`, `Client`,
  request `_meta`, stdio environment isolation) and LangGraph interrupt/resume before
  fixing the design, because both are new major versions.
- Wrote the architecture, database, API, MCP, agent, security and plan documents.

## Phase 1 — foundation

**Implemented**: project layout; `pyproject.toml` with tool configuration; settings via
pydantic-settings with production secret validation; structlog logging with request ids
and secret redaction; error types and a uniform error envelope; app factory with
lifespan; async SQLAlchemy engine and session handling; Alembic (async); Docker Compose
with PostgreSQL; `.env.example`; frontend scaffold (Vite, Vue 3, TypeScript, Tailwind 4,
ESLint, Vitest).

**Files**: `backend/pyproject.toml`, `backend/app/{main,config,logs,errors,runtime}.py`,
`backend/app/db/session.py`, `backend/alembic/`, `docker-compose.yml`, `.env.example`,
`.gitignore`, `frontend/{package.json,vite.config.ts,tsconfig.json,eslint.config.js}`.

**Dependencies**: see `docs/implementation-plan.md`. TypeScript is pinned to 6.0 because
`typescript-eslint` does not support 7 yet; happy-dom is used instead of jsdom because
jsdom 31 needs a newer Node 24 patch release than the one installed.

## Phase 2 — backend and database

**Implemented**: 17 tables (users, credentials, projects, members, repositories, files,
symbols, tasks, runs, events, change sets, file changes, approvals, tool executions, test
runs, test cases, audit log) in one initial migration; registration, login, refresh;
first-administrator bootstrap; login throttling; user administration; project CRUD and
membership with last-owner and last-admin protection; project-scoped authorisation
helpers used by every route; audit logging; liveness and readiness endpoints.

**Database**: `backend/alembic/versions/92617a692cad_initial_schema.py`. LangGraph's
checkpoint tables are created by the library at start-up and excluded from Alembic.

**API**: `/health/*`, `/auth/*`, `/users`, `/projects`, `/projects/{id}/members`.

**Tests**: `tests/test_api_core.py` (11).

**Fixed along the way**: a service account created by the evaluation harness counted as
the "first user" and would have prevented the first human from becoming administrator;
the rule is now "administrator if none exists".

## Phase 3 — repository exploration and code understanding

**Implemented**: registration checks (allowed roots, symlink resolution, Git top level,
refusal of repositories whose `.git/config` can execute programs); hardened Git runner;
confined workspace access (listing, reading with redaction, search, hashing, atomic
conditional writes); language, framework, dependency, package-manager, test-framework and
quality-tool detection for Python and Node projects including nested packages; symbol
extraction (Python via `ast`, JS/TS/Vue via patterns) including HTTP routes and Vue
components; incremental indexing by content hash; symbol search; relevant-file ranking
from paths, symbols and content; repository overview generation; unused-code analysis.

**Files**: `backend/app/repo_intel/*`, `backend/app/security/{paths,gitsafe,redaction}.py`,
`backend/app/api/routes/repositories.py`.

**Tests**: `tests/test_repositories.py` (10).

**Fixed along the way**: `git grep -z` output was parsed with the wrong separator;
ranking let a test file with many similarly named tests outrank the source file.

## Phase 4 — MCP server and development tools

**Implemented**: one MCP server program (`python -m app.mcp_server`) built on
`mcp.server.mcpserver.MCPServer` with five toolsets and 32 tools (7 filesystem, 7 Git,
2 terminal, 5 testing, 11 GitHub); risk level declared in each tool's `_meta`; stdio
transport per repository and streamable HTTP with bearer-token authentication; approval
tokens (HMAC over tool, arguments, workspace, expiry) carried in request `_meta`; command
policy; Docker and local sandboxes with timeouts and output caps; result parsers (JUnit
XML, Vitest/Jest JSON, Ruff, ESLint, mypy, tsc); MCP session manager (owner task per
session, discovery, idle shutdown, restart, safe retry for read tools); Tool Gateway
(policy, approval check, token minting, redaction, injection scan, logging, audit);
internal code-intelligence and proposal tools behind the same gateway.

**Files**: `backend/app/mcp_server/*`, `backend/app/mcp_client/*`,
`backend/app/sandbox/runner.py`, `backend/app/security/{commands,approval_tokens,injection}.py`,
`docker/sandbox.Dockerfile`, `docker/sandbox-entrypoint.sh`.

**Tests**: `tests/test_mcp.py` (11), `tests/test_security_units.py` (83).

**Fixed along the way**: `pytest -p <plugin>` passed as two arguments slipped through the
command policy; a missing test target made the whole run abort instead of being reported
as not executed; read-only sandbox runs created an empty named volume per repository.

## Phase 5 — LangGraph agent workflow

**Implemented**: OpenAI chat model through `langchain-openai` with timeouts and retries;
structured output via a forced tool call; a scripted chat model for tests; twelve-node
graph (intake, classify, retrieve context, analyse, plan, propose changes, request
approval, await approval, apply, verify, repair, respond); per-node prompts and tool
sets; bounded tool loops with context trimming; run lifecycle (start, pause, resume with
a decision, resume from checkpoint, cancel, orphan recovery, shutdown); durable events;
SSE stream; token accounting; a final report whose factual summary is generated from the
database rather than by the model.

**Files**: `backend/app/agent/*`, `backend/app/api/routes/tasks.py`.

**Tests**: `tests/test_agent_flow.py` (14).

**Fixed along the way**: resuming a run returned before its status changed, so clients
briefly saw the old status; an approval row lock would have blocked tool-execution
inserts that reference it (lock removed before any test hit it).

## Phase 6 — controlled code editing and human approval

**Implemented**: change sets with per-file base hash, original content, new content and
unified diff; exact-match edit, create and delete proposals; staleness check before and
during apply; all-or-nothing application with rollback; revert that refuses to touch
files edited since; approvals with payload hash, expiry, single decision under a row
lock, explicit confirmation for deletions; execution of approved operations; checkpoint
clean-up for finished runs.

**Files**: `backend/app/services/{changes,approvals}.py`, `backend/app/api/routes/review.py`.

**Tests**: in `tests/test_agent_flow.py` and `tests/test_mcp.py`.

## Phase 7 — automated testing and verification

**Implemented**: test discovery; selection of tests related to changed files (changed
tests, name match, import match, fall back to the whole suite); pytest, Vitest, Jest and
npm test runners; Ruff and ESLint; mypy, tsc and vue-tsc; npm build; five-outcome
reporting with `not_executed` for missing targets, timeouts and missing dependencies;
persistence of runs and cases; deterministic verification node; repair loop limited to
`AGENT_MAX_REPAIR_ATTEMPTS`, each attempt needing a new approval.

**Files**: `backend/app/mcp_server/tools/testing.py`, `backend/app/mcp_server/parsers.py`,
`backend/app/services/verification.py`.

**Tests**: in `tests/test_mcp.py`, `tests/test_operations.py`, `tests/test_agent_flow.py`.

**Limitation**: the Node runners and parsers have not been exercised against a
repository with `node_modules` installed.

## Phase 8 — GitHub integration

**Implemented**: fine-grained token storage (validated against the API, Fernet
encrypted, per user, shown only as a hint); repository listing and metadata, issues, pull
requests, branches, commits; rate-limit and error mapping; branch creation, commit, push
and pull-request creation as approval-gated operations; push refused for the default
branch as reported by GitHub; token passed to Git through per-process environment only.

**Files**: `backend/app/mcp_server/{github_client.py,tools/github.py,tools/git.py}`,
`backend/app/api/routes/github.py`.

**Tests**: `tests/test_operations.py` (9).

**Limitation**: no live call to GitHub was made. GitHub Apps are not implemented.

## Phase 9 — security, reliability and evaluation

**Implemented and validated by tests**: role and project isolation on every endpoint;
repository and filesystem confinement; command restrictions; MCP approval tokens and HTTP
bearer authentication; credential encryption; secret redaction in tool output, logs and
audit entries; prompt-injection notices; hardened Git; sandbox isolation and limits;
audit log; provider failure handling and resume; MCP restart; GitHub rate limits;
duplicate-task protection (application check plus a partial unique index); recovery of
interrupted executions.

**Evaluation**: nine-case dataset, harness and metrics in `backend/app/evaluation`;
`tests/test_evaluation.py` (3). Scripted self-check: 4 of 4 cases pass, with 6 denied
tool calls from the hostile script. Real-model run on Gemini: 7 of 9 cases succeeded (see
`docs/testing.md`).

**Added after the initial build (Gemini support)**: `LLM_PROVIDER` setting with a native
Gemini client (`langchain-google-genai`), because Gemini's OpenAI-compatible endpoint
rejects multi-step tool conversations; request pacing (`LLM_REQUESTS_PER_MINUTE`);
waiting out provider rate limits; one retry on an empty model reply inside a tool loop;
API keys stripped from provider error text.

## Phase 10 — frontend, Docker and deployment

**Implemented**: Vue 3 application with twelve screens (dashboard; projects and
repositories; developer assistant; task detail with live progress, analysis, plan and
report; code analysis; change review with diff viewer; approvals; tests and checks; MCP
and tool activity; Git and GitHub; execution history with audit log; settings and
permissions); loading, error, empty and success states throughout; role-aware controls;
light and dark colour schemes; phone-width layout; backend, frontend and sandbox images;
Compose stack with health checks; environment template; deployment documentation.

**Files**: `frontend/src/**`, `frontend/{Dockerfile,nginx.conf}`, `backend/Dockerfile`,
`backend/requirements.lock`, `docs/deployment.md`.

**Tests**: `frontend/src/**/*.test.ts` (30); browser walk-through (33 checks).

**Fixed along the way**: page overflow at phone width on the dashboard; unstyled report
sub-headings; the outcome line said "verification passed" without mentioning a failing
linter.

## Added after the initial build (visual design)

The frontend now wears the design system of the Digitory Reservation portal
(`~/reservation/table_reservation`, `web/portal`). Only styling and the shell's markup
changed: no store, API call, route or behaviour.

| Part | What it is now | Where |
|---|---|---|
| Colour | The portal's orange brand (`#ff5a10`) and the grounds, washes, lines and status tones derived from it; light and a designed dark, chosen in the account menu and remembered | `src/style.css`, `src/lib/colorMode.ts` |
| Type | Inter (self-hosted, copied from the portal); six sizes: 32, 24, 18, 15, 13, 11 | `src/style.css`, `src/assets/fonts` |
| Icons | Material Design Icons (`@mdi/font`), the portal's icon set | `src/main.ts` |
| Shell | Top bar with the brand wash and the project and repository selectors; grouped rail that folds to icons; on a phone a bottom tab bar and the rail as a sheet from the right; account menu with the theme switch and sign-out | `src/components/AppShell.vue` |
| Components | Flat cards with a hairline and a wash head; tables with a wash head row; 44 px controls; tonal notices with an accent bar; status chips; skeleton loading and quiet empty and error states; underline tabs; segmented choices in green | `src/style.css`, `src/components/*` |

**Checked**: lint, type-check, the 30 unit tests and the production build pass; the
browser walk-through (now 34 checks, including dark mode) passes against a throwaway
backend and database with a scripted model.

**Not done**: the portal's Vuetify components were not adopted; the look is reproduced
in CSS on the existing Tailwind setup, so pixel-level differences from the portal remain.
The project's own name in the bar uses an icon as its mark, not a drawn logo.

## Remaining work and ideas

- Tune prompts using the evaluation harness (the two failed Gemini cases made changes
  wider than requested); try a stronger model; reduce token use in tool loops; add more
  evaluation cases and a second sample repository.
- Exercise the Node test, lint and type-check paths against a repository with installed
  dependencies.
- Verify the GitHub integration against the live API; consider GitHub App
  authentication.
- A parser-based symbol extractor for JavaScript and TypeScript.
- A job queue and shared state if more than one backend process is ever needed.
- A sandbox runtime that does not require the Docker socket (for example rootless
  Docker or gVisor) for stricter host isolation.
