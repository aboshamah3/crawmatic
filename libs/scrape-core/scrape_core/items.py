"""``ScrapeResult`` — the transport item flowing spider -> persistence pipeline.

Per ``data-model.md`` "Transport shapes": a single ``ScrapeResult``
carries the full ``price_observations`` field set + the full
``request_attempts`` field set + the correlation/scoping identifiers
(``workspace_id``/``match_id``/``product_id``/``product_variant_id``/
``competitor_id``/``scrape_job_id``), so
``scrape_core.pipelines.BatchedPersistencePipeline`` can turn one item
into one observation row + one request-attempt row (+ possibly one
``match_current_prices`` upsert) without a second lookup.

A plain ``dataclass`` rather than a ``scrapy.Item`` — pure stdlib, so it
stays importable/constructible without Scrapy installed (unit-testable
off-reactor); Scrapy's item pipeline machinery only needs duck-typed
attribute access, which a dataclass instance already provides.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app_shared.enums import AdapterKey, AccessMethod, ExtractionMethod, ScrapeErrorCode, StockStatus
from app_shared.observations.offer_observation import OfferObservation

#: EPA C5 (F19). The version of the EXTRACTION CONTRACT this library
#: produces — the ordered chain, the money boundary it crosses, and the
#: shape of the candidate it emits — stamped onto every observation so a
#: price read months ago can be attributed to the extractor that read it.
#:
#: Deliberately NOT a package version: a dependency bump that changes no
#: extraction behaviour must not invalidate the attribution of every
#: historical row. Bump this string only when the chain's *output* for
#: the same bytes can change.
#:
#: It lives here, on the transport item, rather than in
#: `scrape_core.extraction.pipeline`, because `items` is the module both
#: the extractor and the persistence pipeline already depend on;
#: importing the extraction chain from here would make the transport
#: dataclass drag `app_shared.strategy.candidate_ranking` into every
#: process that merely wants to construct a result.
EXTRACTOR_VERSION = "scrape-core-extraction-1"

#: Where the PERSISTED price came from. Distinct from
#: `extraction_method` (which strategy read it) — this says which
#: *policy* decided that reading was the one to keep, which is the fact
#: an audit of a repricing decision needs and the fact that changes when
#: `EXTRACTION_RANKING_POLICY` is flipped at C11.
PROVENANCE_FIRST_HIT = "first_hit"
#: The W3.2 ranked path picked this reading (`EXTRACTION_RANKING_POLICY`
#: = `'v1'`). Never produced while the flag stays `'shadow'` — in shadow
#: mode the ranker runs but does not decide, so the row still says
#: `first_hit`.
PROVENANCE_RANKED_V1 = "ranked_v1"
#: No extraction produced this result at all (a failure/skip row). NOT
#: an unknown-provenance placeholder: it is a positive statement that
#: nothing was read.
PROVENANCE_NONE = "none"

PROVENANCE_VALUES: tuple[str, ...] = (
    PROVENANCE_FIRST_HIT,
    PROVENANCE_RANKED_V1,
    PROVENANCE_NONE,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "PROVENANCE_FIRST_HIT",
    "PROVENANCE_NONE",
    "PROVENANCE_RANKED_V1",
    "PROVENANCE_VALUES",
    "ScrapeResult",
]


@dataclass
class ScrapeResult:
    """One attempted target's full outcome: an observation + a request attempt.

    Required scoping/correlation fields have no default (a caller must
    always supply them); every field that maps to a nullable DB column
    defaults to ``None`` so a failure-path item can be constructed with
    only the fields it actually has.
    """

    # --- Scoping / correlation (never null) ---
    workspace_id: uuid.UUID
    match_id: uuid.UUID
    product_id: uuid.UUID
    product_variant_id: uuid.UUID
    competitor_id: uuid.UUID
    scrape_job_id: uuid.UUID | None

    # --- request_attempts field set ---
    url: str
    access_method: AccessMethod
    attempt_number: int = 1
    proxy_provider_id: uuid.UUID | None = None
    proxy_country: str | None = None
    status_code: int | None = None
    response_time_ms: int | None = None

    # --- price_observations field set ---
    scraped_at: datetime | None = None
    price: Decimal | None = None
    old_price: Decimal | None = None
    currency: str | None = None
    stock_status: StockStatus | None = None
    raw_title: str | None = None
    extraction_method: ExtractionMethod | None = None
    extraction_confidence: Decimal | None = None
    selector_used: str | None = None

    # --- shared outcome (both the observation and the attempt rows) ---
    success: bool = False
    comparable: bool = True
    error_code: ScrapeErrorCode | None = None
    error_message: str | None = None

    # --- SPEC-11 US2 match-lock release-only carry-through (contracts/
    # match-lock.md, contracts/spider-integration.md "Match-lock release")
    # --- NOT persisted to any DB row: `scrape_core.pipelines._flush_batch`
    # reads these only to call `release_match_lock` after the
    # observation/attempt write commits, in the same off-reactor flush.
    # Populated from `response.meta` for a dispatched-and-fetched attempt;
    # `None` for an attempt that never acquired a lock (e.g. the SKIPPED/
    # not-dispatched path) -- a missing token means no release is attempted.
    match_lock_key: str | None = None
    match_lock_token: str | None = None

    # --- SPEC-12 US2 (contracts/consumption.md step 4, D5/D6) ---
    # The `domain_strategy_profiles` row (if any) this attempt's
    # `(competitor_id, url_pattern)` group resolved to, threaded through
    # from `generic_price_spider.load_targets`'s per-group get-or-create
    # seam so US5's off-reactor stats recorder (`_flush_batch` ->
    # `app_shared.strategy.stats_buffer.record_attempt`) has the profile
    # id without a second query. `None` when the group resolved no
    # profile at all (should not happen post-T021, which always
    # get-or-creates one) or for a hand-built pre-SPEC-12 item (unit
    # tests) that never threads it through.
    domain_strategy_profile_id: uuid.UUID | None = None

    # Versioned strategy/profile/adapter audit.  These values reproduce the
    # exact runnable candidate even after an operator reorders or revises the
    # profile, and carry a durable cross-node handoff when one exists.
    strategy_method_id: uuid.UUID | None = None
    scrape_profile_id: uuid.UUID | None = None
    scrape_profile_version: int | None = None
    adapter_key: AdapterKey | None = None
    final_url: str | None = None
    identity_validation_result: str | None = None
    terminal_for_target: bool = True
    next_strategy_method_id: uuid.UUID | None = None
    strategy_attempt_ordinal: int = 0
    chain_token: uuid.UUID | None = None
    canonical_url: str | None = None

    # --- 2026-08-02: "this failure must NOT terminalize the target" ---
    # A failed attempt whose *next* attempt was rate-ceiling-gated is not a
    # terminal outcome -- the target is going back to `scrape_dispatch` to
    # be retried later. `_flush_batch` marks such a target `DEFERRED`
    # instead of `FAILED`, in the SAME write that records the attempt, so
    # the intent can never lose a race against a separate mark (blind
    # last-writer-wins `mark_target` is what let S-Tech's retry path
    # terminal-fail 79 of 96 links in the Cohort B run). The observation/
    # attempt rows themselves are written exactly as any other failure.
    defer_target: bool = False

    # --- 2026-08-24: strategy-chain lifecycle ownership ---
    # Every attempt is persisted, but a failed attempt may be followed by
    # another access/extraction candidate.  Only the owner of that chain can
    # know whether this is its final outcome, so the persistence pipeline
    # must not infer terminality merely from ``success is False``.
    #
    # ``True`` is the compatibility default for hand-built items and
    # single-attempt producers.  Multi-attempt spiders explicitly set it to
    # ``False`` while a retry/fallback remains.  Successful results complete
    # the target regardless of this flag; ``defer_target`` remains the
    # separate, explicit hand-back-to-dispatch signal.
    chain_complete: bool = True

    # --- EPA B6 (folded-in item 2): live NEEDS_REVIEW sidecar wiring ---
    # Carries a B4 adapter's `AdapterResult.metadata["needs_review"]`
    # (set on an `Ambiguous`/`IdentityIncompatible` variant resolution --
    # see `scrape_core.adapters.variant_resolution`) through to
    # `scrape_core.pipelines._flush_batch`, which upserts the match's
    # `match_audit_classifications` sidecar (A6) to `NEEDS_REVIEW` when
    # this is `True`. `False` (the default) leaves the sidecar untouched.
    needs_review: bool = False

    # --- EPA B6 (browser resource blocking policy): byte accounting ---
    # Both TRANSPORT-OBSERVED figures (never provider-billed -- see
    # ``request_attempts.main_document_bytes``/``subresource_bytes``'s own
    # column docstrings, ``alembic/versions/
    # d5e8a3c164f2_request_attempt_byte_accounting.py``, for why those are
    # recorded as a DISTINCT fact reconciled only in C5). ``None`` means
    # "not measured for this attempt" -- the compatibility default for
    # every non-browser (HTTP) producer and any browser attempt this
    # phase's spider does not yet instrument; never coerced to ``0``.
    main_document_bytes: int | None = None
    subresource_bytes: int | None = None

    # --- EPA C4 (network boundary recording): the PHYSICAL operation ---
    # The `network_operations.network_request_id` (C1) of the physical
    # fetch that produced this logical result, stamped onto
    # `request.meta` by `scrape_core.netledger_middleware` before the
    # socket opened. `None` means "this result did not come from a
    # recorded fetch" -- a never-dispatched skip/defer row, or a crawl
    # with `NETLEDGER_ENABLED = False`; never coerced to a fabricated id.
    #
    # This is the field that makes a fan-out honest: five sibling matches
    # riding ONE deduplicated fetch each get their own `RequestAttempt`
    # row, and all five carry the SAME `network_operation_id` -- five
    # logical attempts, one physical operation, one cost.
    network_operation_id: uuid.UUID | None = None

    # --- EPA A5 (2026-09-07): attempt identity + per-phase timing --------
    #
    # `attempt_id` is the PRODUCER-side identity of this one attempt,
    # generated by the spider before the fetch and persisted to
    # `request_attempts.attempt_uuid`. The row's own `(id, created_at)`
    # PK only exists after the flush, so it cannot correlate a spider log
    # line, a `network_operations` entry and the attempt row; this can.
    # `None` is the compatibility default for every hand-built item and
    # every producer that does not mint one -- `pipelines._flush_batch`
    # then generates one at write time rather than leaving the NOT NULL
    # column to a server default, so the identity is knowable in-process.
    attempt_id: uuid.UUID | None = None

    # The three phase boundaries the SPIDER observes, threaded through to
    # `scrape_job_targets` by `_flush_batch` (which adds `persisted_at`
    # itself). `None` means "not instrumented / not reached for this
    # attempt" -- never coerced to a fabricated timestamp, because a
    # fabricated boundary would show up as a zero-length phase in
    # `crawmatic_target_phase_p95_seconds` and quietly flatter the p95.
    #
    #   `first_network_at`       -- first byte on the wire. HTTP:
    #       `request.meta["download_slot_start"]`. Browser: the
    #       Playwright `request.timing["requestStart"]` epoch.
    #   `document_received_at`   -- the response the extractor will read
    #       finished arriving.
    #   `extraction_finished_at` -- extraction/adapters done.
    first_network_at: datetime | None = None
    document_received_at: datetime | None = None
    extraction_finished_at: datetime | None = None

    # The same life, in milliseconds, on the `request_attempts` row --
    # `response_time_ms` split into the four phases that have four
    # different owners (see `app_shared.models.observations.RequestAttempt`
    # for why they are deliberately not constrained to sum to it). All
    # `None` by default: NULL means "not measured", never 0 ms.
    connect_ms: int | None = None
    ttfb_ms: int | None = None
    read_ms: int | None = None
    extract_ms: int | None = None

    # --- EPA C5 (2026-09-08, F19): the structured offer contract on the
    # live path ---------------------------------------------------------
    #
    # `offer` is the W3.1 canonical observation
    # (`app_shared.observations.offer_observation.OfferObservation`) —
    # seller, shipping, fees, promotion facts, per-dimension confidence —
    # which until now existed as a validated contract and a set of
    # `price_observations.offer_*` columns that NOTHING on the live
    # scrape path ever wrote. `scrape_core.pipelines._flush_batch` reads
    # this field to populate those columns. `None` (the default) leaves
    # every `offer_*` column NULL exactly as before, so a producer that
    # does not build one is unaffected.
    offer: OfferObservation | None = None

    # The RAW BYTES the extraction read, carried only as far as
    # `_flush_batch`, which writes them into the content-addressed store
    # (`app_shared.observations.evidence_store.store_evidence`) and keeps
    # the resulting hash on the row. NOT persisted itself and never
    # logged: it is a whole competitor page.
    #
    # It is bytes, not the decoded string, because the hash must name
    # what actually arrived on the wire — a decoded-then-re-encoded page
    # is a different byte sequence and would produce an address for
    # bytes that never existed.
    #
    # `None` means "this attempt has no replayable evidence" (a
    # never-dispatched skip, or a producer not yet wired). The store is
    # only written when `Settings.EVIDENCE_STORE_DIR` is configured —
    # recording a hash for bytes nobody stored is the exact failure
    # `docs/RETENTION_POLICY.md` §2.1 warns about.
    raw_evidence: bytes | None = None

    # The three provenance facts that make a persisted price
    # attributable, all non-optional because all three are always
    # knowable by the producer (unlike the fields above, where NULL is a
    # real state):
    #
    #   `extractor_version` — which extraction contract read it.
    #   `profile_version`   — which revision of the scrape profile
    #       configured that read. `0` is not "unknown": it means "no
    #       scrape profile was resolved for this target" (an unprofiled
    #       or never-dispatched attempt), which is a different fact from
    #       a profile at version 0 — there is no such version, profiles
    #       start at 1.
    #   `provenance`        — which POLICY chose the reading that got
    #       persisted (`PROVENANCE_*` above). In `shadow` mode this stays
    #       `first_hit` even though the ranker also ran, because the
    #       first hit is what was written.
    extractor_version: str = EXTRACTOR_VERSION
    profile_version: int = 0
    provenance: str = PROVENANCE_NONE

    # The extraction's own confidence, as an exact `Decimal` in [0, 1].
    # `Decimal("0")` on a failure/skip row is a positive statement, not a
    # placeholder: nothing was read, so nothing is trusted. It is the
    # value `pipelines._monotonic_conflict_where` compares when refusing
    # to let a worse reading overwrite a better one.
    #
    # Distinct from `extraction_confidence` above only in that this one
    # is never NULL; the two carry the same number for a candidate-bearing
    # result.
    confidence: Decimal = Decimal("0")
