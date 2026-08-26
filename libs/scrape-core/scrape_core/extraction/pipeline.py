"""Ordered extraction orchestrator (contracts/extraction.md "Ordered chain").

``extract(html, profile)`` tries each strategy in :data:`_STRATEGIES`,
in order, first hit wins: JSON-LD -> embedded JSON -> CSS -> regex
(which itself falls back to the single-number heuristic internally,
contracts/extraction.md).
``_STRATEGIES`` is the single extension point so growing the chain never
touches the loop itself.

SPEC-12 US2 (`contracts/consumption.md` step 3, D6): an optional
``preferred_method`` keyword reorders the chain to try the learned
domain's winning strategy first, falling back to the full order only if
it misses -- never a *narrower* chain, so an unconfirmed/still-learning
page never loses coverage. ``REGEX``/``SINGLE_NUMBER`` share one
underlying strategy function (:func:`~scrape_core.extraction.regex.extract_regex`
picks between them internally), so either preferred value simply
prioritizes that one function.

W3.2 (READY-012) adds the *ranked* path alongside — never in place of —
that chain. :func:`collect_extraction_candidates` runs **every**
strategy and :func:`extract_ranked` reduces the result through
:func:`app_shared.strategy.candidate_ranking.rank`, so a page on which
two strategies disagree materially yields a ``Conflict`` for human
review instead of whichever strategy happened to run first. It is
**opt-in**: :func:`extract` is byte-for-byte its historical self unless
a caller passes ``ranking_policy=``, because flipping the ordering of
live price extraction is a pricing-behavior change, not a refactor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace as dataclass_replace
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from app_shared.enums import ExtractionMethod
from app_shared.money import parse_money
from app_shared.observations.evidence_store import compute_hash, store_evidence
from app_shared.strategy.candidate_ranking import (
    POLICY_V1,
    Candidate,
    CandidateCollection,
    CandidateIdentity,
    RankedResult,
    RankingPolicy,
    Rejection,
    RejectionReason,
    UrlCheck,
    Winner,
    collect_candidates,
    rank,
    search_bounded,
)

from scrape_core.extraction.css import extract_css
from scrape_core.extraction.embedded_json import extract_embedded_json
from scrape_core.extraction.jsonld import extract_jsonld
from scrape_core.extraction.regex import extract_regex
from scrape_core.extraction.result import ExtractionCandidate
from scrape_core.money_text import normalize_price_text

__all__ = [
    "candidate_from_extraction",
    "collect_extraction_candidates",
    "extract",
    "extract_ranked",
]

_Strategy = Callable[..., ExtractionCandidate | None]

# Ordered JSON-LD -> EMBEDDED_JSON -> CSS -> regex chain
# (contracts/extraction.md; EMBEDDED_JSON slotted in by Task 3.1 per
# handover 2026-08-15 §7). Regex itself falls back to the SINGLE_NUMBER
# heuristic internally when no configured price_regex matches
# (scrape_core.extraction.regex).
#
# EMBEDDED_JSON's position only ever matters for a profile that sets
# `price_json_path`: without one, `extract_embedded_json` returns `None`
# immediately and the chain is byte-for-byte the historical
# JSON-LD -> CSS -> regex. It sits *after* JSON-LD because a page with
# honest schema.org markup should keep being read the cheap standard way,
# and *before* CSS because a configured pointer into the page's own state
# blob is strictly better evidence than a positional selector.
_STRATEGIES: tuple[_Strategy, ...] = (
    extract_jsonld,
    extract_embedded_json,
    extract_css,
    extract_regex,
)

#: `ExtractionMethod` -> the strategy function that can produce it (SPEC-12
#: US2). `REGEX` and `SINGLE_NUMBER` both map to `extract_regex`, which
#: decides between them internally -- there is no narrower entry point.
_METHOD_TO_STRATEGY: dict[ExtractionMethod, _Strategy] = {
    ExtractionMethod.JSON_LD: extract_jsonld,
    ExtractionMethod.EMBEDDED_JSON: extract_embedded_json,
    ExtractionMethod.CSS: extract_css,
    ExtractionMethod.REGEX: extract_regex,
    ExtractionMethod.SINGLE_NUMBER: extract_regex,
}


def _ordered_strategies(preferred_method: ExtractionMethod | None) -> tuple[_Strategy, ...]:
    """The chain to try, `preferred_method`'s strategy first if recognized.

    An unrecognized/forward-compat `preferred_method` (e.g. a later-spec
    method this pipeline doesn't implement yet, `PLATFORM_JSON`/`XPATH`/
    `PLAYWRIGHT`) is a no-op -- the unmodified default order runs, never
    an error. `EMBEDDED_JSON` stopped being one of those in Task 3.1 and
    is now a real, learnable entry point.
    """
    if preferred_method is None:
        return _STRATEGIES
    preferred_fn = _METHOD_TO_STRATEGY.get(preferred_method)
    if preferred_fn is None:
        return _STRATEGIES
    rest = tuple(strategy for strategy in _STRATEGIES if strategy is not preferred_fn)
    return (preferred_fn, *rest)


def extract(
    html: str,
    profile: Any = None,
    *,
    preferred_method: ExtractionMethod | None = None,
    ranking_policy: RankingPolicy | None = None,
) -> ExtractionCandidate | None:
    """Return the first-hit :class:`ExtractionCandidate`, or ``None``.

    ``None`` means no strategy in the chain found a price — the caller
    (the spider's ``parse``) records a ``success=false`` observation
    with ``error_code=PRICE_NOT_FOUND`` (contracts/errors.md); it is
    never raised as an exception.

    ``preferred_method`` (SPEC-12 US2, learned-domain start, D6) tries
    that strategy first; on a miss, falls back to the full default order
    (never a narrower chain) so a learned domain never loses a price it
    would otherwise have found via a different strategy.

    ``ranking_policy`` is the W3.2 flag. Left ``None`` (the default, and
    what every current caller passes) this function is the historical
    first-hit chain, unchanged. Given a policy it routes through
    :func:`extract_ranked` and returns the winning strategy's original
    ``ExtractionCandidate`` — or ``None`` for a ``Conflict``/``NoValid``,
    because a page whose strategies disagree materially has *not*
    produced a price a caller may quietly persist. Callers that need to
    act on the difference (flag the match ``NEEDS_REVIEW`` rather than
    record ``PRICE_NOT_FOUND``) should call :func:`extract_ranked`
    directly and read the outcome type.
    """
    if ranking_policy is not None:
        result = extract_ranked(
            html, profile, preferred_method=preferred_method, policy=ranking_policy
        )
        if isinstance(result, Winner):
            source = result.candidate.source
            return source if isinstance(source, ExtractionCandidate) else None
        return None

    for strategy in _ordered_strategies(preferred_method):
        candidate = strategy(html, profile=profile)
        if candidate is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# W3.2 ranked path
# ---------------------------------------------------------------------------

def candidate_from_extraction(
    extraction: ExtractionCandidate,
    *,
    identity: CandidateIdentity | None = None,
) -> Candidate | None:
    """Adapt a strategy's raw hit into a rankable :class:`Candidate`.

    Ranking compares prices, so this is where the §19 money boundary is
    crossed: ``raw_price_text`` goes through the same
    :func:`~scrape_core.money_text.normalize_price_text` +
    :func:`~app_shared.money.parse_money` pair that
    ``scrape_core.validation`` uses, never a second parser. Text no money
    boundary accepts (``"call for price"``) yields ``None`` — an
    unparseable hit is not a candidate.

    Currency is passed straight through. ``ExtractionCandidate`` has
    already normalized it (B4: ``ريال`` -> ``SAR`` in its
    ``__post_init__``, the system's single normalization point), and
    re-deriving it here would be the second implementation that ruling
    exists to prevent.
    """
    normalized = normalize_price_text(extraction.raw_price_text)
    if normalized is None:
        return None
    try:
        price = parse_money(normalized)
    except (TypeError, ValueError, InvalidOperation):
        return None

    evidence_text = extraction.matched_text or extraction.raw_price_text
    return Candidate(
        price=price,
        currency=extraction.currency,
        method=extraction.method,
        confidence=extraction.confidence,
        identity=identity
        if identity is not None
        else CandidateIdentity(currency=extraction.currency),
        evidence=evidence_text.encode("utf-8", errors="replace"),
        selector_used=extraction.selector_used,
        matched_text=extraction.matched_text,
        source=extraction,
    )


#: Every regex a resolved ``ScrapeProfile`` can hand to
#: :func:`~scrape_core.extraction.regex.extract_regex`. All of them are
#: learned/DB-sourced text, so all of them are pre-flighted on the ranked
#: path — not just the one whose match becomes the price.
_PROFILE_REGEX_ATTRIBUTES = (
    "price_regex",
    "old_price_regex",
    "currency_regex",
    "stock_regex",
)


def _evidence_hash_for(data: bytes, store_dir: Path | None) -> str:
    """The same content address collection gives an *accepted* candidate.

    ``store_dir`` is what makes the hash replayable: with one, the bytes
    are written into the content-addressed store, mirroring
    ``candidate_ranking._with_evidence_hash``; without one the hash is
    still computed so the rejection stays referenceable.
    """
    if store_dir is not None:
        return store_evidence(data, store_dir=store_dir)
    return compute_hash(data)


def _unparseable_rejection(
    extraction: ExtractionCandidate, store_dir: Path | None
) -> Rejection:
    """The retained record for a hit :func:`candidate_from_extraction` drops.

    W3 gate follow-up M3: a "call for price" node used to leave the
    ranked path with ``candidates: 0, rejections: ()`` — a page that was
    read, produced something, and yielded no trace of why nothing came of
    it. The evidence bytes are the same ones a *successful* candidate
    would carry (``matched_text`` if the strategy captured surrounding
    context, else the raw price text), so the rejection replays.
    """
    evidence = (extraction.matched_text or extraction.raw_price_text or "").encode(
        "utf-8", errors="replace"
    )
    return Rejection(
        reason=RejectionReason.UNPARSEABLE_PRICE,
        detail=(
            f"no money boundary accepts {extraction.raw_price_text!r} "
            f"from the {extraction.method} strategy"
        ),
        method=extraction.method,
        evidence_hash=_evidence_hash_for(evidence, store_dir),
    )


def _bounded_extract_regex(
    html: str, profile: Any, policy: RankingPolicy
) -> tuple[ExtractionCandidate | None, Rejection | None]:
    """``extract_regex`` with the W3.2 ReDoS budget actually applied.

    W3 gate follow-up M1. A profile's ``price_regex`` (and the three
    sibling rules) is learned, DB-stored, competitor-page-influenced
    text, and CPython's ``re`` cannot be interrupted mid-match — so on
    the ranked path every one of them is pre-flighted through
    :func:`~app_shared.strategy.candidate_ranking.search_bounded`, which
    refuses ReDoS shapes and caps how much text a pattern may see, and
    the strategy itself is handed only the capped input.

    A refused pattern **fails closed**: no extraction and a retained
    ``REDOS_PATTERN_REJECTED``, never an exception that would cost the
    page its other four readings.

    Only the ranked path routes through here. ``extract()`` with
    ``ranking_policy=None`` — every current production caller — still
    calls ``extract_regex`` directly and is byte-for-byte unchanged
    (pinned by ``test_default_extract_still_returns_the_first_hit``).
    """
    bounded_html = html[: policy.max_regex_input_chars]
    for attribute in _PROFILE_REGEX_ATTRIBUTES:
        pattern = getattr(profile, attribute, None) if profile is not None else None
        if not pattern:
            continue
        _, refusal = search_bounded(str(pattern), bounded_html, policy=policy)
        if refusal is not None:
            return None, dataclass_replace(
                refusal,
                method=ExtractionMethod.REGEX,
                detail=f"{attribute}: {refusal.detail}",
            )
    return extract_regex(bounded_html, profile=profile), None


def collect_extraction_candidates(
    html: str,
    profile: Any = None,
    *,
    preferred_method: ExtractionMethod | None = None,
    policy: RankingPolicy = POLICY_V1,
    envelope: Any = None,
    url_check: UrlCheck | None = None,
    evidence_store_dir: Path | None = None,
) -> CandidateCollection:
    """Run **every** strategy in the chain and collect all of their hits.

    This is the W3.2 replacement for the ``return`` inside
    :func:`extract`'s loop: the ordering from ``preferred_method`` is
    preserved (it still decides collection order) but no strategy is
    skipped because an earlier one happened to succeed.

    Two things happen here that the default chain does not do, both W3
    gate follow-ups: the regex strategy runs through the bounded engine
    (:func:`_bounded_extract_regex`, M1), and a hit no money boundary
    accepts leaves an ``UNPARSEABLE_PRICE`` rejection rather than
    vanishing (:func:`_unparseable_rejection`, M3).

    ``envelope`` — a
    :class:`~app_shared.strategy.candidate_ranking.FetchEnvelope` — opts
    the page into the collection-boundary recheck: response size,
    decompression ratio, redirect count, content type, and the SSRF
    guard re-applied to **every** hop. Production wiring passes
    ``url_check=partial(validate_resolved_target, resolver=...)`` from
    ``scrape_core.safety.fetch`` so DNS is re-resolved too; omitting it
    falls back to ``app_shared.url_safety.validate_competitor_url``,
    which is the same save-time validator ``SsrfGuardMiddleware`` runs
    per hop. Neither re-implements the W5.5-D fetch-layer caps — they
    re-assert them over whatever body actually reached extraction.
    """
    # Rejections this layer discovers, which `collect_candidates` cannot
    # see: it is handed a strategy that already returned `None` (M1's
    # refused pattern, M3's unparseable hit). Merged into the collection
    # below so the caller gets one record of everything that happened.
    adapter_rejections: list[Rejection] = []

    def _wrap(strategy: _Strategy) -> Any:
        def run(_page: Any) -> Candidate | None:
            if strategy is extract_regex:
                extraction, refusal = _bounded_extract_regex(html, profile, policy)
                if refusal is not None:
                    adapter_rejections.append(refusal)
                    return None
            else:
                extraction = strategy(html, profile=profile)
            if extraction is None:
                return None
            candidate = candidate_from_extraction(extraction)
            if candidate is None:
                adapter_rejections.append(
                    _unparseable_rejection(extraction, evidence_store_dir)
                )
            return candidate

        return run

    wrapped = [_wrap(strategy) for strategy in _ordered_strategies(preferred_method)]
    collection = collect_candidates(
        envelope if envelope is not None else html,
        wrapped,
        policy=policy,
        url_check=url_check,
        evidence_store_dir=evidence_store_dir,
    )
    if adapter_rejections:
        collection = dataclass_replace(
            collection, rejections=collection.rejections + tuple(adapter_rejections)
        )
    return collection


def extract_ranked(
    html: str,
    profile: Any = None,
    *,
    preferred_method: ExtractionMethod | None = None,
    policy: RankingPolicy = POLICY_V1,
    envelope: Any = None,
    url_check: UrlCheck | None = None,
    evidence_store_dir: Path | None = None,
) -> RankedResult:
    """Collect every strategy's reading, then rank it under ``policy``.

    Returns ``Winner``/``Conflict``/``NoValid``. A ``Conflict`` carries
    ``needs_review=True``, which is what the caller hands to the B6
    ``NEEDS_REVIEW`` sidecar
    (``AdapterResult.metadata["needs_review"]`` ->
    ``ScrapeResult.needs_review`` ->
    ``pipelines._write_needs_review_classifications``) so a material
    extraction disagreement reaches a human on exactly the path a B4
    identity ambiguity already does.
    """
    collection = collect_extraction_candidates(
        html,
        profile,
        preferred_method=preferred_method,
        policy=policy,
        envelope=envelope,
        url_check=url_check,
        evidence_store_dir=evidence_store_dir,
    )
    return rank(collection.candidates, policy, rejections=collection.rejections)
