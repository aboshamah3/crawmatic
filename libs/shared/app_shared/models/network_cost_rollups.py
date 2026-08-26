"""``network_cost_rollups`` / ``fleet_network_cost_rollups`` — durable,
bounded-cardinality cost aggregates over C1's network ledger (EPA C6).

## The gap this closes

``GET /ops/metrics`` (audit H5) and any future tenant-facing cost surface
both need to answer "how much are we spending, broken down by domain,
method and profile version" without either (a) running a high-cardinality
``GROUP BY`` over ``network_operations``/``network_operation_allocations``
synchronously on every request — the exact anti-pattern this task exists
to forbid — or (b) inventing an unbounded per-tenant breakdown that grows
without limit as new domains/methods/profile-versions appear.

:mod:`app_shared.netledger.rollups` is the job that populates these two
tables on a durable cursor (:mod:`app_shared.maintenance.rollup_watermark`,
key :data:`app_shared.netledger.rollups.WATERMARK_COST_ROLLUP` — the SAME
``rollup_watermarks`` table W5.5-L2 built, a different key namespace, per
this run's instruction to reuse rather than reinvent). ``/ops/metrics``
and any tenant-scoped cost-breakdown endpoint read ONLY these rollup
rows — never the raw ledger — so both surfaces are O(rollup rows), never
O(ledger rows).

## Two tables, two ownership shapes — the C1 precedent, reused

Mirrors ``network_operations`` (fleet-owned) /
``network_operation_allocations`` (workspace-owned) exactly:

* :class:`FleetNetworkCostRollup` — FLEET-scoped, no ``workspace_id``,
  no RLS. One row per ``(rollup_date, domain, method,
  profile_version, currency)`` bucket, summed across every tenant.
  SYSTEM in ``scripts/rls_table_manifest.txt`` — unlike
  ``network_operations``/``provider_usage_records`` (filed GAP because
  ``domain``/``target_host`` is tenant-LINKED evidence on a row nobody
  can select by tenant), a fleet rollup row is *already* the sum across
  every workspace: it reveals aggregate fleet spend on a domain, not any
  one tenant's usage of it, which is the same non-tenant reasoning
  ``fleet_cost_budgets`` and ``domain_playbooks`` use for their own
  SYSTEM filing.
* :class:`NetworkCostRollup` — WORKSPACE-scoped, RLS'd (ENABLE + FORCE +
  the standard ``workspace_id = <ctx>`` policy). One row per
  ``(workspace_id, rollup_date, domain, method, profile_version)``
  bucket, using this workspace's OWN allocated share
  (``network_operation_allocations.allocated_cost_minor_units``), never
  the operation's fleet-wide cost. Registered in
  ``app_shared.repository.WORKSPACE_OWNED_MODELS`` — unlike
  ``NetworkOperationAllocation`` (deliberately left unregistered pending
  "the C4 query paths that will actually read it"), THIS table's read
  path exists in this same change
  (``apps/api/app/routers/cost_rollups.py``), so registering it here is
  exactly that deferred step, not a new precedent.

## Bounded cardinality: top-N + "other"

Neither table can grow per-(tenant, domain, method, profile-version)
without limit — a tenant watching a thousand distinct competitor domains
would otherwise produce a thousand rows *per day*, defeating the point of
a bounded rollup. The aggregation job
(:func:`app_shared.netledger.rollups.run_cost_rollup`) ranks every
``(domain, method, profile_version)`` tuple for a given scope+day by
operation count, keeps the top
:data:`app_shared.netledger.rollups.TOP_N_COST_ROLLUP_BUCKETS`, and
collapses everything else into exactly ONE additional row per scope+day
using the sentinels below — so row count per (scope, day) is bounded at
``TOP_N_COST_ROLLUP_BUCKETS + 1`` regardless of how many distinct
domains/methods/versions actually occurred.

## "method" and "profile-version" — what they map to, and why NOT

A cost dimension earns its place by SEPARATING spend. Two of these three
were first mapped onto columns that do not, and EPA Phase C's gate review
was right to reject both — a dimension that is constant across every row
buys nothing, and a dimension that is near-unique across every row costs
everything (it shatters the day into one bucket per operation and then
collapses them all into ``__other__``, which is a rollup that has
aggregated nothing).

* **method** -> ``network_operations.transport``
  (``DIRECT``/``PROXY``/``BROWSER``). Three values, and they are the
  three that differ by ORDERS OF MAGNITUDE in cost — a browser
  navigation against a direct fetch is the single most useful split a
  cost report can offer. The literal HTTP-verb column was the original
  mapping and is effectively constant (``GET`` on every scraping fetch
  this fleet makes), so grouping by it produced one bucket per domain
  and called it a breakdown.
* **profile-version** -> ``domain_playbooks.profile_version`` for the
  operation's domain, as text. A REAL profile version: the version of
  the playbook whose strategy the fetch was executed under, which is the
  number an operator compares two days of cost across after promoting a
  playbook. ``budget_decision_version`` was the original mapping and is
  the opposite failure — it is a per-decision counter tag
  (``cb1:2026_08:ws41:fleet7``), so it is near-unique per operation. It
  is still recorded on every operation row; it is simply not a GROUPING
  key.

  The join is by domain and reads the CURRENT playbook version, because
  the operation row carries no version of its own. A playbook promoted
  between an operation and the rollup that aggregates it therefore
  attributes that day's spend to the newer version — a bounded,
  documented approximation, and the version an operator reading the
  bucket would act on. Operations on a domain with no playbook row fall
  to :data:`COST_ROLLUP_UNKNOWN_PROFILE_VERSION`.

Both remain reversible: re-deriving a dimension from a different source
column is a migration, not an architectural change.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase

#: Sentinel bucket values for the collapsed "everything past the top-N"
#: row — see the module docstring's "Bounded cardinality" section.
#: Deliberately not a real domain/method (``__other__`` cannot collide
#: with a real hostname, which is DNS-legal but never contains
#: underscores) or a real budget-decision-version string (empty).
COST_ROLLUP_OTHER_DOMAIN = "__other__"
COST_ROLLUP_OTHER_METHOD = "__other__"
COST_ROLLUP_OTHER_PROFILE_VERSION = "__other__"
#: A ``network_operations`` row with no ``budget_decision_version`` at
#: all (nullable column) rolls up under this sentinel rather than SQL
#: NULL, which would defeat the ``ON CONFLICT`` upsert key below (NULLs
#: never compare equal, so every no-version day would insert a fresh row
#: instead of updating in place).
COST_ROLLUP_UNKNOWN_PROFILE_VERSION = ""


class FleetNetworkCostRollup(Base, TimestampMixin):
    """``fleet_network_cost_rollups`` — bounded fleet-wide daily cost buckets.

    FLEET-scoped like ``network_operations``: no ``workspace_id``, no
    RLS. See the module docstring for the ownership rationale and the
    top-N + "other" bounding contract.
    """

    __tablename__ = "fleet_network_cost_rollups"
    __table_args__ = (
        UniqueConstraint(
            "rollup_date",
            "domain",
            "method",
            "profile_version",
            "currency",
            name="uq_fncr_date_domain_method_version_currency",
        ),
        CheckConstraint("operation_count >= 0", name="fncr_operation_count_non_negative"),
        CheckConstraint(
            "estimated_cost_minor_units >= 0", name="fncr_estimated_cost_non_negative"
        ),
        CheckConstraint(
            "reconciled_cost_minor_units IS NULL OR reconciled_cost_minor_units >= 0",
            name="fncr_reconciled_cost_non_negative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="fncr_currency_is_iso4217"),
        Index("ix_fncr_rollup_date", "rollup_date"),
    )

    rollup_date: Mapped[date_type] = mapped_column(Date(), nullable=False)
    #: :data:`COST_ROLLUP_OTHER_DOMAIN` for the collapsed bucket.
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``network_operations.transport`` (``DIRECT``/``PROXY``/``BROWSER``)
    #: — see the module docstring's "method and profile-version" section
    #: for why not the HTTP verb. :data:`COST_ROLLUP_OTHER_METHOD` for
    #: the collapsed bucket.
    method: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``domain_playbooks.profile_version`` for the operation's domain,
    #: as text — see the module docstring. Empty string
    #: (:data:`COST_ROLLUP_UNKNOWN_PROFILE_VERSION`), never SQL NULL, so
    #: the upsert key below is always comparable.
    profile_version: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    operation_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    #: Sum of ``network_operations.estimated_cost_minor_units`` over the
    #: bucket — the FLEET's own estimate, never a tenant allocation.
    estimated_cost_minor_units: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0
    )
    #: Sum of each covered operation's LATEST settlement
    #: (``network_operation_settlements.reconciled_cost_minor_units``,
    #: highest ``settlement_version``). ``NULL`` iff not one operation in
    #: the bucket has a settlement yet — distinct from ``0``, which would
    #: falsely claim "reconciled to zero cost".
    reconciled_cost_minor_units: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(length=3), nullable=False)


class NetworkCostRollup(Base, WorkspaceScopedBase, TimestampMixin):
    """``network_cost_rollups`` — bounded per-tenant daily cost buckets.

    WORKSPACE-scoped, RLS'd (the creating migration emits
    :func:`app_shared.models.rls.emit_rls_policy`). Registered in
    :data:`app_shared.repository.WORKSPACE_OWNED_MODELS`. See the module
    docstring for the ownership rationale, the top-N + "other" bounding
    contract, and what "method"/"profile-version" mean here.
    """

    __tablename__ = "network_cost_rollups"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "rollup_date",
            "domain",
            "method",
            "profile_version",
            name="uq_ncr_workspace_date_domain_method_version",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_ncr_workspace_id_workspaces",
        ),
        CheckConstraint("operation_count >= 0", name="ncr_operation_count_non_negative"),
        CheckConstraint(
            "estimated_cost_minor_units >= 0", name="ncr_estimated_cost_non_negative"
        ),
        CheckConstraint(
            "reconciled_cost_minor_units IS NULL OR reconciled_cost_minor_units >= 0",
            name="ncr_reconciled_cost_non_negative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ncr_currency_is_iso4217"),
        Index("ix_ncr_workspace_id_rollup_date", "workspace_id", "rollup_date"),
    )

    rollup_date: Mapped[date_type] = mapped_column(Date(), nullable=False)
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``network_operations.transport``; see the fleet table above.
    method: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``domain_playbooks.profile_version``; see the fleet table above.
    profile_version: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    operation_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    #: This workspace's own share
    #: (``network_operation_allocations.allocated_cost_minor_units``),
    #: never the operation's fleet-wide cost.
    estimated_cost_minor_units: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0
    )
    #: This workspace's ``fraction_ppb`` applied to each covered
    #: operation's latest settlement. ``NULL`` iff not one covered
    #: operation has a settlement yet.
    reconciled_cost_minor_units: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(length=3), nullable=False)


__all__ = [
    "COST_ROLLUP_OTHER_DOMAIN",
    "COST_ROLLUP_OTHER_METHOD",
    "COST_ROLLUP_OTHER_PROFILE_VERSION",
    "COST_ROLLUP_UNKNOWN_PROFILE_VERSION",
    "FleetNetworkCostRollup",
    "NetworkCostRollup",
]
