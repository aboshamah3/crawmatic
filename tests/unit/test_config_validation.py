"""Unit tests for `app_shared/config_validation.py` (audit §L1).

Builds `Settings(_env_file=None)` directly (mirrors `test_config.py`) so
these tests never see a developer's local `.env`, and always pass an
explicit `settings=` to `assert_production_safe` so `get_settings()`'s
process-wide cache is never touched.
"""

from __future__ import annotations

import pytest

from app_shared.config import Settings
from app_shared.config_validation import (
    EXTRA_PRODUCTION_CHECKS,
    ProductionConfigError,
    assert_production_safe,
    is_production,
)

#: A fully "safe" production-shaped config: distinct DB role, real
#: secrets, no placeholders.
SAFE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://app_role:s3cr3t-real-pw@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "a-real-generated-scrapyd-password",
    "JWT_SECRET": "a-real-32-byte-or-longer-random-jwt-secret-value",
    "ENCRYPTION_KEYS": "1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}

#: `.env.example`'s local-dev placeholders, unedited.
LOCAL_DEV_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "change-me-local-dev-secret-32-bytes-min",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _settings(env: dict[str, str], **overrides: str) -> Settings:
    return Settings(_env_file=None, **{**env, **overrides})


@pytest.fixture(autouse=True)
def _clear_extra_checks_and_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("PGBOUNCER_AUTH_TYPE", raising=False)
    monkeypatch.delenv("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE", raising=False)
    saved = list(EXTRA_PRODUCTION_CHECKS)
    EXTRA_PRODUCTION_CHECKS.clear()
    yield
    EXTRA_PRODUCTION_CHECKS.clear()
    EXTRA_PRODUCTION_CHECKS.extend(saved)


# --- is_production() ---


def test_is_production_false_by_default() -> None:
    assert is_production() is False


@pytest.mark.parametrize("value", ["production", "PRODUCTION", "Production", "prod", "PROD"])
def test_is_production_true_for_environment_var(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", value)
    assert is_production() is True


def test_is_production_true_for_railway_environment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert is_production() is True


def test_is_production_false_for_other_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert is_production() is False


# --- assert_production_safe(): no-op outside production ---


def test_noop_outside_production_even_with_placeholder_config() -> None:
    """Never blocks local dev / CI, no matter how weak the config looks."""
    assert_production_safe(settings=_settings(LOCAL_DEV_ENV))


# --- assert_production_safe(): production mode ---


def test_passes_in_production_with_a_fully_safe_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert_production_safe(settings=_settings(SAFE_ENV))


def test_refuses_placeholder_jwt_secret_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, JWT_SECRET=LOCAL_DEV_ENV["JWT_SECRET"])

    with pytest.raises(ProductionConfigError, match="JWT_SECRET"):
        assert_production_safe(settings=settings)


def test_refuses_short_jwt_secret_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, JWT_SECRET="short")

    with pytest.raises(ProductionConfigError, match="JWT_SECRET"):
        assert_production_safe(settings=settings)


def test_refuses_placeholder_encryption_key_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, ENCRYPTION_KEYS=LOCAL_DEV_ENV["ENCRYPTION_KEYS"])

    with pytest.raises(ProductionConfigError, match="ENCRYPTION_KEYS"):
        assert_production_safe(settings=settings)


def test_refuses_placeholder_scrapyd_password_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, SCRAPYD_PASSWORD="change-me")

    with pytest.raises(ProductionConfigError, match="SCRAPYD_PASSWORD"):
        assert_production_safe(settings=settings)


def test_refuses_blank_saas_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, SAAS_SERVICE_TOKEN="   ")

    with pytest.raises(ProductionConfigError, match="SAAS_SERVICE_TOKEN"):
        assert_production_safe(settings=settings)


def test_refuses_short_saas_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, SAAS_SERVICE_TOKEN="short-token")

    with pytest.raises(ProductionConfigError, match="SAAS_SERVICE_TOKEN"):
        assert_production_safe(settings=settings)


def test_allows_unset_saas_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset (fail-closed admin surface) is fine; blank-but-present is not."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, SAAS_SERVICE_TOKEN=None)

    assert_production_safe(settings=settings)


def test_refuses_pgbouncer_trust_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PGBOUNCER_AUTH_TYPE", "trust")

    with pytest.raises(ProductionConfigError, match="PGBOUNCER_AUTH_TYPE"):
        assert_production_safe(settings=_settings(SAFE_ENV))


def test_allows_non_trust_pgbouncer_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PGBOUNCER_AUTH_TYPE", "scram_sha_256")

    assert_production_safe(settings=_settings(SAFE_ENV))


def test_refuses_bootstrap_db_role_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(SAFE_ENV, DATABASE_URL=LOCAL_DEV_ENV["DATABASE_URL"])

    with pytest.raises(ProductionConfigError, match="bootstrap/owner role"):
        assert_production_safe(settings=settings)


def test_refuses_placeholder_db_password_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(
        SAFE_ENV,
        DATABASE_URL="postgresql+psycopg://app_role:crawmatic@pgbouncer:6432/crawmatic",
    )

    with pytest.raises(ProductionConfigError, match="password"):
        assert_production_safe(settings=settings)


def test_aggregates_every_violation_in_one_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single boot failure surfaces every problem, not just the first."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PGBOUNCER_AUTH_TYPE", "trust")
    settings = _settings(LOCAL_DEV_ENV)

    with pytest.raises(ProductionConfigError) as exc_info:
        assert_production_safe(settings=settings)

    message = str(exc_info.value)
    assert "JWT_SECRET" in message
    assert "ENCRYPTION_KEYS" in message
    assert "SCRAPYD_PASSWORD" in message
    assert "PGBOUNCER_AUTH_TYPE" in message
    assert "bootstrap/owner role" in message


# --- EXTRA_PRODUCTION_CHECKS extension point (reserved for C3) ---


def test_extra_production_checks_hook_runs_and_can_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    EXTRA_PRODUCTION_CHECKS.append(lambda: "the RLS-bypass probe failed")

    with pytest.raises(ProductionConfigError, match="the RLS-bypass probe failed"):
        assert_production_safe(settings=_settings(SAFE_ENV))


def test_extra_production_checks_hook_passing_check_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    EXTRA_PRODUCTION_CHECKS.append(lambda: None)

    assert_production_safe(settings=_settings(SAFE_ENV))


def test_extra_production_checks_hook_never_runs_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    EXTRA_PRODUCTION_CHECKS.append(lambda: calls.append(True) or "should never run")

    assert_production_safe(settings=_settings(LOCAL_DEV_ENV))

    assert calls == []


# --- bootstrap/owner DB roles ------------------------------------------------
#
# The guard originally matched only `.env.example`'s `crawmatic`, while the
# live Railway deployment connects as `postgres` — a superuser that also owns
# every table. It therefore passed the one deployment it most needed to fail,
# and was quotable as evidence that production was fine. These pin both names.


@pytest.mark.parametrize("role", ["crawmatic", "postgres"])
def test_production_refuses_a_bootstrap_owner_db_role(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(
        SAFE_ENV,
        DATABASE_URL=f"postgresql+psycopg://{role}:s3cr3t-real-pw@pgbouncer:6432/crawmatic",
    )
    with pytest.raises(ProductionConfigError, match=role):
        assert_production_safe(settings)


def test_production_allows_a_distinct_least_privileged_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    settings = _settings(
        SAFE_ENV,
        DATABASE_URL="postgresql+psycopg://crawmatic_app:s3cr3t-real-pw@pgbouncer:6432/crawmatic",
    )
    assert_production_safe(settings)


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_acknowledged_override_lets_a_bootstrap_role_boot(
    monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """Shares rls_guard's switch: one fact, one acknowledgement."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE", flag)
    settings = _settings(
        SAFE_ENV,
        DATABASE_URL="postgresql+psycopg://postgres:s3cr3t-real-pw@pgbouncer:6432/crawmatic",
    )
    assert_production_safe(settings)


def test_override_does_not_excuse_other_unsafe_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override covers the DB role only — not weak secrets."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE", "1")
    settings = _settings(
        SAFE_ENV,
        DATABASE_URL="postgresql+psycopg://postgres:s3cr3t-real-pw@pgbouncer:6432/crawmatic",
        JWT_SECRET="too-short",
    )
    with pytest.raises(ProductionConfigError, match="JWT_SECRET"):
        assert_production_safe(settings)


def test_override_is_ignored_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(LOCAL_DEV_ENV)
    assert_production_safe(settings)
