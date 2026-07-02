"""FastAPI application for the `api` service.

The only application behaviour in scope for SPEC-01 (contracts/health.md):
a single, unauthenticated, dependency-free liveness endpoint. It MUST NOT
touch the database, Redis, or Scrapyd, and MUST NOT construct a per-request
DB engine — any future readiness variant reuses the process-wide lazy
engine from ``app_shared.database`` instead (FR-020).
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="crawmatic-api")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 whenever the process is serving."""
    return {"status": "ok"}
