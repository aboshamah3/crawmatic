"""Partitioned-table registry (SPEC-15 US1/US3, data-model.md §2, research R3).

A fixed, code-level constant — **not** a database table. Each entry binds
a real partitioned parent table to its declared ``postgresql_partition_by``
column, whether it feeds the daily rollup verify-before-drop gate
(``feeds_rollups`` — ``price_observations`` only, FR-016), and the
``Settings`` attribute name that resolves its retention window (Principle
IV: the *set* of tables is a code constant, but retention *durations* are
DB/env-tunable).

``variant_price_daily_rollups`` is deliberately **not** registered here —
it is not partitioned; its 2-year retention is a separate age-based row
policy (research R7) applied directly in ``maintenance.retention``.

Scraping-free (Constitution I/V) — imports nothing beyond stdlib +
``app_shared.config``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app_shared.config import Settings


@dataclass(frozen=True)
class PartitionedTable:
    """One partitioned parent table and how the maintenance jobs treat it.

    Attributes:
        name: The parent table's name (e.g. ``"price_observations"``).
        partition_key: The ``RANGE``-partitioned column (e.g. ``"scraped_at"``).
        feeds_rollups: ``True`` only for ``price_observations`` — retention
            for this table must verify daily-rollup coverage before
            dropping a partition (FR-016). ``False`` entries drop by age
            alone (FR-019).
        retention_setting: The ``Settings`` attribute name whose value (an
            ``int`` day count) is this table's retention window (FR-017).
            Resolved via :func:`retention_days`, never hardcoded.
    """

    name: str
    partition_key: str
    feeds_rollups: bool
    retention_setting: str


PARTITIONED_TABLES: tuple[PartitionedTable, ...] = (
    PartitionedTable(
        name="price_observations",
        partition_key="scraped_at",
        feeds_rollups=True,
        retention_setting="RETENTION_PRICE_OBSERVATIONS_DAYS",
    ),
    PartitionedTable(
        name="request_attempts",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_REQUEST_ATTEMPTS_DAYS",
    ),
    PartitionedTable(
        name="price_alert_events",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_PRICE_ALERT_EVENTS_DAYS",
    ),
    PartitionedTable(
        # Registered ahead of its own migration (SPEC-16) — deliberately
        # absent in this build. `table_exists` (R4) skips it cleanly
        # (FR-002) until it lands.
        name="webhook_events",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_WEBHOOK_EVENTS_DAYS",
    ),
    PartitionedTable(
        # EPA C9 (F14). Partitioned by the swap migration
        # `<rev>_partition_network_operations`; registered here so
        # `create_missing_partitions` keeps the ledger's months ahead of
        # the clock from the first tick after that migration lands. Like
        # `webhook_events` above, `table_exists` skips it cleanly on any
        # database where the swap has not run yet — but note the swap
        # keeps the NAME `network_operations`, so the gate is about the
        # table being partitioned, not about it existing.
        #
        # `feeds_rollups=False`: the network ledger has no
        # `variant_price_daily_rollups` coverage gate. What protects a
        # parent operation's totals from a child drop is
        # `app_shared.maintenance.ledger_summaries`, a separate
        # summarise-then-delete task — not this flag.
        name="network_operations",
        partition_key="created_at",
        feeds_rollups=False,
        retention_setting="RETENTION_NETWORK_OPERATIONS_DAYS",
    ),
)


def retention_days(entry: PartitionedTable, settings: Settings) -> int:
    """Resolve ``entry``'s retention window, in days, from ``settings``.

    Looks up ``entry.retention_setting`` as a ``Settings`` attribute name
    (e.g. ``"RETENTION_PRICE_OBSERVATIONS_DAYS"``) rather than hardcoding
    a duration, so the window stays DB/env-tunable (Principle IV, FR-017).
    """
    return getattr(settings, entry.retention_setting)


# ==========================================================================
# EPA C9 (F14) — retention per DATA CLASS, and the owner-ratification switch
# ==========================================================================
#
# `PARTITIONED_TABLES` above answers one narrow question: which parents
# does the monthly partition-creation job keep months ahead of, and by
# what column. It was never a retention policy — it cannot describe a
# table that is not partitioned (`variant_price_daily_rollups` had to be
# special-cased inside `retention.run_retention` for exactly that
# reason), it cannot describe a family that is a SUBSET of one table's
# rows (a browser subresource is not a table), and it has nowhere to
# record whether the owner has actually authorised deletion.
#
# `RETENTION_FAMILIES` is that missing layer. One entry per data class
# the retention policy names, each binding:
#
#   * `class_key`          — the stable name the OWNER types into
#                            `Settings.RETENTION_ENABLED_CLASSES` and
#                            signs off in `docs/RETENTION_POLICY.md`;
#   * `mechanism`          — HOW rows leave (drop a whole partition,
#                            delete bounded batches of rows, or
#                            summarise-then-delete);
#   * `retention_setting`  — the `Settings` attribute holding the window
#                            (Principle IV: durations stay env/DB
#                            tunable, the SET of families is a code
#                            constant);
#   * `feeds_rollups`      — carried through from `PartitionedTable` so a
#                            family that a rollup depends on keeps its
#                            verify-before-drop gate. FALSE for every
#                            family this task adds: nothing downstream
#                            aggregates the network ledger into
#                            `variant_price_daily_rollups`, and what
#                            protects a parent operation's totals is
#                            `app_shared.maintenance.ledger_summaries`,
#                            not this flag.
#
# THE SWITCH. `retention_class_enabled` is consulted before every drop,
# delete and summarisation in this package. `RETENTION_ENABLED_CLASSES`
# ships EMPTY, so on a fresh deployment every family is inert and the
# windows above are proposals rather than policy. That ordering is
# deliberate: a wrong retention window is discovered by noticing data is
# gone, which is the one class of bug no rollback fixes.

from enum import StrEnum  # noqa: E402  (kept beside the block it serves)


class RetentionMechanism(StrEnum):
    """How a family's expired rows actually leave the database.

    ``PARTITION_DROP``
        A whole monthly child partition is ``DROP TABLE``-ed once its
        ENTIRE range is past the cutoff (FR-015/018). Never a bulk
        ``DELETE`` on a raw append-heavy partition.
    ``ROW_DELETE``
        A bounded, keyed ``DELETE`` — the only mechanism available to a
        family that is not partitioned, or that is a *subset* of one
        table's rows (a terminal-state predicate).
    ``SUMMARIZE_THEN_DELETE``
        Rows are replaced by an aggregate that preserves the totals
        before they go (``app_shared.maintenance.ledger_summaries``).
        The distinction from ``ROW_DELETE`` is not cosmetic: it is the
        difference between losing a fact and compressing it.
    """

    PARTITION_DROP = "PARTITION_DROP"
    ROW_DELETE = "ROW_DELETE"
    SUMMARIZE_THEN_DELETE = "SUMMARIZE_THEN_DELETE"


@dataclass(frozen=True)
class RetentionFamily:
    """One data class the retention policy names, and how it is aged out.

    Attributes:
        class_key: Stable owner-facing name. This is the exact string a
            deployment puts in ``Settings.RETENTION_ENABLED_CLASSES`` and
            the heading `docs/RETENTION_POLICY.md` ratifies. It is NOT
            always a table name — ``network_operation_children`` is a
            subset of ``network_operations``.
        table: The physical table the family lives in.
        mechanism: See :class:`RetentionMechanism`.
        retention_setting: ``Settings`` attribute name holding the window
            in days (never a hardcoded literal — Principle IV).
        feeds_rollups: ``True`` only where a downstream rollup must be
            proven to cover the rows first (``price_observations``).
        timestamp_column: The column the age cutoff is applied to.
        row_predicate: Extra SQL restricting a ``ROW_DELETE`` family to
            the rows it actually owns — a terminal-state filter, so an
            in-flight row is never aged out from under the code still
            working on it. ``None`` for whole-table families.
        group_column: When set, a ``ROW_DELETE`` batch is chosen by
            DISTINCT value of this column so a group is deleted whole or
            not at all. Required wherever a DEFERRED constraint checks a
            group's total at COMMIT — splitting such a group across two
            batches makes the first batch look like a shortfall and
            aborts the pass.
    """

    class_key: str
    table: str
    mechanism: RetentionMechanism
    retention_setting: str
    feeds_rollups: bool
    timestamp_column: str
    row_predicate: str | None = None
    group_column: str | None = None


RETENTION_FAMILIES: tuple[RetentionFamily, ...] = (
    # --- pre-existing families, now named so the switch can gate them ---
    RetentionFamily(
        class_key="price_observations",
        table="price_observations",
        mechanism=RetentionMechanism.PARTITION_DROP,
        retention_setting="RETENTION_PRICE_OBSERVATIONS_DAYS",
        feeds_rollups=True,
        timestamp_column="scraped_at",
    ),
    RetentionFamily(
        class_key="request_attempts",
        table="request_attempts",
        mechanism=RetentionMechanism.PARTITION_DROP,
        retention_setting="RETENTION_REQUEST_ATTEMPTS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
    ),
    RetentionFamily(
        class_key="price_alert_events",
        table="price_alert_events",
        mechanism=RetentionMechanism.PARTITION_DROP,
        retention_setting="RETENTION_PRICE_ALERT_EVENTS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
    ),
    RetentionFamily(
        class_key="webhook_events",
        table="webhook_events",
        mechanism=RetentionMechanism.PARTITION_DROP,
        retention_setting="RETENTION_WEBHOOK_EVENTS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
    ),
    RetentionFamily(
        class_key="variant_price_daily_rollups",
        table="variant_price_daily_rollups",
        mechanism=RetentionMechanism.ROW_DELETE,
        retention_setting="RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS",
        feeds_rollups=False,
        # A `date`, not a timestamp: this table is keyed on the UTC
        # calendar day, which is why `run_retention` compares it against
        # a `date` cutoff rather than a `timestamptz` one.
        timestamp_column="date",
    ),
    # --- families added by EPA C9 (F14) ---------------------------------
    RetentionFamily(
        class_key="network_operations",
        table="network_operations",
        mechanism=RetentionMechanism.PARTITION_DROP,
        retention_setting="RETENTION_NETWORK_OPERATIONS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
    ),
    RetentionFamily(
        # A SUBSET of `network_operations`, not a table: rows carrying a
        # `parent_operation_id` (browser subresources). Its mechanism is
        # SUMMARIZE_THEN_DELETE because the parent's byte/duration totals
        # must survive the children by two years, and a plain delete
        # would take them with it.
        class_key="network_operation_children",
        table="network_operations",
        mechanism=RetentionMechanism.SUMMARIZE_THEN_DELETE,
        retention_setting="RETENTION_NETWORK_OPERATION_CHILDREN_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
        row_predicate="parent_operation_id IS NOT NULL",
    ),
    RetentionFamily(
        class_key="cost_allocations",
        table="network_operation_allocations",
        mechanism=RetentionMechanism.ROW_DELETE,
        retention_setting="RETENTION_COST_ALLOCATIONS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
        # One operation's allocations are deleted together or not at
        # all: `trg_noa_allocation_total` is a DEFERRED constraint
        # trigger that re-checks at COMMIT that they sum exactly to the
        # operation's cost, so a batch boundary inside a set would read
        # as a shortfall and abort the whole pass.
        group_column="operation_id",
    ),
    RetentionFamily(
        class_key="scrape_job_targets",
        table="scrape_job_targets",
        mechanism=RetentionMechanism.ROW_DELETE,
        retention_setting="RETENTION_SCRAPE_JOB_TARGETS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
        # Terminal statuses ONLY. `PENDING`/`STARTED`/`DEFERRED` are
        # live work — `DEFERRED` especially, which reads like an ending
        # and is documented on `ScrapeTargetStatus` as explicitly NOT
        # terminal (it goes back to `STARTED` on re-pickup).
        row_predicate="status IN ('COMPLETED', 'FAILED', 'SKIPPED', 'CANCELLED')",
    ),
    RetentionFamily(
        class_key="dispatch_intents",
        table="dispatch_intents",
        mechanism=RetentionMechanism.ROW_DELETE,
        retention_setting="RETENTION_DISPATCH_INTENTS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
        # `POSTED` is excluded on purpose: it is the one genuinely
        # ambiguous state (the run may or may not exist on the node), and
        # ageing it out would delete the only record that the ambiguity
        # was ever there. `PLANNED` is excluded because a planned intent
        # that is still planned has not been reconciled yet.
        row_predicate=(
            "state IN ('CONFIRMED', 'FAILED', 'RECONCILED_MISSING', 'SUPERSEDED')"
        ),
    ),
    RetentionFamily(
        class_key="costauth_reservations",
        table="cost_reservations",
        mechanism=RetentionMechanism.ROW_DELETE,
        retention_setting="RETENTION_COSTAUTH_RESERVATIONS_DAYS",
        feeds_rollups=False,
        timestamp_column="created_at",
        # Never a `RESERVED` row: that is a live lease against a budget,
        # and deleting one silently returns money the fleet has already
        # committed.
        row_predicate="state IN ('SETTLED', 'RELEASED')",
    ),
)

#: Every valid value of ``Settings.RETENTION_ENABLED_CLASSES`` (besides
#: the ``"*"`` wildcard). Read by that setting's validator so a typo'd
#: class name fails the deploy instead of quietly disabling a family the
#: owner believes they enabled.
RETENTION_CLASS_KEYS: frozenset[str] = frozenset(
    family.class_key for family in RETENTION_FAMILIES
)

#: The wildcard an operator uses to enable every registered family at
#: once. Deliberately explicit rather than "empty means all": the empty
#: list must mean *nothing*, because that is the value a database has
#: before anyone has decided anything.
RETENTION_ALL_CLASSES = "*"


def retention_family(class_key: str) -> RetentionFamily:
    """Return the :class:`RetentionFamily` named ``class_key``.

    Raises ``KeyError`` on an unknown key — a caller asking about a class
    that does not exist is a bug, not a reason to age nothing out.
    """
    for family in RETENTION_FAMILIES:
        if family.class_key == class_key:
            return family
    raise KeyError(f"unknown retention class {class_key!r}")


def retention_class_enabled(class_key: str, settings: Settings) -> bool:
    """Has the OWNER authorised deletion for this data class?

    ``False`` unless ``class_key`` (or the :data:`RETENTION_ALL_CLASSES`
    wildcard) appears in ``settings.RETENTION_ENABLED_CLASSES``. The
    default is an empty list, so the honest answer on an un-ratified
    deployment is "no" for every family — see this section's header for
    why the switch defaults closed.

    Deliberately does NOT validate ``class_key`` against
    :data:`RETENTION_CLASS_KEYS`: that check belongs at ``Settings``
    construction (where a typo can still fail a deploy loudly), and a
    second raise here would turn a misconfiguration into a crashed
    maintenance job rather than a skipped family.
    """
    enabled = getattr(settings, "RETENTION_ENABLED_CLASSES", ()) or ()
    return RETENTION_ALL_CLASSES in enabled or class_key in enabled


def retention_family_days(family: RetentionFamily, settings: Settings) -> int:
    """Resolve ``family``'s retention window, in days, from ``settings``.

    The :func:`retention_days` of the family layer — same Principle IV
    reasoning, same ``getattr``-by-name indirection.
    """
    return getattr(settings, family.retention_setting)
