"""Ledger child summarization — compress a browser navigation's
subresource children into one durable summary, then delete them
(EPA C9, F14, task ``MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN``).

THE PROBLEM
-----------
A browser navigation opens one PARENT ``network_operations`` row and one
CHILD row per subresource it pulls in (``parent_operation_id`` points at
the navigation's ``network_request_id``). An asset-heavy page produces
hundreds of children per page view, and within days they are the
overwhelming majority of rows in the ledger — while answering, after a
month, exactly one question: *what did that navigation cost on the wire?*

That answer is four numbers. This module writes those four numbers to
``network_operation_resource_summaries`` and deletes the children in the
**same transaction**, so the fact survives and only the bulk goes.

TWO GATES, BOTH REQUIRED
------------------------
A parent's children are summarised only when BOTH hold:

1. **Age.** The parent is older than
   ``Settings.RETENTION_NETWORK_OPERATION_CHILDREN_DAYS`` (30 days).
2. **Settled.** A settlement row exists for the parent in
   :data:`SETTLEMENT_TABLE` — the provider's own usage record has
   arrived and the operation's cost has been reconciled.

Gate 2 is the one that matters. Reconciliation
(``app_shared.netledger.reconcile``) apportions a period-billed
provider charge across operations **by their transport-observed bytes**,
and a browser navigation's bytes are its children's bytes. Summarising
an UNSETTLED parent would therefore delete the very rows a not-yet-
arrived invoice will be reconciled against, and the resulting settlement
would be silently wrong rather than loudly impossible. So an unsettled
parent keeps every child, however old — the family simply reports it as
deferred and a later pass picks it up once the settlement lands.

THE OWNER SWITCH
----------------
Like every other retention family, this one is inert until the owner
names ``network_operation_children`` in
``Settings.RETENTION_ENABLED_CLASSES``
(``app_shared.maintenance.registry.retention_class_enabled``). Until
then :func:`run_ledger_child_summarization` reports
``class_not_enabled`` and writes nothing at all. :func:`summarize_parent`
also takes ``dry_run``, which computes and returns the exact summary the
run WOULD write without writing it — the rehearsal mode the D1
production rehearsal drives.

WHY THIS IS NOT "A BULK DELETE ON A PARTITIONED TABLE"
------------------------------------------------------
``network_operations`` is monthly-partitioned since EPA C9 and the
project rule is that a partitioned table's retention is a whole-partition
``DROP``, never a bulk ``DELETE`` (FR-015/SC-003). This is not that.
The rule exists because sweeping millions of rows out of a raw
append-heavy partition churns WAL, bloats the heap and leaves the
partition no smaller. Here the delete is *keyed to one parent*, bounded
by that parent's fan-out, and its purpose is not to reclaim the
partition — the partition is still dropped whole at 730 days by
``run_retention``. The two mechanisms are complementary: this one buys
back the 30-to-730-day window, the drop reclaims the file.

THE FOREIGN KEYS THIS MODULE REPLACES
--------------------------------------
Partitioning ``network_operations`` cost the ledger five foreign keys
(see ``alembic/versions/a5e0c74b13d9_partition_network_operations.py``).
:func:`find_orphan_references` is what stands in for them: a read-only
check that every ``operation_id``/``network_operation_id`` in the
referencing tables still names a live operation. It is a *report*, never
a repair — deciding what to do about an orphan is an operator's call,
and a job that silently deleted them would be a worse bug than the one
it was cleaning up.

Scraping-free (Constitution I/V) — SQLAlchemy + stdlib only. Every read
and write here is inherently cross-tenant (the ledger is fleet-owned and
has no ``workspace_id`` at all), so this module runs on the sanctioned
BYPASSRLS system session, with ``# noqa: workspace-scope`` on the
unscoped statements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.config import Settings, get_settings
from app_shared.maintenance.partitions import table_exists
from app_shared.maintenance.registry import retention_class_enabled
from app_shared.models.network_operation_summaries import (
    HOST_CLASS_FIRST_PARTY,
    HOST_CLASS_THIRD_PARTY,
    HOST_CLASS_UNKNOWN,
)

#: The retention class key this task belongs to
#: (``app_shared.maintenance.registry.RETENTION_FAMILIES``). Nothing here
#: runs unless the owner has named it in
#: ``Settings.RETENTION_ENABLED_CLASSES``.
LEDGER_CHILDREN_CLASS = "network_operation_children"

#: The settlement table gate 2 probes. The plan calls this class of row a
#: "cost_settlements" row; in this schema it is
#: ``network_operation_settlements``
#: (``app_shared.models.network_operations.NetworkOperationSettlement``)
#: — same fact, the name the database actually uses.
SETTLEMENT_TABLE = "network_operation_settlements"

#: Where the compressed children go.
SUMMARY_TABLE = "network_operation_resource_summaries"

#: Structured-log event names (the repo's `EVENT_*` convention).
EVENT_SUMMARIZED = "ledger_children_summarized"
EVENT_CLASS_NOT_ENABLED = "ledger_children_class_not_enabled"
EVENT_STORE_ABSENT = "ledger_children_summary_store_absent"


@dataclass(frozen=True)
class ChildSummary:
    """The four numbers a parent's children are compressed into.

    Returned by :func:`summarize_parent` in BOTH modes, so a dry run and
    a real run produce the identical value and a rehearsal can be diffed
    against the row that would have been written.
    """

    parent_operation_id: object
    parent_created_at: datetime
    child_count: int
    bytes_by_host_class: dict[str, int]
    bytes_total: int
    duration_ms_sum: int


@dataclass
class RunReport:
    """Structured summary of one ``MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN``
    run — logged by the Celery task wrapper, never persisted."""

    #: ``True`` when the owner has not enabled the class; every other
    #: field is then empty by construction.
    class_not_enabled: bool = False
    #: ``True`` when the ledger or the summary table is not migrated yet.
    store_absent: bool = False
    #: ``True`` when nothing was written (rehearsal).
    dry_run: bool = False
    parents_summarized: list[str] = field(default_factory=list)
    children_deleted: int = 0
    #: Parents old enough but NOT settled — kept, deliberately.
    parents_deferred_unsettled: int = 0
    summaries: list[ChildSummary] = field(default_factory=list)


def _eligible_parents_stmt(cutoff: datetime, limit: int):
    """Build the (unexecuted) statement selecting parents whose children
    may be summarised — gate 1 (age) AND gate 2 (settled), together.

    Split out so its rendered SQL can be asserted in a pure unit test
    without a live DB (the repo-wide
    ``app_shared.maintenance.partitions._to_regclass_stmt`` pattern).

    A parent is any operation that HAS at least one child
    (``EXISTS`` on ``parent_operation_id``) and is not itself a child.
    ``EXISTS`` rather than a join so a navigation with three hundred
    subresources contributes one row, not three hundred.

    The settlement probe is an ``EXISTS`` too, and it is the reason an
    unsettled parent is *skipped* rather than *excluded from the count*:
    :func:`run_ledger_child_summarization` runs a second, cheap count of
    age-eligible-but-unsettled parents so the report can say how much
    work is waiting on reconciliation rather than silently reporting
    "nothing to do".
    """
    return text(
        f"""
        SELECT p.network_request_id, p.created_at
        FROM network_operations p
        WHERE p.created_at < :cutoff
          AND p.parent_operation_id IS NULL
          AND EXISTS (
              SELECT 1 FROM network_operations c
              WHERE c.parent_operation_id = p.network_request_id
          )
          AND EXISTS (
              SELECT 1 FROM {SETTLEMENT_TABLE} s
              WHERE s.operation_id = p.network_request_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM {SUMMARY_TABLE} m
              WHERE m.parent_operation_id = p.network_request_id
          )
        ORDER BY p.created_at
        LIMIT :limit
        """
    ).bindparams(cutoff=cutoff, limit=limit)


def _deferred_unsettled_count_stmt(cutoff: datetime):
    """Build the (unexecuted) statement counting parents that are old
    enough but whose settlement has not arrived — the number that makes
    "nothing was summarised" legible.
    """
    return text(
        f"""
        SELECT count(*)
        FROM network_operations p
        WHERE p.created_at < :cutoff
          AND p.parent_operation_id IS NULL
          AND EXISTS (
              SELECT 1 FROM network_operations c
              WHERE c.parent_operation_id = p.network_request_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM {SETTLEMENT_TABLE} s
              WHERE s.operation_id = p.network_request_id
          )
        """
    ).bindparams(cutoff=cutoff)


def _child_aggregate_stmt():
    """Build the (unexecuted) per-parent child aggregation.

    Host class is decided against the PARENT's own ``domain``: a child
    fetched from the same registrable host (or a subdomain of it) is
    ``first_party``, an empty/absent host is ``unknown``, everything else
    is ``third_party``. A CLOSED set of three — keying the map by raw
    hostname instead would make the summary of a hostile page bigger than
    the rows it replaced, which is the opposite of the point.

    ``COALESCE(bytes_compressed, 0)``: an unobserved byte count is not
    the same fact as zero bytes, but a summary must still sum to
    something, and the honest place to notice missing bytes is
    ``crawmatic_ledger_bytes_missing_fraction_24h`` (A7) at the time the
    operation closes — not thirty days later when nothing can be done.
    """
    return text(
        f"""
        SELECT
            CASE
                WHEN c.domain IS NULL OR c.domain = '' THEN '{HOST_CLASS_UNKNOWN}'
                WHEN c.domain = p.domain
                  OR right(c.domain, length(p.domain) + 1)
                     = '.' || p.domain THEN '{HOST_CLASS_FIRST_PARTY}'
                ELSE '{HOST_CLASS_THIRD_PARTY}'
            END AS host_class,
            count(*) AS child_count,
            COALESCE(SUM(COALESCE(c.bytes_compressed, 0)), 0) AS bytes,
            COALESCE(SUM(COALESCE(c.duration_ms, 0)), 0) AS duration_ms
        FROM network_operations c
        JOIN network_operations p
          ON p.network_request_id = :parent_id
        WHERE c.parent_operation_id = :parent_id
        GROUP BY 1
        """
    )


def _insert_summary_stmt():
    """Build the (unexecuted) summary insert.

    ``ON CONFLICT (parent_operation_id) DO NOTHING`` makes a re-run of an
    already-summarised parent a no-op rather than a unique violation —
    belt and braces with the ``NOT EXISTS`` in
    :func:`_eligible_parents_stmt`, which a concurrent run could race.
    """
    return text(
        f"""
        INSERT INTO {SUMMARY_TABLE} (
            id, parent_operation_id, child_count, bytes_by_host_class,
            bytes_total, duration_ms_sum, parent_created_at
        )
        VALUES (
            gen_random_uuid(), :parent_id, :child_count,
            CAST(:bytes_by_host_class AS jsonb),
            :bytes_total, :duration_ms_sum, :parent_created_at
        )
        ON CONFLICT (parent_operation_id) DO NOTHING
        """
    )


def _delete_children_stmt():
    """Build the (unexecuted) child delete.

    Keyed to ONE parent — see the module docstring for why this is not
    the bulk ``DELETE`` on a partitioned table the project forbids.
    """
    return text(
        "DELETE FROM network_operations WHERE parent_operation_id = :parent_id"
    )


def summarize_parent(
    session: Session,
    parent_id,
    parent_created_at: datetime,
    *,
    dry_run: bool = False,
) -> ChildSummary:
    """Aggregate ``parent_id``'s children, and (unless ``dry_run``) write
    the summary and delete them.

    Returns the :class:`ChildSummary` in either mode, so a rehearsal
    produces exactly the value a real run would have persisted.

    The insert and the delete are issued back to back on the caller's
    session and are therefore in the caller's transaction: either the
    summary exists and the children are gone, or neither happened. That
    atomicity is the entire safety argument for this family — a delete
    that outran its summary would destroy the parent's byte breakdown
    with nothing standing in for it.

    The PARENT row is never touched. Its own
    ``bytes_compressed``/``duration_ms``/``estimated_cost_micro_units``
    are immutable-by-trigger facts about the navigation itself and stay
    exactly as they were closed — which is what makes "totals are equal
    before and after" true rather than merely intended.
    """
    rows = session.execute(  # noqa: workspace-scope
        _child_aggregate_stmt(), {"parent_id": parent_id}
    ).all()

    bytes_by_host_class: dict[str, int] = {}
    child_count = 0
    duration_ms_sum = 0
    for host_class, count_, bytes_, duration_ms in rows:
        child_count += int(count_)
        duration_ms_sum += int(duration_ms)
        if bytes_:
            bytes_by_host_class[str(host_class)] = int(bytes_)

    summary = ChildSummary(
        parent_operation_id=parent_id,
        parent_created_at=parent_created_at,
        child_count=child_count,
        bytes_by_host_class=bytes_by_host_class,
        bytes_total=sum(bytes_by_host_class.values()),
        duration_ms_sum=duration_ms_sum,
    )

    if dry_run or child_count == 0:
        return summary

    session.execute(  # noqa: workspace-scope
        _insert_summary_stmt(),
        {
            "parent_id": parent_id,
            "child_count": summary.child_count,
            "bytes_by_host_class": json.dumps(summary.bytes_by_host_class),
            "bytes_total": summary.bytes_total,
            "duration_ms_sum": summary.duration_ms_sum,
            "parent_created_at": parent_created_at,
        },
    )
    session.execute(  # noqa: workspace-scope
        _delete_children_stmt(), {"parent_id": parent_id}
    )
    return summary


def run_ledger_child_summarization(
    session: Session,
    *,
    now_utc: datetime,
    settings: Settings | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Run one ``MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN`` pass.

    Ordering of the checks is deliberate and is the safety story:

    1. **Owner switch first.** If ``network_operation_children`` is not
       in ``Settings.RETENTION_ENABLED_CLASSES`` the function returns
       immediately with ``class_not_enabled=True`` — before it has even
       looked at the data, so an un-ratified deployment cannot be one
       bug away from deleting rows.
    2. **Store existence.** A database where the summary table is not
       migrated yet reports ``store_absent`` and writes nothing, rather
       than raising — the ``to_regclass`` capability-probe convention
       this package uses everywhere (FR-002).
    3. **Age + settlement**, per parent, in SQL.

    Each parent is summarised and its children deleted, then committed,
    so a killed invocation loses at most one parent's work and the next
    invocation resumes on the rest (the parent is no longer eligible,
    because a summary row now exists for it).
    """
    if settings is None:
        settings = get_settings()

    report = RunReport(dry_run=dry_run)

    if not retention_class_enabled(LEDGER_CHILDREN_CLASS, settings):
        report.class_not_enabled = True
        return report

    if not table_exists(session, "network_operations") or not table_exists(
        session, SUMMARY_TABLE
    ):
        report.store_absent = True
        return report

    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware (UTC)")

    cutoff = now_utc - timedelta(
        days=settings.RETENTION_NETWORK_OPERATION_CHILDREN_DAYS
    )

    deferred = session.execute(  # noqa: workspace-scope
        _deferred_unsettled_count_stmt(cutoff)
    ).scalar()
    report.parents_deferred_unsettled = int(deferred or 0)

    parents = session.execute(  # noqa: workspace-scope
        _eligible_parents_stmt(cutoff, settings.LEDGER_SUMMARIZE_BATCH_SIZE)
    ).all()

    for parent_id, parent_created_at in parents:
        summary = summarize_parent(
            session, parent_id, parent_created_at, dry_run=dry_run
        )
        if summary.child_count == 0:
            continue
        report.parents_summarized.append(str(parent_id))
        report.children_deleted += summary.child_count
        report.summaries.append(summary)
        if not dry_run:
            session.commit()

    return report


#: The referencing columns the five dropped foreign keys used to protect,
#: as ``(table, column)``. Kept as data so
#: :func:`find_orphan_references` and its test enumerate the SAME list —
#: a check that silently stopped covering a table would be worse than no
#: check at all.
ORPHAN_REFERENCE_SOURCES: tuple[tuple[str, str], ...] = (
    ("network_operation_allocations", "operation_id"),
    ("network_operation_settlements", "operation_id"),
    ("request_attempts", "network_operation_id"),
    ("network_operations", "retry_parent_id"),
    ("network_operations", "parent_operation_id"),
)


def _orphan_count_stmt(table: str, column: str):
    """Build the (unexecuted) orphan count for one referencing column."""
    return text(
        f"""
        SELECT count(*)
        FROM {table} r
        WHERE r.{column} IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM network_operations o
              WHERE o.network_request_id = r.{column}
          )
        """
    )


def find_orphan_references(session: Session) -> dict[str, int]:
    """Count rows whose operation reference no longer resolves.

    The scheduled stand-in for the five foreign keys the partition swap
    dropped. Returns ``{"table.column": count}`` for every entry of
    :data:`ORPHAN_REFERENCE_SOURCES` that resolves to an existing table;
    an absent table is skipped, not reported as zero, so "no orphans" and
    "not migrated yet" never look alike.

    **Read-only, on purpose.** It reports; it never repairs. Some orphans
    are expected and correct — a referencing row naturally outlives an
    operation whose 730-day partition has been dropped — and a job that
    deleted them would be destroying the newest evidence to tidy up after
    the oldest. What an operator wants from this number is its TREND: a
    step change means a writer is minting references to operations that
    were never recorded, which is a bug in the ledger, not in retention.
    """
    counts: dict[str, int] = {}
    for table, column in ORPHAN_REFERENCE_SOURCES:
        if not table_exists(session, table):
            continue
        value = session.execute(  # noqa: workspace-scope
            _orphan_count_stmt(table, column)
        ).scalar()
        counts[f"{table}.{column}"] = int(value or 0)
    return counts


__all__ = [
    "ChildSummary",
    "EVENT_CLASS_NOT_ENABLED",
    "EVENT_STORE_ABSENT",
    "EVENT_SUMMARIZED",
    "LEDGER_CHILDREN_CLASS",
    "ORPHAN_REFERENCE_SOURCES",
    "RunReport",
    "SETTLEMENT_TABLE",
    "SUMMARY_TABLE",
    "find_orphan_references",
    "run_ledger_child_summarization",
    "summarize_parent",
]
