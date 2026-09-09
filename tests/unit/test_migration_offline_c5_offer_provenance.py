"""Offline migration render tests for EPA C5's two revisions.

Mirrors `tests/unit/test_migration_offline_network_cost_rollups.py`: runs
alembic `--sql` (offline, no DB connection) via subprocess and asserts
what each migration actually renders, in chain order:

* `c1e7b93a40df` — `extraction_shadow_events`, the fleet-scoped table
  the ranker's shadow disagreements land in. The assertions that matter
  are the NEGATIVE ones: no `workspace_id`, no RLS, and no `CHECK` on
  the two vocabulary columns — each is a deliberate classification the
  revision's docstring argues for, and each would be silently
  reintroduced by a future autogenerate.
* `e2a4f80c6b71` — the five `price_observations` provenance columns.
  `price_observations` is PARTITIONED, so the assertion with teeth is
  that every statement is a bare `ADD COLUMN` with no default and no
  constraint: anything else recurses into every monthly partition and
  takes a long lock on the hottest write path in the system.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

C5_SHADOW_REVISION = "c1e7b93a40df"
C5_SHADOW_DOWN_REVISION = "a7c31d0f9e42"
C5_PROVENANCE_REVISION = "e2a4f80c6b71"


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


class TestChain:
    def test_alembic_heads_reports_exactly_one_head(self) -> None:
        result = _run_alembic("heads")
        assert result.returncode == 0, result.stderr
        heads = [line for line in result.stdout.splitlines() if "(head)" in line]
        assert len(heads) == 1, result.stdout

    def test_the_two_revisions_are_chained_one_after_the_other(self) -> None:
        history = _run_alembic("history")
        assert history.returncode == 0, history.stderr
        lines = history.stdout.splitlines()
        assert any(
            line.startswith(f"{C5_SHADOW_DOWN_REVISION} -> {C5_SHADOW_REVISION}")
            for line in lines
        ), history.stdout
        assert any(
            line.startswith(f"{C5_SHADOW_REVISION} -> {C5_PROVENANCE_REVISION}")
            for line in lines
        ), history.stdout


class TestShadowEventsTable:
    def test_it_creates_the_table_with_its_indexes(self) -> None:
        sql = _upgrade_sql(C5_SHADOW_DOWN_REVISION, C5_SHADOW_REVISION)
        assert "CREATE TABLE extraction_shadow_events" in sql
        assert "ix_extraction_shadow_events_observed_at" in sql
        assert "ix_extraction_shadow_events_domain" in sql

    def test_it_has_no_workspace_id_and_no_rls(self) -> None:
        """Fleet-scoped by classification, and the classification has to be
        visible in the DDL or it is only a comment."""
        sql = _upgrade_sql(C5_SHADOW_DOWN_REVISION, C5_SHADOW_REVISION)
        create = sql.split("CREATE TABLE extraction_shadow_events", 1)[1]
        create = create.split(";", 1)[0]
        assert "workspace_id" not in create
        assert "ROW LEVEL SECURITY" not in sql
        assert "CREATE POLICY" not in sql

    def test_the_vocabulary_columns_carry_no_db_check(self) -> None:
        sql = _upgrade_sql(C5_SHADOW_DOWN_REVISION, C5_SHADOW_REVISION)
        assert "CHECK" not in sql

    def test_prices_are_numeric_18_4_never_double_precision(self) -> None:
        sql = _upgrade_sql(C5_SHADOW_DOWN_REVISION, C5_SHADOW_REVISION)
        assert re.search(r"first_hit_price NUMERIC\(18, 4\)", sql), sql
        assert re.search(r"ranked_price NUMERIC\(18, 4\)", sql), sql
        assert "DOUBLE PRECISION" not in sql

    def test_downgrade_drops_what_upgrade_created(self) -> None:
        sql = _downgrade_sql(C5_SHADOW_REVISION, C5_SHADOW_DOWN_REVISION)
        assert "DROP TABLE extraction_shadow_events" in sql
        assert "DROP INDEX ix_extraction_shadow_events_domain" in sql
        assert "DROP INDEX ix_extraction_shadow_events_observed_at" in sql


class TestProvenanceColumns:
    COLUMNS = (
        "extractor_version",
        "profile_version",
        "confidence",
        "provenance",
        "availability_state",
    )

    def test_it_adds_exactly_the_five_named_columns(self) -> None:
        sql = _upgrade_sql(C5_SHADOW_REVISION, C5_PROVENANCE_REVISION)
        for column in self.COLUMNS:
            assert f"ADD COLUMN {column}" in sql, f"{column} missing from:\n{sql}"

    def test_every_added_column_is_nullable_with_no_default(self) -> None:
        """On a PARTITIONED table, a default or a constraint is not a style
        question: `ADD COLUMN` with neither is catalog-only, and anything
        else recurses into every monthly partition."""
        sql = _upgrade_sql(C5_SHADOW_REVISION, C5_PROVENANCE_REVISION)
        for statement in sql.split(";"):
            if "ADD COLUMN" not in statement:
                continue
            assert "NOT NULL" not in statement, statement
            assert "DEFAULT" not in statement, statement
            assert "CHECK" not in statement, statement

    def test_availability_state_is_plain_text_not_an_enum(self) -> None:
        sql = _upgrade_sql(C5_SHADOW_REVISION, C5_PROVENANCE_REVISION)
        assert "ADD COLUMN availability_state TEXT" in sql
        assert "CREATE TYPE" not in sql

    def test_confidence_is_a_numeric_fraction_not_money(self) -> None:
        sql = _upgrade_sql(C5_SHADOW_REVISION, C5_PROVENANCE_REVISION)
        assert "ADD COLUMN confidence NUMERIC(5, 4)" in sql

    def test_no_backfill_touches_existing_rows(self) -> None:
        """`NULL` means 'this row predates the provenance contract', which is
        a more useful statement than a back-filled guess."""
        sql = _upgrade_sql(C5_SHADOW_REVISION, C5_PROVENANCE_REVISION)
        assert "UPDATE price_observations" not in sql

    def test_downgrade_drops_all_five(self) -> None:
        sql = _downgrade_sql(C5_PROVENANCE_REVISION, C5_SHADOW_REVISION)
        for column in self.COLUMNS:
            assert f"DROP COLUMN {column}" in sql, sql
