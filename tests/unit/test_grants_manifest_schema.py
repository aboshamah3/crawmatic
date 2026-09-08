"""Schema tests for `scripts/sql/grants_expected.yaml` (EPA A3/F03).

Pure/off-database: parses the YAML file, the plain-text table manifest
it must cover, and the provisioning SQL that must APPLY it, and asserts
the shape the packet's acceptance criteria name — no live Postgres
involved (that is `tests/integration/test_grants_manifest.py`'s job).

Three files carry the same privilege inventory and must agree:

    scripts/rls_table_manifest.txt    the reviewed table inventory
    scripts/sql/grants_expected.yaml  the reviewed per-role privileges
    scripts/provision_db_roles.sql    the DDL that grants them

The last section of this module (added by EPA B2-fix1, after the B10
release rehearsal found the provisioning SQL three tables behind)
closes the loop on the third file.
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


# ---------------------------------------------------------------------
# provision_db_roles.sql must APPLY exactly what grants_expected.yaml
# REVIEWS.
#
# `scripts/provision_db_roles.sql` carries the same information a third
# time, as hand-written `FOREACH tbl IN ARRAY ARRAY[...]` literals
# grouped by privilege set -- and nothing used to check the three copies
# against each other. The EPA B10 release rehearsal found the cost of
# that: Stage B added three tables (`domain_rules`,
# `refresh_rule_occurrences`, `strategy_discovery_state`) to
# `rls_table_manifest.txt` and `grants_expected.yaml`, the arrays in the
# provisioning SQL were never touched, and a clean migrate therefore
# left `scripts/verify_grants.py` reporting 17 MISSING grants -- the new
# tables were unreadable and unwritable by every application role the
# moment the migration landed.
#
# These tests make the SQL file's arrays a DERIVED artifact in practice:
# the manifest stays the single reviewed source, and any table added to
# it without a matching entry in the provisioning SQL fails the unit
# gate rather than a post-deploy verifier.
# ---------------------------------------------------------------------

PROVISION_SQL_PATH = REPO_ROOT / "scripts" / "provision_db_roles.sql"


def _provisioned_grants() -> dict[str, dict[str, list[str]]]:
    """Parse `provision_db_roles.sql`'s grant loops into role -> table ->
    sorted privileges.

    Each loop opens with a `FOREACH` over an array literal of table
    names, then a `REVOKE ALL ... FROM <role>` (which names the role)
    and either a `GRANT <privs> ON TABLE %I TO <role>` or no GRANT at
    all (the deliberate empty-privilege groups).

    `--` comment lines are stripped first: this file's own prose
    describes the loop shape, and a comment must never be parsed as a
    grant.
    """
    import re

    sql = "\n".join(
        line
        for line in PROVISION_SQL_PATH.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    parsed: dict[str, dict[str, list[str]]] = {}
    for chunk in sql.split("FOREACH tbl IN ARRAY ARRAY[")[1:]:
        array_literal, rest = chunk.split("] LOOP", 1)
        rest = rest.split("FOREACH tbl IN ARRAY")[0]
        revoke = re.search(r"REVOKE ALL ON TABLE %I FROM (\w+)", rest)
        if not revoke:  # a loop that is not a per-table grant loop
            continue
        role = revoke.group(1)
        grant = re.search(r"GRANT ([A-Z, ]+) ON TABLE %I TO (\w+)", rest)
        privileges = (
            sorted(p.strip() for p in grant.group(1).split(",")) if grant else []
        )
        role_tables = parsed.setdefault(role, {})
        for table in re.findall(r"'([^']+)'", array_literal):
            assert table not in role_tables, (
                f"{table} appears in two grant loops for {role} in "
                f"provision_db_roles.sql -- the later REVOKE/GRANT pair "
                f"silently wins"
            )
            role_tables[table] = privileges
    return parsed


@pytest.mark.parametrize("role", ["crawmatic_app", "crawmatic_auth", "crawmatic_scraper"])
def test_provisioning_sql_covers_every_table_the_manifest_reviews(role: str) -> None:
    expected = _load_grants()[role]
    applied = _provisioned_grants().get(role, {})

    missing = sorted(set(expected) - set(applied))
    extra = sorted(set(applied) - set(expected))
    assert not missing, (
        f"scripts/provision_db_roles.sql never grants {role} anything on: "
        f"{missing}. Add each table to the ARRAY[...] of the loop whose "
        f"GRANT matches its privilege set in grants_expected.yaml "
        f"(verify_grants.py reports these as MISSING after a clean migrate)."
    )
    assert not extra, (
        f"scripts/provision_db_roles.sql names tables for {role} that "
        f"grants_expected.yaml does not review: {extra}"
    )


@pytest.mark.parametrize("role", ["crawmatic_app", "crawmatic_auth", "crawmatic_scraper"])
def test_provisioning_sql_applies_exactly_the_reviewed_privileges(role: str) -> None:
    expected = _load_grants()[role]
    applied = _provisioned_grants()[role]
    mismatched = {
        table: (sorted(expected[table] or []), applied[table])
        for table in sorted(set(expected) & set(applied))
        if sorted(expected[table] or []) != applied[table]
    }
    assert not mismatched, (
        f"provision_db_roles.sql applies privileges for {role} that differ "
        f"from grants_expected.yaml (table: (expected, applied)): {mismatched}"
    )
