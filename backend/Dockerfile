# Backend API + agent + MCP servers.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PYTHONPATH=/app

# git: repository tools. docker CLI: starts sandbox containers through the mounted socket.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:29-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /usr/local/bin/uv

WORKDIR /app
# Exact, hash-free pins generated with: uv pip compile pyproject.toml -o requirements.lock
COPY requirements.lock ./
RUN uv pip install --system --no-cache -r requirements.lock

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Runs as the uid/gid given by compose (the owner of the repositories directory), not as root.
USER 1000:1000
EXPOSE 8000

# Migrations are applied before the API starts; the process is a single worker because
# agent runs are in-process tasks (see docs/deployment.md).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
