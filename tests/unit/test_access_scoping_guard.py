"""Access/proxy CI scoping-guard unit tests (SPEC-10 T014, FR-006, SC-005).

Mirrors `tests/unit/test_alerts_scoping_guard.py` (SPEC-09), but for the
mixed-scope SPEC-10 models: `DomainAccessRule` is tenant-only and IS
guarded by `scripts/check_workspace_scoping.py` (registered in
`WORKSPACE_OWNED_MODELS`); `ProxyProvider`/`AccessPolicy` are dual-scope
and are **intentionally** absent from the guarded set — they are queried
through `app_shared.access.repository`, whose `visible_*` selects would
be flagged as "unscoped" by this guard if it covered them (it must not).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.check_workspace_scoping as guard  # noqa: E402

_DOMAIN_RULE_VIOLATION_SOURCE = '''\
"""A fixture module planting unscoped DomainAccessRule access
(a scoped_get/scoped_select omission)."""
from sqlalchemy import select

from app_shared.models.access import DomainAccessRule


def unscoped_get_by_id(session, id_):
    return session.get(DomainAccessRule, id_)


def unscoped_select_domain_access_rules():
    return select(DomainAccessRule)
'''

_DOMAIN_RULE_CLEAN_SOURCE = '''\
"""A fixture module using only sanctioned, scoped domain-access-rule
access patterns."""
from sqlalchemy import select

from app_shared.models.access import DomainAccessRule
from app_shared.repository import scoped_select


def scoped_by_helper(workspace_id):
    return scoped_select(DomainAccessRule, workspace_id)


def scoped_by_explicit_where(workspace_id):
    return select(DomainAccessRule).where(DomainAccessRule.workspace_id == workspace_id)
'''

# ProxyProvider/AccessPolicy dual-scope "visible_*" selects have NO
# workspace_id predicate chained (they OR in a global NULL row) — this
# is legitimate and must NOT be flagged, since these models are not in
# WORKSPACE_OWNED_MODELS.
_DUAL_SCOPE_UNGUARDED_SOURCE = '''\
"""A fixture module using the dual-scope access.repository pattern —
must NOT be flagged even though it has no workspace_id-scoped select
chained (ProxyProvider/AccessPolicy are excluded from the guarded set)."""
from sqlalchemy import or_, select

from app_shared.models.access import AccessPolicy, ProxyProvider


def visible_providers_select(workspace_id):
    return select(ProxyProvider).where(
        or_(ProxyProvider.workspace_id == workspace_id, ProxyProvider.workspace_id.is_(None))
    )


def unscoped_select_proxy_provider():
    return select(ProxyProvider)


def unscoped_select_access_policy():
    return select(AccessPolicy)
'''


def _write_fixture(tmp_path: Path, source: str, filename: str = "sample_module.py") -> Path:
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = apps_dir / filename
    fixture_path.write_text(source, encoding="utf-8")
    return fixture_path


# --- real repo tree: exits 0 -------------------------------------------------


def test_guard_exits_0_on_the_real_repo_tree() -> None:
    """The guard's real `apps/`+`libs/` scan (this repo) is clean."""
    assert guard.main([]) == 0


def test_guard_scan_finds_no_violations_on_the_real_repo_tree() -> None:
    violations = guard.scan()
    assert violations == [], "\n".join(str(v) for v in violations)


def test_domain_access_rule_is_registered_as_workspace_owned() -> None:
    from app_shared.models.access import DomainAccessRule
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    assert DomainAccessRule in WORKSPACE_OWNED_MODELS


def test_proxy_provider_and_access_policy_are_not_registered_as_workspace_owned() -> None:
    from app_shared.models.access import AccessPolicy, ProxyProvider
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    assert ProxyProvider not in WORKSPACE_OWNED_MODELS
    assert AccessPolicy not in WORKSPACE_OWNED_MODELS


# --- planted unscoped select(...) / session.get(...) on DomainAccessRule ----


def test_guard_flags_planted_unscoped_select_and_session_get_on_domain_access_rule(
    tmp_path: Path,
) -> None:
    _write_fixture(tmp_path, _DOMAIN_RULE_VIOLATION_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert len(violations) == 2
    reasons = "\n".join(v.reason for v in violations)
    assert "DomainAccessRule" in reasons
    assert "get(DomainAccessRule" in reasons or "get(DomainAccessRule, ...)" in reasons
    assert "select(DomainAccessRule)" in reasons


def test_guard_passes_clean_scoped_domain_access_rule_snippets(tmp_path: Path) -> None:
    _write_fixture(tmp_path, _DOMAIN_RULE_CLEAN_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert violations == []


def test_guard_main_exits_non_zero_on_planted_domain_access_rule_violation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    violation_root = tmp_path / "violation"
    _write_fixture(violation_root, _DOMAIN_RULE_VIOLATION_SOURCE)

    original_scan = guard.scan
    monkeypatch.setattr(guard, "scan", lambda: original_scan(repo_root=violation_root))

    assert guard.main([]) != 0
    out = capsys.readouterr().out
    assert "select(DomainAccessRule)" in out


# --- ProxyProvider/AccessPolicy are intentionally NOT guarded here ----------


def test_guard_does_not_flag_unscoped_proxy_provider_or_access_policy_selects(
    tmp_path: Path,
) -> None:
    """Dual-scope `ProxyProvider`/`AccessPolicy` selects are out of this
    guard's remit — even a bare `select(ProxyProvider)` (which a workspace-
    owned model would trip) must pass clean, since these two models are
    queried through `app_shared.access.repository`, not `scoped_select`."""
    _write_fixture(tmp_path, _DUAL_SCOPE_UNGUARDED_SOURCE)

    violations = guard.scan(repo_root=tmp_path)

    assert violations == []
