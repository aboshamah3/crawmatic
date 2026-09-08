"""Observation/current-price ORM models: price_observations, request_attempts,
match_current_prices (SPEC-07).

Per ``contracts/models-observations.md`` / ``data-model.md`` — three
workspace-owned tables, all on
:class:`~app_shared.models.base.WorkspaceScopedBase`
(``workspace_id NOT NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration
(``alembic/versions/<rev>_observations_current_prices_tables.py``), not
here — this module only declares ORM shape.

* :class:`PriceObservation` — immutable record of one extraction-attempt
  result. **First partitioned table in the repo** (research D3,
  Constitution §22/§29): monthly-partitioned by ``scraped_at`` via
  ``__table_args__``'s ``postgresql_partition_by``, with ``scraped_at``
  declared ``primary_key=True`` alongside the inherited ``id`` so the
  composite ``PRIMARY KEY (id, scraped_at)`` satisfies Postgres's rule
  that a partitioned table's primary key must include the partition
  key.
* :class:`RequestAttempt` — audit record of one HTTP fetch attempt.
  Same partitioning shape, partitioned by ``created_at``.
* :class:`MatchCurrentPrice` — current-state (not partitioned) latest
  price snapshot per match, ``unique(workspace_id, match_id)`` as the
  upsert conflict arbiter.

All three carry only a real FK on ``workspace_id`` (the RLS anchor);
``match_id``/``product_id``/``product_variant_id``/``competitor_id``/
``scrape_job_id``/``observation_id`` are **soft** references (plain
indexed UUID columns, no FK) — matching §22's soft-reference philosophy
and avoiding FK-into/among-partitioned-table complications with
retention-by-drop (later spec).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    AdapterKey,
    AccessMethod,
    ExtractionMethod,
    RequestOrigin,
    ScrapeErrorCode,
    StockStatus,
    enum_column,
)
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase
from app_shared.money import Money


class PriceObservation(Base, WorkspaceScopedBase):
    """``price_observations`` — immutable extraction-attempt result. PARTITIONED.

    Monthly-partitioned by ``scraped_at``; composite
    ``PRIMARY KEY (id, scraped_at)``. ``success=False`` on a failure/
    rejection observation (``price``/``currency``/etc. left ``NULL``);
    ``comparable=False`` iff ``error_code=CURRENCY_MISMATCH`` (still
    saved, excluded from comparison, no FX — Principle VII).
    """

    __tablename__ = "price_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_price_observations_workspace_id_workspaces",
        ),
        # EPA F05 / plan task B1: the idempotency key the durable result
        # spool's replay collides on. Includes the partition key because
        # Postgres requires every partition-key column in a partitioned
        # table's unique index; `workspace_id` leads because every read
        # here is workspace-scoped anyway.
        UniqueConstraint(
            "workspace_id",
            "attempt_uuid",
            "scraped_at",
            name="uq_price_observations_workspace_id_attempt_uuid_scraped_at",
        ),
        {"postgresql_partition_by": "RANGE (scraped_at)"},
    )

    # PK part 2 = partition key (Postgres requires the partition key be
    # part of the primary key on a partitioned table).
    scraped_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

    #: EPA F05 / plan task B1. The SAME producer-side attempt identity the
    #: `request_attempts` row carries (`ScrapeResult.attempt_id`), which is
    #: what makes the persistence flush replayable: with
    #: `UNIQUE (workspace_id, attempt_uuid, scraped_at)` (see
    #: `a4e91c7d2b58`) a replayed batch's `ON CONFLICT DO NOTHING` insert
    #: is a no-op instead of a duplicate price point.
    #:
    #: NULLABLE, no default: `NULL` means "this observation has no
    #: producer-side attempt identity" -- a pre-B1 row, or a writer that is
    #: not the scrape pipeline. NULLs never participate in a unique index,
    #: so such rows neither collide nor block; forcing NOT NULL would make
    #: every other writer invent an identity it does not have.
    attempt_uuid: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scrape_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    stock_status: Mapped[StockStatus | None] = enum_column(StockStatus, nullable=True)
    raw_title: Mapped[str | None] = mapped_column(Text(), nullable=True)

    success: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    comparable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)

    extraction_method: Mapped[ExtractionMethod | None] = enum_column(
        ExtractionMethod, nullable=True
    )
    # A confidence score in [0, 1] — plain Numeric(5,4), never Money.
    extraction_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    selector_used: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # --- EPA W3.1 (2026-08-26, READY-012): OfferObservation superset -----
    # persisted alongside the SPEC-07 columns above, not replacing them.
    # See `app_shared.observations.offer_observation.OfferObservation` for
    # the pydantic contract these columns back; that model is the
    # validation/serialization boundary, this table is the storage. All
    # `offer_*` and never a bare name already used above so a reader of
    # `\d price_observations` can tell which columns are the W3.1 addition
    # at a glance. Every column is NULLABLE by design (§7: unknown = NULL,
    # never 0/""): a pre-W3.1 row and any observation missing a fact are
    # both legitimately absent here, not zero.
    #
    # Money fields reuse `app_shared.money.Money` (`NUMERIC(18,4)`, exact
    # `Decimal`, never float) — the SAME contract `price`/`old_price`
    # above already use — rather than the scaled-integer "minor units"
    # convention `app_shared.models.network_operations` introduced for
    # provider-cost accounting; see `offer_observation.py`'s
    # `_MONEY_FIELD_NAMES` comment for why those are different contracts
    # and this table deliberately doesn't mix them.
    offer_source_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_canonical_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_domain: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_market: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_source_timezone: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Expected vs. observed product identity (IdentityFacet, JSON) —
    # compound/optional-per-subfield, so JSONB (the codebase's existing
    # convention for flexible sub-structures: `scrape_profiles.headers`,
    # `webhook_events.payload`) rather than ~9 more scalar columns each.
    offer_expected_identity: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    offer_observed_identity: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    offer_seller_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Free text, not `enum_column` — same "gain a value without a
    #: migration" posture `network_operations.identity_confidence`/
    #: `comparability` already use for this table's sibling ledger.
    #: Validated against `OfferObservation`'s local `str` typing at the
    #: pydantic boundary, not by a DB CHECK.
    offer_seller_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_fulfillment: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_condition: Mapped[str | None] = mapped_column(Text(), nullable=True)

    offer_item_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    offer_list_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    offer_shipping_cost: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    offer_tax_included: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    offer_fees: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    offer_deposit: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    offer_unit_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    #: Computed by `OfferObservation`, never caller-supplied — see that
    #: model's `_compute_landed_total`. Persisted so a reader of this
    #: table doesn't have to re-derive it from the components above.
    offer_landed_total: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)

    #: PromotionFacts, JSON — same JSONB rationale as the identity facets
    #: above. Money sub-fields inside are serialized as exact decimal
    #: strings (pydantic's default JSON encoding for `Decimal`), never as
    #: a JSON float, so §19 holds inside the JSONB payload too.
    offer_promotion_facts: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    #: Free text, validated against `app_shared.enums.AccessMethod` at the
    #: pydantic boundary (reused for validation only — this table doesn't
    #: gain a DB dependency on that enum's exact members).
    offer_access_method: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_profile_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_parser_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Content hash resolvable through
    #: `app_shared.observations.evidence_store.resolve_hash`/`replay`.
    offer_raw_evidence_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_strategy: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Confidence, broken out per dimension (§7) — `extraction_confidence`
    # above already covers the `extraction` dimension; these three are
    # the remaining dimensions, each `Numeric(5,4)` like it (a fraction
    # in [0, 1], never Money).
    offer_discovery_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    offer_identity_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    offer_comparability_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    #: list[str], JSON — an ordered/repeatable set of reason codes, not a
    #: single categorical value, so JSONB rather than Text.
    offer_validation_reasons: Mapped[list | None] = mapped_column(JSONB(), nullable=True)

    offer_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    offer_comparability_class: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_rejection_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    offer_human_review_required: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)

    # --- EPA C5 (2026-09-08, F19): provenance on the LIVE path ----------
    # W3.1 added the `offer_*` superset above and nothing on the scraping
    # path ever wrote it. C5 wires that path, and these five columns are
    # what make a written row *attributable* afterwards. They are
    # deliberately NOT `offer_`-prefixed: the prefix marks the W3.1
    # OfferObservation projection, and these describe the ACT of
    # observing (which extractor, which profile revision, which policy
    # decided, how well) rather than the offer that was observed.
    #
    # All nullable: every pre-C5 row legitimately has none of them, and
    # `NULL` here means "this row predates the provenance contract",
    # which is a different and more useful statement than a back-filled
    # guess.
    #
    #: Which extraction contract read this price
    #: (the scraping library's `EXTRACTOR_VERSION` constant on its
    #: transport item -- named indirectly because this package must not
    #: reference that library even in a comment, see
    #: `tests/unit/test_import_boundaries.py`). Text, not a number: it
    #: names a contract, and contracts get names.
    extractor_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Which revision of the scrape profile configured that read.
    #: Distinct from `offer_profile_version` above (Text, part of the
    #: W3.1 pydantic projection, free-form) — this is the integer
    #: `scrape_profiles.version` counter, comparable and orderable.
    profile_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: The extraction's own confidence in [0, 1] — the same fraction
    #: `extraction_confidence` carries, kept as its own column because
    #: it is the one `match_current_prices`' C5 conflict guard compares
    #: and a column that a guard depends on should not be one whose
    #: meaning is "whatever the SPEC-07 extractor happened to record".
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    #: Which POLICY chose the reading that was persisted —
    #: the scraping library's `PROVENANCE_*` vocabulary
    #: (`first_hit`/`ranked_v1`/`none`).
    #: This is the column that answers "was this price chosen by the
    #: chain or by the ranker?" after `EXTRACTION_RANKING_POLICY` is
    #: flipped, which is the whole reason the flip can be audited.
    provenance: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: `available` | `unavailable` | `blocked` | `stale` | `conditional`.
    #: Availability as a FIRST-CLASS fact, separate from `stock_status`
    #: (which only ever knew IN_STOCK/OUT_OF_STOCK/UNKNOWN): "we were
    #: blocked", "the offer we have has expired" and "the price is
    #: conditional on a coupon" are three different reasons a price is
    #: not simply usable, and collapsing them into `UNKNOWN` is what
    #: makes an unavailable product indistinguishable from a failed
    #: scrape.
    #:
    #: TEXT with no DB `CHECK`, matching `offer_seller_type`'s precedent
    #: on this same table: `price_observations` is partitioned, and
    #: adding a validated constraint to a partitioned table recurses into
    #: every partition. The vocabulary is enforced at the write boundary
    #: (the persistence pipeline's `AVAILABILITY_STATES`), which is where a
    #: bad value can still be rejected before it exists.
    availability_state: Mapped[str | None] = mapped_column(Text(), nullable=True)


class RequestAttempt(Base, WorkspaceScopedBase):
    """``request_attempts`` — audit record of one HTTP fetch attempt. PARTITIONED.

    Monthly-partitioned by ``created_at``; composite
    ``PRIMARY KEY (id, created_at)``. Exactly one row is written per
    attempted target (FR-013).
    """

    __tablename__ = "request_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_request_attempts_workspace_id_workspaces",
        ),
        ForeignKeyConstraint(
            ["strategy_method_id"],
            ["domain_strategy_methods.id"],
            name="fk_request_attempts_strategy_method_id_domain_strategy_methods",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["scrape_profile_id"],
            ["scrape_profiles.id"],
            name="fk_request_attempts_scrape_profile_id_scrape_profiles",
            ondelete="SET NULL",
        ),
        # EPA C1 (2026-08-25): the logical attempt's link to the PHYSICAL
        # operation that carried it (`app_shared.models.network_operations`).
        # Targets `network_request_id` — the identity generated BEFORE
        # dispatch — not the operation's surrogate `id`, so a writer can
        # stamp the link onto attempt telemetry without first
        # round-tripping the operation insert.
        # EPA C9 (F14): this FK was DROPPED by the `network_operations`
        # partition swap. A foreign key must reference a UNIQUE
        # constraint, and a partitioned table's unique constraint must
        # include the partition key — so the target became
        # `(network_request_id, created_at)`, which a `request_attempts`
        # row does not carry (its own `created_at` is the attempt's, not
        # the operation's). The link is now checked, not constrained:
        # `app_shared.maintenance.ledger_summaries.find_orphan_references`.
        # EPA F05 / plan task B1: see the twin on `price_observations`.
        # A5 minted `attempt_uuid` for correlation only; B1 is what makes
        # it arbitrate, so a replayed flush cannot write a second attempt
        # row for one fetch.
        UniqueConstraint(
            "workspace_id",
            "attempt_uuid",
            "created_at",
            name="uq_request_attempts_workspace_id_attempt_uuid_created_at",
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # PK part 2 = partition key.
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

    #: EPA A5 (2026-09-07). A stable, producer-side identity for ONE
    #: attempt, generated by the spider before the fetch and carried on
    #: `ScrapeResult.attempt_id` -- distinct from the row's own
    #: `(id, created_at)` composite PK, which only exists once the
    #: persistence pipeline has written the row. It is what lets a spider
    #: log line, a `network_operations` entry and this row be joined for
    #: the SAME attempt without guessing by timestamp proximity.
    #:
    #: NOT NULL with `server_default=gen_random_uuid()`: every pre-A5 row
    #: gets a distinct identity at migration time (backfilling one shared
    #: sentinel would make historical rows look like ONE attempt), and a
    #: writer that does not supply one still cannot produce a row without
    #: an identity. Deliberately NOT unique-constrained here -- uniqueness
    #: on a partitioned table would have to include the partition key, and
    #: this column's job is correlation, not arbitration.
    attempt_uuid: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    scrape_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    strategy_method_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    scrape_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    scrape_profile_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    adapter_key: Mapped[AdapterKey | None] = enum_column(AdapterKey, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    url: Mapped[str] = mapped_column(Text(), nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    identity_validation_result: Mapped[str | None] = mapped_column(Text(), nullable=True)
    terminal_for_target: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=True, server_default=text("true")
    )
    access_method: Mapped[AccessMethod] = enum_column(AccessMethod, nullable=False)
    proxy_provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    proxy_country: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    #: EPA A5 (2026-09-07). `response_time_ms` is one number for four
    #: very different problems; these four split it so a slow domain can
    #: be attributed rather than argued about:
    #:
    #:   `connect_ms` -- TCP + TLS (+ proxy CONNECT) until the request
    #:       could be written. A proxy vendor's problem, not the site's.
    #:   `ttfb_ms`    -- request written until the first response byte.
    #:       The site's/CDN's think time.
    #:   `read_ms`    -- first byte until the document finished arriving.
    #:       Bandwidth and page weight.
    #:   `extract_ms` -- extraction/adapter time after the document was
    #:       in hand. Entirely ours -- the only one a code change fixes.
    #:
    #: All nullable, no server default: NULL means "this boundary was not
    #: measured for this attempt" (every pre-A5 row, and any transport
    #: that does not report the split), never zero milliseconds. They are
    #: NOT constrained to sum to `response_time_ms` -- they are measured
    #: at different layers and a phase that was never entered is absent,
    #: so forcing the identity would require inventing numbers.
    connect_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    read_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    extract_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Task 2.3 (proxy-cost-reduction §2.3): ``'scrape'`` (default, the
    #: batched persistence pipeline) or ``'discovery'`` (the discovery
    #: probe ladder, ``tasks_strategy._probe_sample``). Readers that score
    #: *scrape* outcomes (daily rollup, rediscovery's
    #: ``build_recent_signals``) MUST filter to ``SCRAPE``; spend/volume
    #: accounting (the proxy circuit breaker, ops-snapshot counters)
    #: deliberately reads both origins unfiltered.
    origin: Mapped[RequestOrigin] = enum_column(
        RequestOrigin,
        nullable=False,
        default=RequestOrigin.SCRAPE,
        server_default=RequestOrigin.SCRAPE.value,
    )

    #: EPA B6 (2026-08-25, browser resource blocking policy). Both are
    #: TRANSPORT-OBSERVED byte counts ONLY -- summed from what the
    #: browser/HTTP client actually reported receiving on the wire for
    #: this attempt. Provider-billed bytes are a DISTINCT fact
    #: (compression, CONNECT/TLS overhead, redirects, service workers,
    #: and provider-side accounting all diverge from what the transport
    #: observed) -- reconciled against these two columns in C5, never
    #: conflated with them here. Nullable, no `server_default`: every
    #: pre-B6 row and every non-browser attempt legitimately has no
    #: breakdown to report (NULL == "not measured", not zero).
    #:
    #: `main_document_bytes` -- the page's own top-level document
    #: response (the one navigation the scraper actually wants).
    main_document_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    #: `subresource_bytes` -- every OTHER response the browser loaded
    #: while rendering that document (images/media/fonts/XHR/ads/
    #: analytics/...) -- exactly what
    #: `app_shared.profiles.browser_resource_policy.should_block` decides
    #: to block or let through.
    subresource_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)

    #: EPA C1 (2026-08-25, READY-005): the physical `network_operations`
    #: row this logical attempt was carried by, keyed by that ledger's
    #: pre-dispatch `network_request_id`. NULLABLE, and nullable is not a
    #: coverage claim: every attempt written before C1 legitimately has
    #: no operation, and "every new attempt HAS one" is an invariant
    #: C3/C4 must establish at the write sites — this column only makes
    #: the link expressible and referentially sound.
    network_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )


class MatchCurrentPrice(Base, WorkspaceScopedBase, TimestampMixin):
    """``match_current_prices`` — latest-known price snapshot per match.

    Current-state (not partitioned); single-column PK (``id``);
    ``unique(workspace_id, match_id)`` is the upsert conflict arbiter
    (``insert(...).on_conflict_do_update``, the scraping-side batched
    persistence pipeline). A failure observation never overwrites the
    current price (FR-014).
    """

    __tablename__ = "match_current_prices"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "match_id",
            name="uq_match_current_prices_workspace_id_match_id",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_match_current_prices_workspace_id_workspaces",
        ),
    )

    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_variant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    competitor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    stock_status: Mapped[StockStatus | None] = enum_column(StockStatus, nullable=True)
    comparable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    # Soft ref to the winning price_observations row — no FK (may dangle
    # after a retention-by-drop partition removal, §22).
    observation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    error_code: Mapped[ScrapeErrorCode | None] = enum_column(ScrapeErrorCode, nullable=True)
    extraction_method: Mapped[ExtractionMethod | None] = enum_column(
        ExtractionMethod, nullable=True
    )
    extraction_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    scraped_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)


class ExtractionShadowEvent(Base):
    """``extraction_shadow_events`` — one recorded disagreement between the
    first-hit extraction chain and the W3.2 ranker (EPA C5, F19).

    Written only while ``Settings.EXTRACTION_RANKING_POLICY`` is
    ``'shadow'``, by the scraping library's persistence flush draining the
    in-process buffer its extraction pipeline fills. It exists
    to turn "would the ranker have priced this differently?" into a
    counted number, which is the evidence the C11 owner gate needs before
    the flag may move to ``'v1'``.

    **FLEET-scoped: no ``workspace_id``, no RLS.** This is a deliberate
    classification, not an omission. The row is evidence about an
    EXTRACTION POLICY applied to a DOMAIN — which strategy read what off
    a competitor's public page — and the decision it feeds is fleet-wide
    (one flag, one fleet). Attaching a tenant would be inventing one:
    the same page is read on behalf of every workspace that tracks it,
    so any single ``workspace_id`` here would be an arbitrary pick among
    them, and the sum over tenants is the only meaningful aggregation
    anyway. Filed SYSTEM in ``scripts/rls_table_manifest.txt`` alongside
    ``domain_playbooks``, granted to no tenant role beyond the ingestion
    INSERT the scraper needs.

    Append-only by convention: nothing updates or deletes a row here
    except retention.
    """

    __tablename__ = "extraction_shadow_events"

    #: When the comparison ran (producer clock), NOT when the row was
    #: written — the two differ by a flush interval, and a rate computed
    #: over write time would smear a burst across the wrong window.
    observed_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)

    #: Registrable domain of the page. Nullable because a producer that
    #: could not supply a URL still produced a real disagreement, and
    #: dropping it would bias the measured rate toward whichever call
    #: sites happen to be wired.
    domain: Mapped[str | None] = mapped_column(Text(), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text(), nullable=True)

    #: ``RankingPolicy.version`` the shadow run used ("v1" today) — the
    #: gate is a statement about a specific policy, so a later policy
    #: revision must not silently inherit this one's evidence.
    policy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text(), nullable=False)
    profile_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    #: ``price`` | ``currency`` | ``outcome``. A different *method*
    #: reaching the same price is not a disagreement and produces no row
    #: at all — see the extraction pipeline's `_shadow_disagreement`.
    disagreement_kind: Mapped[str] = mapped_column(Text(), nullable=False)

    first_hit_method: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Money, not a float — the same ``NUMERIC(18,4)`` contract
    #: ``price_observations.price`` uses, because these two numbers are
    #: compared against each other and against labeled truth.
    first_hit_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    first_hit_currency: Mapped[str | None] = mapped_column(Text(), nullable=True)

    #: ``winner`` | ``conflict`` | ``no_valid``.
    ranked_outcome: Mapped[str] = mapped_column(Text(), nullable=False)
    ranked_method: Mapped[str | None] = mapped_column(Text(), nullable=True)
    ranked_price: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    ranked_currency: Mapped[str | None] = mapped_column(Text(), nullable=True)

    #: Content address of the DECODED page text the extractor read. NOT
    #: guaranteed equal to the observation's ``offer_raw_evidence_hash``
    #: (raw bytes as received) — see the buffer's own field docstring.
    page_evidence_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, server_default=text("now()")
    )
