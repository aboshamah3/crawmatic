"""``DomainRule`` ORM model: ``domain_rules`` (EPA B5/F10, 2026-09-07).

The FLEET-wide, workspace-independent per-domain limits table. One row
per bare domain, carrying the operator's override of the
``FLEET_HOST_*_DEFAULT`` settings that
``app_shared.limiter.fleet.admit_fleet`` admits physical requests
against.

**Three things this table deliberately is not**, because the codebase
already has one of each and conflating them would be a data-model bug:

* NOT ``access_policies``/``DomainAccessRule``
  (``app_shared.models.access``, surfaced by
  ``apps/api/app/routers/domain_access_rules.py``). That is a TENANT
  access policy — workspace-scoped, tenant-writable, and it answers
  "how fast may THIS workspace hit this domain?". This one answers "how
  fast may the WHOLE FLEET hit this domain?", which no tenant may see
  or set, because one tenant's answer is every other tenant's ceiling.
* NOT ``domain_playbooks``. That is curated *strategy* (which transport
  to start with, which extraction profile, certification state). This is
  *capacity*. They are both fleet-scoped and both keyed by domain, and
  they still answer unrelated questions on unrelated change cadences —
  a playbook changes when someone re-certifies a domain, a fleet limit
  changes when the host complains.
* NOT a tenant surface at all. **No ``workspace_id`` column, ever**
  (Global Constraints: "new fleet tables without a ``workspace_id`` are
  never exposed to tenant roles"). It is filed ``SYSTEM`` in
  ``scripts/rls_table_manifest.txt`` and carries no RLS policy, exactly
  like ``domain_playbooks``/``fleet_cost_budgets``: a domain's fleet
  ceiling identifies no workspace and reveals nothing about one.

**Additive by design.** Every non-key column is nullable with no server
default, and ``NULL`` means "use the setting", never "unlimited". A
later task adding a column (EPA C1 adds ``request_timeout_seconds``)
therefore needs a plain ``ALTER TABLE ... ADD COLUMN`` with no default
— a catalog-only change in PostgreSQL 11+, no table rewrite, no
backfill, and every existing row keeps behaving exactly as it did.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TimestampMixin

__all__ = ["DomainRule"]


class DomainRule(Base, TimestampMixin):
    """``domain_rules`` — one row per domain with a fleet-limit override."""

    __tablename__ = "domain_rules"
    __table_args__ = (Index("uq_domain_rules_domain", "domain", unique=True),)

    #: Bare competitor domain exactly as ``competitors.domain`` and
    #: ``domain_playbooks.domain`` store it (e.g. ``amazon.sa`` — no
    #: scheme, no ``www.``). Unique: the fleet has exactly one ceiling
    #: per domain, and a second row would silently mean whichever the
    #: query happened to read first.
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: Simultaneous in-flight physical requests the WHOLE fleet may hold
    #: against this domain. ``NULL`` -> ``Settings
    #: .FLEET_HOST_CONCURRENCY_DEFAULT``. Never "unlimited": a value that
    #: resolves below 1 is floored to 1 by ``resolve_fleet_limits``.
    fleet_concurrency: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: Physical requests per minute the WHOLE fleet may make against this
    #: domain. ``NULL`` -> ``Settings.FLEET_HOST_RATE_PER_MINUTE_DEFAULT``.
    fleet_rate_per_minute: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: EPA C1/F08 (2026-09-07). Seconds this domain's requests may run
    #: before they are abandoned. ``NULL`` -> ``Settings
    #: .SCRAPE_DOWNLOAD_TIMEOUT_SECONDS`` (60), same "NULL means use the
    #: setting" rule as the two columns above.
    #:
    #: Unlike them this one is normally written by a JOB, not an
    #: operator: ``maintenance.domain_timeout_tune``
    #: (``app_shared.maintenance.domain_timeouts``) sets it to
    #: ``clamp(1.5 x p95(successful attempt duration, 7 d), 10 s, 60 s)``.
    #: The deep dive measured a 46.9 s average on proxied HTTP, which is
    #: a measurement of the 60 s global default rather than of any
    #: domain: every doomed fetch pays the full ceiling before anyone
    #: learns anything. A domain with too few successes to measure is
    #: left ``NULL`` — an invented timeout is worse than the default.
    request_timeout_seconds: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: Why this override exists (e.g. "host 429s above 30 rpm, measured
    #: 2026-09-07"). Operator notes only — nothing reads it.
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
