"""target lifecycle timestamps + attempt identity/phase timings (A5)

Revision ID: b6f1c40a97d2
Revises: 193ac27f0dc2
Create Date: 2026-09-07

EPA A5 (deep dive §5, audit Stage A). Makes the *life* of a target
recordable, not just its final verdict.

Why these columns exist
-----------------------
``scrape_job_targets.status`` answers "where is this target now". It has
never been able to answer "where did the time go", and the answer
mattered: the only latency figure the engine kept was
``request_attempts.response_time_ms`` -- one number covering proxy
connect, the site's think time, the download, and our own extraction, so
every "scrapes are slow" investigation ended in an argument instead of an
attribution.

Two halves, one revision because they describe one attempt from two
sides:

``scrape_job_targets`` (six ``TIMESTAMPTZ NULL``)
    ``claimed_at``, ``remote_accepted_at``, ``first_network_at``,
    ``document_received_at``, ``extraction_finished_at``,
    ``persisted_at`` -- the wall-clock boundaries of the target's life,
    written ONLY through ``app_shared.jobs.targets`` (``mark_target`` /
    ``stamp_target_timestamps``) with ``COALESCE`` first-writer-wins
    semantics, so a retried attempt can never move a boundary that
    already happened. Together with the existing ``created_at`` /
    ``dispatched_at`` / ``started_at`` they are what
    ``crawmatic_target_phase_p95_seconds{phase=due_to_dispatch|
    dispatch_to_first_network|first_network_to_persisted}``
    (``app_shared.opsmetrics.emit``) is computed from.

``request_attempts`` (one ``UUID NOT NULL`` + four ``INT NULL``)
    ``attempt_uuid`` is the PRODUCER-side identity of one attempt, minted
    by the spider before the fetch. The row's own ``(id, created_at)``
    composite PK only exists once the persistence pipeline has written
    it, so it cannot correlate a spider log line, a ``network_operations``
    entry and this row; ``attempt_uuid`` can. ``connect_ms`` / ``ttfb_ms``
    / ``read_ms`` / ``extract_ms`` split ``response_time_ms`` into the
    four phases that have four different owners (the proxy vendor, the
    site, page weight, and our own code -- the last being the only one a
    code change fixes).

NULL means "not measured", never zero
-------------------------------------
Every column here except ``attempt_uuid`` is nullable with no server
default, and nothing coerces a missing boundary to ``0``/``now()``. A
pre-A5 row, a never-dispatched target and a transport that does not
report a given boundary all legitimately have nothing to say; a
fabricated value would show up as a zero-length phase and quietly
flatter the p95 gauges this migration exists to make honest. The metric
SQL filters NULLs out rather than counting them.

``attempt_uuid``: NOT NULL, ``DEFAULT gen_random_uuid()``
---------------------------------------------------------
Deliberately NOT backfilled with one shared sentinel -- that would make
every historical row look like the SAME attempt, which is worse than no
identity at all. ``gen_random_uuid()`` (built into PostgreSQL 13+, no
``pgcrypto`` extension needed) gives each existing row its own. It is
deliberately NOT unique-constrained: a unique index on a
RANGE-partitioned table must include the partition key, and this column's
job is correlation, not arbitration.

**Operational note — this ADD COLUMN rewrites ``request_attempts``.** A
non-volatile default is stored as metadata and is free; a *volatile* one
(``gen_random_uuid()``) is not -- PostgreSQL must materialise a distinct
value per row, so every existing monthly partition is rewritten under an
``ACCESS EXCLUSIVE`` lock. That is bounded by the retention window
(``docs``: 90 days of ``request_attempts``), and this repository's
migrations run as a one-shot job (``contracts/migration-job.md``) with no
concurrent writer, so it is acceptable here. If a future deployment's
table is large enough for the lock to matter, the staged equivalent is:
add the column NULL with no default, backfill in batches per partition,
``SET DEFAULT``, then ``SET NOT NULL`` -- same end state, no long lock.

``request_attempts`` is monthly-RANGE-partitioned (``2db33dea5e14``);
``ALTER TABLE ... ADD COLUMN`` on the partitioned **parent** propagates
to every existing partition automatically (the ``0fc4c9c9c8b3`` /
``d5e8a3c164f2`` precedent) -- no per-partition ``op.execute``.

RLS: unaffected. Plain column additions to already-RLS'd tables need no
new policy.

Reversible: ``downgrade`` drops all eleven columns. It loses the timing
evidence and the attempt identities, never an attempt or a target row.

Hand-authored (matches ``app_shared.models.jobs.ScrapeJobTarget`` and
``app_shared.models.observations.RequestAttempt`` exactly) -- this build
environment has no live Postgres connection for autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b6f1c40a97d2'
down_revision: Union[str, Sequence[str], None] = '193ac27f0dc2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The six per-phase boundaries on `scrape_job_targets`, in lifecycle
#: order. All `TIMESTAMPTZ NULL`, no server default.
_TARGET_PHASE_COLUMNS: tuple[str, ...] = (
    "claimed_at",
    "remote_accepted_at",
    "first_network_at",
    "document_received_at",
    "extraction_finished_at",
    "persisted_at",
)

#: The four per-phase millisecond splits on `request_attempts`.
#: All `INTEGER NULL`, no server default.
_ATTEMPT_PHASE_MS_COLUMNS: tuple[str, ...] = (
    "connect_ms",
    "ttfb_ms",
    "read_ms",
    "extract_ms",
)


def upgrade() -> None:
    """Upgrade schema: target phase timestamps + attempt identity/timings."""
    for column in _TARGET_PHASE_COLUMNS:
        op.add_column(
            "scrape_job_targets",
            sa.Column(column, sa.DateTime(timezone=True), nullable=True),
        )

    # NOT NULL with a volatile default: every pre-existing row gets its
    # OWN identity (see the module docstring on why a shared sentinel
    # would be worse than nothing). This rewrites the partitions.
    op.add_column(
        "request_attempts",
        sa.Column(
            "attempt_uuid",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    for column in _ATTEMPT_PHASE_MS_COLUMNS:
        op.add_column(
            "request_attempts",
            sa.Column(column, sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    """Downgrade schema: drop the A5 lifecycle/timing columns."""
    for column in reversed(_ATTEMPT_PHASE_MS_COLUMNS):
        op.drop_column("request_attempts", column)
    op.drop_column("request_attempts", "attempt_uuid")

    for column in reversed(_TARGET_PHASE_COLUMNS):
        op.drop_column("scrape_job_targets", column)
