# Testing

## How to run everything

```bash
# Database for tests (uses the asea_test database in the same container)
docker compose up -d db

# Backend
cd backend
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app
.venv/bin/python -m pytest              # add "-m 'not docker'" to skip the Docker sandbox test

# Frontend (Node 22.12+ or 24)
cd frontend
npm run lint && npm run typecheck && npm test && npm run build
```

The backend suite creates `asea_test` if it is missing, rebuilds its schema with
`alembic upgrade head`, and truncates all tables before each test. It refuses to run
against a database whose name does not end in `_test`.

## What the backend tests are

They are integration tests by default: the real FastAPI app (in-process ASGI), a real
PostgreSQL database migrated by Alembic, the real LangGraph workflow, the real Tool
Gateway and real MCP servers. Two things are substituted:

| Substituted | With | Why |
|---|---|---|
| The LLM | `ScriptedChatModel`: a function that returns canned assistant messages and tool calls | Decisions must be deterministic and tests must not spend quota |
| GitHub's REST API | `httpx.MockTransport`; pushes go to a local bare repository | No token was available |

MCP servers run in-process (`Client(server)`) for speed; one test uses the real stdio
transport with child processes. The sandbox runs in local mode in most tests; one test
exercises the Docker sandbox and is skipped when Docker or the image is unavailable.

| File | Covers |
|---|---|
| `tests/test_security_units.py` | Path confinement, symlink escapes, secret-file patterns, redaction, command policy (safe, sensitive, denied), approval tokens, injection heuristics, unsafe Git configuration, password hashing, credential encryption, production secret checks |
| `tests/test_api_core.py` | Health, error envelope, registration and first-admin rule, login, refresh, token types, login throttling, user administration, project CRUD, membership roles, cross-user isolation, audit log, dashboard |
| `tests/test_repositories.py` | Registration validation (allowed roots, symlinks, non-repositories, unsafe Git config), profile detection, explorer endpoints, secret-file denial, symbols, routes, relevance ranking, context, unused code, incremental re-index, isolation, unregistering |
| `tests/test_mcp.py` | Policy table equals server-declared risk levels; filesystem tools and confinement; approval-token enforcement; stale-write refusal; Git tools; hooks not executed; terminal and testing tools; timeouts; Docker isolation (no network, read-only, non-root); stdio transport, environment isolation and restart after a kill; gateway authorisation matrix, logging and audit |
| `tests/test_agent_flow.py` | Explain flow; bug-fix flow with approval, apply and verification; rejection; stale proposals; repair loop with second approval; revert and revert conflicts; deletion confirmation; a hostile model; duplicate tasks; cancel; LLM failure and resume from checkpoint; LLM not configured; recovery of orphaned runs; roles on tasks and approvals; SSE stream |
| `tests/test_operations.py` | User-triggered checks and their five outcomes; sensitive command requests; branch and commit through approvals; GitHub credential storage; GitHub read endpoints; rate limits and API errors; push and pull request through approvals; default-branch refusal; role checks |
| `tests/test_evaluation.py` | Dataset integrity; harness measurements and clean-up; behaviour without auto-approve |

## What the frontend tests are

Vitest with happy-dom (`src/**/*.test.ts`): the unified-diff parser, the Markdown
renderer (including that hostile input cannot produce scripts, event handlers or
`javascript:` links), the SSE parser, formatting helpers, the API client (error envelope,
single token refresh, sign-out on refresh failure), and components (`DiffViewer`,
`ApprovalPanel` including deletion confirmation and read-only users, `AsyncBlock`,
`RunTimeline`, `TestCounts`, `StatusBadge`, `MarkdownView`).

## Manual and browser verification performed

These were run during development and are not part of the automated suites:

| Check | Result |
|---|---|
| Browser walk-through (headless Chrome, 33 checks) against the real backend with a scripted model, PostgreSQL checkpointer and Docker sandbox: sign-in guard, registration, project and repository management, code analysis, explain task, bug-fix task with diff review and approval, duplicate-task rejection, test runs, hostile-content task, MCP activity, commit through approval, audit log, settings, phone-width layout, sign-out | all passed |
| Docker Compose stack (`db`, `backend`, `frontend`): health through nginx, repository registered at `/repos/...`, sandboxed pytest started from inside the backend container (host-path mapping), task without an LLM key fails with `llm_not_configured` | passed |
| MCP server in streamable HTTP mode: 401 without or with a wrong bearer token; tools listed and callable with the right one; write refused without approval | passed |
| `alembic check` after `upgrade head` | no drift |
| Browser walk-through repeated after the visual redesign (34 checks, adding dark mode, the folded rail and the phone navigation), against a throwaway backend and database with a scripted model | all passed |

## Evaluation

`backend/app/evaluation` contains a dataset of nine cases against a generated sample
repository (`fixtures.py`) and a harness that runs each case through the real workflow.

```bash
cd backend
# Real model: needs OPENAI_API_KEY and LLM_MODEL
.venv/bin/python -m app.evaluation --auto-approve --output ../eval-results/report.json
# Harness self-check with canned model decisions (four cases)
.venv/bin/python -m app.evaluation --auto-approve --scripted
```

Metrics reported: task completion rate, task success rate, classification accuracy,
repository understanding accuracy (expected terms present in the report), correct file
identification (recall of expected files among files read, ranked or planned), tool
selection accuracy, code correctness (an independent pytest run on the final tree), test
pass rate, cases with unnecessary modifications, unauthorized action attempts (denied
tool calls), security events, token usage and mean execution time.

`--auto-approve` lets the harness approve proposals on the throwaway repositories it
creates, under a dedicated evaluation account. Without it, proposals are rejected and the
change-requiring cases fail by design.

**Status of the numbers.** One full run with a real model has been made
(2026-11-13, `gemini-3.5-flash-lite` through Google's native API, Docker sandbox,
auto-approve, paced at 12 requests per minute; report in
`eval-results/gemini-3.5-flash-lite-run2.json`). It is a single run of nine cases on one
small sample repository, so treat it as a first reading, not a benchmark.

| Metric | Result |
|---|---|
| Task completion rate | 8 of 9 (0.889) |
| Task success rate | 7 of 9 (0.778) |
| Task classification accuracy | 1.0 |
| Repository understanding accuracy | 0.944 |
| Correct file identification | 1.0 |
| Tool selection accuracy | 1.0 |
| Code correctness (independent pytest on the final tree) | 3 of 4 verifiable cases (0.75) |
| Test pass rate in agent verification | 0.857 |
| Cases with unnecessary modifications | 2 |
| Unauthorized action attempts (denied tool calls) | 0 |
| Security events | 1 (the injected README text was flagged; the model did not act on it) |
| Tokens | 607,997 in / 17,783 out over 161 model calls |
| Mean execution time per case | 136 s (includes rate-limit pacing) |

Passed: both explanations, the discount bug fix, the failing-test investigation, unused
code, test generation, and the prompt-injection case. Failed:

- `add-customer-endpoint`: the model also edited `app/pricing.py`, which the task did not
  call for; verification ended in a collection error; a repair was approved; then the
  provider returned 503 and the run failed (it could be resumed from its checkpoint).
- `refactor-order-total`: completed, but the model also changed `tests/test_pricing.py`
  and verification ended in a collection error.

Both failures are model behaviour (changes wider than requested, code that does not
import cleanly), which the approval step exists to catch. An earlier run without pacing
completed two cases and lost seven to the free-tier limit of 15 requests per minute.

A second full run the same day (report in `eval-results/report.json`) completed 9 of 9
cases and again succeeded on 7 of 9. The same two cases failed, this time only because
the model changed a file outside the task's scope (`app/pricing.py` and
`tests/test_pricing.py` respectively); verification passed in both. Classification
accuracy was 0.889 (the failing-test investigation was classified as a bug fix), code
correctness 4 of 4 verifiable cases, 577,776 input tokens over 153 calls, mean 83 s per
case. The two runs differ, so results vary from run to run.

The scripted self-check (4 of 4 cases; 6 denied calls from the deliberately hostile
script) validates the harness and the workflow only.

## Security checks by phase

| Phase | Check | Where |
|---|---|---|
| 2 | Cross-project data is invisible (404, not 403); viewers cannot write | `test_api_core`, `test_repositories`, `test_agent_flow`, `test_operations` |
| 3 | Paths outside allowed roots, symlink tricks and unsafe Git configs are refused at registration | `test_repositories` |
| 4 | Path traversal, symlink escape, `.git`, secret files, binary files; write tools without, with forged, mismatched or expired tokens; denied commands; hooks not run; child process environment has no backend secrets | `test_mcp` |
| 5 | A model that tries to read secrets, escape the root, write, delete, run `rm`, commit and push is denied every time and the repository is byte-for-byte unchanged | `test_agent_flow::test_hostile_model_cannot_act` |
| 6 | Stale proposals never overwrite; revert never clobbers later edits; deletions need confirmation; decisions are single-use | `test_agent_flow` |
| 7 | Not-executed checks are never reported as passed; timeouts are enforced | `test_mcp`, `test_operations` |
| 8 | Tokens are encrypted at rest and absent from responses, logs, audit entries and `.git/config`; default branch cannot be pushed; no merge endpoint exists | `test_operations` |
| 9 | Duplicate tasks, interrupted runs, provider failures, MCP process death, rate limits | `test_agent_flow`, `test_mcp`, `test_operations` |

## Known limitations

- **Limited real-model evidence.** The agent has been run against one model
  (`gemini-3.5-flash-lite`) on nine cases, once. The OpenAI provider path has not been
  exercised live. Prompts have not been tuned. A small, fast model makes changes wider
  than requested in some cases.
- **Token use is high** (about 68,000 input tokens per case on average) because each
  step of a tool loop resends the conversation.
- **Free-tier rate limits** make runs slow; pacing and waiting are built in, but a daily
  quota can still stop a run, which then has to be resumed later.
- **No live GitHub call.** All GitHub behaviour is tested against a mock of the REST API
  and a local bare remote. Only fine-grained tokens are supported; GitHub Apps are not
  implemented.
- **Symbol extraction for JavaScript, TypeScript and Vue is regex based.** Unusual
  formatting, nested declarations and re-exports can be missed. Text search still finds
  them. Other languages are indexed as files only.
- **Unused-code detection** is reliable for Python imports. Unreferenced definitions are
  name based and cannot see dynamic use; JS/TS covers imports only.
- **Vitest, Jest, ESLint, tsc and vue-tsc result parsing** is covered by parser code but
  has not been run against a repository with installed Node dependencies. Without
  `node_modules` these checks correctly report `not_executed`.
- **Local sandbox mode** limits time, output and environment but not network or
  filesystem access. It is a development fallback.
- **The Docker socket mount** in the Compose deployment gives the backend container
  control of the host's Docker daemon.
- **Dependency installation in the Docker sandbox** runs with network access and a
  writable mount once approved; install scripts of the packages are not otherwise
  restricted.
- **Builds (`npm run build`)** write their output into the working tree.
- **Agent runs are in-process tasks**, so the backend must run as a single worker.
  A restart interrupts running tasks; they can be resumed from their last checkpoint.
- **Login throttling is in-memory** and per process.
- **Prompt-injection detection is heuristic** and only raises a notice. Safety relies on
  the permission model, not on detection.
- **Access tokens are stateless** and cannot be revoked before they expire (30 minutes
  by default); disabling a user takes effect immediately because the account is checked
  on every request.
