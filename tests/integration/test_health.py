"""Integration test for the API health endpoint (contracts/health.md).

Uses FastAPI's TestClient (httpx-based) against the `app.main` FastAPI
instance directly — no running server/container required.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_200_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
