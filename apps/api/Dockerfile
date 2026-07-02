# apps/api/Dockerfile
#
# Build context is the repo root (docker-compose.yml: build.context: .),
# because `uv sync --package api` needs the whole uv workspace (root
# pyproject.toml + uv.lock + every member's pyproject.toml) to resolve,
# even though only the `api` member's runtime deps end up installed.
#
# PORT/API_PORT (analyze I1): API_PORT is the single canonical env var a
# developer sets. `docker-compose.yml` derives the container's PORT from
# it (`environment: PORT: ${API_PORT}`) and publishes the same value, so
# the container's uvicorn listen port always equals the published host
# port. ENV PORT=8000 below is only a standalone-run default; compose
# always overrides it.
FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Whole workspace (filtered by .dockerignore: no .git/.venv/tests/specs/...)
COPY . .

RUN uv sync --locked --package api --no-dev

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn app.main:app --host :: --port ${PORT}"]
