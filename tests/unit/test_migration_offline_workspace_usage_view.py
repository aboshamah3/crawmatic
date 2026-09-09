"""`workspace_usage_v` must survive the `network_operations` swap (review R13).

The defect
----------
`193ac27f0dc2` creates the tenant-scoped view `workspace_usage_v` over
`network_operations`. `a5e0c74b13d9` then partitions that table by the
standard build-copy-swap: the original relation is renamed to
`network_operations_pre_partition` and the new partitioned table takes
the old NAME. Postgres binds a view to the relation's OID, not its name,
so the view silently keeps pointing at `network_operations_pre_partition`
— the legacy copy the swap deliberately leaves behind for the downgrade
path. Two consequences, neither loud:

* the tenant usage view answers from a table that stops receiving rows
  the moment the swap commits, so a merchant's usage read goes stale
  instead of failing;
* the legacy copy can never be removed — dropping
  `network_operations_pre_partition` errors on the dependent view — which
  is precisely the disk the swap exists to reclaim.

The fix, and the shape it had to take
-------------------------------------
The correction is a NEW revision, `4b06b233e0b8`, chained after the
current head: drop the view, recreate it (rebinding it by name onto
whatever relation now holds `network_operations`), re-issue the grant a
recreated view does not inherit. It is NOT an edit to either existing
revision. Both have already been applied to databases, and editing an
applied revision means two environments reporting the same
`alembic_version` no longer have the same schema — silently. So
`193ac27f0dc2` keeps its inline `CREATE VIEW` frozen exactly as it ran,
and everything from the rebind forward issues the shared constants in
`app_shared.models.workspace_usage_view`. The first test below is what
holds that line.

**These are offline-render/static-structure tests, and that is a
deliberate limitation.** They run `alembic upgrade head --sql` and the
reverse render with no database connection at all — the same harness
`test_migration_offline_network_operations_partition.py` uses — so they
prove the chain EMITS the recreation, with the right definition, in the
right ORDER relative to the rename, and that nothing afterwards still
references the legacy table. They do NOT prove Postgres rebinds the view,
because that needs a scratch database this packet is not authorised to
create. A live rebind check belongs with the owner-run production
rehearsal the swap migration's own docstring already calls for; what is
provable without a database is proved here, and the one remaining step is
named rather than implied.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from app_shared.models.network_operations import NetworkOperation
from app_shared.models.workspace_usage_view import (
    CREATE_WORKSPACE_USAGE_VIEW_SQL,
    DROP_WORKSPACE_USAGE_VIEW_SQL,
    GRANT_WORKSPACE_USAGE_VIEW_SQL,
    LEGACY_NETWORK_OPERATIONS_TABLE,
    NETWORK_OPERATIONS_TABLE,
    WORKSPACE_USAGE_VIEW,
    create_workspace_usage_view_sql,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

VIEW_REVISION = "193ac27f0dc2"
SWAP_REVISION = "a5e0c74b13d9"
SWAP_DOWN_REVISION = "d1f7a3c9e284"
REBIND_REVISION = "4b06b233e0b8"
REBIND_DOWN_REVISION = "f6b28c714a93"

#: The statement in the swap that makes the view stale. Everything the
#: recreation has to do, it has to do AFTER this line.
RENAME_AWAY = "ALTER TABLE network_operations RENAME TO network_operations_pre_partition;"
#: The other half of the swap: the partitioned table takes the name the
#: view (and every writer) resolves.
RENAME_INTO_PLACE = "ALTER TABLE network_operations_p RENAME TO network_operations;"
#: The statement in the reverse direction that removes the partitioned
#: table — the one that FAILS if the view is still bound to it.
REVERSE_DROP = "DROP TABLE network_operations;"


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
def _reverse_sql() -> str:
    """The reverse render from the rebind all the way past the swap.

    Both halves matter and they are one render on purpose: the rebind's
    downgrade only makes sense in terms of what runs AFTER it.
    """
    result = _run_alembic("downgrade", f"{REBIND_REVISION}:{SWAP_DOWN_REVISION}", "--sql")
    assert result.returncode == 0, (
        f"the offline reverse render failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result.stdout


def _revision_module(revision: str) -> Path:
    return next((REPO_ROOT / "alembic" / "versions").glob(f"{revision}_*.py"))


def _load_revision(revision: str) -> Any:
    path = _revision_module(revision)
    spec = importlib.util.spec_from_file_location(f"_rev_{revision}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";")


# --- D6: an applied revision is history ------------------------------------


def test_the_already_applied_revisions_were_not_edited() -> None:
    """The fix runs FORWARD, from its own revision.

    Appending the recreation to `a5e0c74b13d9.upgrade()` is the shorter
    diff and the wrong one: that revision has run, so editing it makes two
    databases at the same `alembic_version` structurally different with
    nothing to say so. Asserted as source text on both applied revisions —
    neither may reach for the shared constants, because reaching for them
    is what an in-place fix would look like.
    """
    for revision in (VIEW_REVISION, SWAP_REVISION):
        source = _revision_module(revision).read_text()
        assert "app_shared.models.workspace_usage_view" not in source, (
            f"{revision} was edited to issue the shared view DDL. It has already been "
            "applied; the rebind belongs in a NEW revision after it, not in its body"
        )

    history = _run_alembic("history")
    assert history.returncode == 0, history.stderr
    assert f"-> {REBIND_REVISION}" in history.stdout, (
        f"{REBIND_REVISION} is not in the alembic chain"
    )

    # ...and it runs after the swap, which is the only ordering that fixes
    # anything. `alembic history` prints newest-first, so the rebind's line
    # comes BEFORE the swap's line in that output.
    lines = history.stdout.splitlines()
    rebind_at = next(i for i, line in enumerate(lines) if f"-> {REBIND_REVISION}" in line)
    swap_at = next(i for i, line in enumerate(lines) if f"-> {SWAP_REVISION}" in line)
    assert rebind_at < swap_at, "the rebind must run after the partition swap, not before"


def test_the_rebind_recreates_the_same_view_the_original_revision_created() -> None:
    """The rebind must not quietly RESHAPE the view.

    `193ac27f0dc2`'s inline definition is frozen (it has been applied);
    the shared constant is what everything after it issues. At the moment
    of the rebind those two must be the same view, or a "fix" for a stale
    binding silently ships a column change to whoever reads
    `workspace_usage_v`.

    If a later revision deliberately reshapes the view, that revision —
    not this test — is the place to state the new shape, and this
    assertion is then expected to be updated in the same change.
    """
    original = _load_revision(VIEW_REVISION)._CREATE_VIEW_SQL

    assert _normalise(original) == _normalise(CREATE_WORKSPACE_USAGE_VIEW_SQL)


# --- upgrade: recreate AFTER the rename -------------------------------------


def test_the_rebind_recreates_the_view_after_the_rename() -> None:
    """Order is the whole finding: recreating before the rename rebinds
    the view to the relation that is about to be renamed away."""
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)

    assert recreate_at > rename_at
    # ...and the ORIGINAL creation is still before it (the view is created
    # by `193ac27f0dc2`, long before the swap runs).
    assert sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip()) < rename_at


def test_the_rebind_removes_the_stale_view_before_recreating_it() -> None:
    """`CREATE VIEW` on an existing name errors, and `CREATE OR REPLACE`
    cannot change a view's underlying relation binding — it preserves the
    dependencies that are the whole problem. So the stale definition has
    to go first."""
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    removed_at = sql.index(DROP_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)

    assert rename_at < removed_at < recreate_at


def test_the_rebind_reapplies_the_view_grant() -> None:
    """A recreated view is a NEW object: every grant on the old one is
    gone. `crawmatic_app` losing SELECT would take the merchant usage
    read down as surely as the stale binding would."""
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    recreate_at = sql.index(CREATE_WORKSPACE_USAGE_VIEW_SQL.strip(), rename_at)
    grant_at = sql.index(GRANT_WORKSPACE_USAGE_VIEW_SQL.strip(), recreate_at)

    assert grant_at > recreate_at
    assert "crawmatic_app" in GRANT_WORKSPACE_USAGE_VIEW_SQL


# --- what the merchant actually gets ----------------------------------------


def test_operations_written_after_the_swap_are_what_the_view_reads() -> None:
    """The acceptance question, assembled from the three facts a render can carry.

    1. after the swap, the name `network_operations` belongs to the
       PARTITIONED table (`RENAME_INTO_PLACE`);
    2. every writer resolves that same name — the ORM model's
       `__tablename__` is it, so post-swap inserts land in the partitioned
       table and nowhere else;
    3. the view the chain ends with selects `FROM network_operations`, and
       is created after the rename, so it resolves the same relation the
       writers do.

    What no offline render can carry is step 4 — Postgres resolving that
    name to an OID at `CREATE VIEW` time. That is the one link left to the
    live rehearsal, and it is the link every part of this fix is designed
    around rather than one this test quietly assumes away.
    """
    sql = _upgrade_sql()

    assert NetworkOperation.__tablename__ == NETWORK_OPERATIONS_TABLE

    rename_into_place_at = sql.index(RENAME_INTO_PLACE)
    final_view_at = sql.rindex(f"CREATE VIEW {WORKSPACE_USAGE_VIEW}")
    assert final_view_at > rename_into_place_at

    final_view = sql[final_view_at : sql.index(";", final_view_at)]
    assert f"FROM {NETWORK_OPERATIONS_TABLE} no_" in final_view
    assert LEGACY_NETWORK_OPERATIONS_TABLE not in final_view


def test_the_legacy_table_can_be_dropped_once_the_chain_has_run() -> None:
    """The other half of the finding: reclaiming the disk.

    `DROP TABLE network_operations_pre_partition` fails while any object
    depends on it, and before the rebind the tenant view did. After the
    chain, the legacy name must appear NOWHERE later than the rename that
    creates it — no view, no index, no constraint reaching back — which is
    the offline statement of "nothing depends on it any more".

    (The drop itself is deliberately not in any migration: it is an
    irreversible operator decision, and the legacy copy is also what makes
    the swap's own downgrade possible.)
    """
    sql = _upgrade_sql()

    rename_at = sql.index(RENAME_AWAY)
    last_mention = sql.rindex(LEGACY_NETWORK_OPERATIONS_TABLE)

    assert last_mention < rename_at + len(RENAME_AWAY), (
        "something after the swap's rename still references "
        f"{LEGACY_NETWORK_OPERATIONS_TABLE}: "
        f"{sql[last_mention - 200 : last_mention + 200]!r}"
    )


# --- the reverse direction: the mirror --------------------------------------


def test_the_reverse_unbinds_the_view_before_the_swap_drops_the_table() -> None:
    """The downgrade is a real undo, not a one-way door.

    `a5e0c74b13d9.downgrade()` does `DROP TABLE network_operations` — which
    ERRORS while `workspace_usage_v` depends on that table. Alembic runs
    the newest revision's downgrade first, so the rebind's own downgrade is
    the only place that can move the view out of the way, and it does so by
    putting the view back exactly where the revision below it left it: on
    the legacy copy.
    """
    sql = _reverse_sql()

    legacy_view = create_workspace_usage_view_sql(LEGACY_NETWORK_OPERATIONS_TABLE).strip()
    rebind_at = sql.index(legacy_view)
    drop_at = sql.index(REVERSE_DROP)

    assert rebind_at < drop_at, (
        "the reverse drops the partitioned table while the tenant view still depends "
        "on it — the downgrade would fail on the dependency"
    )
    assert sql.index(DROP_WORKSPACE_USAGE_VIEW_SQL.strip()) < rebind_at


def test_the_reverse_rebind_is_guarded_on_the_legacy_table_still_existing() -> None:
    """This revision is what makes the legacy copy droppable, so by the
    time anyone downgrades it may already be gone. The guard is a server-
    side `to_regclass` check rather than a Python one because the offline
    `--sql` render has no database to ask."""
    sql = _reverse_sql()

    guard = f"to_regclass('public.{LEGACY_NETWORK_OPERATIONS_TABLE}') IS NULL"
    assert guard in sql
    assert sql.index(guard) < sql.index(
        create_workspace_usage_view_sql(LEGACY_NETWORK_OPERATIONS_TABLE).strip()
    )


def test_the_view_definition_still_targets_network_operations_by_name() -> None:
    """Name-based, which is what makes recreating it enough.

    If the forward definition ever gained an explicit
    `network_operations_pre_partition` mention, recreation would silently
    reintroduce the bug it fixes.
    """
    assert f"FROM {NETWORK_OPERATIONS_TABLE} no_" in CREATE_WORKSPACE_USAGE_VIEW_SQL
    assert LEGACY_NETWORK_OPERATIONS_TABLE not in CREATE_WORKSPACE_USAGE_VIEW_SQL
