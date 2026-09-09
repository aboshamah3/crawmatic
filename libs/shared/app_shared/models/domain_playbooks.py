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

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import AccessMethod, StrEnum, enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime

__all__ = ["DomainLifecycleAudit", "DomainPlaybook", "DomainState"]


class DomainState(StrEnum):
    """Minimum enforced domain lifecycle state (EPA C2, READY-004/READY-006
    critical path for the C3 authorization service).

    Declared **locally** rather than in ``app_shared.enums`` (the usual
    home for ``StrEnum`` members, per ``enum_column``'s module docstring):
    that module is concurrently owned by other in-flight EPA tasks and
    must not be edited here to avoid a merge collision at the phase gate.

    This is deliberately the MINIMUM subset needed for authorization, not
    the full domain lifecycle machine — the approval workflow, transition
    audit trail, and admin UI for moving a domain between these states all
    remain W4.1 scope. ``app_shared.domains.state_lookup`` is the only
    reader; see its ``authorization_rules_for_state`` for what each value
    means to C3.
    """

    #: No certification signal yet (or the domain has no
    #: ``domain_playbooks`` row at all) — the fail-safe default.
    UNKNOWN = "UNKNOWN"
    #: Early canary: a small volume of DIRECT (non-proxied) requests only.
    DIRECT_CANARY = "DIRECT_CANARY"
    #: Later canary: a small volume of requests through a specific
    #: extraction/access profile, ahead of full certification.
    PROFILE_CANARY = "PROFILE_CANARY"
    #: Certified — Phase B5/B5b outcome (e.g. Amazon CSS-certified 5/5,
    #: Noon proxy-certified 21/21). Full authorization.
    ACTIVE = "ACTIVE"
    #: Previously ACTIVE but showing enough failure signal that expensive
    #: escalation should stop by default while cheaper paths keep running.
    DEGRADED = "DEGRADED"
    #: Operator-quarantined pending investigation — all paid work denied.
    QUARANTINED = "QUARANTINED"
    #: Determined not scrapeable by supported methods — all paid work
    #: denied.
    UNSUPPORTED = "UNSUPPORTED"


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
    #: Generic ordered candidate templates consumed when a workspace/domain
    #: strategy is first materialized. Data, not runtime domain branches:
    #: each entry may name an access method, reusable profile/adapter config,
    #: fallback outcomes, proof state, and cooldown/canary policy.
    method_templates: Mapped[list] = mapped_column(JSONB(), nullable=False, default=list)
    #: Operator notes (why this method, e.g. "TLS-fingerprint blocked,
    #: needs residential proxy" / "rate-limits direct at >10 rpm").
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Minimum enforced domain lifecycle state (EPA C2) consulted by C3's
    #: authorization rule table via
    #: ``app_shared.domains.state_lookup.get_domain_state``. Defaults
    #: ``UNKNOWN`` — everything beyond this minimum subset (approvals,
    #: transition audit trail, admin UI) is W4.1 scope.
    state: Mapped[DomainState] = enum_column(DomainState, nullable=False, default=DomainState.UNKNOWN)

    # ------------------------------------------------------------------
    # W4.1 (EPA, 2026-08-26): the versioned certification profile.
    #
    # A "versioned domain profile" is NOT a new parallel config store --
    # most of the fields the W4.1 plan text names are already reachable
    # from this row's three existing pointers, which this migration
    # deliberately does not duplicate:
    #   * method sequence, fallback outcomes, proof state, cooldown/
    #     canary policy -> ``method_templates`` (already on this row).
    #   * selectors, adapter config, extraction version/rollback ->
    #     ``scrape_profile_name`` -> ``ScrapeProfile``/``ScrapeProfileRevision``
    #     (``version`` + immutable snapshots already exist there).
    #   * provider/region eligibility, retry limits, concurrency/rate
    #     ceilings -> ``access_policy_name`` -> ``AccessPolicy``
    #     (``provider_id``, ``country_code``, ``max_retries``,
    #     ``max_requests_per_*`` already exist there).
    # What genuinely has no home yet -- legal/robots posture, URL
    # patterns, canonicalization, identity evidence, expected bytes/
    # latency/cost, fixtures, and an explicit rollback pointer -- is
    # carried in ``profile_fields`` below rather than as a dozen new
    # narrow columns, since none of them need to be individually
    # indexed/queried yet and every value that materially changes this
    # domain's certification already gets a durable, evidenced copy in
    # the ``domain_lifecycle_audit`` row for that transition (see
    # ``app_shared.domains.lifecycle.transition``) -- this column is a
    # convenience *current* view, the audit trail is the source of truth.
    #: Monotonic version counter for this domain's certification
    #: profile. Bumped by every ``app_shared.domains.lifecycle.transition``
    #: call (each transition IS a new evidenced profile version, exactly
    #: like ``ScrapeProfile.version`` + ``ScrapeProfileRevision``, and
    #: ``network_operation_settlements.settlement_version`` before it).
    #: "Rollback version" is expressed as a value of this counter named
    #: in a later transition's ``evidence`` (e.g.
    #: ``{"rollback_to_profile_version": 3, ...}``) rather than an
    #: in-place revert -- appending a new version that happens to match
    #: an old one, never rewriting history, matching the append-only
    #: audit trail this counter is versioned alongside.
    profile_version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    #: Accountable human/team for this domain's certification (e.g. an
    #: email or team handle) — one of the required profile fields with
    #: no existing home. Nullable: not every seeded/legacy row has one
    #: yet (EPA C2's seed predates this column).
    profile_owner: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Timestamp of the most recent canary run against this domain
    #: (``DIRECT_CANARY``/``PROFILE_CANARY`` evidence), independent of
    #: whether that canary passed. Nullable for domains never canaried.
    last_canary_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    #: The remaining required profile fields with no existing column
    #: home: legal/robots policy (e.g. ``{"robots_txt_respected": bool,
    #: "tos_reviewed_at": ..., "notes": ...}``), ``url_patterns`` (list
    #: of glob/regex strings this playbook applies to), ``canonicalization``
    #: (URL-normalization rule reference), ``identity_evidence`` (the
    #: proof-of-identity signal from the most recent certification —
    #: mirrors the scraping runtime's identity-adapter status enum
    #: without importing the scraping package here, same boundary rule
    #: as ``ScrapeProfile.price_json_path``'s docstring), ``expected_bytes``/
    #: ``expected_latency_ms``/``expected_cost_micro_units`` (certified
    #: performance envelope), and ``fixtures`` (identifiers of the
    #: recorded fixtures/golden pages a re-certification replays against).
    #: All keys optional; unset keys mean "not yet captured for this
    #: domain", not "known to be empty". Defaults to ``{}`` so every row
    #: (including C2's seeded ones) starts from a valid, empty profile.
    profile_fields: Mapped[dict] = mapped_column(JSONB(), nullable=False, default=dict)

    # ------------------------------------------------------------------
    # C4 (EPA, 2026-09-08, F08 / plan §11 items 2 and 5): the versioned
    # ESCALATION STRATEGY. Read by
    # ``app_shared.strategy.methods.PlaybookStrategy.from_row`` and
    # applied by the ladder (``resolve_next_physical_attempt``); seeded
    # by ``scripts/seed_domain_playbooks.sql``.
    #
    # Why a second counter next to ``profile_version`` above: that one is
    # bumped by every ``app_shared.domains.lifecycle.transition`` — an
    # approval/evidence event about whether the domain may be scraped at
    # all. This one moves only when the strategy SHAPE changes. Stamping
    # an attempt with a counter that also moves on an unrelated approval
    # would make "which strategy produced this attempt" unanswerable,
    # which is exactly the question a regressed canary has to answer.
    #: Version of this domain's escalation strategy, stamped onto every
    #: ladder decision (``StrategyMethodSelection.strategy_version``,
    #: ``LadderDecision.strategy_version``). Never ``NULL``: a domain with
    #: no explicit strategy is version 1, not "unknown version".
    strategy_version: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=1, server_default=text("1")
    )
    #: ``AccessMethod`` value naming the near-free rung that should be
    #: tried FIRST for this domain when nothing else pins the start (no
    #: durable cursor, no preferred method). ``NULL`` = no hint; the
    #: ladder's ordinary priority order applies. Stored as text, not an
    #: enum column, deliberately: this is curated operator data on a
    #: fleet reference table, and an unrecognised value must degrade to
    #: "no hint" (``PlaybookStrategy`` validates it against
    #: ``AccessMethod``) rather than fail a seed INSERT.
    cheap_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: ``AccessMethod`` value naming the EXPENSIVE rung whose use is
    #: rationed by ``fallback_cap_per_refresh`` (in practice a browser
    #: method — ``PLAYWRIGHT_DIRECT``/``PLAYWRIGHT_PROXY``). ``NULL`` =
    #: nothing is treated as the rationed fallback for this domain.
    fallback_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: How many times ONE target may take :attr:`fallback_path` in ONE
    #: refresh. ``NULL`` = uncapped (the pre-C4 behaviour, so an
    #: unseeded row changes nothing); ``0`` = never. Enforced by the
    #: ladder against the per-target counter the scraping runtime's
    #: attempt budget holds, so the cap is a property of the ladder
    #: rather than of each call site's discipline.
    fallback_cap_per_refresh: Mapped[int | None] = mapped_column(
        Integer(), nullable=True
    )
    #: Per-domain override of ``Settings.SCRAPE_RECOVERY_PROBE_FRACTION``
    #: — the sampled share of targets that ignore a DOMAIN-scope method
    #: suppression and probe it anyway (C1). ``NULL`` = use the setting.
    #: ``Numeric`` (not float) so the seeded value is exact; the ladder
    #: and the budget both coerce it to ``float`` at the boundary.
    recovery_probe_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True
    )


class DomainLifecycleAudit(Base):
    """``domain_lifecycle_audit`` — append-only transition log for
    ``domain_playbooks.state`` (EPA W4.1, report §6).

    One row per call to ``app_shared.domains.lifecycle.transition``:
    the ``(from_state, to_state)`` edge, the evidence that justified it,
    the human approver (required for a transition into ``ACTIVE``, the
    grant edge the task contract names; ``NULL`` for every other,
    non-granting transition, including automatic ones like ``-> DEGRADED``
    on failure signals), and the ``profile_version`` this row's domain
    carried immediately after the transition.

    **Append-only by construction, not by convention**: UPDATE and
    DELETE are both rejected by ``DOMAIN_LIFECYCLE_AUDIT_APPEND_ONLY_SQL``'s
    trigger (installed by the creating migration), the same pattern
    ``network_operation_settlements`` uses — a correction is a new row,
    never an edit of an old one. Uses plain ``Base`` (not
    ``TimestampMixin``): an ``updated_at`` column would imply this row
    is ever updated, which it structurally cannot be.

    **No ``workspace_id``**: ``domain_playbooks`` is fleet-wide,
    operator-curated reference data with no tenant column at all (see
    this module's top docstring) — a transition of its certification
    state is exactly as fleet-scoped as the row it transitions, so this
    audit trail is filed ``SYSTEM`` in ``scripts/rls_table_manifest.txt``,
    matching ``domain_playbooks``' own entry, not ``WORKSPACE``.

    **Seeded states predate this trail** (EPA C2, 2026-08-26): the
    migration that added ``domain_playbooks.state`` seeded
    ``amazon.sa``/``noon.com``/``stech.ink`` directly to ``ACTIVE`` by
    ``UPDATE``, not through ``transition`` — there is no audit row
    explaining *why* those three domains started ``ACTIVE``, because
    the audit trail did not exist yet. The first transition run against
    any of those domains is therefore the FIRST row this table ever
    gets for it, with ``from_state='ACTIVE'`` and nothing before it —
    expected and documented, not a bug in either C2's migration or this
    one.
    """

    __tablename__ = "domain_lifecycle_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["domain"],
            ["domain_playbooks.domain"],
            name="fk_domain_lifecycle_audit_domain_domain_playbooks",
        ),
        Index("ix_domain_lifecycle_audit_domain", "domain"),
    )

    #: Bare domain, exactly as ``domain_playbooks.domain`` stores it —
    #: FK'd to that column's unique index (``uq_domain_playbooks_domain``).
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: State immediately before this transition. Always populated (even
    #: for the first-ever row of a seeded domain — see class docstring):
    #: ``transition`` reads the row's *current* ``state`` before
    #: mutating it, so there is always a "from".
    from_state: Mapped[str] = mapped_column(String(length=32), nullable=False)
    #: State this transition moved the domain to.
    to_state: Mapped[str] = mapped_column(String(length=32), nullable=False)
    #: Structured justification for this transition — canary results,
    #: failure-signal counts, an approval ticket reference, whatever
    #: ``transition``'s caller passed. Required (``nullable=False``):
    #: the task contract states evidence is mandatory for every
    #: transition, with no exceptions.
    evidence: Mapped[dict] = mapped_column(JSONB(), nullable=False)
    #: Identifier (email/handle) of the human who approved this
    #: transition. ``NULL`` for every transition that does not grant
    #: capability (denies, lateral canary moves, automatic degrades) —
    #: ``transition`` refuses to write a row with ``approver IS NULL``
    #: for a transition into ``ACTIVE``.
    approver: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: ``domain_playbooks.profile_version`` immediately AFTER this
    #: transition (i.e. the version this evidence certifies).
    profile_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, server_default=text("now()")
    )


# --- DDL emitted by the creating migration (W4.1) --------------------------
#
# Kept here, next to the model whose invariant it enforces — same
# convention ``app_shared.models.network_operations`` established for
# ``NETWORK_OPERATION_SETTLEMENTS_APPEND_ONLY_SQL``. No ``%`` or bare
# ``:`` in the message text (psycopg3 scans for placeholders whenever a
# statement is executed with parameters).
DOMAIN_LIFECYCLE_AUDIT_APPEND_ONLY_SQL = """
CREATE OR REPLACE FUNCTION domain_lifecycle_audit_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'domain_lifecycle_audit is append-only',
        DETAIL  = 'UPDATE and DELETE are rejected. A correction is a new '
                  'transition row, written by app_shared.domains.lifecycle.transition.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_domain_lifecycle_audit_append_only
BEFORE UPDATE OR DELETE ON domain_lifecycle_audit
FOR EACH ROW EXECUTE FUNCTION domain_lifecycle_audit_reject_mutation();
"""
