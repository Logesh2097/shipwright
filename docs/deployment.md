# Deployment

## Components

| Service | Image | Port | Notes |
|---|---|---|---|
| `db` | `postgres:18-alpine` | 5432 (host: 55432, loopback only) | Data in the `pgdata` volume |
| `backend` | built from `backend/Dockerfile` | 8000 (loopback only) | API, agent, MCP servers as child processes. Single worker |
| `frontend` | built from `frontend/Dockerfile` | 8080 (loopback only) | nginx serving the SPA and proxying `/api` |
| sandbox | built from `docker/sandbox.Dockerfile` | none | Not a service. One short-lived container per command, started by the backend |

All published ports bind to `127.0.0.1`. Put a TLS-terminating reverse proxy in front of
`frontend` to expose the application.

## Docker Compose

```bash
cp .env.example .env
# Edit .env:
#   POSTGRES_PASSWORD, JWT_SECRET, APPROVAL_TOKEN_SECRET, CREDENTIAL_ENCRYPTION_KEY  (generate; commands are in the file)
#   REPOS_HOST_DIR   directory on the host that contains the repositories to work on
#   HOST_UID/HOST_GID  owner of that directory (id -u; id -g)
#   DOCKER_GID       group of the Docker socket (stat -c %g /var/run/docker.sock)
#   LLM_PROVIDER, its API key (OPENAI_API_KEY or GEMINI_API_KEY) and LLM_MODEL   required for agent tasks
#   ENVIRONMENT=production      refuses to start with weak or missing secrets

docker compose --profile build build sandbox-image     # image used for sandboxed commands
docker compose --profile app up -d --build             # db, backend, frontend
```

Open <http://localhost:8080>. The first account you register becomes the administrator.
Repositories are registered by their path inside the backend container: a repository at
`$REPOS_HOST_DIR/my-service` is `/repos/my-service`.

The backend container applies database migrations (`alembic upgrade head`) every time it
starts, before serving requests.

### Health checks

| Check | URL | Meaning |
|---|---|---|
| Liveness | `GET /api/v1/health/live` | Process is up |
| Readiness | `GET /api/v1/health/ready` | Database reachable and migrated, runner started. Also reports LLM configuration, sandbox mode and MCP session count. Returns 503 otherwise |
| Frontend | `GET /healthz` on port 8080 | nginx is serving |

Compose uses these for container health and start-up ordering.

### What the backend container is allowed to do

- It mounts `REPOS_HOST_DIR` read-write at `/repos` and runs as `HOST_UID:HOST_GID`, so
  files it writes (approved changes, commits) keep the right owner.
- It mounts `/var/run/docker.sock` to start sandbox containers. **Access to the Docker
  socket is equivalent to root on the host.** Run this stack on a machine dedicated to
  it, do not expose the backend port, and keep the image up to date. If that risk is
  not acceptable, set `SANDBOX_MODE=local` and remove the socket mount, accepting that
  repository commands then run inside the backend container without network isolation.
- `SANDBOX_PATH_MAP=/repos=<host path>` tells the backend how to translate its own paths
  into the host paths the Docker daemon needs for sandbox mounts. Compose sets it from
  `REPOS_HOST_DIR`.

### Operations

```bash
docker compose --profile app logs -f backend     # structured logs (set LOG_JSON=true for JSON)
docker compose --profile app restart backend     # running tasks become "interrupted" and can be resumed
docker compose exec db pg_dump -U asea asea > backup.sql
docker volume ls | grep asea-deps-               # per-repository dependency volumes created by approved installs
```

Upgrading: pull or rebuild the images and `up -d` again; migrations run automatically.
Rotating `CREDENTIAL_ENCRYPTION_KEY` makes stored GitHub tokens unreadable; users then
save their token again. Rotating `JWT_SECRET` signs everyone out.

### Scaling limits

The backend must run as **one process**: agent runs are asyncio tasks inside it, and MCP
sessions and login throttling are in-process state. Run state itself is durable (run
rows, events, LangGraph checkpoints in PostgreSQL), so a restart loses no completed work.
Running several replicas would need a shared job queue and is not supported.

## Local development

```bash
docker compose up -d db                       # PostgreSQL on localhost:55432

cd backend
uv venv --python 3.12 .venv                   # Python 3.11+ is required; 3.12 is what is tested
uv pip install --python .venv/bin/python -r requirements.lock -e . --group dev
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000

cd frontend                                    # Node 22.12+ or 24
npm ci
npm run dev                                    # http://localhost:5173, proxies /api to :8000
```

The backend reads `.env` from the repository root. For local development set
`REPOS_ALLOWED_ROOTS` to a directory on your machine. A sample repository for trying the
application can be created with:

```bash
cd backend && .venv/bin/python -c "from pathlib import Path; from app.evaluation.fixtures import create_sample_repository; create_sample_repository(Path('<allowed root>/shop-demo'))"
```

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` enforces strong secrets |
| `DATABASE_URL` | local dev URL | `postgresql+psycopg://user:pass@host:port/db` |
| `JWT_SECRET` | dev value | Signs access and refresh tokens (≥ 32 characters in production) |
| `APPROVAL_TOKEN_SECRET` | dev value | Signs approval tokens checked by the MCP server |
| `CREDENTIAL_ENCRYPTION_KEY` | derived in dev | Fernet key for stored GitHub tokens; required in production |
| `ACCESS_TOKEN_MINUTES`, `REFRESH_TOKEN_DAYS` | 30, 7 | Session lifetimes |
| `ALLOW_REGISTRATION` | `true` | Self-registration after the first administrator exists |
| `CORS_ORIGINS` | `http://localhost:5173` | Only needed when the SPA is served from another origin |
| `REPOS_ALLOWED_ROOTS` | empty | Comma-separated directories repositories may be registered from |
| `MAX_FILE_BYTES`, `INDEX_MAX_FILES` | 1 000 000, 20 000 | Indexing and reading limits |
| `SANDBOX_MODE` | `auto` | `docker`, `local`, or `auto` (Docker when reachable) |
| `SANDBOX_IMAGE`, `SANDBOX_MEMORY_MB`, `SANDBOX_CPUS`, `SANDBOX_PIDS_LIMIT` | `asea-sandbox:latest`, 1024, 1.0, 256 | Sandbox container limits |
| `SANDBOX_PATH_MAP` | empty | `container_prefix=host_prefix` when the backend runs in a container |
| `LLM_PROVIDER` | `openai` | `openai` or `gemini` |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | empty | Key for the selected provider |
| `LLM_MODEL` | empty | Required for agent tasks. No model is assumed |
| `OPENAI_BASE_URL` | empty | For an OpenAI-compatible endpoint (not for Gemini; see agent-workflow.md) |
| `LLM_REQUESTS_PER_MINUTE` | 0 | Client-side pacing; set just below the provider's per-minute quota |
| `LLM_RATE_LIMIT_WAIT_SECONDS` | 180 | Longest total wait for a rate limit to clear in one model call |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | 90, 2 | Provider failure handling |
| `AGENT_MAX_TOOL_STEPS`, `AGENT_MAX_REPAIR_ATTEMPTS` | 16, 2 | Agent loop limits |
| `APPROVAL_EXPIRY_HOURS`, `APPROVAL_TOKEN_TTL_SECONDS` | 72, 120 | Approval lifetimes |
| `MCP_CALL_TIMEOUT_SECONDS`, `MCP_LONG_CALL_TIMEOUT_SECONDS` | 60, 900 | Tool call timeouts |
| `LOG_LEVEL`, `LOG_JSON` | `INFO`, `false` | Logging |

## Google Cloud

The sandbox needs a Docker daemon, which rules out Cloud Run and makes GKE Autopilot a
poor fit. The straightforward layout is one Compute Engine VM running this Compose file.

| Concern | Recommendation |
|---|---|
| Compute | A dedicated Compute Engine VM (for example `e2-standard-4`), Container-Optimized OS or Debian with Docker. No other workloads, because of the Docker socket |
| Database | Keep the Compose `db` service for a small installation, or use Cloud SQL for PostgreSQL and point `DATABASE_URL` at it (remove the `db` service). Cloud SQL gives backups and point-in-time recovery |
| Secrets | Secret Manager; write them into `.env` at boot from a start-up script using the VM's service account. Do not bake them into the image |
| Ingress | HTTPS load balancer or Identity-Aware Proxy in front of port 8080. No public IP on the VM; firewall allows only the load balancer and IAP ranges |
| Repositories | A persistent disk mounted at `REPOS_HOST_DIR`, with repositories cloned there by whoever administers the machine |
| Egress | The backend needs outbound HTTPS to the model provider (OpenAI or Google) and to GitHub. Sandbox containers have no network except during approved dependency installs |
| Images | Build in Cloud Build, store in Artifact Registry, pull on the VM |
| Logs | `LOG_JSON=true`; the Docker `gcplogs` driver or the Ops Agent forwards them to Cloud Logging |

This layout has not been deployed as part of this work; it is a recommendation.
Deploying to production is an operator action and is outside what the application or
its agent can do.
