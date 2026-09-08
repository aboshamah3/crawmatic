# apps/fixture-origin/Dockerfile — the EPA D1 controlled origin (audit §13).
#
# Deliberately NOT a uv-workspace member (root pyproject.toml excludes
# `apps/fixture-origin`, same precedent as `apps/dr-backup`): this service
# must not be able to import `app_shared`, reach the database, or share a
# dependency set with the engine. It is a target, not a component — the
# whole value of the fleet test is that the thing being measured and the
# thing serving the pages have nothing in common but HTTP.
#
# Consequently its two dependencies are pinned HERE rather than in
# uv.lock, and adding this service changes neither uv.lock nor any
# engine image.
FROM python:3.13.5-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Pinned exactly, no resolver at build time: a fixture origin whose
# behaviour drifts between staging rebuilds would invalidate the very
# comparison the fleet test exists to make.
RUN pip install --no-cache-dir \
        "fastapi==0.115.12" \
        "uvicorn[standard]==0.34.2"

COPY apps/fixture-origin/app /app/app

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

# Single worker by default: Railway scales this with replicas, and the
# per-store latency model is per-request `asyncio.sleep`, so one event
# loop serves thousands of concurrent slow requests without threads.
ENV PORT=8080 \
    FIXTURE_ORIGIN_WORKERS=1
EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${FIXTURE_ORIGIN_WORKERS} --no-access-log"]
