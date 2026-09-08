"""``refresh_rule_occurrences`` — the scheduler's due-time identity ledger.

EPA B3 (F07, 2026-09-07). One row per *occurrence* a refresh rule was
actually fired for, where an occurrence is identified by
``(rule_id, scheduled_for)`` and ``scheduled_for`` is the rule's
``next_run_at`` at the moment of the claim, truncated to whole seconds.

## Why a table and not a lock

``fire_refresh_rule`` already holds the rule row under ``FOR UPDATE SKIP
LOCKED``, which stops two schedulers firing the SAME row at the SAME
instant. It does not stop the other shape of duplicate: replica A loads a
due candidate, replica B fires it and advances ``next_run_at``, A's lock
then succeeds on a row that is no longer due and fires the occurrence a
second time. The recheck (``next_run_at <= now``) closes most of that
window; this table closes it completely and *durably*, because the
primary key is the occurrence identity itself — a second INSERT for the
same ``(rule_id, scheduled_for)`` raises ``IntegrityError`` no matter how
the two transactions interleaved, and no matter which replica, process or
restart the second attempt came from.

Duplicate suppression therefore stops depending on two schedulers
observing each other and starts depending on Postgres, which is the only
participant that can see both.

## Shape: fleet-wide, no RLS

Deliberately **no** ``workspace_id`` and **no** RLS — the same shape and
rationale as ``maintenance_cadences`` / ``rollup_watermarks``: scheduler
bookkeeping, written only by the cross-tenant claim on the BYPASSRLS
system session, with no tenant CRUD surface and nothing a tenant may read
(``scripts/rls_table_manifest.txt`` files it SYSTEM). ``rule_id`` is not
declared as a foreign key for the same reason the fleet tables are not:
the occurrence log is an audit of what the scheduler DID, and a rule that
is later deleted must not take the evidence of its firings with it.

## Not a ``Base`` subclass

:class:`~app_shared.models.base.Base` mints a surrogate UUIDv7 ``id``
primary key on every subclass (FR-003). Here the composite
``(rule_id, scheduled_for)`` IS the identity — a surrogate key would
make the uniqueness that carries the whole guarantee a secondary
constraint. So this is a Core :class:`~sqlalchemy.schema.Table` declared
against the shared metadata: same migrations, same naming convention,
same autogenerate visibility, no surrogate key.
"""

from __future__ import annotations

from sqlalchemy import Column, Index, Table, Uuid

from app_shared.models.base import TZDateTime, metadata

#: ``refresh_rule_occurrences`` — PK ``(rule_id, scheduled_for)``.
refresh_rule_occurrences = Table(
    "refresh_rule_occurrences",
    metadata,
    #: The rule that fired. Not an FK — see the module docstring.
    Column("rule_id", Uuid(as_uuid=True), primary_key=True, nullable=False),
    #: The occurrence's due time: the rule's ``next_run_at`` at claim
    #: time, truncated to whole seconds so a microsecond of clock or
    #: round-trip drift cannot mint a second "distinct" occurrence.
    Column("scheduled_for", TZDateTime(), primary_key=True, nullable=False),
    #: Wall clock of the firing itself (``>= scheduled_for`` for an
    #: on-time pass, later for a backlogged one).
    Column("fired_at", TZDateTime(), nullable=False),
    #: The job the firing created, stamped after ``create_scope_job``
    #: returns. ``NULL`` when the rule's scope resolved to zero matches
    #: (FR-015: no matches -> no job) — the occurrence is still claimed,
    #: because it still happened.
    Column("scrape_job_id", Uuid(as_uuid=True), nullable=True),
    #: Operator query: "what did rule X actually fire, most recent first".
    Index("ix_refresh_rule_occurrences_fired_at", "fired_at"),
)

__all__ = ["refresh_rule_occurrences"]
