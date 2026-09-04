"""Writers for ``workspace_entitlements`` — the seed and its staleness refresh.

C3 gave the engine a durable, local entitlement gate
(:meth:`app_shared.costauth.service.CostAuthorizationService.
_check_entitlement`) and, deliberately, **no writer**: W1.1 owns the real
SaaS->engine billing replication, and inventing a second source of truth
for "is this workspace paying" would have been worse than having none.

The consequence, discovered at go-live prep: the gate is fail-closed in
three independent ways (no row / stale row / non-ACTIVE row all deny), so
an engine whose ``workspace_entitlements`` table is EMPTY denies **all
paid work for every workspace**. The table is empty. Nothing writes it.

Owner decision (2026-08-26): *seed now, real ingest later.* This module
is the "seed now" half, and it is shaped so the "later" half can land
without a fight:

``evidence_version`` is the seam
---------------------------------
Every row this module writes carries an ``evidence_version`` beginning
:data:`SEEDED_EVIDENCE_VERSION_PREFIX` (``"seeded-"``). That prefix is
not decoration — it is the ownership marker both writers here honour:

* :func:`seed_workspace_entitlements` creates a row for a workspace that
  has none, and REFRESHES a row it recognises as its own. A row whose
  ``evidence_version`` does not start with the prefix was written by
  something else (the future real ingest, or an operator), and is left
  **exactly** as found — the seed never overwrites evidence it did not
  produce, because evidence a real billing event created is strictly
  better than the placeholder this module can synthesise.
* :func:`refresh_seeded_entitlements` re-stamps ``observed_at`` on
  seeded rows ONLY, so a placeholder never ages past
  ``DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS`` (86400) and starts
  denying — while a real-ingest row's freshness stays entirely the
  ingest's business. Two writers touching the same rows on different
  clocks is exactly the fight this prefix exists to prevent.

``state`` is deliberately NOT part of the refresh predicate
------------------------------------------------------------
The refresh matches on the prefix alone, so it also touches a seeded row
an operator has manually moved to ``PAST_DUE``/``SUSPENDED``/
``CANCELLED``. That is safe and intentional: ``observed_at`` is a
freshness fact, not an authorization one, and the gate denies a
non-ACTIVE row regardless of how fresh it is. Making the refresh skip
non-ACTIVE rows would instead mean an operator's deliberate suspension
silently decays into "stale" — the same denial, with a misleading reason
in the operator's face.

Why a module and not just a script
-----------------------------------
The seed runs from ``scripts/seed_workspace_entitlements.py`` (an
operator, once, at deploy) and the refresh runs from the scheduler's
durable cadence (a worker, every few hours, forever). Two entry points,
one contract — and the prefix constant above must have exactly one
spelling or the two writers stop recognising each other's rows.

Both functions take a :class:`~sqlalchemy.orm.Session` and never open one:
they are cross-tenant by construction (one pass must see every
workspace's row under ``FORCE ROW LEVEL SECURITY``), so the caller
supplies the sanctioned BYPASSRLS system seam
(:func:`app_shared.database.get_system_session`) — the same seam, for the
same reason, as the lease sweeper and the scheduler's due-rule claim.
Neither function commits; the caller owns the transaction boundary, which
is what lets the seeding script offer a genuinely read-only dry run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app_shared.models.cost_authorization import EntitlementState, WorkspaceEntitlement
from app_shared.models.identity import Workspace

__all__ = [
    "SEEDED_EVIDENCE_VERSION_PREFIX",
    "EntitlementSeedReport",
    "is_seeded_evidence_version",
    "refresh_seeded_entitlements",
    "seed_workspace_entitlements",
    "seeded_evidence_version",
]

#: The ownership marker on every ``evidence_version`` this module writes.
#: Both writers in this module key off it, and NOTHING else may write a
#: version starting with it — a real SaaS billing event's version tag is
#: whatever the SaaS calls it, and the day it happens to start with
#: ``"seeded-"`` is the day the refresher starts fighting the ingest.
SEEDED_EVIDENCE_VERSION_PREFIX = "seeded-"


def seeded_evidence_version(day: date_type) -> str:
    """The evidence version for a seed run on ``day`` — ``seeded-YYYY-MM-DD``.

    Dated rather than constant so an operator reading a denial (or a
    ``network_operations.entitlement_version`` stamp) can tell WHICH seed
    run produced the evidence a decision was made under, exactly as C1's
    version tags allow for the ledger. Text, per the same precedent: a
    later change of scheme must not silently reinterpret an old decision.
    """
    return f"{SEEDED_EVIDENCE_VERSION_PREFIX}{day.isoformat()}"


def is_seeded_evidence_version(evidence_version: str | None) -> bool:
    """True when ``evidence_version`` was written by this module.

    ``None`` is **not** seeded: a row with no version tag at all is a row
    this module did not write, and the fail-safe reading of "I don't
    recognise this" is always "leave it alone".
    """
    return evidence_version is not None and evidence_version.startswith(
        SEEDED_EVIDENCE_VERSION_PREFIX
    )


@dataclass(frozen=True)
class EntitlementSeedReport:
    """What one seed pass did — or, in dry-run, would have done.

    The three buckets are mutually exclusive and together account for
    every existing workspace, so ``created + refreshed + left_alone`` is
    the workspace count the pass observed. ``left_alone`` is the
    interesting one for an operator: those workspaces already carry
    evidence some other writer produced, and a growing ``left_alone``
    across runs is how the real ingest landing becomes visible.
    """

    created: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    refreshed: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    left_alone: tuple[uuid.UUID, ...] = field(default_factory=tuple)

    @property
    def workspaces_seen(self) -> int:
        """Every workspace the pass classified."""
        return len(self.created) + len(self.refreshed) + len(self.left_alone)


def seed_workspace_entitlements(
    session: Session,
    *,
    now: datetime,
    evidence_version: str,
    apply: bool,
) -> EntitlementSeedReport:
    """Upsert an ACTIVE, freshly-observed entitlement row per workspace.

    Idempotent by classification rather than by ``ON CONFLICT``: every
    workspace falls into exactly one of three cases, and re-running the
    pass moves nothing between them except by re-stamping a row this
    module already owns.

    * **no row** -> create one, ``ACTIVE``, ``observed_at=now``,
      ``evidence_version=evidence_version``.
    * **a seeded row** (:func:`is_seeded_evidence_version`) -> re-stamp
      ``state``/``evidence_version``/``observed_at`` in place. Re-stamping
      ``state`` back to ``ACTIVE`` is deliberate: a seeded row is a
      placeholder for "we have not wired billing yet", and the only thing
      that should ever move it off ``ACTIVE`` permanently is a real
      ingest row replacing it wholesale.
    * **anything else** -> left untouched, and reported. See the module
      docstring: real evidence always beats a placeholder.

    Args:
        session: a BYPASSRLS system session (the pass is cross-tenant).
        now: aware UTC instant stamped as ``observed_at`` — injected, not
            read from the clock here, so a dry run and its apply report
            the same numbers and tests are deterministic.
        evidence_version: the version tag to write. MUST start with
            :data:`SEEDED_EVIDENCE_VERSION_PREFIX` or the rows written
            would be invisible to :func:`refresh_seeded_entitlements` and
            would go stale-deny within a day — so this raises rather than
            producing rows nothing will maintain.
        apply: ``False`` classifies and reports without mutating
            anything (not even in the session's identity map). Never
            commits either way — the caller owns the transaction.

    Returns:
        The :class:`EntitlementSeedReport` for this pass.

    Raises:
        ValueError: if ``evidence_version`` is not a seeded version.
    """
    if not is_seeded_evidence_version(evidence_version):
        raise ValueError(
            f"evidence_version {evidence_version!r} must start with "
            f"{SEEDED_EVIDENCE_VERSION_PREFIX!r}: a seeded row that the refresh "
            "cadence cannot recognise goes stale-deny within "
            "DEFAULT_ENTITLEMENT_MAX_EVIDENCE_AGE_SECONDS"
        )

    workspace_ids = list(
        session.execute(select(Workspace.id).order_by(Workspace.id)).scalars()
    )
    # One read of the whole (tiny, one-row-per-workspace) table rather
    # than a per-workspace SELECT: the pass is O(workspaces) either way,
    # but this shape keeps it to two queries regardless of fleet size.
    existing = {
        row.workspace_id: row
        for row in session.execute(select(WorkspaceEntitlement)).scalars()
    }

    created: list[uuid.UUID] = []
    refreshed: list[uuid.UUID] = []
    left_alone: list[uuid.UUID] = []

    for workspace_id in workspace_ids:
        row = existing.get(workspace_id)
        if row is None:
            created.append(workspace_id)
            if apply:
                session.add(
                    WorkspaceEntitlement(
                        workspace_id=workspace_id,
                        state=EntitlementState.ACTIVE,
                        plan_code=None,
                        evidence_version=evidence_version,
                        observed_at=now,
                    )
                )
            continue
        if is_seeded_evidence_version(row.evidence_version):
            refreshed.append(workspace_id)
            if apply:
                row.state = EntitlementState.ACTIVE
                row.evidence_version = evidence_version
                row.observed_at = now
            continue
        left_alone.append(workspace_id)

    return EntitlementSeedReport(
        created=tuple(created),
        refreshed=tuple(refreshed),
        left_alone=tuple(left_alone),
    )


def refresh_seeded_entitlements(session: Session, *, now: datetime) -> int:
    """Re-stamp ``observed_at`` on SEEDED rows only. Returns rows touched.

    One set-based ``UPDATE ... WHERE evidence_version LIKE 'seeded-%'``,
    not a read-then-write loop: the predicate IS the ownership check, so
    there is no window in which a real-ingest row could be selected as
    seeded and then written. A row with a ``NULL`` ``evidence_version``
    never matches (``NULL LIKE ...`` is ``NULL``), which is the correct
    fail-safe — an untagged row is not ours.

    Deliberately does not filter on ``state``; see the module docstring
    for why re-stamping a suspended placeholder is both harmless and
    kinder to the operator reading the denial.

    EPA C2 (2026-09-03) makes that ownership predicate load-bearing in a
    second place: the SaaS control plane
    (:mod:`app_shared.control_plane.service`) now writes real billing
    evidence onto these same rows, stamping ``evidence_version`` as
    ``str(int)`` — never with the ``seeded-`` prefix. So this refresher
    and the replication path can never fight over a row: a replicated row
    stops matching ``LIKE 'seeded-%'`` the moment the SaaS first writes to
    it, and its ``observed_at`` is thereafter owned solely by the SaaS's
    ``as_of``. ``test_refresh_leaves_a_control_plane_numeric_version_alone``
    holds that line.

    Idempotent and safe to run concurrently with itself: the statement is
    a blind re-stamp to the caller's ``now``, so a duplicate delivery
    writes the same freshness twice and two runners cannot disagree about
    anything. Does not commit — the caller owns the transaction.
    """
    result = session.execute(
        update(WorkspaceEntitlement)
        .where(
            WorkspaceEntitlement.evidence_version.startswith(
                SEEDED_EVIDENCE_VERSION_PREFIX, autoescape=True
            )
        )
        .values(observed_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
