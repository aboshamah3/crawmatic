"""Offline migration render tests for EPA C9's two revisions (F14):
`d1f7a3c9e284` (`network_operation_resource_summaries`) and
`a5e0c74b13d9` (the `network_operations` partition swap).

The repo's standard guard for a new revision (the
`test_migration_offline_rollup_completion.py` precedent): run `alembic
upgrade head --sql` offline — no DB connection — and assert the rendered
DDL, the single head, and the chain.

The swap migration in particular has to be offline-renderable *and*
data-driven, which is why everything data-dependent inside it is a
PL/pgSQL `DO` block: a `DO` block is one statement, so it renders
verbatim offline and does its work server-side online. Asserting that
here is the thing that stops someone "simplifying" it into Python that
queries the connection and silently breaks `--sql` mode for the whole
repository — offline render is how a DBA reviews a migration before it
touches production, and this is the migration most in need of that.
"""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SUMMARIES_REVISION = "d1f7a3c9e284"
SWAP_REVISION = "a5e0c74b13d9"
#: The head each chains from: C7's `rollup_completion`, then the summary table.
SUMMARIES_DOWN_REVISION = "b3f0c95a7d21"
SWAP_DOWN_REVISION = SUMMARIES_REVISION


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


@lru_cache(maxsize=1)
def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, (
        "alembic upgrade head --sql failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


# --- chain / head -----------------------------------------------------------


def test_single_head() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected exactly one head, got: {heads}"
    assert SWAP_REVISION in heads[0]


def test_revisions_chain_in_the_declared_order() -> None:
    module_dir = REPO_ROOT / "alembic" / "versions"
    summaries = (
        module_dir / f"{SUMMARIES_REVISION}_network_operation_resource_summaries.py"
    ).read_text()
    swap = (module_dir / f"{SWAP_REVISION}_partition_network_operations.py").read_text()
    assert f"down_revision: Union[str, Sequence[str], None] = '{SUMMARIES_DOWN_REVISION}'" in summaries
    assert f"down_revision: Union[str, Sequence[str], None] = '{SWAP_DOWN_REVISION}'" in swap


# --- d1f7a3c9e284: the summary table ---------------------------------------


def test_summary_table_is_rendered() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE network_operation_resource_summaries" in sql


def test_summary_table_has_the_idempotence_key() -> None:
    """`UNIQUE (parent_operation_id)` is what makes summarise-then-delete
    idempotent — without it a re-run double-counts instead of conflicting."""
    assert "uq_nors_parent_operation_id" in _upgrade_sql()


def test_summary_table_carries_no_foreign_key_to_the_ledger() -> None:
    """A summary must outlive a parent whose partition is eventually
    dropped at 730 days; a cascade would delete the compressed evidence
    at the moment it becomes the only evidence left."""
    sql = _upgrade_sql()
    start = sql.index("CREATE TABLE network_operation_resource_summaries")
    body = sql[start : sql.index(";", start)]
    assert "REFERENCES" not in body.upper()


def test_bytes_map_is_jsonb_with_an_empty_default() -> None:
    sql = _upgrade_sql()
    start = sql.index("CREATE TABLE network_operation_resource_summaries")
    body = sql[start : sql.index(";", start)]
    assert "bytes_by_host_class JSONB" in body
    assert "'{}'::jsonb" in body


# --- a5e0c74b13d9: the swap -------------------------------------------------


def test_swap_creates_a_range_partitioned_shadow_table() -> None:
    sql = _upgrade_sql()
    assert "CREATE TABLE network_operations_p" in sql
    assert "PARTITION BY RANGE (created_at)" in sql


def test_swap_copies_the_shape_from_the_live_catalog_not_a_column_list() -> None:
    """`LIKE network_operations INCLUDING ...` means a column added by
    some later migration cannot be silently dropped by this one."""
    sql = _upgrade_sql()
    assert "LIKE network_operations" in sql
    assert "INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING COMMENTS" in sql


def test_swap_unique_key_includes_the_partition_key() -> None:
    """Postgres requires it, and it is the reason the five foreign keys
    below are dropped."""
    assert (
        "UNIQUE (network_request_id, created_at)" in _upgrade_sql()
    )


def test_swap_copies_in_keyset_batches_not_one_statement() -> None:
    sql = _upgrade_sql()
    assert "(created_at, id) > (v_prev_ts, v_prev_id)" in sql
    assert "LIMIT 50000" in sql


def test_swap_aborts_on_a_short_copy() -> None:
    """The one failure mode that would otherwise be discovered months
    later: a copy that silently moved fewer rows than it read."""
    sql = _upgrade_sql()
    assert "partition copy is short" in sql


def test_swap_drops_exactly_the_five_unrepresentable_foreign_keys() -> None:
    sql = _upgrade_sql()
    # Name presence and drop COUNT are asserted separately: the longest
    # of the five wraps onto its own line in the rendered SQL, so a
    # single contiguous "DROP CONSTRAINT IF EXISTS <name>" match would
    # depend on the source file's line wrapping rather than on the DDL.
    for constraint in (
        "fk_noa_operation_id_network_operations",
        "fk_nos_operation_id_network_operations",
        "fk_request_attempts_network_operation_id_network_operations",
        "fk_no_retry_parent_id_network_operations",
        "fk_no_parent_operation_id_network_operations",
    ):
        assert constraint in sql, constraint
    assert sql.count("DROP CONSTRAINT IF EXISTS") == 5


def test_swap_keeps_the_legacy_table_rather_than_dropping_it() -> None:
    """Dropping it is an owner step. It is the only rollback that does
    not require re-copying, and disk is the cheaper of the two risks."""
    sql = _upgrade_sql()
    assert "RENAME TO network_operations_pre_partition" in sql
    assert "DROP TABLE network_operations_pre_partition" not in sql


def test_swap_recreates_the_immutability_trigger_on_the_new_parent() -> None:
    """A row trigger on a partitioned table propagates to every child —
    but only if it is actually there."""
    sql = _upgrade_sql()
    assert "CREATE TRIGGER trg_network_operations_immutable" in sql
    assert "BEFORE UPDATE ON network_operations" in sql


def test_swap_runs_the_partition_rls_guard() -> None:
    """A no-op for this fleet-owned table, issued anyway so "a partition
    was created, the guard ran" stays true at every seam that creates one."""
    assert "child.relispartition" in _upgrade_sql()


def test_swap_is_idempotent_against_an_already_partitioned_parent() -> None:
    assert _upgrade_sql().count("is already partitioned") >= 2


def test_everything_data_dependent_is_a_do_block() -> None:
    """Offline-renderable AND data-driven. If this ever becomes Python
    that queries the connection, `--sql` mode breaks for the whole
    repository — and offline render is how this migration gets reviewed
    before it touches production."""
    sql = _upgrade_sql()
    start = sql.index("CREATE TABLE network_operations_p")
    assert "DO $$" in sql[:start]
