"""`require_service_token` — the SaaS admin auth seam (PLAN §7.1)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.service_auth import require_service_token


class _Settings:
    def __init__(self, token: str | None) -> None:
        self.SAAS_SERVICE_TOKEN = token


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch):
    holder = {"token": "s3cret-service-token"}

    def _get_settings():
        return _Settings(holder["token"])

    monkeypatch.setattr("app.service_auth.get_settings", _get_settings)
    return holder


def test_correct_token_is_accepted() -> None:
    assert require_service_token("Bearer s3cret-service-token") is None


def test_wrong_token_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        require_service_token("Bearer wrong")
    assert exc.value.status_code == 401


def test_missing_header_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        require_service_token(None)
    assert exc.value.status_code == 401


def test_non_bearer_scheme_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        require_service_token("Basic s3cret-service-token")
    assert exc.value.status_code == 401


def test_unconfigured_token_refuses_everything(_settings) -> None:
    """An engine with no SAAS_SERVICE_TOKEN set must not expose admin routes."""
    _settings["token"] = None
    with pytest.raises(HTTPException) as exc:
        require_service_token("Bearer anything")
    assert exc.value.status_code == 401


def test_empty_configured_token_refuses_everything(_settings) -> None:
    _settings["token"] = ""
    with pytest.raises(HTTPException) as exc:
        require_service_token("Bearer ")
    assert exc.value.status_code == 401
