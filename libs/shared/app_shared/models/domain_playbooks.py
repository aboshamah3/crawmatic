"""``DomainPlaybook`` ORM model: ``domain_playbooks`` (2026-08-11 proxy-cost
Fix 4, PLAN_PROXY_COST_REDUCTION.md).

A curated, fully **global** catalog of well-known competitor domains and
how to scrape them: the transport start (``preferred_access_method``) and,
optionally, the name of a **global** ``scrape_profiles`` row carrying the
domain's extraction config. Consumed in two places:

* ``app_shared.strategy.resolution.resolve_or_create_strategy_profile`` —
  a brand-new ``(workspace, competitor, domain)`` strategy key whose
  domain has a playbook entry is seeded ``LEARNING`` with the playbook's
  access method instead of ``DISCOVERY_REQUIRED``, skipping the discovery
  probe ladder entirely (up to ~20 paid fetches per key). Live promotion/
  rediscovery then confirm or degrade it exactly like any learned value —
  the playbook is a starting hint, never an override.
* ``POST /v1/competitors`` — a new competitor for a playbook domain whose
  caller passed no ``default_scrape_profile_id`` gets the entry's global
  profile assigned, so every workspace extracts that domain identically.

Deliberately **no workspace column**: rows are operator-curated reference
data (seeded by ``scripts/seed_domain_playbooks.sql``, the
``seed_proxy.sh`` pattern), written by no tenant path, readable by every
workspace. Workspaces never publish back into it — a workspace's own
learned divergence lives in its ``domain_strategy_profiles`` rows.
"""

from __future__ import annotations

from sqlalchemy import Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import AccessMethod, enum_column
from app_shared.models.base import Base, TimestampMixin

__all__ = ["DomainPlaybook"]


class DomainPlaybook(Base, TimestampMixin):
    """``domain_playbooks`` — one row per well-known competitor domain."""

    __tablename__ = "domain_playbooks"
    __table_args__ = (Index("uq_domain_playbooks_domain", "domain", unique=True),)

    #: Bare competitor domain exactly as ``competitors.domain`` stores it
    #: (e.g. ``amazon.sa``, ``stech.ink`` — no scheme, no ``www.``).
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: The transport start seeded into a fresh strategy profile for this
    #: domain (``PROXY_HTTP`` for bot-walled sites, ``DIRECT_HTTP``/
    #: ``DIRECT_HTTP_RETRY`` for open ones).
    preferred_access_method: Mapped[AccessMethod] = enum_column(AccessMethod, nullable=False)
    #: Name of a global ``scrape_profiles`` row (``workspace_id IS NULL``)
    #: carrying this domain's extraction config; ``NULL`` = the global
    #: default extraction chain is fine for this domain.
    scrape_profile_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Name of a global ``access_policies`` row to associate at competitor
    #: creation; ``NULL`` = the resolution chain's defaults apply.
    #: Informational for now — policy resolution is name-based already.
    access_policy_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Operator notes (why this method, e.g. "TLS-fingerprint blocked,
    #: needs residential proxy" / "rate-limits direct at >10 rpm").
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
