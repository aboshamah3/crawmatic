"""FastAPI application for the `api` service.

SPEC-01 (contracts/health.md) established a single, unauthenticated,
dependency-free liveness endpoint that MUST NOT touch the database,
Redis, or Scrapyd, and MUST NOT construct a per-request DB engine — any
readiness variant reuses the process-wide lazy engine from
``app_shared.database`` instead (FR-020). ``/health`` still holds to
that.

SPEC-03 adds the `/v1/auth/*` router (login/refresh/logout,
contracts/api-auth.md, US1) — those endpoints do use the shared lazy
DB/Redis singletons (never a per-request engine), consistent with the
same FR-020 discipline.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.routers import auth

app = FastAPI(title="crawmatic-api")

app.include_router(auth.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is serving."""
    return {"status": "ok"}
