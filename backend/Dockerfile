# Stages:
#   dev     -- what docker-compose builds: uv toolchain, source bind-mounted, `--reload`.
#   builder -- resolves the locked, production-only venv.
#   runtime -- the default (last) target, shipped to the cluster: slim Python, the venv and the
#              app, no build tools, non-root. One server process per container; the orchestrator
#              scales pods, so every process owns its own drain (app/core/lifecycle.py).
# Secrets never enter a layer: every one is read from the environment at start-up.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm AS dev

WORKDIR /app

ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

EXPOSE 8000 8001

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM ghcr.io/astral-sh/uv:python3.12-bookworm AS builder

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project


FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

# HOME points at the writable /tmp mount: the root filesystem is read-only in the cluster and
# the agent SDK keeps scratch state under the home directory.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

USER 10001:10001

EXPOSE 8000 8001

# Exec form, no uv wrapper: uvicorn is PID 1 and receives SIGTERM directly. The worker,
# preview proxy and migration Job override the command (deploy/k8s/base).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
