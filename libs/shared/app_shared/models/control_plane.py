"""Control-plane rule ORM model: ``control_plane_rules`` (EPA C1, 2026-09-03).

One new workspace-owned table on
:class:`~app_shared.models.base.WorkspaceScopedBase` (``workspace_id
NOT NULL``, indexed) + :class:`~app_shared.models.base.TimestampMixin`
(``created_at``/``updated_at``), with
:func:`app_shared.models.rls.emit_rls_policy` applied in the creating
Alembic migration (``alembic/versions/c8d2e3f4a5b6_control_plane.py``),
not here — this module only declares ORM shape. The mixin/RLS/migration
split mirrors :mod:`app_shared.models.refresh_rules` exactly.

What this table is FOR — and what it is not
-------------------------------------------
:class:`ControlPlaneRule` records **what the SaaS asked the engine for**:
a ``MONITOR`` or ``REPRICE`` rule, on a coarse named ``cadence``, over a
set of the SaaS's own ``target_external_ids``. It is the control plane's
side of the contract, addressed by the SaaS's own ``external_id``.

:class:`~app_shared.models.refresh_rules.RefreshRule` is the engine's
side — the thing the scheduler actually claims and runs. The two are
deliberately separate tables with a nullable link
(``refresh_rule_id``) rather than one table with more columns, because
they have different lifetimes and different owners:

* the engine rule can be retired, replaced, or dead-lettered by engine
  machinery that knows nothing about billing;
* the customer's request must survive that, so an operator can still
  answer "what did this tenant ask for, and why is nothing running?"

Hence ``ON DELETE SET NULL`` on the link (NOT ``CASCADE``, the ondelete
every scope-target FK on ``refresh_rules`` itself uses): deleting the
engine rule nulls the pointer and leaves the intent row standing. A
``CASCADE`` here would delete the evidence of the request along with the
mechanism, which is precisely the audit trail the quarantine columns
below exist to preserve.

``kind`` and ``cadence`` are plain ``TEXT``, not database enums or CHECK
constraints, and that is deliberate: the vocabulary is owned by the SaaS
(``MONITOR``/``REPRICE``; ``HOURLY``/``EVERY_6H``/``TWICE_DAILY``/
``DAILY``/``WEEKLY``), and the engine records what it was told rather
than adjudicating it. Validation belongs in the control-plane route's
Pydantic layer, where a rejected value produces a 422 the caller can
read — not in a CHECK violation that surfaces as a 500 and needs a
migration to widen. This follows the ``plan_code``/``evidence_version``
precedent on :class:`~app_shared.models.cost_authorization.WorkspaceEntitlement`
("free text — the engine never branches on it, it only records what the
denial was about").

``(workspace_id, external_id)`` is UNIQUE, not ``external_id`` alone:
the SaaS's rule id is unique within one tenant's namespace, and two
tenants may each name a rule ``rule-1``. That unique key is also the
idempotency key the control-plane upsert route targets, so a retried
delivery updates the row it already wrote instead of creating a second.

The quarantine columns (``quarantined``/``quarantine_reason``/
``grace_until``) are the engine's brake on a rule that is misbehaving or
out of entitlement: ``quarantined`` never defaults to true (a rule is
born running), and a quarantine always carries a human-readable reason,
because a silently stopped rule is indistinguishable from a broken
scheduler.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class ControlPlaneRule(Base, WorkspaceScopedBase, TimestampMixin):
    """``control_plane_rules`` — the SaaS's declared monitor/reprice intent."""

    __tablename__ = "control_plane_rules"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "external_id",
            name="uq_control_plane_rules_workspace_id_external_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_control_plane_rules_workspace_id_workspaces",
        ),
        ForeignKeyConstraint(
            ["refresh_rule_id"],
            ["refresh_rules.id"],
            name="fk_control_plane_rules_refresh_rule_id_refresh_rules",
            ondelete="SET NULL",
        ),
    )

    #: The SaaS's own id for this rule — unique WITHIN the workspace (see
    #: the module docstring), and the idempotency key of the upsert route.
    external_id: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``MONITOR`` | ``REPRICE``. Free text by design — see the module
    #: docstring for why the vocabulary is not pinned in the schema.
    kind: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``HOURLY`` | ``EVERY_6H`` | ``TWICE_DAILY`` | ``DAILY`` | ``WEEKLY``.
    #: The COARSE, named cadence the SaaS sells; the engine translates it
    #: into the exact `refresh_rules` cron/interval it schedules on.
    cadence: Mapped[str] = mapped_column(Text(), nullable=False)
    #: Lifecycle switch owned by the SaaS. Disabled rules stay on file
    #: (contrast a delete) so re-enabling is not a re-create.
    enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=text("true")
    )
    #: The SaaS's own product ids this rule covers. JSONB rather than a
    #: join table: these are FOREIGN identifiers the engine never joins on
    #: — it hands them back to the SaaS, which resolves them.
    target_external_ids: Mapped[list[str]] = mapped_column(
        JSONB(), nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: Free-text operator/team attribution carried through from the SaaS.
    owner_tag: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # --- Quarantine (the engine's brake) ----------------------------------
    #: ``false`` for a healthy rule. Never defaults to true: a rule is born
    #: running, and stopping one is an explicit act with a reason.
    quarantined: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=text("false")
    )
    #: Why the rule was quarantined, in words an operator can act on. A
    #: quarantine without a reason is indistinguishable from a bug.
    quarantine_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Until when the rule keeps running despite the condition that would
    #: otherwise quarantine it (``NULL`` = no grace granted).
    grace_until: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)

    #: The engine :class:`~app_shared.models.refresh_rules.RefreshRule`
    #: this intent currently drives. Nullable, ``ON DELETE SET NULL``:
    #: retiring the engine rule must not erase the record of the request
    #: (module docstring).
    refresh_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
