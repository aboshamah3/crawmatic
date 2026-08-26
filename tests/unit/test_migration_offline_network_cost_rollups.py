"""Offline migration render tests for EPA C6/W4.2's three new revisions.

Mirrors `test_migration_offline_offer_observation.py`/`test_migration_
offline_rollups.py`: runs alembic `--sql` (offline, no DB connection)
via subprocess and asserts what each migration actually renders.

Covers, in chain order:

* `ccea48aab7b1` (network_cost_rollups, EPA C6) — both tables, the
  bounded top-N + "other" cardinality contract has no schema shape of
  its own to assert here (it's a property of the JOB, proven in
  `tests/unit/netledger/test_rollups.py`), but RLS on
  `network_cost_rollups` ONLY (never `fleet_network_cost_rollups`) does.
* `42bb8b878fe9` (refresh_rules retry-ledger columns, EPA W4.2 owed
  revision) — the exact four `ADD COLUMN` statements + partial index
  from the plan text, and upgrade/downgrade symmetry.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

C6_REVISION = "ccea48aab7b1"
C6_DOWN_REVISION = "7c2b9e5a41d6"
W42_REVISION = "42bb8b878fe9"
W42_DOWN_REVISION = C6_REVISION


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _upgrade_sql(down: str, rev: str) -> str:
    result = _run_alembic("upgrade", f"{down}:{rev}", "--sql")
    assert result.returncode == 0, (
        f"alembic upgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def _downgrade_sql(rev: str, down: str) -> str:
    result = _run_alembic("downgrade", f"{rev}:{down}", "--sql")
    assert result.returncode == 0, (
        f"alembic downgrade --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


class TestSingleHead:
    def test_alembic_heads_reports_exactly_one_head(self) -> None:
        result = _run_alembic("heads")
        assert result.returncode == 0, result.stderr
        heads = [line for line in result.stdout.splitlines() if "(head)" in line]
        assert len(heads) == 1, result.stdout
        # W42_REVISION need not be the CURRENT global head -- later EPA
        # lane holders land further revisions on top of it (e.g.
        # e92029e9902c, EPA W5.5-L1 item 2). What must remain true is that
        # it still sits on the single linear chain leading to whatever the
        # current head is: `test_down_revision_chain_is_c5_to_c6_to_w42`
        # proves the chain BELOW w42 (its own down_revision); this proves
        # the chain does not fork ABOVE it either, by walking `alembic
        # history` through w42 on the way to the tip.
        history = _run_alembic("history")
        assert history.returncode == 0, history.stderr
        assert any(
            line.startswith(f"{W42_DOWN_REVISION} -> {W42_REVISION}")
            for line in history.stdout.splitlines()
        ), history.stdout

    def test_down_revision_chain_is_c5_to_c6_to_w42(self) -> None:
        c6_source = (
            REPO_ROOT / "alembic" / "versions" / f"{C6_REVISION}_network_cost_rollups.py"
        ).read_text()
        assert f"down_revision: Union[str, Sequence[str], None] = '{C6_DOWN_REVISION}'" in c6_source

        w42_source = (
            REPO_ROOT
            / "alembic"
            / "versions"
            / f"{W42_REVISION}_refresh_rules_retry_ledger_columns.py"
        ).read_text()
        assert f"down_revision: Union[str, Sequence[str], None] = '{W42_DOWN_REVISION}'" in w42_source


class TestNetworkCostRollupsMigration:
    def test_upgrade_renders_both_tables(self) -> None:
        sql = _upgrade_sql(C6_DOWN_REVISION, C6_REVISION)
        assert "CREATE TABLE fleet_network_cost_rollups" in sql
        assert "CREATE TABLE network_cost_rollups" in sql

    def test_upgrade_renders_rls_on_the_workspace_table_only(self) -> None:
        sql = _upgrade_sql(C6_DOWN_REVISION, C6_REVISION)
        assert "ALTER TABLE network_cost_rollups ENABLE ROW LEVEL SECURITY" in sql
        assert "ALTER TABLE network_cost_rollups FORCE ROW LEVEL SECURITY" in sql
        assert "CREATE POLICY network_cost_rollups_workspace_isolation" in sql
        # The fleet-scoped sibling gets NO RLS at all -- same contrast as
        # network_operations (no RLS) vs network_operation_allocations
        # (RLS) in c4b19e7a2f08.
        assert "fleet_network_cost_rollups ENABLE ROW LEVEL SECURITY" not in sql
        assert "fleet_network_cost_rollups FORCE ROW LEVEL SECURITY" not in sql
        assert "fleet_network_cost_rollups_workspace_isolation" not in sql

    def test_upgrade_renders_the_workspace_fk_and_both_unique_constraints(self) -> None:
        sql = _upgrade_sql(C6_DOWN_REVISION, C6_REVISION)
        assert "fk_ncr_workspace_id_workspaces" in sql
        assert "uq_ncr_workspace_date_domain_method_version" in sql
        assert "uq_fncr_date_domain_method_version_currency" in sql

    def test_the_dimension_columns_are_method_and_profile_version(self) -> None:
        """EPA Phase C F4: the "method" dimension is the operation's
        TRANSPORT, not the HTTP verb, so the column is `method`.

        A column named `http_method` holding `PROXY` would be a lie in the
        schema itself, and the schema is the part an operator reads
        without the docstring. Both tables carry the same two dimension
        columns, and neither carries the old name."""
        sql = _upgrade_sql(C6_DOWN_REVISION, C6_REVISION)
        for table in ("fleet_network_cost_rollups", "network_cost_rollups"):
            start = sql.index(f"CREATE TABLE {table}")
            create_stmt = sql[start : sql.index(");", start)]
            assert "method TEXT NOT NULL" in create_stmt, table
            assert "profile_version TEXT DEFAULT '' NOT NULL" in create_stmt, table
            assert "http_method" not in create_stmt, table

    def test_downgrade_drops_both_tables(self) -> None:
        sql = _downgrade_sql(C6_REVISION, C6_DOWN_REVISION)
        assert "DROP TABLE network_cost_rollups" in sql
        assert "DROP TABLE fleet_network_cost_rollups" in sql


class TestRefreshRulesRetryLedgerColumnsMigration:
    _EXPECTED_ADD_COLUMNS = (
        "consecutive_failures",
        "last_failure_at",
        "last_failure_error",
        "dead_lettered_at",
    )

    def test_upgrade_renders_exactly_the_four_plan_columns(self) -> None:
        sql = _upgrade_sql(W42_DOWN_REVISION, W42_REVISION)
        added = re.findall(r"ALTER TABLE refresh_rules ADD COLUMN (\w+)", sql)
        assert added == list(self._EXPECTED_ADD_COLUMNS), added

    def test_upgrade_renders_the_partial_dead_lettered_index(self) -> None:
        sql = _upgrade_sql(W42_DOWN_REVISION, W42_REVISION)
        assert (
            "CREATE INDEX ix_refresh_rules_dead_lettered ON refresh_rules "
            "(dead_lettered_at) WHERE dead_lettered_at IS NOT NULL" in sql
        )

    def test_consecutive_failures_defaults_to_zero_not_null(self) -> None:
        sql = _upgrade_sql(W42_DOWN_REVISION, W42_REVISION)
        assert "consecutive_failures INTEGER DEFAULT '0' NOT NULL" in sql

    def test_downgrade_drops_the_same_four_columns_in_reverse(self) -> None:
        sql = _downgrade_sql(W42_REVISION, W42_DOWN_REVISION)
        dropped = re.findall(r"ALTER TABLE refresh_rules DROP COLUMN (\w+)", sql)
        assert dropped == list(reversed(self._EXPECTED_ADD_COLUMNS)), dropped

    def test_downgrade_drops_the_index_before_the_columns(self) -> None:
        sql = _downgrade_sql(W42_REVISION, W42_DOWN_REVISION)
        index_pos = sql.index("DROP INDEX ix_refresh_rules_dead_lettered")
        first_column_drop_pos = sql.index("ALTER TABLE refresh_rules DROP COLUMN")
        assert index_pos < first_column_drop_pos
