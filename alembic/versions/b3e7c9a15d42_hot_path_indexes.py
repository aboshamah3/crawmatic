"""Hot-path indexes for the maintenance sweeps and the daily rollup (M3).

Every index below is justified by an `EXPLAIN (ANALYZE, BUFFERS)` taken
against production (1 workspace, 3.5k products, 4.6k matches, 32k
observations, 67 MB) — see the per-index notes.

**Online-safe by construction.** Each ordinary-table index is built with
`CREATE INDEX CONCURRENTLY` inside an `autocommit_block()` (Postgres
forbids `CONCURRENTLY` inside a transaction, and Alembic runs migrations
in one by default). `CONCURRENTLY` is ALSO forbidden directly on a
*partitioned parent*, so `price_observations` uses the three-step
recipe already documented in `f87cf9a237cd_usage_export_indexes.py`:

1. `CREATE INDEX ... ON ONLY <parent>` — registers an (initially
   invalid) parent index without locking any partition.
2. `CREATE INDEX CONCURRENTLY` the matching index on each existing
   partition — no `ACCESS EXCLUSIVE`, no write outage.
3. `ALTER INDEX <parent> ATTACH PARTITION <child>` per partition; once
   every partition is attached the parent index flips to valid.

Partitions are discovered from `pg_inherits` at run time (never
hard-coded), so this works whatever months exist when it runs. **Future**
partitions need no extra work: `app_shared.maintenance.partitions`
creates them with `CREATE TABLE ... PARTITION OF`, and Postgres
automatically creates + attaches a matching child index for every index
on the parent (this is how `price_observations_2026_08` already carries
`..._workspace_id_scraped_at_idx`).

Idempotent: `IF NOT EXISTS` on every create, `IF EXISTS` on every drop.
Caveat — an interrupted `CREATE INDEX CONCURRENTLY` leaves an *invalid*
index behind, which `IF NOT EXISTS` will then skip; drop the invalid
index by name and re-run if that happens (query `pg_index.indisvalid`).

Revision ID: b3e7c9a15d42
Revises: a91b6f3c27de
Create Date: 2026-08-15 02:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e7c9a15d42"
down_revision: Union[str, Sequence[str], None] = "a91b6f3c27de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Parent of the partitioned observation table + the new composite.
_PO_PARENT = "price_observations"
_PO_INDEX = "ix_price_observations_ws_variant_scraped"
_PO_COLUMNS = "(workspace_id, product_variant_id, scraped_at)"

#: Suffix for the per-partition child indexes of `_PO_INDEX`. Child names
#: must be unique and <= 63 chars: the longest partition name is
#: `price_observations_YYYY_MM` (26) + this suffix (23) = 49.
_PO_CHILD_SUFFIX = "_ws_variant_scraped_idx"

#: Plain (non-partitioned) indexes: (name, table, definition tail).
#:
#: * `ix_sjt_deferred` — `redispatch_pending_jobs` probes "does this job
#:   still have a DEFERRED target?" once per non-terminal job per 60s
#:   tick. Measured on prod's largest job (2,653 targets): Bitmap Index
#:   Scan over 2,914 entries of
#:   `uq_scrape_job_targets_scrape_job_id_match_id` + 68 heap blocks =
#:   338 buffers to return 0 rows. This partial index holds only the 16
#:   DEFERRED rows in the whole table, so the probe becomes a 2-3 buffer
#:   index lookup and never touches the heap of a finished job.
#: * `ix_sjt_pending_unlocked` — same shape for `recover_stalled_batches`
#:   (measured 338 buffers, 2,653 rows discarded, 0 returned). The
#:   predicate currently matches 0 rows table-wide.
#: * `ix_scrape_jobs_active` — `_scan_job_refs` runs 3x per 60s tick and
#:   Seq Scans `scrape_jobs` (measured: 22 buffers, 527 rows removed, 0
#:   returned). `scrape_jobs` has NO retention policy, so that scan grows
#:   without bound. The predicate is `_NON_TERMINAL_JOB_STATUSES` from
#:   `apps/workers/app/workers/tasks_jobs.py`, which is
#:   `frozenset(ScrapeJobStatus) - {COMPLETED, PARTIAL_FAILED, FAILED,
#:   CANCELLED}` = **{PENDING, RUNNING}**. There is no `DISPATCHED`
#:   member of `ScrapeJobStatus`; the predicate must stay exactly in sync
#:   with that constant or the planner cannot prove the index applies.
#: * `ix_cpm_ws_competitor_url_pattern` — the strategy/rediscovery sample
#:   probes (`tasks_strategy._sample_urls`, `tasks_strategy` url-pattern
#:   re-derivation, `strategy/rediscovery.py`) filter exactly
#:   `(workspace_id, competitor_id, url_pattern)` and measured a full Seq
#:   Scan of all 4,588 rows (454-913 buffers) to return 1 row.
#:   `competitor_product_matches` took 183,711 seq scans in the 4.5-day
#:   stats window — the highest of any table here.
_PLAIN_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_sjt_deferred",
        "scrape_job_targets",
        "(scrape_job_id) WHERE status = 'DEFERRED'",
    ),
    (
        "ix_sjt_pending_unlocked",
        "scrape_job_targets",
        "(scrape_job_id) WHERE status = 'PENDING' AND locked_at IS NULL",
    ),
    (
        "ix_scrape_jobs_active",
        "scrape_jobs",
        "(status, workspace_id) WHERE status IN ('PENDING', 'RUNNING')",
    ),
    (
        "ix_cpm_ws_competitor_url_pattern",
        "competitor_product_matches",
        "(workspace_id, competitor_id, url_pattern)",
    ),
)


def _partitions_of(parent: str) -> list[str]:
    """Return the names of ``parent``'s direct partitions (public schema).

    Live connection only — in Alembic's ``--sql`` (offline) mode there is
    no connection to introspect, so callers skip the per-partition work
    entirely (see :func:`upgrade`).
    """
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
            WHERE parent.relname = :parent
              AND parent.relnamespace = 'public'::regnamespace
            ORDER BY child.relname
            """
        ),
        {"parent": parent},
    ).fetchall()
    return [row[0] for row in rows]


def _child_index_attached(child_index: str) -> bool:
    """``True`` iff ``child_index`` is already attached to ``_PO_INDEX``.

    ``ALTER INDEX ... ATTACH PARTITION`` has no ``IF NOT EXISTS`` form and
    errors on a re-attach, and the parent's ``indisvalid`` only flips once
    EVERY child is attached — so it cannot serve as the guard. Hence this
    per-child catalog probe.
    """
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT 1
                FROM pg_inherits
                JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
                JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
                WHERE child.relname = :child_index
                  AND parent.relname = :parent_index
                """
            ),
            {"child_index": child_index, "parent_index": _PO_INDEX},
        )
        .fetchone()
        is not None
    )


#: Emitted into an offline (`--sql`) script in place of the per-partition
#: `CREATE INDEX CONCURRENTLY` + `ATTACH PARTITION` loop, which needs a
#: live catalog to enumerate partitions.
_OFFLINE_PARTITION_NOTE = (
    "-- OFFLINE MODE: the per-partition CREATE INDEX CONCURRENTLY + "
    f"ALTER INDEX {_PO_INDEX} ATTACH PARTITION steps are omitted -- they "
    "require a live catalog to enumerate price_observations' partitions. "
    "Run this migration against a live connection, or add them by hand."
)


def upgrade() -> None:
    offline = op.get_context().as_sql  # Alembic `--sql` (offline) mode

    with op.get_context().autocommit_block():
        for name, table, tail in _PLAIN_INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}")

        # --- partitioned parent: ON ONLY + per-child CONCURRENTLY + ATTACH ---
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_PO_INDEX} ON ONLY {_PO_PARENT} {_PO_COLUMNS}"
        )
        if offline:
            op.execute(_OFFLINE_PARTITION_NOTE)
            return
        for child in _partitions_of(_PO_PARENT):
            child_index = f"{child}{_PO_CHILD_SUFFIX}"
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {child_index} "
                f"ON {child} {_PO_COLUMNS}"
            )
            if not _child_index_attached(child_index):
                op.execute(f"ALTER INDEX {_PO_INDEX} ATTACH PARTITION {child_index}")


def downgrade() -> None:
    offline = op.get_context().as_sql  # Alembic `--sql` (offline) mode

    with op.get_context().autocommit_block():
        # Dropping the partitioned parent index drops every attached child
        # index with it. `CONCURRENTLY` is not supported on a partitioned
        # index, so this one takes a brief lock.
        op.execute(f"DROP INDEX IF EXISTS {_PO_INDEX}")
        # Any child index that was built but never attached (interrupted
        # upgrade) is not covered by the parent drop -- clean those up too.
        if offline:
            op.execute(_OFFLINE_PARTITION_NOTE)
        else:
            for child in _partitions_of(_PO_PARENT):
                op.execute(
                    f"DROP INDEX CONCURRENTLY IF EXISTS {child}{_PO_CHILD_SUFFIX}"
                )

        for name, _table, _tail in reversed(_PLAIN_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
