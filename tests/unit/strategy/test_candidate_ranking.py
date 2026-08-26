"""W3.2 (READY-012): candidate collection + ranking replaces first-candidate-wins.

Pure unit tests — no DB, no network, no ``.env``. The one real fetch
artifact used here is the committed ``tests/fixtures/html/noon_product_real.html``
capture (see its provenance header), which is what makes the
"plausible-but-wrong early candidate" case a regression test rather
than a thought experiment.
"""

from __future__ import annotations

import gzip
import itertools
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app_shared.enums import CompetitorIdentifierType, ExtractionMethod
from app_shared.observations.evidence_store import replay
from app_shared.strategy.candidate_ranking import (
    POLICY_V1,
    Candidate,
    CandidateIdentity,
    Conflict,
    FetchEnvelope,
    NoValid,
    RejectionReason,
    SourceTier,
    Winner,
    collect_candidates,
    rank,
    search_bounded,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
NOON_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "html" / "noon_product_real.html"

# The SKU both noon strategies agree on — identity agreement is what makes
# the price divergence in that fixture *material* rather than two readings
# of two different products.
NOON_SKU = "N28408683A"

SAFE_URL = "https://www.noon.com/saudi-en/x/N28408683A/p/"


def _identity(sku: str = NOON_SKU, currency: str | None = "SAR") -> CandidateIdentity:
    return CandidateIdentity(
        identifiers=((CompetitorIdentifierType.SKU, sku),),
        currency=currency,
    )


def _candidate(
    price: str,
    method: ExtractionMethod,
    *,
    confidence: float = 0.95,
    identity: CandidateIdentity | None = None,
    evidence: bytes | None = None,
) -> Candidate:
    return Candidate(
        price=Decimal(price),
        currency="SAR",
        method=method,
        confidence=confidence,
        identity=identity if identity is not None else _identity(),
        evidence=evidence if evidence is not None else f"{method}:{price}".encode(),
    )


def _envelope(**overrides: object) -> FetchEnvelope:
    base: dict[str, object] = {
        "url": SAFE_URL,
        "final_url": SAFE_URL,
        "content_type": "text/html; charset=utf-8",
        "redirect_chain": (SAFE_URL,),
        "transferred_bytes": 1024,
        "decoded_bytes": 4096,
    }
    base.update(overrides)
    return FetchEnvelope(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. Stale JSON-LD vs fresh DOM, identity agreeing -> the correct one wins
# ---------------------------------------------------------------------------


def test_stale_jsonld_loses_to_fresh_dom_under_policy_v1() -> None:
    """Server-cached JSON-LD markup and the rendered DOM disagree slightly.

    Both name the same SKU and the same currency, and the gap is inside
    policy v1's tolerance, so this is *not* a conflict — it is a stale
    mirror of the truth. Policy v1 ranks on source freshness first
    (rendered DOM > page state > structured metadata > heuristic), so
    the DOM price wins even though JSON-LD carries the higher §17
    per-method confidence (0.95 vs 0.85).
    """
    stale = _candidate("99.00", ExtractionMethod.JSON_LD, confidence=0.95)
    fresh = _candidate("94.50", ExtractionMethod.CSS, confidence=0.85)

    result = rank([stale, fresh], POLICY_V1)

    assert isinstance(result, Winner), result
    assert result.candidate is fresh
    assert result.candidate.price == Decimal("94.50")
    assert result.policy_version == "v1"
    # The loser is retained as corroboration, not discarded.
    assert stale in result.corroborating


def test_source_tier_orders_dom_above_structured_metadata() -> None:
    assert SourceTier.for_method(ExtractionMethod.CSS) > SourceTier.for_method(
        ExtractionMethod.JSON_LD
    )
    assert SourceTier.for_method(ExtractionMethod.EMBEDDED_JSON) > SourceTier.for_method(
        ExtractionMethod.JSON_LD
    )
    assert SourceTier.for_method(ExtractionMethod.JSON_LD) > SourceTier.for_method(
        ExtractionMethod.REGEX
    )


# ---------------------------------------------------------------------------
# 2. Material divergence -> Conflict, never a silent pick
# ---------------------------------------------------------------------------


def test_material_price_divergence_is_a_conflict_not_a_silent_pick() -> None:
    high = _candidate("499", ExtractionMethod.CSS)
    low = _candidate("129", ExtractionMethod.JSON_LD)

    result = rank([high, low], POLICY_V1)

    assert isinstance(result, Conflict), result
    assert set(result.top_candidates) == {high, low}
    assert result.needs_review is True
    assert result.policy_version == "v1"


def test_winner_is_not_returned_for_a_conflict() -> None:
    result = rank(
        [_candidate("499", ExtractionMethod.CSS), _candidate("129", ExtractionMethod.JSON_LD)],
        POLICY_V1,
    )
    assert not isinstance(result, Winner)


# ---------------------------------------------------------------------------
# 3. Regression on a REAL captured page: a plausible-but-wrong early
#    candidate no longer suppresses the later correct one.
# ---------------------------------------------------------------------------


def _noon_html() -> str:
    return NOON_FIXTURE.read_text(encoding="utf-8")


def test_first_candidate_wins_suppressed_the_correct_noon_price() -> None:
    """Documents the bug W3.2 replaces (real 2026-08-16 noon capture).

    A learned domain whose winning strategy is REGEX (SPEC-12 US2) with a
    sloppy ``price:(\\d+)`` rule matches ``price:499`` — the strikethrough
    list price — inside noon's ``$tsr`` state blob. Under first-hit-wins
    that plausible 499 is returned and the correct 129 (JSON-LD's
    ``offers.price``, and the blob's own ``sale_price``) is never even
    computed.
    """
    from scrape_core.extraction.pipeline import extract

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    suppressed = extract(
        _noon_html(), profile, preferred_method=ExtractionMethod.REGEX
    )

    assert suppressed is not None
    assert suppressed.raw_price_text == "499"
    assert suppressed.method is ExtractionMethod.REGEX


def test_noon_wrong_early_candidate_no_longer_suppresses_the_correct_one() -> None:
    """Same page, same profile, ranked path: 129 survives and nothing is picked."""
    from scrape_core.extraction.pipeline import collect_extraction_candidates

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    collection = collect_extraction_candidates(
        _noon_html(), profile, preferred_method=ExtractionMethod.REGEX
    )

    prices = {candidate.price for candidate in collection.candidates}
    assert Decimal("499") in prices, "the plausible-but-wrong candidate is still collected"
    assert Decimal("129") in prices, "the correct later candidate is no longer suppressed"

    result = rank(collection.candidates, POLICY_V1)
    assert isinstance(result, Conflict), result
    assert Decimal("129") in {candidate.price for candidate in result.top_candidates}
    assert result.needs_review is True


def test_noon_ranked_extract_returns_no_winner_and_flags_review() -> None:
    from scrape_core.extraction.pipeline import extract_ranked

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    result = extract_ranked(
        _noon_html(), profile, preferred_method=ExtractionMethod.REGEX
    )
    assert isinstance(result, Conflict)
    assert result.needs_review is True


# ---------------------------------------------------------------------------
# 4. ReDoS guard
# ---------------------------------------------------------------------------

# Generous relative to the ~0.0001s a rejected pattern actually takes, but
# tiny relative to the minutes `(a+)+$` would burn unguarded.
REDOS_TIME_BUDGET_SECONDS = 2.0


def test_pathological_pattern_completes_under_the_time_budget() -> None:
    pathological = r"(a+)+$"
    hostile_input = "a" * 20000 + "!"

    started = time.perf_counter()
    match, rejection = search_bounded(pathological, hostile_input, policy=POLICY_V1)
    elapsed = time.perf_counter() - started

    assert elapsed < REDOS_TIME_BUDGET_SECONDS, f"took {elapsed:.3f}s"
    assert match is None
    assert rejection is not None
    assert rejection.reason is RejectionReason.REDOS_PATTERN_REJECTED


def test_alternation_redos_pattern_is_rejected() -> None:
    match, rejection = search_bounded(r"(a|a)*$", "a" * 20000 + "!", policy=POLICY_V1)
    assert match is None
    assert rejection is not None
    assert rejection.reason is RejectionReason.REDOS_PATTERN_REJECTED


def test_safe_pattern_on_oversized_input_is_truncated_not_rejected() -> None:
    started = time.perf_counter()
    match, rejection = search_bounded(r"price:(\d+)", "x" * 5_000_000 + "price:129", policy=POLICY_V1)
    elapsed = time.perf_counter() - started

    assert elapsed < REDOS_TIME_BUDGET_SECONDS, f"took {elapsed:.3f}s"
    assert rejection is None
    # Input past the policy budget is not scanned, so the tail match is
    # simply not found — bounded, never a hang.
    assert match is None


def test_safe_pattern_within_budget_still_matches() -> None:
    match, rejection = search_bounded(r"price:(\d+)", "price:129", policy=POLICY_V1)
    assert rejection is None
    assert match is not None
    assert match.group(1) == "129"


def test_oversized_pattern_is_rejected() -> None:
    match, rejection = search_bounded("a" * (POLICY_V1.max_regex_pattern_chars + 1), "aaa", policy=POLICY_V1)
    assert match is None
    assert rejection is not None
    assert rejection.reason is RejectionReason.REDOS_PATTERN_REJECTED


def test_invalid_pattern_is_rejected_not_raised() -> None:
    match, rejection = search_bounded(r"(unclosed", "aaa", policy=POLICY_V1)
    assert match is None
    assert rejection is not None
    assert rejection.reason is RejectionReason.REDOS_PATTERN_REJECTED


# ---------------------------------------------------------------------------
# 5. Response-size / decompression-ratio / redirect / content-type limits
# ---------------------------------------------------------------------------


def _always_one_candidate(page: object) -> Candidate:
    return _candidate("129", ExtractionMethod.JSON_LD)


def test_response_size_limit_is_enforced_before_any_strategy_runs() -> None:
    envelope = _envelope(
        transferred_bytes=POLICY_V1.max_response_bytes + 1,
        decoded_bytes=POLICY_V1.max_response_bytes + 1,
    )
    collection = collect_candidates(envelope, [_always_one_candidate], policy=POLICY_V1)

    assert collection.candidates == ()
    assert collection.blocked is True
    assert RejectionReason.RESPONSE_TOO_LARGE in {r.reason for r in collection.rejections}
    assert isinstance(rank(collection.candidates, POLICY_V1, rejections=collection.rejections), NoValid)


def test_decompression_ratio_limit_is_enforced() -> None:
    payload = b"\0" * (4 * 1024 * 1024)
    compressed = gzip.compress(payload)
    envelope = _envelope(
        transferred_bytes=len(compressed),
        decoded_bytes=len(payload),
    )
    collection = collect_candidates(envelope, [_always_one_candidate], policy=POLICY_V1)

    assert collection.blocked is True
    assert RejectionReason.DECOMPRESSION_RATIO_EXCEEDED in {r.reason for r in collection.rejections}


def test_redirect_limit_is_enforced() -> None:
    chain = tuple(f"https://example.com/hop-{index}" for index in range(POLICY_V1.max_redirects + 2))
    envelope = _envelope(url=chain[0], final_url=chain[-1], redirect_chain=chain)
    collection = collect_candidates(envelope, [_always_one_candidate], policy=POLICY_V1)

    assert collection.blocked is True
    assert RejectionReason.TOO_MANY_REDIRECTS in {r.reason for r in collection.rejections}


def test_content_type_limit_is_enforced() -> None:
    envelope = _envelope(content_type="application/pdf")
    collection = collect_candidates(envelope, [_always_one_candidate], policy=POLICY_V1)

    assert collection.blocked is True
    assert RejectionReason.DISALLOWED_CONTENT_TYPE in {r.reason for r in collection.rejections}


def test_limits_within_policy_let_collection_proceed() -> None:
    collection = collect_candidates(_envelope(), [_always_one_candidate], policy=POLICY_V1)
    assert collection.blocked is False
    assert len(collection.candidates) == 1


# ---------------------------------------------------------------------------
# 6. SSRF recheck on every hop + on DNS resolution (reusing url_safety)
# ---------------------------------------------------------------------------


def test_ssrf_recheck_rejects_an_unsafe_intermediate_redirect_hop() -> None:
    """The *final* URL is public; a middle hop is the cloud metadata service.

    Checking only the final URL is the classic redirect-SSRF miss, so
    collection re-runs the save-time validator on every hop.
    """
    chain = (
        SAFE_URL,
        "http://169.254.169.254/latest/meta-data/",
        SAFE_URL,
    )
    envelope = _envelope(url=chain[0], final_url=chain[-1], redirect_chain=chain)
    collection = collect_candidates(envelope, [_always_one_candidate], policy=POLICY_V1)

    assert collection.blocked is True
    unsafe = [r for r in collection.rejections if r.reason is RejectionReason.UNSAFE_URL]
    assert unsafe, collection.rejections
    assert "169.254.169.254" in unsafe[0].detail


def test_ssrf_recheck_rejects_an_internal_hostname_hop() -> None:
    chain = (SAFE_URL, "http://scrapyd-http/status", SAFE_URL)
    envelope = _envelope(url=chain[0], final_url=chain[-1], redirect_chain=chain)
    collection = collect_candidates(envelope, [_always_one_candidate], policy=POLICY_V1)
    assert collection.blocked is True
    assert RejectionReason.UNSAFE_URL in {r.reason for r in collection.rejections}


def test_ssrf_recheck_runs_dns_resolution_when_a_resolver_is_injected() -> None:
    """A public-looking host that resolves to loopback (DNS rebinding).

    The injected checker is the production one
    (``scrape_core.safety.fetch.validate_resolved_target``) with a fake
    resolver, so this asserts the ranking path goes through the EXISTING
    guard rather than a second implementation.
    """
    from scrape_core.safety.fetch import validate_resolved_target

    resolved: list[str] = []

    def fake_resolver(host: str) -> list[str]:
        resolved.append(host)
        return ["127.0.0.1"] if host == "rebind.example.com" else ["93.184.216.34"]

    def url_check(url: str) -> None:
        validate_resolved_target(url, resolver=fake_resolver)

    chain = (SAFE_URL, "https://rebind.example.com/product", SAFE_URL)
    envelope = _envelope(url=chain[0], final_url=chain[-1], redirect_chain=chain)
    collection = collect_candidates(
        envelope, [_always_one_candidate], policy=POLICY_V1, url_check=url_check
    )

    assert collection.blocked is True
    assert RejectionReason.UNSAFE_URL in {r.reason for r in collection.rejections}
    # Every hop was resolved, not just the final one.
    assert resolved.count("rebind.example.com") == 1
    assert "www.noon.com" in resolved


def test_safe_chain_with_injected_resolver_is_not_blocked() -> None:
    from scrape_core.safety.fetch import validate_resolved_target

    def url_check(url: str) -> None:
        validate_resolved_target(url, resolver=lambda host: ["93.184.216.34"])

    collection = collect_candidates(
        _envelope(), [_always_one_candidate], policy=POLICY_V1, url_check=url_check
    )
    assert collection.blocked is False


# ---------------------------------------------------------------------------
# 7. Evidence hashes + rejection reasons retained for replay
# ---------------------------------------------------------------------------


def test_collected_candidates_carry_replayable_evidence_hashes(tmp_path: Path) -> None:
    def strategy(page: object) -> Candidate:
        return _candidate("129", ExtractionMethod.JSON_LD, evidence=b'{"price": 129}')

    collection = collect_candidates(
        _envelope(), [strategy], policy=POLICY_V1, evidence_store_dir=tmp_path
    )
    candidate = collection.candidates[0]

    assert candidate.evidence_hash is not None
    assert replay(candidate.evidence_hash, store_dir=tmp_path).data == b'{"price": 129}'


def test_rejections_retain_the_rejected_candidates_evidence_hash(tmp_path: Path) -> None:
    def strategy(page: object) -> Candidate:
        return _candidate(
            "129",
            ExtractionMethod.SINGLE_NUMBER,
            confidence=0.40,
            evidence=b"129",
        )

    collection = collect_candidates(
        _envelope(), [strategy], policy=POLICY_V1, evidence_store_dir=tmp_path
    )
    result = rank(collection.candidates, POLICY_V1, rejections=collection.rejections)

    assert isinstance(result, NoValid), result
    low = [r for r in result.reasons if r.reason is RejectionReason.LOW_CONFIDENCE]
    assert low, result.reasons
    assert low[0].evidence_hash is not None
    assert replay(low[0].evidence_hash, store_dir=tmp_path).data == b"129"


def test_strategy_exception_becomes_a_retained_rejection_not_a_crash() -> None:
    def boom(page: object) -> Candidate:
        raise RuntimeError("selector blew up")

    collection = collect_candidates(
        _envelope(), [boom, _always_one_candidate], policy=POLICY_V1
    )

    assert len(collection.candidates) == 1
    errors = [r for r in collection.rejections if r.reason is RejectionReason.STRATEGY_ERROR]
    assert errors
    assert "selector blew up" in errors[0].detail


def test_collection_is_bounded_by_policy_max_candidates() -> None:
    strategies = [_always_one_candidate] * (POLICY_V1.max_candidates + 5)
    collection = collect_candidates(_envelope(), strategies, policy=POLICY_V1)

    assert len(collection.candidates) == POLICY_V1.max_candidates
    assert RejectionReason.CANDIDATE_LIMIT_EXCEEDED in {r.reason for r in collection.rejections}


# ---------------------------------------------------------------------------
# 8. Identity semantics — the B4 ruling must not regress
# ---------------------------------------------------------------------------


def test_unknown_typed_identifier_never_establishes_identity_agreement() -> None:
    """B4: ``UNKNOWN`` is an honest type, not a variant id.

    Two candidates whose only shared identifier is ``UNKNOWN`` have not
    been shown to describe the same offer, so an ``UNKNOWN`` value can
    neither prove agreement nor prove disagreement.
    """
    left = CandidateIdentity(
        identifiers=((CompetitorIdentifierType.UNKNOWN, "12345"),), currency="SAR"
    )
    right = CandidateIdentity(
        identifiers=((CompetitorIdentifierType.UNKNOWN, "67890"),), currency="SAR"
    )

    assert left.conflicts_with(right) is False
    assert left.corroborates(right) is False


def test_same_typed_identifier_value_corroborates() -> None:
    assert _identity().corroborates(_identity()) is True


def test_different_sku_values_conflict() -> None:
    assert _identity("A").conflicts_with(_identity("B")) is True


def test_currency_mismatch_conflicts() -> None:
    assert _identity(currency="SAR").conflicts_with(_identity(currency="AED")) is True


def test_conflicting_identities_reject_the_minority_reading() -> None:
    agreed_a = _candidate("129", ExtractionMethod.CSS, identity=_identity("A"))
    agreed_b = _candidate("129", ExtractionMethod.JSON_LD, identity=_identity("A"))
    other = _candidate("77", ExtractionMethod.REGEX, confidence=0.75, identity=_identity("B"))

    result = rank([agreed_a, agreed_b, other], POLICY_V1)

    assert isinstance(result, Winner), result
    assert result.candidate.price == Decimal("129")
    assert RejectionReason.IDENTITY_DISAGREEMENT in {r.reason for r in result.rejections}


def test_evenly_split_identity_disagreement_is_a_conflict() -> None:
    left = _candidate("129", ExtractionMethod.CSS, identity=_identity("A"))
    right = _candidate("77", ExtractionMethod.JSON_LD, identity=_identity("B"))

    result = rank([left, right], POLICY_V1)
    assert isinstance(result, Conflict), result
    assert result.needs_review is True


# ---------------------------------------------------------------------------
# 9. NoValid + low-confidence gate
# ---------------------------------------------------------------------------


def test_no_candidates_yields_novalid() -> None:
    result = rank([], POLICY_V1)
    assert isinstance(result, NoValid)
    assert result.policy_version == "v1"


def test_single_number_heuristic_is_below_the_policy_v1_confidence_gate() -> None:
    weak = _candidate("129", ExtractionMethod.SINGLE_NUMBER, confidence=0.40)
    result = rank([weak], POLICY_V1)

    assert isinstance(result, NoValid), result
    assert {r.reason for r in result.reasons} == {RejectionReason.LOW_CONFIDENCE}


# ---------------------------------------------------------------------------
# 10. Wiring: default extraction behavior is byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_default_extract_still_returns_the_first_hit() -> None:
    """The ranked path is opt-in; the live default is untouched (W3.2 flag)."""
    from scrape_core.extraction.pipeline import extract

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    candidate = extract(_noon_html(), profile)
    assert candidate is not None
    assert candidate.method is ExtractionMethod.JSON_LD
    assert candidate.raw_price_text == "129"


def test_ranked_extract_returns_the_original_extraction_candidate_on_agreement() -> None:
    """With the flag on and no disagreement, the caller gets the SAME
    ``ExtractionCandidate`` object the chain would have produced — with
    its ``stock``/``matched_text`` intact, not a reconstruction."""
    from scrape_core.extraction.pipeline import extract

    # No price_regex configured, so only JSON-LD sees a price here.
    winner = extract(_noon_html(), None, ranking_policy=POLICY_V1)

    assert winner is not None
    assert winner.method is ExtractionMethod.JSON_LD
    assert winner.raw_price_text == "129"
    assert winner.stock is not None
    assert winner.matched_text is not None


def test_ranked_extract_returns_none_rather_than_silently_picking_on_conflict() -> None:
    from scrape_core.extraction.pipeline import extract

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    assert (
        extract(
            _noon_html(),
            profile,
            preferred_method=ExtractionMethod.REGEX,
            ranking_policy=POLICY_V1,
        )
        is None
    )


def test_riyal_currency_normalization_survives_into_a_ranked_candidate() -> None:
    """B4: ``ريال`` -> ``SAR`` happens at ``ExtractionCandidate``; the ranking
    adapter must carry the normalized code through, never re-derive it."""
    from scrape_core.extraction.result import ExtractionCandidate
    from scrape_core.extraction.pipeline import candidate_from_extraction

    extraction = ExtractionCandidate(
        raw_price_text="129.00",
        currency="ريال",
        method=ExtractionMethod.CSS,
        confidence=0.85,
    )
    assert extraction.currency == "SAR"

    ranked_candidate = candidate_from_extraction(extraction)
    assert ranked_candidate is not None
    assert ranked_candidate.currency == "SAR"
    assert ranked_candidate.identity.currency == "SAR"
    assert ranked_candidate.price == Decimal("129.00")


def test_unparseable_price_text_is_a_rejection_not_an_exception() -> None:
    from scrape_core.extraction.result import ExtractionCandidate
    from scrape_core.extraction.pipeline import candidate_from_extraction

    extraction = ExtractionCandidate(
        raw_price_text="call for price",
        currency="SAR",
        method=ExtractionMethod.CSS,
        confidence=0.85,
    )
    assert candidate_from_extraction(extraction) is None


# ---------------------------------------------------------------------------
# 11. W3 gate follow-ups
# ---------------------------------------------------------------------------

# --- M2: ranking is a function of the candidate SET, not of arrival order ---


def _outcome_fingerprint(result: object) -> tuple[object, ...]:
    """Everything a caller can act on, flattened for equality comparison.

    Deliberately includes the *ordered* winning/top candidates, not just
    the outcome class: "same verdict, different winner" would still be an
    order-dependence bug.
    """
    if isinstance(result, Winner):
        return (
            "Winner",
            result.candidate.price,
            result.candidate.method,
            tuple((c.price, c.method) for c in result.corroborating),
            tuple(sorted(r.reason for r in result.rejections)),
        )
    if isinstance(result, Conflict):
        return (
            "Conflict",
            tuple((c.price, c.method) for c in result.top_candidates),
            tuple(sorted(r.reason for r in result.rejections)),
        )
    assert isinstance(result, NoValid), result
    return ("NoValid", tuple(sorted(r.reason for r in result.reasons)))


def test_identity_less_candidate_does_not_make_the_outcome_order_dependent() -> None:
    """The W3 gate reviewer's case: ``[SKU-A@129, no-identity@129, SKU-B@777]``.

    Under the old greedy grouping the identity-less reading was absorbed
    into whichever conflicting group was encountered FIRST, so order
    A,B,C produced ``Winner(129)`` and order C,B,A produced
    ``Conflict(129 vs 777)`` — the same three readings, two different
    prices published. All six permutations must now agree.

    They agree on ``Conflict``, which is the conservative half of the
    ambiguity: the page gave two equally-supported identities and the
    third reading corroborates neither, so nothing here entitles the
    system to pick 129 over 777 without a human.
    """
    identified_a = _candidate("129", ExtractionMethod.CSS, identity=_identity("SKU-A"))
    identity_less = _candidate(
        "129", ExtractionMethod.JSON_LD, identity=CandidateIdentity(currency="SAR")
    )
    identified_b = _candidate(
        "777", ExtractionMethod.EMBEDDED_JSON, identity=_identity("SKU-B")
    )

    fingerprints = {
        _outcome_fingerprint(rank(list(order), POLICY_V1))
        for order in itertools.permutations([identified_a, identity_less, identified_b])
    }

    assert len(fingerprints) == 1, fingerprints
    result = rank([identified_a, identity_less, identified_b], POLICY_V1)
    assert isinstance(result, Conflict), result
    assert result.needs_review is True
    # Every reading is still reported — the ambiguous one is not dropped.
    assert len(result.top_candidates) == 3


def test_four_candidate_ranking_is_independent_of_candidate_order() -> None:
    """Two agreeing SKU-A readings, one SKU-B, one identity-less.

    A majority identity exists here, so the identity-less reading joins
    it (rule 3's size fallback) and all 24 permutations produce the same
    ``Winner`` — the freshest reading of the consensus — rather than
    depending on whether SKU-B happened to be collected first.
    """
    agreed_dom = _candidate("129", ExtractionMethod.CSS, identity=_identity("SKU-A"))
    agreed_metadata = _candidate("129", ExtractionMethod.JSON_LD, identity=_identity("SKU-A"))
    identity_less = _candidate(
        "129", ExtractionMethod.EMBEDDED_JSON, identity=CandidateIdentity(currency="SAR")
    )
    other = _candidate(
        "777", ExtractionMethod.REGEX, confidence=0.75, identity=_identity("SKU-B")
    )

    fingerprints = {
        _outcome_fingerprint(rank(list(order), POLICY_V1))
        for order in itertools.permutations(
            [agreed_dom, agreed_metadata, identity_less, other]
        )
    }

    assert len(fingerprints) == 1, fingerprints
    result = rank([agreed_dom, agreed_metadata, identity_less, other], POLICY_V1)
    assert isinstance(result, Winner), result
    assert result.candidate.price == Decimal("129")
    assert result.candidate.method is ExtractionMethod.CSS
    assert RejectionReason.IDENTITY_DISAGREEMENT in {r.reason for r in result.rejections}


def test_real_noon_ranked_collection_is_order_independent() -> None:
    """The same invariance on the real capture, over the real chain."""
    from scrape_core.extraction.pipeline import collect_extraction_candidates

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    collected = collect_extraction_candidates(
        _noon_html(), profile, preferred_method=ExtractionMethod.REGEX
    ).candidates

    fingerprints = {
        _outcome_fingerprint(rank(list(order), POLICY_V1))
        for order in itertools.permutations(collected)
    }
    assert len(fingerprints) == 1, fingerprints


# --- M1: the ReDoS guard has a caller on the ranked path ---


def test_ranked_path_refuses_a_redos_shaped_profile_regex() -> None:
    """A hostile learned ``price_regex`` fails closed, and only for itself.

    The pattern is never run: the guard refuses it pre-flight and the
    regex strategy contributes no candidate. Collection continues — the
    page's JSON-LD reading survives — because a refused pattern must not
    cost the other strategies their readings.
    """
    from scrape_core.extraction.pipeline import collect_extraction_candidates

    hostile = SimpleNamespace(price_regex=r"(a+)+$", confidence_rules=None)
    started = time.perf_counter()
    collection = collect_extraction_candidates(
        _noon_html(), hostile, preferred_method=ExtractionMethod.REGEX
    )
    elapsed = time.perf_counter() - started

    assert elapsed < REDOS_TIME_BUDGET_SECONDS, f"took {elapsed:.3f}s"
    assert ExtractionMethod.REGEX not in {c.method for c in collection.candidates}
    assert ExtractionMethod.JSON_LD in {c.method for c in collection.candidates}
    refusals = [
        r for r in collection.rejections if r.reason is RejectionReason.REDOS_PATTERN_REJECTED
    ]
    assert refusals, collection.rejections
    assert refusals[0].method is ExtractionMethod.REGEX
    assert "price_regex" in refusals[0].detail


def test_ranked_path_benign_regex_is_identical_to_the_unguarded_call() -> None:
    """The guard is a budget, not a behavior change: same hit, no rejection."""
    from scrape_core.extraction.pipeline import collect_extraction_candidates
    from scrape_core.extraction.regex import extract_regex

    profile = SimpleNamespace(price_regex=r"price:(\d+)", confidence_rules=None)
    unguarded = extract_regex(_noon_html(), profile=profile)
    assert unguarded is not None

    collection = collect_extraction_candidates(
        _noon_html(), profile, preferred_method=ExtractionMethod.REGEX
    )
    regex_candidates = [c for c in collection.candidates if c.method is ExtractionMethod.REGEX]

    assert len(regex_candidates) == 1
    assert regex_candidates[0].price == Decimal(unguarded.raw_price_text)
    assert regex_candidates[0].selector_used == unguarded.selector_used
    assert collection.rejections == ()


# --- M3: an unparseable hit leaves a replayable record ---

# One node, one price-shaped selector, and text no money boundary accepts.
CALL_FOR_PRICE_HTML = '<html><body><span class="price">Call for price</span></body></html>'


def test_unparseable_hit_yields_an_unparseable_price_rejection(tmp_path: Path) -> None:
    """A "call for price" node used to vanish: ``candidates: 0, rejections: ()``.

    It now leaves exactly one retained ``UNPARSEABLE_PRICE`` whose
    evidence hash resolves back to the bytes that were read, so the
    decision can be re-argued from the page rather than from a log line.
    """
    from scrape_core.extraction.pipeline import collect_extraction_candidates

    profile = SimpleNamespace(price_selector=".price", confidence_rules=None)
    collection = collect_extraction_candidates(
        CALL_FOR_PRICE_HTML, profile, evidence_store_dir=tmp_path
    )

    assert collection.candidates == ()
    unparseable = [
        r for r in collection.rejections if r.reason is RejectionReason.UNPARSEABLE_PRICE
    ]
    assert len(unparseable) == 1, collection.rejections
    assert unparseable[0].method is ExtractionMethod.CSS
    assert unparseable[0].evidence_hash is not None
    assert b"Call for price" in replay(unparseable[0].evidence_hash, store_dir=tmp_path).data


def test_unparseable_hit_reaches_the_ranked_result_as_a_retained_reason(tmp_path: Path) -> None:
    from scrape_core.extraction.pipeline import extract_ranked

    profile = SimpleNamespace(price_selector=".price", confidence_rules=None)
    result = extract_ranked(CALL_FOR_PRICE_HTML, profile, evidence_store_dir=tmp_path)

    assert isinstance(result, NoValid), result
    assert RejectionReason.UNPARSEABLE_PRICE in {r.reason for r in result.reasons}


@pytest.mark.parametrize(
    "method",
    [
        ExtractionMethod.JSON_LD,
        ExtractionMethod.EMBEDDED_JSON,
        ExtractionMethod.CSS,
        ExtractionMethod.REGEX,
        ExtractionMethod.SINGLE_NUMBER,
        ExtractionMethod.PLATFORM_JSON,
        ExtractionMethod.XPATH,
        ExtractionMethod.PLAYWRIGHT,
    ],
)
def test_every_extraction_method_has_a_source_tier(method: ExtractionMethod) -> None:
    assert isinstance(SourceTier.for_method(method), SourceTier)
