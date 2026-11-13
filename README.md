# ASEA — MCP-based autonomous software engineering agent

Register a local Git repository, describe a development task in plain language, and an
agent explores the code, plans, and proposes changes. You review the exact diff; after
you approve, the changes are applied, relevant tests run in a sandbox, and you get a
report. Branches, commits, pushes and pull requests go through the same approval step.

Built with FastAPI, LangGraph, LangChain, the official MCP Python SDK, PostgreSQL and
Vue 3. The model can be OpenAI or Google Gemini (`LLM_PROVIDER`).

## What it does

- **Understands a repository**: languages, frameworks, dependencies, entry points, HTTP
  routes, functions, classes and Vue components are indexed; files relevant to a request
  are ranked; unused imports and unreferenced functions are reported.
- **Runs an agent workflow** (one LangGraph graph): classify the request, retrieve
  context, analyse, plan, propose changes, wait for approval, apply, verify, repair if
  verification fails, report.
- **Acts only through MCP tools**: filesystem, Git, terminal, testing and GitHub tools
  served by an MCP server process confined to one repository.
- **Keeps a human in control**: code changes, file deletions, dependency installs,
  migrations, branches, commits, pushes and pull requests each need an explicit, recorded
  approval. There is no merge, force-push, default-branch push or deploy capability.
- **Never overwrites your work**: a proposal records the hash of each file it was based
  on; if a file has changed since, nothing is written.
- **Isolates repository commands**: tests and linters run in throwaway Docker containers
  with no network, a read-only mount and resource limits.
- **Records everything**: every tool call (including denied ones), approval, operation
  and security event is stored and visible in the interface.

## Status

All ten phases of the plan are implemented and tested; see [docs/progress.md](docs/progress.md).
Two things have not been exercised against the real service:

| Not verified live | What exists instead |
|---|---|
| The OpenAI provider | The agent has been run live on Google Gemini (`gemini-3.5-flash-lite`): 7 of 9 evaluation cases succeeded in one run (see [docs/testing.md](docs/testing.md#evaluation)). The OpenAI path uses the same workflow but has not been called live |
| Real GitHub API calls and pushes | Every GitHub call is tested against a mock of the REST API; pushing is tested against a local bare repository |

Other limits are listed in [docs/testing.md](docs/testing.md#known-limitations).

## Quick start (Docker Compose)

Requirements: Docker with Compose.

```bash
cp .env.example .env        # then edit it: see the comments in the file
docker compose --profile build build sandbox-image
docker compose --profile app up -d --build
```

Open <http://localhost:8080> and create an account; the first account is the
administrator. Then:

1. **Projects & repositories**: create a project and register a repository by its path
   inside the container (`/repos/<name>`, where `/repos` is `REPOS_HOST_DIR` on the host).
2. **Code analysis**: see what the index found.
3. **Developer assistant**: ask for something, for example "Explain how the payment
   module works" or "Find and fix the bug in the customer login API".
4. **Change review**: read the diff, then approve or reject.
5. **Tests & checks**, **MCP & tool activity**, **Git & GitHub**, **Execution history**:
   follow up, commit and push through approvals, inspect what happened.

Agent tasks need a model in `.env`: `LLM_PROVIDER` (`openai` or `gemini`), the matching
key (`OPENAI_API_KEY` or `GEMINI_API_KEY`) and `LLM_MODEL`. Without them, tasks fail
with a clear `llm_not_configured` message and everything else still works.

To try it on something disposable, create the bundled sample project (a small FastAPI +
Vue repository with a real bug and a failing test) inside your repositories directory:

```bash
docker compose --profile app exec backend python -c "from pathlib import Path; from app.evaluation.fixtures import create_sample_repository; create_sample_repository(Path('/repos/shop-demo'))"
```

## Development

```bash
docker compose up -d db                                   # PostgreSQL on localhost:55432

cd backend                                                # Python 3.12 (3.11+ required)
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.lock -e . --group dev
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000       # API docs at /api/docs

cd frontend                                               # Node 22.12+ or 24
npm ci && npm run dev                                     # http://localhost:5173
```

Demo data (optional; register an account first and stop the backend while it runs):

```bash
cd backend && .venv/bin/python -m app.seed
```

It creates a "Demo: Shop API" project for the first administrator, registers the sample
repository and runs four tasks with a scripted stand-in model (no model quota is used):
three finish and one bug fix waits for your approval. Tool calls, diffs and denied
actions in it are real; the report texts are canned.

Checks:

```bash
cd backend  && .venv/bin/ruff check . && .venv/bin/mypy app && .venv/bin/python -m pytest
cd frontend && npm run lint && npm run typecheck && npm test && npm run build
```

Evaluation (needs an LLM key unless `--scripted`):

```bash
cd backend && .venv/bin/python -m app.evaluation --auto-approve --output ../eval-results/report.json
```

## How safety is enforced

The model can only emit tool calls. Each call passes through the **Tool Gateway**
(role check, per-step tool allowlist, approval check, logging, redaction) and then
through the **MCP server**, which independently confines paths to the repository, applies
the command allowlist, and refuses write tools unless the call carries a short-lived
token signed by the backend for those exact arguments after a human approval. Repository
content is treated as untrusted data: it is redacted, delimited, and cannot change what
is permitted. Details: [docs/security.md](docs/security.md).

## Layout

```
backend/    FastAPI API, LangGraph agent, MCP server and client, tests
frontend/   Vue 3 + TypeScript single-page application
docker/     Sandbox image
docs/       Design and operations documentation
```

## Documentation

| Document | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | Components, diagrams, key decisions, frontend screens |
| [implementation-plan.md](docs/implementation-plan.md) | Environment findings, dependencies, phase sequence, risks |
| [database-design.md](docs/database-design.md) | Tables, relationships, migrations |
| [api-documentation.md](docs/api-documentation.md) | REST endpoints, roles, errors |
| [mcp-tools.md](docs/mcp-tools.md) | MCP servers, tools, risk levels, approval tokens |
| [agent-workflow.md](docs/agent-workflow.md) | Graph, nodes, state, approval and failure handling |
| [security.md](docs/security.md) | Threat model and controls |
| [testing.md](docs/testing.md) | Test suites, verification performed, evaluation, known limitations |
| [deployment.md](docs/deployment.md) | Docker Compose, configuration, operations, Google Cloud |
| [progress.md](docs/progress.md) | What was built in each phase and what remains |
