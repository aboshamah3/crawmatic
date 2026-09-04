"""Control-plane service — apply the SaaS's declared intent to the engine.

The routes in ``apps/api/app/routers/control_plane.py`` are a thin HTTP
shell over this module: they validate the wire shape (Pydantic), resolve
the workspace, and translate the outcomes below into status codes. Every
rule that decides *what happens to engine state* lives here, so it can be
exercised without a request and without a database.

Three deliberate design points, each of which cost a bug somewhere else
first:

**1. Two vocabularies, one translation table.** The SaaS speaks
``ACTIVE``/``PAUSED``/``DELINQUENT``/``CANCELED``/``KILLED``; the engine's
:class:`~app_shared.models.cost_authorization.EntitlementState` speaks
``ACTIVE``/``PAST_DUE``/``SUSPENDED``/``CANCELLED`` (British spelling, and
no separate "killed"). :data:`SAAS_STATUS_TO_ENGINE_STATE` is the only
place that mapping exists, and :data:`ENGINE_STATE_TO_SAAS_STATUS` is its
inverse — used when the state route echoes evidence back, because the
SaaS reconciler compares the echoed status against ITS vocabulary and
would otherwise see permanent drift on every non-``ACTIVE`` tenant.

The inverse is lossy in exactly one place: ``PAUSED`` and ``KILLED`` both
land on ``SUSPENDED``, so a killed tenant is echoed back as ``PAUSED``.
That is a bounded cost (the SaaS re-pushes an entitlement it already
agrees with, and the engine keeps denying paid work either way), not a
correctness hole, and closing it would need a new column — see this
module's note in the C2 task report.

**2. Evidence versions are monotonic, and a seed never outranks the
SaaS.** ``evidence_version`` is stored as text (C1's precedent: a scheme
change must not silently reinterpret an old decision) but compared as an
integer. A stored ``seeded-*`` version — written by
:mod:`app_shared.costauth.entitlements`, which stamps freshness onto
placeholder rows — reads as version ``0``, so real SaaS evidence always
wins over a seed. A stored value that is neither numeric nor seeded is
also read as ``0``, with a warning: refusing the write instead would
wedge replication behind a row nobody can explain.

**3. A monitor rule adopts an existing workspace refresh rule rather than
adding a second one.** A tenant that already has an enabled
``WORKSPACE``-scoped :class:`~app_shared.models.refresh_rules.RefreshRule`
(every long-lived tenant does) must not gain a second daily sweep the
first time the SaaS declares a monitor — that would double the crawl bill
for no new coverage. So the first ``MONITOR`` upsert links to the rule
already there and drives it; only a workspace with none gets a fresh
``cp:<external_id>`` rule.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app_shared.costauth.entitlements import is_seeded_evidence_version
from app_shared.enums import ProductStatus, ScrapeScope
from app_shared.models.catalog import Product
from app_shared.models.control_plane import ControlPlaneRule
from app_shared.models.cost_authorization import EntitlementState, WorkspaceEntitlement
from app_shared.models.identity import Workspace
from app_shared.models.refresh_rules import RefreshRule
from app_shared.scheduling.cadence import compute_next_run_at

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_INTERVAL_MINUTES",
    "CeilingVerdict",
    "ENGINE_STATE_TO_SAAS_STATUS",
    "EntitlementWrite",
    "RuleSpec",
    "SAAS_STATUS_TO_ENGINE_STATE",
    "check_product_ceiling",
    "check_product_ceiling_for_upsert",
    "count_new_products",
    "delete_rule",
    "get_entitlement",
    "get_rule",
    "list_rules",
    "quarantine_entitlement",
    "quarantine_rule",
    "replicate_entitlement",
    "saas_status_for",
    "stored_evidence_version",
    "upsert_rule",
    "workspace_exists",
]


#: The coarse cadence the SaaS sells -> the exact interval the engine
#: schedules on. The vocabulary is the SaaS's (C1: the engine records what
#: it was told rather than adjudicating it); this table is the only place
#: it becomes minutes.
CADENCE_INTERVAL_MINUTES: dict[str, int] = {
    "HOURLY": 60,
    "EVERY_6H": 360,
    "TWICE_DAILY": 720,
    "DAILY": 1440,
    "WEEKLY": 10080,
}

#: SaaS status -> engine entitlement state. ``KILLED`` and ``PAUSED``
#: deliberately collapse onto ``SUSPENDED``: the engine's question is
#: only "may this tenant do paid work", and both answers are "no,
#: reversibly". ``CANCELED`` (US spelling on the wire) becomes
#: ``CANCELLED`` (the engine enum's spelling).
SAAS_STATUS_TO_ENGINE_STATE: dict[str, EntitlementState] = {
    "ACTIVE": EntitlementState.ACTIVE,
    "DELINQUENT": EntitlementState.PAST_DUE,
    "PAUSED": EntitlementState.SUSPENDED,
    "KILLED": EntitlementState.SUSPENDED,
    "CANCELED": EntitlementState.CANCELLED,
}

#: The inverse, for the state echo. Lossy on ``SUSPENDED`` (see the module
#: docstring): a killed tenant reads back as ``PAUSED``.
ENGINE_STATE_TO_SAAS_STATUS: dict[EntitlementState, str] = {
    EntitlementState.ACTIVE: "ACTIVE",
    EntitlementState.PAST_DUE: "DELINQUENT",
    EntitlementState.SUSPENDED: "PAUSED",
    EntitlementState.CANCELLED: "CANCELED",
}


@dataclass(frozen=True)
class RuleSpec:
    """One rule as the SaaS declared it — the payload of :func:`upsert_rule`.

    ``owner_tag`` is ``None`` on the replace path (``PUT /rules/{id}``
    carries no owner tag), and a ``None`` never CLEARS a stored tag: the
    tag is durable attribution, not a toggle, and the only caller that
    knows it is the create path.
    """

    external_id: str
    kind: str
    cadence: str
    enabled: bool = True
    target_external_ids: list[str] = field(default_factory=list)
    owner_tag: str | None = None


@dataclass(frozen=True)
class EntitlementWrite:
    """One entitlement replication, in the SaaS's own vocabulary."""

    plan: str
    status: str
    product_ceiling: int | None
    as_of: datetime
    evidence_version: int


@dataclass(frozen=True)
class CeilingVerdict:
    """The product-admission answer for one write.

    ``exceeded`` is ``False`` whenever there is no ceiling to enforce (no
    evidence row, or a ``NULL`` ``product_ceiling`` — C1: ``NULL`` means
    "no ceiling was recorded", never "zero").
    """

    exceeded: bool
    ceiling: int | None = None
    active: int = 0
    requested: int = 0


# --- workspace + rule reads --------------------------------------------


def workspace_exists(session: Session, workspace_id: uuid.UUID) -> bool:
    """True when ``workspace_id`` names a real workspace.

    Every control-plane route calls this first so a stale SaaS-side
    workspace id produces a readable 404 instead of an FK violation
    surfacing as a 500 (the same reasoning as
    ``admin.create_connector_key``).
    """
    row = session.execute(
        select(Workspace.id).where(Workspace.id == workspace_id)  # noqa: workspace-scope
    ).first()
    return row is not None


def get_rule(
    session: Session, workspace_id: uuid.UUID, external_id: str
) -> ControlPlaneRule | None:
    """The workspace's rule with this SaaS id, or ``None``.

    ``ControlPlaneRule`` is deliberately NOT in
    :data:`app_shared.repository.WORKSPACE_OWNED_MODELS`: like
    :mod:`app_shared.costauth.service`, this surface runs on the
    cross-workspace admin session, so every statement predicates
    ``workspace_id`` explicitly instead of relying on a scoped helper.
    """
    return session.execute(
        select(ControlPlaneRule).where(
            ControlPlaneRule.workspace_id == workspace_id,
            ControlPlaneRule.external_id == external_id,
        )
    ).scalar_one_or_none()


def list_rules(session: Session, workspace_id: uuid.UUID) -> list[ControlPlaneRule]:
    """Every rule declared for this workspace (the ``/state`` echo)."""
    return list(
        session.execute(
            select(ControlPlaneRule)
            .where(ControlPlaneRule.workspace_id == workspace_id)
            .order_by(ControlPlaneRule.external_id)
        )
        .scalars()
        .all()
    )


def get_entitlement(
    session: Session, workspace_id: uuid.UUID
) -> WorkspaceEntitlement | None:
    """The workspace's entitlement evidence row, or ``None``."""
    rows = (
        session.execute(
            select(WorkspaceEntitlement).where(
                WorkspaceEntitlement.workspace_id == workspace_id
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


def saas_status_for(state: EntitlementState | str | None) -> str:
    """Echo an engine state back in the SaaS's vocabulary (module docstring)."""
    if state is None:
        return ""
    try:
        member = EntitlementState(state)
    except ValueError:
        return str(state)
    return ENGINE_STATE_TO_SAAS_STATUS.get(member, str(member))


def stored_evidence_version(row: WorkspaceEntitlement | None) -> int:
    """The stored ``evidence_version`` as an integer, defaulting to ``0``.

    ``0`` for: no row, no version tag, a ``seeded-*`` placeholder, or a
    value that will not parse as an integer. In every one of those cases
    the correct answer is "the SaaS's evidence outranks what we hold" —
    the alternative (refusing the write) would wedge replication behind a
    row nobody can explain. The unparseable case is logged because,
    unlike the others, it means something wrote a version tag this engine
    does not understand.
    """
    if row is None or row.evidence_version is None:
        return 0
    raw = row.evidence_version
    if is_seeded_evidence_version(raw):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "control-plane: workspace %s holds a non-numeric, non-seeded "
            "evidence_version %r; treating it as version 0 so replicated "
            "SaaS evidence is applied",
            row.workspace_id,
            raw,
        )
        return 0


# --- rules --------------------------------------------------------------


def _adopt_or_create_refresh_rule(
    session: Session, row: ControlPlaneRule
) -> RefreshRule | None:
    """Return the engine rule this intent drives, creating one if needed."""
    if row.refresh_rule_id is not None:
        return (
            session.execute(
                select(RefreshRule).where(
                    RefreshRule.workspace_id == row.workspace_id,
                    RefreshRule.id == row.refresh_rule_id,
                )
            )
            .scalars()
            .first()
        )

    adopted = (
        session.execute(
            select(RefreshRule).where(
                RefreshRule.workspace_id == row.workspace_id,
                RefreshRule.scope == ScrapeScope.WORKSPACE,
                RefreshRule.enabled.is_(True),
            )
        )
        .scalars()
        .first()
    )
    if adopted is not None:
        row.refresh_rule_id = adopted.id
        return adopted

    created = RefreshRule(
        workspace_id=row.workspace_id,
        name=f"cp:{row.external_id}",
        scope=ScrapeScope.WORKSPACE,
        interval_minutes=CADENCE_INTERVAL_MINUTES[row.cadence],
        enabled=row.enabled and not row.quarantined,
    )
    session.add(created)
    session.flush()
    row.refresh_rule_id = created.id
    return created


def _apply_cadence(rule: RefreshRule, row: ControlPlaneRule, *, now: datetime) -> None:
    """Drive the engine rule from the intent row's cadence + lifecycle.

    A rule the control plane ADOPTED may be cron-driven, and
    ``refresh_rules`` allows exactly one cadence field
    (``ck_refresh_rules_exactly_one_cadence``). Clobbering an operator's
    cron with an interval would both violate that invariant mid-write and
    silently discard a hand-tuned schedule, so a cron rule keeps its cron
    and only its ``enabled``/``next_run_at`` are driven from here.
    """
    if rule.cron_expression:
        logger.warning(
            "control-plane: refresh rule %s adopted by %r is cron-driven; "
            "keeping its cron expression rather than applying cadence %s",
            rule.id,
            row.external_id,
            row.cadence,
        )
    else:
        rule.interval_minutes = CADENCE_INTERVAL_MINUTES[row.cadence]

    rule.enabled = bool(row.enabled) and not bool(row.quarantined)
    rule.next_run_at = compute_next_run_at(rule, now)


def upsert_rule(
    session: Session,
    workspace_id: uuid.UUID,
    payload: RuleSpec,
    *,
    now: datetime,
) -> tuple[ControlPlaneRule, bool]:
    """Create or replace one declared rule. Returns ``(row, created)``.

    Idempotent on ``(workspace_id, external_id)`` — a re-delivered create
    updates the row it already wrote and reports ``created=False``, which
    is what lets the route answer 201 once and 200 on every replay with
    the same id.

    ``REPRICE`` is stored and nothing else: the engine has no repricing
    executor, the SaaS runs pricing rules locally, and inventing a
    refresh rule for one would schedule crawls nobody asked for.
    """
    row = get_rule(session, workspace_id, payload.external_id)
    created = row is None
    if row is None:
        row = ControlPlaneRule(
            workspace_id=workspace_id, external_id=payload.external_id
        )
        session.add(row)

    row.kind = payload.kind
    row.cadence = payload.cadence
    row.enabled = payload.enabled
    row.target_external_ids = list(payload.target_external_ids)
    if payload.owner_tag is not None:
        row.owner_tag = payload.owner_tag
    session.flush()

    if payload.kind == "MONITOR":
        refresh_rule = _adopt_or_create_refresh_rule(session, row)
        if refresh_rule is not None:
            _apply_cadence(refresh_rule, row, now=now)
    session.flush()
    return row, created


def quarantine_rule(
    session: Session,
    row: ControlPlaneRule,
    *,
    reason: str,
    grace_until: datetime | None,
) -> None:
    """Stop the rule, on the record. Reversible by a later upsert."""
    row.quarantined = True
    row.quarantine_reason = reason
    row.grace_until = grace_until
    refresh_rule = _linked_refresh_rule(session, row)
    if refresh_rule is not None:
        refresh_rule.enabled = False
    session.flush()


def delete_rule(session: Session, row: ControlPlaneRule) -> None:
    """Forget the declared intent and stop what it was driving.

    The engine rule is DISABLED, never deleted: it may have been adopted
    (it predates the control plane and other things may depend on it),
    and deleting it would take a tenant's whole refresh schedule with a
    single rule retirement.
    """
    refresh_rule = _linked_refresh_rule(session, row)
    if refresh_rule is not None:
        refresh_rule.enabled = False
    session.delete(row)
    session.flush()


def _linked_refresh_rule(
    session: Session, row: ControlPlaneRule
) -> RefreshRule | None:
    if row.refresh_rule_id is None:
        return None
    return (
        session.execute(
            select(RefreshRule).where(
                RefreshRule.workspace_id == row.workspace_id,
                RefreshRule.id == row.refresh_rule_id,
            )
        )
        .scalars()
        .first()
    )


# --- entitlement --------------------------------------------------------


def replicate_entitlement(
    session: Session,
    workspace_id: uuid.UUID,
    payload: EntitlementWrite,
) -> bool:
    """Apply replicated billing evidence. ``False`` == ignored as stale.

    Monotonic by ``evidence_version``: an incoming version at or below
    the stored one changes nothing and is reported as ignored, so an
    out-of-order redelivery can never roll a tenant back onto older
    evidence.
    """
    row = get_entitlement(session, workspace_id)
    if row is not None and payload.evidence_version <= stored_evidence_version(row):
        return False

    if row is None:
        row = WorkspaceEntitlement(workspace_id=workspace_id)
        session.add(row)

    row.state = SAAS_STATUS_TO_ENGINE_STATE[payload.status]
    row.plan_code = payload.plan
    row.product_ceiling = payload.product_ceiling
    row.observed_at = payload.as_of
    row.evidence_version = str(int(payload.evidence_version))
    session.flush()
    return True


def quarantine_entitlement(session: Session, row: WorkspaceEntitlement) -> None:
    """Suspend a tenant's entitlement — the engine's brake on paid work."""
    row.state = EntitlementState.SUSPENDED
    session.flush()


# --- product ceiling ----------------------------------------------------


def _count_active_products(session: Session, workspace_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(Product)
            .where(
                Product.workspace_id == workspace_id,
                Product.status == ProductStatus.ACTIVE,
            )
        ).scalar_one()
        or 0
    )


def count_new_products(
    session: Session,
    workspace_id: uuid.UUID,
    identities: list[tuple[str, str] | None],
) -> int:
    """How many of ``identities`` are not already products in the workspace.

    ``identities`` is one entry per deduped bulk-upsert item, as
    ``app_shared.catalog.upsert.resolve_identity`` returns them:
    ``("external_id", value)``, ``("sku", value)``, or ``None`` for an
    item with neither. An identity-less item has nothing to match on and
    is therefore always an insert, so it always counts as new.

    Existence is checked by identity regardless of status, because that
    is exactly what the ``ON CONFLICT`` arbiter matches on — an archived
    product being revived by an upsert is not a new row.
    """
    wanted_external = {v for kind, v in (i for i in identities if i) if kind == "external_id"}
    wanted_sku = {v for kind, v in (i for i in identities if i) if kind == "sku"}

    existing_external: set[str] = set()
    if wanted_external:
        existing_external = {
            value
            for (value,) in session.execute(
                select(Product.external_id).where(
                    Product.workspace_id == workspace_id,
                    Product.external_id.in_(sorted(wanted_external)),
                )
            ).all()
            if value is not None
        }

    existing_sku: set[str] = set()
    if wanted_sku:
        existing_sku = {
            value
            for (value,) in session.execute(
                select(Product.sku).where(
                    Product.workspace_id == workspace_id,
                    Product.sku.in_(sorted(wanted_sku)),
                )
            ).all()
            if value is not None
        }

    new = 0
    for identity in identities:
        if identity is None:
            new += 1
            continue
        kind, value = identity
        known = existing_external if kind == "external_id" else existing_sku
        if value not in known:
            new += 1
    return new


def check_product_ceiling(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    requested: int,
) -> CeilingVerdict:
    """May this workspace add ``requested`` more products?

    Cheap by construction: the entitlement row is read FIRST and, when
    there is no ceiling to enforce, nothing else is queried at all. That
    ordering is deliberate — the overwhelming majority of catalog writes
    run against a workspace with no recorded ceiling, and they must not
    pay for a ``COUNT(*)`` over the catalog to learn that.

    ``NULL`` ``product_ceiling`` means "no ceiling was recorded" and is
    never a cap; ``0`` is a real ceiling meaning this plan allows no
    products at all (C1's column docstring).
    """
    if requested <= 0:
        return CeilingVerdict(exceeded=False, requested=requested)

    entitlement = get_entitlement(session, workspace_id)
    if entitlement is None or entitlement.product_ceiling is None:
        return CeilingVerdict(exceeded=False, requested=requested)

    ceiling = int(entitlement.product_ceiling)
    active = _count_active_products(session, workspace_id)
    return CeilingVerdict(
        exceeded=active + requested > ceiling,
        ceiling=ceiling,
        active=active,
        requested=requested,
    )


def check_product_ceiling_for_upsert(
    session: Session,
    workspace_id: uuid.UUID,
    identities: list[tuple[str, str] | None],
) -> CeilingVerdict:
    """The bulk-upsert form: only NEW products count against the ceiling.

    A batch that re-pushes a tenant's entire unchanged catalog must not
    be refused for "exceeding" a ceiling it already sits exactly at —
    that is the normal steady-state shape of a connector sync, and
    counting updates as additions would break every one of them.

    Same cheap-path ordering as :func:`check_product_ceiling`: no
    entitlement row, or a ``NULL`` ceiling, and nothing else is queried.
    """
    entitlement = get_entitlement(session, workspace_id)
    if entitlement is None or entitlement.product_ceiling is None:
        return CeilingVerdict(exceeded=False)

    ceiling = int(entitlement.product_ceiling)
    requested = count_new_products(session, workspace_id, identities)
    if requested <= 0:
        return CeilingVerdict(exceeded=False, ceiling=ceiling, requested=0)

    active = _count_active_products(session, workspace_id)
    return CeilingVerdict(
        exceeded=active + requested > ceiling,
        ceiling=ceiling,
        active=active,
        requested=requested,
    )
