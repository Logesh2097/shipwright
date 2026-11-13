# Image used to run repository commands (tests, linters, builds) in isolation.
# Containers are started with no network, a read-only root filesystem and resource limits.
FROM node:24-slim AS node

FROM python:3.12-slim

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Baseline tools so plain Python repositories can be checked without installing anything.
RUN pip install --no-cache-dir pytest ruff mypy

# /deps holds per-repository dependencies (a named volume). World-writable with the sticky
# bit so the unprivileged sandbox user can create its virtualenv there.
RUN mkdir -p /deps /workspace && chmod 1777 /deps

COPY sandbox-entrypoint.sh /usr/local/bin/asea-entrypoint
RUN chmod 755 /usr/local/bin/asea-entrypoint

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/asea-entrypoint"]
