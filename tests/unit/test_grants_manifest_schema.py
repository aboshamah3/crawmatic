"""Schema tests for `scripts/sql/grants_expected.yaml` (EPA A3/F03).

Pure/off-database: parses the YAML file and the plain-text table manifest
it must cover, and asserts the shape the packet's acceptance criteria
name — no live Postgres involved (that is
`tests/integration/test_grants_manifest.py`'s job).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

GRANTS_PATH = REPO_ROOT / "scripts" / "sql" / "grants_expected.yaml"
RLS_MANIFEST_PATH = REPO_ROOT / "scripts" / "rls_table_manifest.txt"

VALID_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}


def _load_grants() -> dict[str, dict[str, list[str]]]:
    return yaml.safe_load(GRANTS_PATH.read_text())


def _rls_table_names() -> set[str]:
    names: set[str] = set()
    for raw_line in RLS_MANIFEST_PATH.read_text().splitlines():
        line = raw_line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        table = line.split("\t", 1)[0].strip()
        if table:
            names.add(table)
    return names


def test_grants_file_parses_as_a_mapping_of_roles() -> None:
    data = _load_grants()
    assert isinstance(data, dict)
    assert set(data) == {"crawmatic_app", "crawmatic_auth", "crawmatic_scraper"}


@pytest.mark.parametrize("role", ["crawmatic_app", "crawmatic_auth", "crawmatic_scraper"])
def test_every_rls_table_appears_exactly_once_per_role(role: str) -> None:
    data = _load_grants()
    rls_tables = _rls_table_names()
    role_tables = data[role]

    assert isinstance(role_tables, dict)
    # No duplicates: a YAML mapping already collapses duplicate keys, so
    # the real assertion is that the KEY SET matches exactly -- neither a
    # table missing from this role's mapping nor an extra one that is not
    # in the reviewed rls_table_manifest.txt inventory.
    missing = rls_tables - set(role_tables)
    extra = set(role_tables) - rls_tables
    assert not missing, f"{role} is missing an explicit entry for: {sorted(missing)}"
    assert not extra, f"{role} has entries for tables absent from rls_table_manifest.txt: {sorted(extra)}"


@pytest.mark.parametrize("role", ["crawmatic_app", "crawmatic_auth", "crawmatic_scraper"])
def test_every_privilege_list_is_explicit_never_implicit_all(role: str) -> None:
    data = _load_grants()
    for table, privileges in data[role].items():
        assert isinstance(privileges, list), (
            f"{role}.{table} must be an explicit list, got {type(privileges)!r}"
        )
        assert privileges != ["ALL"], f"{role}.{table} uses implicit ALL, not an explicit set"
        for priv in privileges:
            assert priv in VALID_PRIVILEGES, (
                f"{role}.{table} names unknown privilege {priv!r} "
                f"(must be one of {sorted(VALID_PRIVILEGES)})"
            )
        # No duplicate privileges within one table's list.
        assert len(privileges) == len(set(privileges)), (
            f"{role}.{table} lists a duplicate privilege: {privileges}"
        )


def test_alembic_version_is_read_only_for_every_role() -> None:
    """Matches the long-standing convention: only crawmatic_migrate writes it."""
    data = _load_grants()
    for role in ("crawmatic_app", "crawmatic_auth", "crawmatic_scraper"):
        assert data[role]["alembic_version"] in ([], ["SELECT"]), (
            f"{role}.alembic_version must be read-only or empty, "
            f"got {data[role]['alembic_version']}"
        )


def test_scraper_has_nothing_on_budgets_entitlements_or_users() -> None:
    """Acceptance criterion (A3, verbatim): 'nothing on budgets/entitlements/users'."""
    data = _load_grants()
    scraper = data["crawmatic_scraper"]
    for table in (
        "cost_budgets",
        "cost_reservations",
        "fleet_cost_budgets",
        "workspace_entitlements",
        "users",
    ):
        assert scraper[table] == [], f"crawmatic_scraper.{table} must be empty, got {scraper[table]}"


def test_scraper_gets_exactly_the_named_ingestion_grants() -> None:
    scraper = _load_grants()["crawmatic_scraper"]
    assert scraper["request_attempts"] == ["INSERT"]
    assert scraper["price_observations"] == ["INSERT"]
    assert scraper["network_operations"] == ["INSERT"]
    assert scraper["scrape_job_targets"] == ["UPDATE"]
    assert scraper["domain_strategy_profiles"] == ["SELECT"]
    assert scraper["scrape_profiles"] == ["SELECT"]
    assert scraper["competitor_product_matches"] == ["SELECT"]


def test_auth_identity_tables_are_select_only_except_refresh_tokens() -> None:
    auth = _load_grants()["crawmatic_auth"]
    for table in ("users", "api_keys", "proxy_providers", "webhook_endpoints"):
        assert auth[table] == ["SELECT"], f"crawmatic_auth.{table} should be SELECT-only"
    # refresh_tokens is a documented, evidenced exception (issue/rotate/
    # revoke run on the same auth seam) -- not SELECT-only.
    assert "UPDATE" in auth["refresh_tokens"]


def test_auth_has_no_update_on_products() -> None:
    auth = _load_grants()["crawmatic_auth"]
    assert "UPDATE" not in auth["products"]


def test_app_has_no_update_on_fleet_tables_except_the_evidenced_exception() -> None:
    """Acceptance criterion (A3, verbatim): 'crawmatic_app has no UPDATE on
    fleet tables' -- fleet tables are the SYSTEM/GAP/TENANT_ROOT-classed
    (non-workspace) tables in rls_table_manifest.txt. `api_abuse_limit_
    counters` is a documented, evidenced exception: its ON CONFLICT DO
    UPDATE upsert runs on the ordinary crawmatic_app session because the
    abuse limiter must work before authentication.
    """
    app = _load_grants()["crawmatic_app"]
    fleet_tables = {
        "_smoke_foundation",
        "domain_lifecycle_audit",
        "domain_playbooks",
        "fleet_cost_budgets",
        "fleet_network_cost_rollups",
        "maintenance_cadences",
        "proxy_circuit_breakers",
        "rollup_watermarks",
        "workspaces",
        "network_operations",
        "network_operation_settlements",
        "provider_usage_records",
    }
    for table in fleet_tables:
        assert "UPDATE" not in app[table], f"crawmatic_app.{table} must not have UPDATE"
    assert "UPDATE" in app["api_abuse_limit_counters"]
