# apps/workers/Dockerfile
#
# Build context is the repo root (docker-compose.yml: build.context: .),
# because `uv sync --package workers` needs the whole uv workspace to
# resolve, even though only the `workers` member's runtime deps end up
# installed (celery, redis, app_shared — no scrapy/twisted/playwright,
# FR-003; Scrapy always runs under Scrapyd, never inside Celery, per
# Constitution V).
FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY . .

RUN uv sync --locked --package workers --no-dev

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

CMD ["celery", "-A", "app.workers.celery_app", "worker", "--loglevel=info"]
