"""Dangling soft-reference tolerance check (SPEC-15 US4, T032,
contracts/soft-reference-tolerance.md, research R10).

The one soft reference that dangles into a droppable partition is
``match_current_prices.observation_id`` (plain nullable ``Uuid``, **no
FK**, into ``price_observations.id`` --
``app_shared.models.observations.MatchCurrentPrice``). After retention
(this spec) drops the referenced month's partition, a previously-valid
``observation_id`` may point at a row that no longer exists -- by
design (Constitution §22 forbids FKs into partitioned tables, since
they would block partition drop). This is the steady state, not
corruption.

:func:`count_tolerated_dangling_refs` is a small operator-visibility
probe -- it counts how many ``match_current_prices`` rows currently
have a dangling ``observation_id`` and reports that count as
**expected/tolerated**, never as an error. It is inherently
cross-tenant (one count spans every workspace), so it runs on the
BYPASSRLS system session like the rest of this package's catalog/
coverage reads (research R9, ``# noqa: workspace-scope``). The caller
(the retention task, T033) MUST treat this as best-effort: any failure
here must never block or fail the core create/rollup/drop guarantees
(FR-024).

Scraping-free (Constitution I/V) -- SQLAlchemy + stdlib only.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def _count_tolerated_dangling_refs_stmt():
    """Build the (unexecuted) dangling-soft-reference count statement.

    Split out from :func:`count_tolerated_dangling_refs` so its
    rendered SQL can be asserted in a pure unit test without a live DB
    or session (mirrors ``app_shared.maintenance.partitions._to_regclass_stmt``
    / ``app_shared.maintenance.retention._rollups_cover_stmt``). Counts
    every ``match_current_prices`` row whose ``observation_id`` is set
    but does not resolve to a live ``price_observations.id`` --
    contracts/soft-reference-tolerance.md §FR-022.
    """
    return text(
        """
        SELECT COUNT(*) FROM match_current_prices
        WHERE observation_id IS NOT NULL
          AND observation_id NOT IN (SELECT id FROM price_observations)
        """
    )


def count_tolerated_dangling_refs(session: Session) -> int:
    """Count ``match_current_prices`` rows with a dangling ``observation_id``.

    Informational only (FR-022) -- a non-zero count is the *expected*
    steady state once retention has dropped a ``price_observations``
    partition a winning observation pointed into, never a corruption
    signal. Cross-tenant by nature (one count spans every workspace),
    hence the unscoped read on the system session.
    """
    result = session.execute(_count_tolerated_dangling_refs_stmt())  # noqa: workspace-scope
    value = result.scalar()
    return int(value) if value is not None else 0
