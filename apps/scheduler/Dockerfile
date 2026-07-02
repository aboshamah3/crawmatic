# apps/scheduler/Dockerfile
#
# Build context is the repo root (docker-compose.yml: build.context: .),
# because `uv sync --package scheduler` needs the whole uv workspace to
# resolve, even though only the `scheduler` member's runtime deps end up
# installed (app_shared only — no scrapy/twisted/playwright, FR-003).
FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY . .

RUN uv sync --locked --package scheduler --no-dev

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

CMD ["python", "-m", "app.scheduler.scheduler_app"]
