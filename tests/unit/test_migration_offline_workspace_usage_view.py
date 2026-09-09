"""`workspace_usage_v` must survive the `network_operations` swap (review R13).

The defect
----------
`193ac27f0dc2` creates the tenant-scoped view `workspace_usage_v` over
`network_operations`. `a5e0c74b13d9` then partitions that table by the
standard build-copy-swap: the original relation is renamed to
`network_operations_pre_partition` and the new partitioned table takes
the old NAME. Postgres binds a view to the relation's OID, not its name,
so the view silently keeps pointing at `network_operations_pre_partition`
— which is the legacy copy the swap deliberately leaves behind for the
downgrade path. Two consequences, neither loud:

* the tenant usage view answers from a table that stops receiving rows
  the moment the swap commits, so a merchant's usage read goes stale
  instead of failing;
* the legacy copy can never be removed — dropping
  `network_operations_pre_partition` errors on the dependent view — which
  is precisely the disk the swap exists to reclaim.

The fix, and what these tests check
-----------------------------------
The view's DDL and its grant moved into ONE shared constant
(`app_shared.models.workspace_usage_view`) that both migrations issue, so
the swap's recreation cannot drift from the original definition. The swap
then re-creates the view AFTER the rename (so it rebinds to the new
relation) and re-applies the grant; the reverse mirrors it after the
rename back.

**These are offline-render/static-structure tests, and that is a
deliberate limitation.** They run `alembic upgrade head --sql` and the
reverse render with no database connection at all — the same harness
`test_migration_offline_network_operations_partition.py` uses — so they
prove the migration EMITS the recreation in the right ORDER relative to
the rename, and that both migrations issue one shared definition. They do
NOT prove Postgres rebinds the view, because that needs a scratch
database this repo's unit suite deliberately does not have. A live rebind
check belongs with the owner-run production rehearsal the swap
migration's own docstring already calls for.
"""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from app_shared.models.workspace_usage_view import (
    CREATE_WORKSPACE_USAGE_VIEW_SQL,
    DROP_WORKSPACE_USAGE_VIEW_SQL,
    GRANT_WORKSPACE_USAGE_VIEW_SQL,
    WORKSPACE_USAGE_VIEW,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

VIEW_REVISION = "193ac27f0dc2"
SWAP_REVISION = "a5e0c74b13d9"
SWAP_DOWN_REVISION = "d1f7a3c9e284"

#: The statement in the swap that makes the view stale. Everything the
#: recreation has to do, it has to do AFTER this line.
RENAME_AWAY = "ALTER TABLE network_operations RENAME TO network_operations_pre_partition;"
#: The statement in the reverse direction that puts the legacy table back
#: under the canonical name — the mirror point for the reverse recreation.
RENAME_BACK = "ALTER TABLE network_operations_pre_partition RENAME TO network_operations;"


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
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


@lru_cache(maxsize=1)
def _swap_reverse_sql() -> str:
    result = _run_alembic("downgrade", f"{SWAP_REVISION}:{SWAP_DOWN_REVISION}", "--sql")
    assert result.returncode == 0, (
        f"the offline reverse render failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


# --- one definition, two migrations -----------------------------------------


def test_both_migrations_issue_the_same_view_definition() -> None:
    """The recreation is the SAME constant, not a re-typed copy.

    A re-typed `CREATE VIEW` in the swap is the version of this fix that
    breaks a year from now, when a column is added to the view in one
    place. Asserted as source text: neither module may contain a
    `CREATE VIEW workspace_usage_v` of its own.
    """
    for revision in (VIEW_REVISION, SWAP_REVISION):
        module = next((REPO_ROOT / "alembic" / "versions").glob(f"{revision}_*.py"))
        source = module.read_text()
        assert "from app_shared.models.workspace_usage_view import" in source, (
            f"{module.name} must issue the shared view DDL, not its own copy"
        )
        assert f"CREATE VIEW {WORKSPACE_USAGE_VIEW}" not in source, (
            f"{module.name} still spells out a CREATE VIEW of its own"
        )


# --- upgrade: recreate AFTER the rename -------------------------------------


def test_the_swap_recreates_the_view_after_the_rename() -> None:
    """Order is the whole finding: recreating before the rename rebinds
    the view to the relation that is about to be renamed away."""
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)

    assert recreate_at > rename_at
    # ...and the ORIGINAL creation is still before it (the view is created
    # by `193ac27f0dc2`, long before the swap runs).
    assert sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip()) < rename_at


def test_the_swap_removes_the_stale_view_before_recreating_it() -> None:
    """`CREATE VIEW` on an existing name errors, and `CREATE OR REPLACE`
    cannot change a view's underlying relation binding — so the stale
    definition has to go first."""
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    removed_at = sql.index(DROP_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)

    assert rename_at < removed_at < recreate_at


def test_the_swap_reapplies_the_view_grant() -> None:
    """A recreated view is a NEW object: every grant on the old one is
    gone. `crawmatic_app` losing SELECT would take the merchant usage
    read down as surely as the stale binding would."""
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)
    grant_at = sql.index(GRANT_WORKSPACE_USAGE_VIEW_SQL.strip(), recreate_at)

    assert grant_at > recreate_at
    assert "crawmatic_app" in GRANT_WORKSPACE_USAGE_VIEW_SQL


# --- the reverse direction: the mirror --------------------------------------


def test_the_reverse_rebinds_the_view_to_the_restored_table() -> None:
    """The reverse rename leaves the view bound to the partitioned table
    the reverse has just removed — a dangling view, and a removal that
    fails. The mirror recreation is what makes the reverse a real undo
    rather than a one-way door."""
    sql = _swap_reverse_sql()

    rename_at = sql.index(RENAME_BACK)
    removed_at = sql.index(DROP_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)
    grant_at = sql.index(GRANT_WORKSPACE_USAGE_VIEW_SQL.strip(), recreate_at)

    assert rename_at < removed_at < recreate_at < grant_at


def test_the_view_definition_still_targets_network_operations_by_name() -> None:
    """Name-based, which is what makes recreating it enough.

    If the definition ever gained an explicit
    `network_operations_pre_partition` mention, recreation would silently
    reintroduce the bug it fixes.
    """
    assert "FROM network_operations no_" in CREATE_WORKSPACE_USAGE_VIEW_SQL
    assert "network_operations_pre_partition" not in CREATE_WORKSPACE_USAGE_VIEW_SQL
