# Implementation plan

Status of each phase is tracked in [progress.md](progress.md).

## Environment findings (analysis stage)

| Finding | Consequence |
|---|---|
| Project directory was empty apart from a zero-byte `docs` file; not a Git repository | Greenfield build. The empty file was replaced by the `docs/` directory; `git init` was run, nothing staged or committed |
| Host Python is 3.10 | Backend uses a uv-managed Python 3.12 virtualenv (`backend/.venv`) |
| Host Node is v12; nvm provides v24.12 | Frontend uses Node 24 |
| Docker 29 and Compose v5 available | PostgreSQL for development and tests runs in Docker on port 55432 (a host PostgreSQL already owns 5432 and is left alone) |
| No `OPENAI_API_KEY`, no GitHub token | LLM and GitHub paths are implemented and tested against a scripted model and a mocked HTTP transport. Live behaviour is not claimed |
| `mcp` 2.3 renamed `FastMCP` to `MCPServer` and added `Client` | Server and client are written against the 2.x API, verified by a prototype |
| `typescript-eslint` supports TypeScript < 6.1 | TypeScript pinned to 6.0.x |
| `jsdom` 31 needs Node ≥ 24.15 | Vitest uses `happy-dom` |

## Dependencies

### Backend (Python 3.12)

| Package | Why |
|---|---|
| fastapi, uvicorn[standard] | HTTP API |
| pydantic, pydantic-settings | Validation and configuration |
| sqlalchemy[asyncio], alembic, psycopg[binary,pool] | PostgreSQL access and migrations |
| langgraph, langgraph-checkpoint-postgres | Agent workflow and durable checkpoints |
| langchain, langchain-openai | Chat model interface, tool binding, OpenAI client |
| langchain-google-genai | Gemini through Google's native API (needed for multi-step tool use; added when the Gemini provider was requested) |
| mcp | Official MCP SDK (server and client) |
| httpx | GitHub API client |
| structlog | Structured logging |
| pyjwt, argon2-cffi, cryptography | Tokens, password hashing, credential encryption |
| pytest, pytest-asyncio, ruff, mypy (dev) | Tests, lint, types |

Not used, deliberately: Celery/Redis, MongoDB, a vector database, tree-sitter, GitPython
(the `git` CLI is called with a hardened configuration), PyGithub (a few REST calls with
httpx).

### Frontend (Node 24)

| Package | Why |
|---|---|
| vue, vue-router, pinia | Application framework, routing, state |
| vite, @vitejs/plugin-vue | Build |
| typescript (6.0), vue-tsc | Type checking |
| tailwindcss, @tailwindcss/vite | Styling |
| @mdi/font | Icons (the icon set of the design system the interface follows) |
| vitest, @vue/test-utils, happy-dom | Unit tests |
| eslint, eslint-plugin-vue, typescript-eslint | Lint |

## Phase sequence

| # | Phase | Main deliverables | Depends on |
|---|---|---|---|
| 1 | Foundation | Layout, config, logging, errors, app factory, DB engine, Docker base, tooling | — |
| 2 | Backend and database | Models, migration, auth, users, projects, repositories, tasks, approvals, logs, health | 1 |
| 3 | Repository understanding | Validation, scanner, detectors, symbol index, search, context, unused code, explorer API | 2 |
| 4 | MCP | Server with five toolsets, sandbox, client manager, gateway, policy, logs | 3 |
| 5 | Agent | LLM layer, state, graph, nodes, runner, events, SSE | 4 |
| 6 | Controlled editing | Proposals, diffs, approval flow, safe apply, stale detection, revert, audit | 5 |
| 7 | Verification | Discovery, selection, runners, parsers, results, repair loop | 6 |
| 8 | GitHub | Credential storage, read tools, branch/commit/push/PR through approvals | 6 |
| 9 | Security, reliability, evaluation | Hardening pass, recovery, dedupe, evaluation dataset and harness | 5–8 |
| 10 | Frontend, Docker, deployment | Twelve screens, Dockerfiles, Compose, docs | 2–9 |

Phases 5–7 share the graph, so the graph is built once with all nodes and each phase
fills in and tests its part.

## Validation after each phase

1. `ruff check` and `ruff format --check`
2. `mypy app`
3. `pytest` (unit and integration against the Docker PostgreSQL)
4. From phase 10: `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`
5. Phase-specific security checks listed in [testing.md](testing.md)
6. Entry in [progress.md](progress.md)

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| No LLM key during the build | Scripted chat model drives the full graph in tests; real-model behaviour is documented as unverified |
| No GitHub token | `httpx.MockTransport` tests for every call; push tested against a local bare remote |
| New major versions of MCP and LangGraph | APIs verified by prototypes before design was fixed; versions pinned with lower and upper bounds |
| Docker socket mount is root-equivalent | Documented; sandbox containers are locked down; local mode available; GCP guidance uses a dedicated VM |
| Regex symbol extraction misses JS/TS constructs | Text search remains available to the agent as a fallback; limitation documented |
| Large repositories | File and size caps, `git ls-files` to honour ignore rules, incremental re-index by content hash, bounded context packs |
| LLM produces a wrong edit | Exact-match edits fail loudly; human reviews the diff; verification and limited repair follow |
| In-process runner loses work on restart | Checkpoints plus startup sweep; runs resume |
