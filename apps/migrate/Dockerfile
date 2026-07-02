# apps/migrate/Dockerfile — one-shot Alembic migration job (SPEC-02 US1).
#
# Build context is the repo root (docker-compose.yml: build.context: .),
# same pattern as every other app image: `uv sync --package migrate`
# needs the whole uv workspace (root pyproject.toml + uv.lock + every
# member's pyproject.toml) to resolve, and it installs from the LOCKED
# root uv.lock so the migration job runs under the exact pinned
# `alembic`/`app_shared` versions (contracts/migration-job.md §5).
#
# Runs `alembic upgrade head` against `MIGRATION_DATABASE_URL`
# (direct-to-Postgres, e.g. postgres:5432 — never the PgBouncer pooler
# at pgbouncer:6432; see alembic/env.py and contracts/migration-job.md).
# One-shot: the container exits after the migration completes (compose
# service sets `restart: "no"`).
FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Whole workspace (filtered by .dockerignore: no .git/.venv/tests/specs/...)
# — includes alembic.ini + alembic/ (env.py, versions/) at the repo root,
# which `alembic upgrade head` needs at runtime.
COPY . .

RUN uv sync --locked --package migrate --no-dev

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

CMD ["alembic", "upgrade", "head"]
