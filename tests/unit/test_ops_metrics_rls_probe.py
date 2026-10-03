"""The RLS probe checks the role that SERVES TENANTS (2026-09-29, plan E8).

`security.rls_inert` fired CRITICAL on every `/ops/metrics` call in
production. The probe ran `current_user` on the session it was handed --
the API passes the BYPASSRLS auth session, the scheduler the BYPASSRLS
system session -- so it reported, correctly, that a BYPASSRLS role bypasses
RLS. The tenant role (`crawmatic_app`, NOBYPASSRLS) was never examined. A
CRITICAL that is always on is a CRITICAL nobody reads.

Now the tenant engine is probed with `rls_guard.inspect_ordinary_role`
(superuser, BYPASSRLS AND table ownership); CRITICAL when it is not
confined OR when the probe could not run. The privileged session is
reported for what it is -- `expected_privileged` -- and alerts only when it
is a superuser or is the tenant role itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app_shared.db.rls_guard import OrdinaryRoleFacts
from app_shared.opsmetrics import snapshot as snapshot_mod
from app_shared.opsmetrics.rules import Severity, evaluate, worst_severity
from app_shared.opsmetrics.snapshot import DatabaseRoleHealth, OpsSnapshot

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def _snap(db_role: DatabaseRoleHealth) -> OpsSnapshot:
    return OpsSnapshot(collected_at=NOW, db_role=db_role)


def _ids(alerts) -> dict[str, Any]:  # noqa: ANN001
    return {a.rule_id: a for a in alerts}


def _health(**overrides: Any) -> DatabaseRoleHealth:
    base = dict(
        available=True,
        role="crawmatic_app",
        is_superuser=False,
        bypasses_rls=False,
        owned_public_tables=0,
        system_role="crawmatic_auth",
        system_is_superuser=False,
        system_bypasses_rls=True,
    )
    base.update(overrides)
    return DatabaseRoleHealth(**base)


def test_production_shape_is_quiet() -> None:
    """Tenant role confined, privileged session BYPASSRLS: exactly as designed."""
    health = _health()
    assert health.rls_effective is True
    assert health.system_role_status == "expected_privileged"
    fired = _ids(evaluate(_snap(health)))
    assert "security.rls_inert" not in fired
    assert "security.privileged_role_misconfigured" not in fired


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_superuser": True},
        {"bypasses_rls": True},
        {"owned_public_tables": 42},
    ],
)
def test_an_unconfined_tenant_role_is_critical(overrides: dict[str, Any]) -> None:
    fired = _ids(evaluate(_snap(_health(**overrides))))
    alert = fired["security.rls_inert"]
    assert alert.severity is Severity.CRITICAL
    assert alert.observed["role"] == "crawmatic_app"


def test_a_probe_that_could_not_run_is_critical() -> None:
    """No verdict is not a pass: the tenant role is unverified."""
    health = DatabaseRoleHealth(
        available=False, unavailable_reason="OperationalError: connection refused"
    )
    alert = _ids(evaluate(_snap(health)))["security.rls_inert"]
    assert alert.severity is Severity.CRITICAL
    assert "connection refused" in alert.observed["unavailable_reason"]


def test_a_snapshot_that_never_collected_the_section_does_not_alert() -> None:
    """The dataclass default (no reason) is 'not collected here', e.g. a
    unit-test snapshot -- only a probe that RAN and failed is critical."""
    assert "security.rls_inert" not in _ids(evaluate(OpsSnapshot(collected_at=NOW)))


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"system_is_superuser": True}, "superuser"),
        ({"system_role": "crawmatic_app"}, "same_as_tenant"),
    ],
)
def test_a_misconfigured_privileged_role_alerts_high(
    overrides: dict[str, Any], status: str
) -> None:
    health = _health(**overrides)
    assert health.system_role_status == status
    alert = _ids(evaluate(_snap(health)))["security.privileged_role_misconfigured"]
    assert alert.severity is Severity.HIGH
    assert alert.observed["status"] == status


def test_severity_vocabulary_stays_closed() -> None:
    """The SaaS monitor greps `worst_severity` and treats CRITICAL|HIGH as
    failing; the vocabulary it knows is CRITICAL|HIGH|WARNING|null."""
    alerts = evaluate(_snap(_health(is_superuser=True, system_is_superuser=True)))
    assert {a.as_dict()["severity"] for a in alerts} <= {"CRITICAL", "HIGH", "WARNING"}
    assert str(worst_severity(alerts)) == "CRITICAL"
    assert all(a.rule_id for a in alerts)


# --- the collector ---------------------------------------------------------


class _Session:
    def __init__(self, row: tuple | None) -> None:
        self.row = row

    def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(first=lambda: self.row)


def test_the_collector_probes_the_tenant_engine_not_the_session(monkeypatch) -> None:
    probed: list[Any] = []
    tenant_engine = object()

    def fake_inspect(bind: Any) -> OrdinaryRoleFacts:
        probed.append(bind)
        return OrdinaryRoleFacts(
            role_name="crawmatic_app",
            is_superuser=False,
            has_bypassrls=False,
            owned_public_tables=0,
            rls_without_force=0,
        )

    monkeypatch.setattr(snapshot_mod, "inspect_ordinary_role", fake_inspect)
    health = snapshot_mod._collect_db_role(
        _Session(("crawmatic_auth", False, True)), tenant_bind=tenant_engine
    )

    assert probed == [tenant_engine]
    assert health.role == "crawmatic_app"
    assert health.rls_effective is True
    assert health.system_role == "crawmatic_auth"
    assert health.system_role_status == "expected_privileged"


def test_without_a_tenant_engine_the_collector_reports_unverified() -> None:
    health = snapshot_mod._collect_db_role(_Session(("crawmatic_auth", False, True)), tenant_bind=None)
    assert health.available is False
    assert "tenant" in (health.unavailable_reason or "")
    assert _ids(evaluate(_snap(health)))["security.rls_inert"].severity is Severity.CRITICAL


def test_collect_snapshot_threads_the_tenant_engine(monkeypatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        snapshot_mod,
        "_collect_db_role",
        lambda session, tenant_bind=None: seen.append(tenant_bind) or DatabaseRoleHealth(available=True),
    )
    marker = object()
    snapshot_mod.collect_snapshot(_Session(None), now=NOW, tenant_bind=marker)
    assert seen == [marker]


@pytest.mark.parametrize(
    "path",
    [
        "apps/api/app/routers/ops_metrics.py",
        "apps/api/app/routers/admin_ops.py",
        "apps/scheduler/app/scheduler/scheduler_app.py",
    ],
)
def test_every_snapshot_caller_passes_the_tenant_engine(path: str) -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / path).read_text()
    calls = text.count("collect_snapshot(")
    assert calls >= 1
    # Passed as the function (resolved inside the section, so an engine that
    # cannot be built is reported rather than raised).
    assert text.count("tenant_bind=get_engine,") >= calls - text.count("def collect_snapshot(")


def test_an_engine_that_cannot_be_built_is_reported_not_raised() -> None:
    def broken_engine() -> Any:
        raise RuntimeError("settings do not validate")

    health = snapshot_mod._section(
        lambda: snapshot_mod._collect_db_role(
            _Session(("crawmatic_auth", False, True)), tenant_bind=broken_engine
        ),
        lambda r: DatabaseRoleHealth(available=False, unavailable_reason=r),
    )
    assert health.available is False
    assert "settings do not validate" in health.unavailable_reason
