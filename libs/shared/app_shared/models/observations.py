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
        {"postgresql_partition_by": "RANGE (scraped_at)"},
    )

    # PK part 2 = partition key (Postgres requires the partition key be
    # part of the primary key on a partitioned table).
    scraped_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)

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
        ForeignKeyConstraint(
            ["network_operation_id"],
            ["network_operations.network_request_id"],
            name="fk_request_attempts_network_operation_id_network_operations",
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
