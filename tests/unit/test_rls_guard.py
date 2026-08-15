"""Unit tests for the ordinary-role RLS assertion (audit C3).

Covers the three things that make this guard safe to ship:

* it correctly classifies every combination of role attributes,
* it is **off** by default so local development and the existing test
  suite are untouched, and it is on in production,
* it runs its query at most once per engine (it is a startup check, not
  a per-request one) and honours the named escape hatch.

No database is required: the guard's single query is stubbed with a
fake bind.
"""

from __future__ import annotations

import pytest

from app_shared.db import rls_guard
from app_shared.db.rls_guard import (
    OrdinaryRoleFacts,
    RlsRoleViolation,
    assert_ordinary_role_cannot_bypass_rls,
    enforce_rls_role_on_startup,
    reset_rls_guard_cache,
    rls_assertion_enabled,
)

_ENV_VARS = (
    "RLS_ROLE_ASSERTION",
    "RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE",
    "APP_ENV",
    "ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from a known, un-production environment."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_rls_guard_cache()
    yield
    reset_rls_guard_cache()


class _FakeBind:
    """Minimal stand-in for an Engine that counts inspections."""

    def __init__(self, facts: OrdinaryRoleFacts) -> None:
        self.facts = facts
        self.calls = 0


def _install_fake_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    def _inspect(bind: object) -> OrdinaryRoleFacts:
        assert isinstance(bind, _FakeBind)
        bind.calls += 1
        return bind.facts

    monkeypatch.setattr(rls_guard, "inspect_ordinary_role", _inspect)


def _facts(**overrides: object) -> OrdinaryRoleFacts:
    base = {
        "role_name": "crawmatic_app",
        "is_superuser": False,
        "has_bypassrls": False,
        "owned_public_tables": 0,
        "rls_without_force": 0,
    }
    base.update(overrides)
    return OrdinaryRoleFacts(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------


def test_clean_role_is_confined():
    facts = _facts()
    assert facts.is_confined
    assert facts.violations == ()


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"is_superuser": True}, "SUPERUSER"),
        ({"has_bypassrls": True}, "BYPASSRLS"),
        ({"owned_public_tables": 40}, "owns 40 table(s)"),
    ],
)
def test_each_attribute_is_a_violation(overrides: dict, expected_fragment: str):
    facts = _facts(**overrides)
    assert not facts.is_confined
    assert any(expected_fragment in v for v in facts.violations)


def test_all_three_violations_are_reported_together():
    facts = _facts(is_superuser=True, has_bypassrls=True, owned_public_tables=40)
    assert len(facts.violations) == 3


# --------------------------------------------------------------------
# Enablement — must not break local dev or tests
# --------------------------------------------------------------------


def test_disabled_by_default_outside_production():
    assert rls_assertion_enabled() is False


@pytest.mark.parametrize(
    "var", ["APP_ENV", "ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"]
)
@pytest.mark.parametrize("value", ["production", "PRODUCTION", "prod"])
def test_auto_enables_in_production(monkeypatch: pytest.MonkeyPatch, var: str, value: str):
    monkeypatch.setenv(var, value)
    assert rls_assertion_enabled() is True


def test_explicit_off_wins_over_production(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "off")
    assert rls_assertion_enabled() is False


def test_explicit_on_wins_outside_production(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    assert rls_assertion_enabled() is True


def test_unknown_mode_falls_back_to_auto(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "maybe")
    assert rls_assertion_enabled() is False
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert rls_assertion_enabled() is True


def test_skipped_when_disabled_runs_no_query(monkeypatch: pytest.MonkeyPatch):
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts(is_superuser=True))
    assert assert_ordinary_role_cannot_bypass_rls(bind) is None
    assert bind.calls == 0


# --------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------


def test_raises_on_superuser(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts(role_name="postgres", is_superuser=True, has_bypassrls=True))
    with pytest.raises(RlsRoleViolation) as excinfo:
        assert_ordinary_role_cannot_bypass_rls(bind)
    message = str(excinfo.value)
    assert "SUPERUSER" in message
    assert "RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE" in message


def test_raises_on_table_ownership(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts(owned_public_tables=40))
    with pytest.raises(RlsRoleViolation):
        assert_ordinary_role_cannot_bypass_rls(bind)


def test_passes_on_clean_role(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts())
    result = assert_ordinary_role_cannot_bypass_rls(bind)
    assert result is not None and result.is_confined


def test_escape_hatch_downgrades_to_warning(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    monkeypatch.setenv("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE", "1")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts(has_bypassrls=True))
    with caplog.at_level("ERROR"):
        result = assert_ordinary_role_cannot_bypass_rls(bind)
    assert result is not None and not result.is_confined
    assert any("rls_guard" in record.message for record in caplog.records)


def test_unforced_rls_tables_warn_but_do_not_block(
    monkeypatch: pytest.MonkeyPatch, caplog
):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts(rls_without_force=3))
    with caplog.at_level("WARNING"):
        result = assert_ordinary_role_cannot_bypass_rls(bind)
    assert result is not None and result.is_confined
    assert any("FORCE ROW LEVEL SECURITY" in record.message for record in caplog.records)


# --------------------------------------------------------------------
# Cost — one query per engine, not per call
# --------------------------------------------------------------------


def test_query_runs_once_per_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts())
    for _ in range(5):
        assert_ordinary_role_cannot_bypass_rls(bind)
    assert bind.calls == 1


def test_force_re_runs_the_query(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts())
    assert_ordinary_role_cannot_bypass_rls(bind)
    assert_ordinary_role_cannot_bypass_rls(bind, force=True)
    assert bind.calls == 2


def test_reset_cache_allows_recheck(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts())
    assert_ordinary_role_cannot_bypass_rls(bind)
    reset_rls_guard_cache()
    assert_ordinary_role_cannot_bypass_rls(bind)
    assert bind.calls == 2


# --------------------------------------------------------------------
# Startup hook
# --------------------------------------------------------------------


def test_startup_hook_is_a_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """The hook must be safe to call unconditionally, with no DB present."""

    def _boom() -> None:  # pragma: no cover - must never run
        raise AssertionError("get_engine() must not be touched when disabled")

    monkeypatch.setattr("app_shared.database.get_engine", _boom)
    assert enforce_rls_role_on_startup() is None


def test_startup_hook_uses_the_ordinary_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    bind = _FakeBind(_facts())
    monkeypatch.setattr("app_shared.database.get_engine", lambda: bind)
    result = enforce_rls_role_on_startup()
    assert result is not None and result.role_name == "crawmatic_app"
    assert bind.calls == 1


# --------------------------------------------------------------------
# config_validation.EXTRA_PRODUCTION_CHECKS adapter
# --------------------------------------------------------------------


def test_production_check_returns_none_when_clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    monkeypatch.setattr("app_shared.database.get_engine", lambda: _FakeBind(_facts()))
    assert rls_guard.production_check() is None


def test_production_check_returns_message_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    _install_fake_inspect(monkeypatch)
    monkeypatch.setattr(
        "app_shared.database.get_engine",
        lambda: _FakeBind(_facts(role_name="postgres", is_superuser=True)),
    )
    message = rls_guard.production_check()
    assert message is not None and "SUPERUSER" in message


def test_production_check_reports_when_escape_hatch_hides_a_violation(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RLS_ROLE_ASSERTION", "on")
    monkeypatch.setenv("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE", "1")
    _install_fake_inspect(monkeypatch)
    monkeypatch.setattr(
        "app_shared.database.get_engine", lambda: _FakeBind(_facts(has_bypassrls=True))
    )
    message = rls_guard.production_check()
    assert message is not None
    assert "RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE" in message


def test_production_check_is_a_noop_outside_production(monkeypatch: pytest.MonkeyPatch):
    def _boom() -> None:  # pragma: no cover - must never run
        raise AssertionError("must not connect outside production")

    monkeypatch.setattr("app_shared.database.get_engine", _boom)
    assert rls_guard.production_check() is None


def test_registration_is_idempotent():
    from app_shared.config_validation import EXTRA_PRODUCTION_CHECKS

    before = list(EXTRA_PRODUCTION_CHECKS)
    try:
        rls_guard.register_production_check()
        rls_guard.register_production_check()
        assert EXTRA_PRODUCTION_CHECKS.count(rls_guard.production_check) == 1
    finally:
        EXTRA_PRODUCTION_CHECKS[:] = before
