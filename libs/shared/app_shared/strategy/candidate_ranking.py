"""Candidate collection + ranking — the replacement for first-candidate-wins (W3.2, READY-012).

The scraping library's ordered extraction chain has always
returned the **first** strategy that produced anything
(``contracts/extraction.md`` "Ordered chain"). That is fine when only one
strategy can see a price, and silently wrong when two can: a
server-cached JSON-LD block and the rendered DOM disagree, the cheap
strategy runs first, and the honest one never runs at all. On the real
2026-08-16 noon capture in ``tests/fixtures/html/noon_product_real.html``
a learned ``price:(\\d+)`` regex reads ``price:499`` — the strikethrough
list price — out of the page's own state blob, and the correct ``129``
is never computed.

This module replaces that with two explicit steps:

``collect_candidates(page, strategies)``
    Run **every** strategy, bounded, turning each into a
    :class:`Candidate` that carries its own evidence bytes (hashed
    content-addressed into
    :mod:`app_shared.observations.evidence_store`, so a rejection is
    replayable months later) and whatever identity it could prove.
    Strategy crashes, limit breaches and unsafe fetch provenance become
    retained :class:`Rejection` records rather than exceptions or
    silence.

``rank(candidates, policy)``
    Reduce them to exactly one of :class:`Winner` / :class:`Conflict` /
    :class:`NoValid`. A **material** disagreement — price divergence
    beyond the policy's tolerance between candidates whose identities
    agree — is a :class:`Conflict` that goes to human review. It is
    never resolved by picking the higher-confidence reading, because
    "two strategies read this page and got different prices" is exactly
    the situation in which confidence is not evidence.

Layering: this package is pure learning logic (see
``tests/unit/test_strategy_import_boundary.py``) — it may not reach into
the scraping library, Scrapy, or DNS. The SSRF recheck therefore reuses
:func:`app_shared.url_safety.validate_competitor_url` by default and
takes the resolving fetch-time variant (``safety.fetch``'s
``validate_resolved_target``) through the injected ``url_check`` seam,
exactly the way the browser guard already does. Nothing here
re-implements a cap that W5.5-D already installed at
the fetch layer: the ``DOWNLOAD_MAXSIZE``/``DOWNLOAD_WARNSIZE`` bounds in
the two Scrapy ``settings.py`` modules are the enforcement point, and
the numbers below are the *recheck* applied to whatever body actually
reached extraction.

Currency is **not** normalized here. Per the B4 ruling that is
``ExtractionCandidate.__post_init__``'s single job (``ريال`` -> ``SAR``);
a :class:`Candidate` is built from an already-normalized value and this
module only ever compares codes.

**Order independence (W3 gate follow-up M2).** Ranking is a function of
the candidate *set*, never of the order the strategies happened to run
in. The original greedy grouping was not: with
``[SKU-A@129, no-identity@129, SKU-B@777]`` the identity-less reading
was absorbed into whichever conflicting group came first, so one
permutation returned ``Winner(129)`` and its reverse returned
``Conflict(129 vs 777)``. :func:`_identity_groups` now:

1. walks candidates in a canonical, content-derived order
   (:func:`_canonical_sort_key`) instead of collection order;
2. builds groups from the *contradicted* candidates only — those that
   provably conflict with at least one other reading — so a candidate
   that can join any group never decides which groups exist;
3. attaches each remaining (uncontradicted) candidate afterwards, first
   to the single group whose members corroborate it best, and failing
   that to the single largest group.

The tie-rule at step 3 is deliberately conservative: when neither
corroboration nor group size names **one** group, the candidate is *not*
guessed into a group — it becomes its own group, which can only add a
contender to an already-tied largest-group vote. A tie there is a
:class:`Conflict` (rule 2 of :func:`rank`), so an ambiguous attachment
can never manufacture a ``Winner`` that a different permutation would
not have produced.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from re import _parser as _re_parser
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from app_shared.enums import CompetitorIdentifierType, ExtractionMethod, StrEnum
from app_shared.observations.evidence_store import compute_hash, store_evidence
from app_shared.profiles.confidence import DEFAULT_MIN_ACCEPTED_CONFIDENCE
from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

__all__ = [
    "POLICY_V1",
    "Candidate",
    "CandidateCollection",
    "CandidateIdentity",
    "Conflict",
    "FetchEnvelope",
    "NoValid",
    "RankedResult",
    "RankingPolicy",
    "Rejection",
    "RejectionReason",
    "SourceTier",
    "UrlCheck",
    "Winner",
    "collect_candidates",
    "enforce_fetch_limits",
    "rank",
    "search_bounded",
]


# ---------------------------------------------------------------------------
# Rejection vocabulary
# ---------------------------------------------------------------------------


class RejectionReason(StrEnum):
    """Why a candidate — or a whole collection — was not usable.

    Every value is retained alongside the offending candidate's evidence
    hash so a decision can be re-argued from the bytes rather than from
    a log line.
    """

    #: Below the policy's minimum accepted confidence (§17 gate).
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    #: The strategy produced text no money boundary could parse.
    UNPARSEABLE_PRICE = "UNPARSEABLE_PRICE"
    #: The strategy hit produced no price at all.
    NO_PRICE = "NO_PRICE"
    #: This candidate names a different product/variant/currency than the
    #: consensus reading of the page.
    IDENTITY_DISAGREEMENT = "IDENTITY_DISAGREEMENT"
    #: The strategy raised; collection continued with the rest.
    STRATEGY_ERROR = "STRATEGY_ERROR"
    #: More strategies produced hits than the policy allows to be ranked.
    CANDIDATE_LIMIT_EXCEEDED = "CANDIDATE_LIMIT_EXCEEDED"
    #: A regex was refused by the bounded engine (ReDoS shape, oversized
    #: pattern, or simply uncompilable).
    REDOS_PATTERN_REJECTED = "REDOS_PATTERN_REJECTED"
    #: Body larger than the policy's post-fetch recheck bound.
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    #: Decoded/transferred ratio beyond the policy's bound (zip bomb).
    DECOMPRESSION_RATIO_EXCEEDED = "DECOMPRESSION_RATIO_EXCEEDED"
    #: More redirect hops than the policy allows.
    TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
    #: Content type outside the policy's allow-list.
    DISALLOWED_CONTENT_TYPE = "DISALLOWED_CONTENT_TYPE"
    #: A hop (or its resolved IP) failed the reused SSRF guard.
    UNSAFE_URL = "UNSAFE_URL"


@dataclass(frozen=True)
class Rejection:
    """One retained "why not" — replayable via ``evidence_hash``."""

    reason: RejectionReason
    detail: str
    method: ExtractionMethod | None = None
    #: sha256 content address of the rejected candidate's evidence, or of
    #: the page body when the whole collection was refused. ``None`` only
    #: when there were no bytes to point at (e.g. a strategy that raised
    #: before producing anything).
    evidence_hash: str | None = None


# ---------------------------------------------------------------------------
# Source tiers
# ---------------------------------------------------------------------------


class SourceTier(enum.IntEnum):
    """How close a strategy's reading is to what the shopper actually pays.

    This is a *freshness* ordering, deliberately distinct from the §17
    per-method confidence in ``app_shared.profiles.confidence``, which
    measures how precisely a strategy parses. JSON-LD parses precisely
    (0.95) and is routinely stale, because it is server-rendered markup
    that a CDN happily caches past a price change; the rendered DOM is
    what the customer sees. When two readings agree on identity and
    differ only slightly, freshness is the tiebreak that matters.
    """

    #: Blind text matching. Lowest: it has no idea what it matched.
    HEURISTIC = 10
    #: Server-rendered structured metadata (schema.org JSON-LD).
    STRUCTURED_METADATA = 20
    #: The page's own hydration/state payload, or the platform's product
    #: JSON — the app's live view of its own offer.
    PAGE_STATE = 30
    #: The rendered document the shopper reads.
    RENDERED_DOM = 40

    @classmethod
    def for_method(cls, method: ExtractionMethod) -> "SourceTier":
        return _TIER_BY_METHOD.get(method, cls.HEURISTIC)


_TIER_BY_METHOD: Mapping[ExtractionMethod, SourceTier] = {
    ExtractionMethod.CSS: SourceTier.RENDERED_DOM,
    ExtractionMethod.XPATH: SourceTier.RENDERED_DOM,
    ExtractionMethod.PLAYWRIGHT: SourceTier.RENDERED_DOM,
    ExtractionMethod.EMBEDDED_JSON: SourceTier.PAGE_STATE,
    ExtractionMethod.PLATFORM_JSON: SourceTier.PAGE_STATE,
    ExtractionMethod.JSON_LD: SourceTier.STRUCTURED_METADATA,
    ExtractionMethod.REGEX: SourceTier.HEURISTIC,
    ExtractionMethod.SINGLE_NUMBER: SourceTier.HEURISTIC,
}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateIdentity:
    """What a candidate could *prove* about which offer it read.

    ``identifiers`` are ``(type, value)`` pairs using the same B4
    vocabulary as ``match_competitor_identifiers``
    (:class:`~app_shared.enums.CompetitorIdentifierType`). The B4 ruling
    is enforced here rather than assumed: an ``UNKNOWN``-typed value is
    an honest "we never established what this string addresses", so it
    can neither corroborate nor contradict another reading. Treating it
    as a variant id is the exact S-Tech bug B4 closed.
    """

    identifiers: tuple[tuple[CompetitorIdentifierType, str], ...] = ()
    currency: str | None = None

    def _typed(self) -> dict[CompetitorIdentifierType, str]:
        """Comparable identifiers only — ``UNKNOWN`` rows are dropped."""
        return {
            identifier_type: str(value).strip().casefold()
            for identifier_type, value in self.identifiers
            if identifier_type is not CompetitorIdentifierType.UNKNOWN
            and str(value).strip()
        }

    def conflicts_with(self, other: "CandidateIdentity") -> bool:
        """True iff the two readings provably describe different offers."""
        if (
            self.currency is not None
            and other.currency is not None
            and self.currency.strip().upper() != other.currency.strip().upper()
        ):
            return True
        mine, theirs = self._typed(), other._typed()
        return any(
            mine[identifier_type] != theirs[identifier_type]
            for identifier_type in mine.keys() & theirs.keys()
        )

    def corroborates(self, other: "CandidateIdentity") -> bool:
        """True iff the two readings share at least one *typed* identifier value."""
        mine, theirs = self._typed(), other._typed()
        return any(
            mine[identifier_type] == theirs[identifier_type]
            for identifier_type in mine.keys() & theirs.keys()
        )


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One strategy's ranked-path reading of a page.

    Unlike the scraping library's ``ExtractionCandidate`` (raw
    text, pre-validation), a ``Candidate`` has already crossed the §19
    money boundary — ``price`` is an exact :class:`~decimal.Decimal` —
    because ranking compares prices and comparing raw strings would be a
    second, divergent parser.
    """

    price: Decimal
    currency: str | None
    method: ExtractionMethod
    confidence: float
    identity: CandidateIdentity = field(default_factory=CandidateIdentity)
    #: The exact bytes this reading came from (the JSON-LD offer blob,
    #: the matched text node, the selector's subtree). Hashed at
    #: collection time so a later argument about this candidate can be
    #: replayed rather than re-scraped.
    evidence: bytes | None = None
    evidence_hash: str | None = None
    selector_used: str | None = None
    matched_text: str | None = None
    #: The strategy-native object this reading was adapted from, opaque
    #: to ranking. It exists so a caller can hand back the exact object
    #: its own chain produced (``ExtractionCandidate``, with its
    #: ``stock``/``matched_text`` intact) instead of a lossy
    #: reconstruction. Ranking never reads it.
    source: Any = None

    @property
    def tier(self) -> SourceTier:
        return SourceTier.for_method(self.method)

    def _rejection(self, reason: RejectionReason, detail: str) -> Rejection:
        return Rejection(
            reason=reason,
            detail=detail,
            method=self.method,
            evidence_hash=self.evidence_hash,
        )


# ---------------------------------------------------------------------------
# Fetch provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchEnvelope:
    """How the bytes handed to extraction got here.

    Collection re-checks this rather than trusting it: the fetch-layer
    caps (W5.5-D's ``DOWNLOAD_MAXSIZE``/``DOWNLOAD_WARNSIZE`` and
    ``SsrfGuardMiddleware``) run in the spider process, and the ranking
    path can also be fed a body from a cache, a replay, or a browser
    context that never passed through them.
    """

    url: str | None = None
    final_url: str | None = None
    content: str | bytes | None = None
    content_type: str | None = None
    #: Bytes actually read off the wire (compressed, if compressed).
    transferred_bytes: int | None = None
    #: Bytes after content-encoding was undone — the number a
    #: decompression bomb inflates.
    decoded_bytes: int | None = None
    #: Every URL in the hop chain, request order, final URL last.
    redirect_chain: tuple[str, ...] = ()

    def body_bytes(self) -> bytes | None:
        if self.content is None:
            return None
        if isinstance(self.content, bytes):
            return self.content
        return self.content.encode("utf-8", errors="replace")


#: ``url -> None``; raises :class:`~app_shared.url_safety.UnsafeUrlError`
#: on an unsafe target. The default is the pure save-time validator;
#: production wiring injects the scraping library's fetch-time
#: ``validate_resolved_target`` bound to a real resolver so DNS is
#: checked too.
UrlCheck = Callable[[str], None]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankingPolicy:
    """A named, versioned ruleset. Frozen so a ``policy_version`` on a
    stored decision means exactly one thing forever."""

    version: str
    #: §17 gate — below this a reading is not evidence.
    min_confidence: float = DEFAULT_MIN_ACCEPTED_CONFIDENCE
    #: Relative gap two agreeing-identity readings may show before the
    #: disagreement is material.
    price_tolerance_ratio: Decimal = Decimal("0.05")
    #: Absolute floor, so a 2-riyal item is not thrown to review over
    #: rounding.
    price_tolerance_absolute: Decimal = Decimal("0.05")
    #: Hard bound on how many readings are ranked at all.
    max_candidates: int = 16
    #: Post-fetch recheck of the body size. Mirrors
    #: ``Settings.SCRAPE_DOWNLOAD_MAXSIZE_BYTES`` (8 MiB), spelled as a
    #: constant so no ``.env`` is read on an extraction path.
    max_response_bytes: int = 8 * 1024 * 1024
    #: decoded/transferred ratio ceiling (gzip on HTML runs ~4-8x).
    max_decompression_ratio: int = 100
    #: Hops beyond the original request.
    max_redirects: int = 10
    allowed_content_types: frozenset[str] = frozenset(
        {
            "text/html",
            "application/xhtml+xml",
            "application/json",
            "application/ld+json",
            "text/plain",
        }
    )
    max_regex_pattern_chars: int = 512
    max_regex_input_chars: int = 64 * 1024


#: The ruleset W3.2 ships. Freshness-first within an agreeing identity,
#: material divergence to human review, §17 confidence gate, and the
#: bounds above as the post-fetch recheck.
POLICY_V1 = RankingPolicy(version="v1")


# ---------------------------------------------------------------------------
# Bounded regex (ReDoS guard)
# ---------------------------------------------------------------------------

# CPython's `re` cannot be interrupted mid-match: a catastrophic pattern
# is not stoppable by a timeout, a signal, or a thread, because the
# scanning loop never checks for them. The only real budget is therefore
# a *pre-flight* one — refuse the pattern shapes that backtrack
# exponentially, and cap how much text any pattern is allowed to see.
#
# `re._parser` (imported above) is the 3.11+ home of what used to be
# `sre_parse`. Using it
# is deliberate: it is the only way to inspect a pattern's structure
# without writing a second regex parser, and the two opcode names read
# below (`MAX_REPEAT`/`MIN_REPEAT`/`POSSESSIVE_REPEAT`, `BRANCH`) have
# been stable since 2.x. A future rename fails loudly in
# `_pattern_is_redos_shaped`, never silently.
_MAXREPEAT = _re_parser.MAXREPEAT

_REPEAT_OPCODES = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})

#: A bounded repeat this large backtracks like an unbounded one.
_REPEAT_CAP = 1000


def _is_unbounded(max_repeat: Any) -> bool:
    if max_repeat is _MAXREPEAT:
        return True
    return isinstance(max_repeat, int) and max_repeat > _REPEAT_CAP


def _first_literals(subpattern: Any) -> frozenset[int] | None:
    """Characters a branch can start with, or ``None`` when unknowable.

    ``None`` is the conservative answer: an unknowable first-set is
    treated as overlapping everything, so ``(x|.)*`` is refused.
    """
    for opcode, arg in subpattern:
        name = str(opcode)
        if name == "LITERAL":
            return frozenset({arg})
        if name == "SUBPATTERN":
            return _first_literals(arg[-1])
        if name in _REPEAT_OPCODES:
            return _first_literals(arg[2])
        return None
    return frozenset()


def _pattern_is_redos_shaped(pattern: str) -> str | None:
    """A refusal reason, or ``None`` when the pattern is safe to run.

    Two shapes are refused, both of which turn linear input into
    exponential work:

    * an unbounded repeat nested inside another unbounded repeat
      (``(a+)+``, ``(a*)*``, ``(\\d+)*``);
    * an alternation inside an unbounded repeat whose branches can start
      with the same character (``(a|a)*``, ``(a|ab)*``), so the engine
      has two equally valid ways to consume every position.
    """
    try:
        parsed = _re_parser.parse(pattern)
    except re.error as exc:  # pragma: no cover - message varies by pattern
        return f"uncompilable pattern: {exc}"
    except RecursionError:  # pragma: no cover - defensive
        return "pattern nests too deeply to analyze"

    def walk(subpattern: Any, *, inside_unbounded: bool) -> str | None:
        for opcode, arg in subpattern:
            name = str(opcode)
            if name in _REPEAT_OPCODES:
                _, max_repeat, body = arg
                unbounded = _is_unbounded(max_repeat)
                if unbounded and inside_unbounded:
                    return "nested unbounded repeat"
                found = walk(body, inside_unbounded=inside_unbounded or unbounded)
                if found:
                    return found
            elif name == "SUBPATTERN":
                found = walk(arg[-1], inside_unbounded=inside_unbounded)
                if found:
                    return found
            elif name == "BRANCH":
                _, branches = arg
                if inside_unbounded and _branches_overlap(branches):
                    return "ambiguous alternation inside an unbounded repeat"
                for branch in branches:
                    found = walk(branch, inside_unbounded=inside_unbounded)
                    if found:
                        return found
            elif name in {"ATOMIC_GROUP", "GROUPREF_EXISTS"}:
                found = walk(arg if name == "ATOMIC_GROUP" else arg[1], inside_unbounded=inside_unbounded)
                if found:
                    return found
            elif name in {"ASSERT", "ASSERT_NOT"}:
                found = walk(arg[1], inside_unbounded=inside_unbounded)
                if found:
                    return found
        return None

    return walk(parsed, inside_unbounded=False)


def _branches_overlap(branches: Sequence[Any]) -> bool:
    """True when two alternatives can consume the same next character.

    An **empty** first-set means that branch matches the empty string, so
    the repeat can iterate without consuming — unbounded ambiguity, the
    worst case of all. That is not a corner case: ``re`` factors the
    common prefix out of ``(a|a)*`` and leaves literally
    ``BRANCH(None, [[], []])``, which is how the textbook ReDoS pattern
    actually reaches this function.
    """
    if len(branches) < 2:
        return False
    seen: list[frozenset[int]] = []
    for branch in branches:
        first = _first_literals(branch)
        if first is None or not first:
            return True
        if any(first & other for other in seen):
            return True
        seen.append(first)
    return False


def search_bounded(
    pattern: str,
    text: str,
    *,
    policy: RankingPolicy = POLICY_V1,
) -> tuple[re.Match[str] | None, Rejection | None]:
    """``re.search`` with a real budget: refuse ReDoS shapes, cap the input.

    Returns ``(match, None)`` on a completed scan (``match`` may be
    ``None`` — no hit is a normal outcome) or ``(None, rejection)`` when
    the pattern itself was refused. Never raises, and never runs a
    pattern whose worst case is not linear in the capped input.
    """
    if len(pattern) > policy.max_regex_pattern_chars:
        return None, Rejection(
            RejectionReason.REDOS_PATTERN_REJECTED,
            f"pattern is {len(pattern)} chars, over the {policy.max_regex_pattern_chars} budget",
        )

    unsafe = _pattern_is_redos_shaped(pattern)
    if unsafe is not None:
        return None, Rejection(
            RejectionReason.REDOS_PATTERN_REJECTED,
            f"refused pattern {pattern!r}: {unsafe}",
        )

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return None, Rejection(
            RejectionReason.REDOS_PATTERN_REJECTED,
            f"uncompilable pattern {pattern!r}: {exc}",
        )

    # Truncation, not sampling: the head of the document is where a price
    # lives, and an unbounded tail is the other half of the ReDoS budget.
    return compiled.search(text[: policy.max_regex_input_chars]), None


# ---------------------------------------------------------------------------
# Fetch-limit + SSRF recheck
# ---------------------------------------------------------------------------


def _content_type_allowed(content_type: str, policy: RankingPolicy) -> bool:
    essence = content_type.split(";", 1)[0].strip().lower()
    return essence in policy.allowed_content_types


def enforce_fetch_limits(
    envelope: FetchEnvelope,
    policy: RankingPolicy = POLICY_V1,
    *,
    url_check: UrlCheck | None = None,
) -> tuple[Rejection, ...]:
    """Re-check the provenance of the bytes about to be ranked.

    Every hop in ``redirect_chain`` (plus ``url``/``final_url`` when the
    chain does not already name them) is passed through ``url_check`` —
    the save-time :func:`~app_shared.url_safety.validate_competitor_url`
    by default, or the resolving fetch-time
    ``validate_resolved_target`` when the caller
    injects it. Checking only the final URL is precisely the
    redirect-SSRF miss: a chain can bounce through
    ``169.254.169.254`` and land back on a public host.

    Returns an empty tuple when the envelope is acceptable.
    """
    check = url_check if url_check is not None else validate_competitor_url
    rejections: list[Rejection] = []
    body = envelope.body_bytes()
    body_hash = compute_hash(body) if body is not None else None

    def refuse(reason: RejectionReason, detail: str) -> None:
        rejections.append(Rejection(reason, detail, evidence_hash=body_hash))

    if envelope.content_type and not _content_type_allowed(envelope.content_type, policy):
        refuse(
            RejectionReason.DISALLOWED_CONTENT_TYPE,
            f"content-type {envelope.content_type!r} is not an extractable document type",
        )

    observed_sizes = [
        size
        for size in (envelope.transferred_bytes, envelope.decoded_bytes, len(body) if body else None)
        if size is not None
    ]
    if observed_sizes and max(observed_sizes) > policy.max_response_bytes:
        refuse(
            RejectionReason.RESPONSE_TOO_LARGE,
            f"body of {max(observed_sizes)} bytes exceeds the "
            f"{policy.max_response_bytes}-byte post-fetch bound",
        )

    if (
        envelope.transferred_bytes
        and envelope.decoded_bytes
        and envelope.decoded_bytes > envelope.transferred_bytes * policy.max_decompression_ratio
    ):
        ratio = envelope.decoded_bytes / envelope.transferred_bytes
        refuse(
            RejectionReason.DECOMPRESSION_RATIO_EXCEEDED,
            f"decoded/transferred ratio {ratio:.1f}x exceeds "
            f"{policy.max_decompression_ratio}x",
        )

    hops = list(envelope.redirect_chain)
    for edge in (envelope.url, envelope.final_url):
        if edge and edge not in hops:
            hops.append(edge)
    if len(hops) > policy.max_redirects + 1:
        refuse(
            RejectionReason.TOO_MANY_REDIRECTS,
            f"{len(hops) - 1} redirect hops exceed the {policy.max_redirects} allowed",
        )

    for hop in hops:
        try:
            check(hop)
        except UnsafeUrlError as exc:
            refuse(
                RejectionReason.UNSAFE_URL,
                f"redirect hop {hop!r} failed the SSRF guard ({exc.reason}): {exc}",
            )

    return tuple(rejections)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateCollection:
    """Everything collection learned: what it found, and what it refused."""

    candidates: tuple[Candidate, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    #: True when the fetch envelope itself was refused, so no strategy
    #: ran at all. An empty ``candidates`` with ``blocked=False`` just
    #: means the page had no price.
    blocked: bool = False
    page_evidence_hash: str | None = None


#: A collection strategy: given the page (a :class:`FetchEnvelope`, a raw
#: string, or whatever the caller's chain speaks), return zero, one, or
#: many candidates.
Strategy = Callable[[Any], "Candidate | Iterable[Candidate] | None"]


def _as_candidates(produced: Any) -> list[Candidate]:
    if produced is None:
        return []
    if isinstance(produced, Candidate):
        return [produced]
    return [item for item in produced if isinstance(item, Candidate)]


def collect_candidates(
    page: Any,
    strategies: Iterable[Strategy],
    *,
    policy: RankingPolicy = POLICY_V1,
    url_check: UrlCheck | None = None,
    evidence_store_dir: Path | None = None,
) -> CandidateCollection:
    """Run **every** strategy over ``page``, bounded, and hash the evidence.

    The replacement for the ordered chain's ``if candidate is not None:
    return candidate``. A strategy that raises is recorded and skipped
    rather than aborting the page — one broken selector must not cost the
    other four readings.

    ``evidence_store_dir`` writes each candidate's evidence bytes into
    the content-addressed store from
    :mod:`app_shared.observations.evidence_store`, so
    ``Rejection.evidence_hash`` resolves back to real bytes. Without it
    the hash is still computed (candidates stay comparable and
    referenceable), just not persisted — callers that need replay pass
    the directory.
    """
    if isinstance(page, FetchEnvelope):
        envelope_rejections = enforce_fetch_limits(page, policy, url_check=url_check)
        body = page.body_bytes()
        page_hash = compute_hash(body) if body is not None else None
        if envelope_rejections:
            return CandidateCollection(
                rejections=envelope_rejections, blocked=True, page_evidence_hash=page_hash
            )
    else:
        page_hash = None

    collected: list[Candidate] = []
    rejections: list[Rejection] = []
    overflow = 0

    for strategy in strategies:
        try:
            produced = strategy(page)
        except Exception as exc:  # noqa: BLE001 - a strategy must never kill the page
            rejections.append(
                Rejection(
                    RejectionReason.STRATEGY_ERROR,
                    f"{type(exc).__name__}: {exc}",
                    method=getattr(strategy, "extraction_method", None),
                    evidence_hash=page_hash,
                )
            )
            continue

        for candidate in _as_candidates(produced):
            if len(collected) >= policy.max_candidates:
                overflow += 1
                continue
            collected.append(_with_evidence_hash(candidate, evidence_store_dir))

    if overflow:
        rejections.append(
            Rejection(
                RejectionReason.CANDIDATE_LIMIT_EXCEEDED,
                f"{overflow} candidate(s) past the {policy.max_candidates} the policy ranks",
                evidence_hash=page_hash,
            )
        )

    return CandidateCollection(
        candidates=tuple(collected),
        rejections=tuple(rejections),
        blocked=False,
        page_evidence_hash=page_hash,
    )


def _with_evidence_hash(candidate: Candidate, store_dir: Path | None) -> Candidate:
    if candidate.evidence is None or candidate.evidence_hash is not None:
        return candidate
    if store_dir is not None:
        evidence_hash = store_evidence(candidate.evidence, store_dir=store_dir)
    else:
        evidence_hash = compute_hash(candidate.evidence)
    return replace(candidate, evidence_hash=evidence_hash)


# ---------------------------------------------------------------------------
# Ranking outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Winner:
    """One reading survived and nothing material contradicts it."""

    candidate: Candidate
    policy_version: str
    #: Other surviving readings that agree with the winner — retained so
    #: a stored observation can say *how many* strategies concurred.
    corroborating: tuple[Candidate, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    needs_review: bool = False


@dataclass(frozen=True)
class Conflict:
    """Readings disagree materially. A human decides; the system does not.

    ``needs_review`` is always ``True`` — it exists so the outcome can be
    handed to the existing B6 ``NEEDS_REVIEW`` sidecar in the scraping
    library's item pipeline without the caller re-deriving the
    judgement.
    """

    top_candidates: tuple[Candidate, ...]
    policy_version: str
    detail: str = ""
    rejections: tuple[Rejection, ...] = ()
    needs_review: bool = True


@dataclass(frozen=True)
class NoValid:
    """Nothing survived. ``reasons`` is the whole retained argument."""

    reasons: tuple[Rejection, ...]
    policy_version: str
    needs_review: bool = False


RankedResult = Winner | Conflict | NoValid


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _within_tolerance(left: Decimal, right: Decimal, policy: RankingPolicy) -> bool:
    difference = abs(left - right)
    if difference <= policy.price_tolerance_absolute:
        return True
    base = min(abs(left), abs(right))
    if base == 0:
        return False
    return (difference / base) <= policy.price_tolerance_ratio


def _canonical_sort_key(candidate: Candidate) -> tuple[Any, ...]:
    """A total order over candidates derived from their *content* only.

    Grouping walks this order rather than collection order, which is what
    makes the partition a function of the candidate set (M2). Two
    candidates that tie on every field here are interchangeable by
    construction, so an arbitrary order between them cannot change the
    outcome. Best-first (freshest tier, then confidence) so the leading
    member of a group — the one a :class:`Conflict` reports — is its
    strongest reading.
    """
    identity = candidate.identity
    identifier_key = "|".join(
        f"{identifier_type}={value}"
        for identifier_type, value in sorted(
            (str(identifier_type), str(value)) for identifier_type, value in identity.identifiers
        )
    )
    return (
        -int(candidate.tier),
        -float(candidate.confidence),
        candidate.price,
        str(candidate.method),
        candidate.currency or "",
        identifier_key,
        identity.currency or "",
        candidate.evidence_hash or "",
        candidate.selector_used or "",
        candidate.matched_text or "",
    )


def _identity_groups(candidates: Sequence[Candidate]) -> list[list[Candidate]]:
    """Partition into maximal sets no member of which contradicts another.

    Permutation-invariant (W3 gate follow-up M2 — see the module
    docstring for the bug this replaces). Three steps:

    1. **Canonical order.** Everything below iterates
       :func:`_canonical_sort_key` order, so the partition depends on
       *which* candidates were collected, never on the order they
       arrived in.
    2. **Contradicted candidates decide the groups.** Only candidates
       that provably conflict with at least one other reading are
       greedily partitioned. A candidate that contradicts nothing (the
       common case: identity proves nothing, or only a currency every
       other reading agrees with) can join *any* group, so letting it
       seed or extend one is exactly what made the outcome depend on
       arrival order.
    3. **Uncontradicted candidates attach afterwards**, to the single
       group whose members corroborate them best (a shared *typed* B4
       identifier), else to the single largest group — "this strategy
       could not prove which product it read" is still not evidence that
       it read a different one, so it belongs with the consensus.
       When neither rule names one group, the candidate becomes its own
       group rather than being guessed into one: the vote for largest
       group is already tied at that point, and a tie is a
       :class:`Conflict`, so this can never invent a ``Winner``.
    """
    ordered = sorted(candidates, key=_canonical_sort_key)
    contradicted = [
        any(
            left.identity.conflicts_with(right.identity)
            for right_index, right in enumerate(ordered)
            if right_index != left_index
        )
        for left_index, left in enumerate(ordered)
    ]

    groups: list[list[Candidate]] = []
    for candidate, conflicts in zip(ordered, contradicted):
        if not conflicts:
            continue
        for group in groups:
            if not any(candidate.identity.conflicts_with(member.identity) for member in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])

    free = [candidate for candidate, conflicts in zip(ordered, contradicted) if not conflicts]
    if not groups:
        # Nothing contradicts anything: one consensus group, which is what
        # the greedy version produced for this (overwhelmingly common) case
        # too. Any price divergence inside it is `rank`'s to judge.
        return [free] if free else []

    # Pass 1: corroboration is real evidence of belonging, so it is applied
    # before any size-based fallback can be affected by an attachment.
    unplaced: list[Candidate] = []
    for candidate in free:
        scores = [
            sum(1 for member in group if candidate.identity.corroborates(member.identity))
            for group in groups
        ]
        best = max(scores)
        winners = [group for group, score in zip(groups, scores) if score == best]
        if best > 0 and len(winners) == 1:
            winners[0].append(candidate)
        else:
            unplaced.append(candidate)

    # Pass 2: the size fallback, and the conservative tie-rule.
    for candidate in unplaced:
        largest = max(len(group) for group in groups)
        biggest = [group for group in groups if len(group) == largest]
        if len(biggest) == 1:
            biggest[0].append(candidate)
        else:
            groups.append([candidate])
    return groups


def _preference(candidate: Candidate) -> tuple[int, float]:
    return (int(candidate.tier), candidate.confidence)


def rank(
    candidates: Iterable[Candidate],
    policy: RankingPolicy = POLICY_V1,
    *,
    rejections: Iterable[Rejection] = (),
) -> RankedResult:
    """Reduce collected candidates to a :class:`Winner`/:class:`Conflict`/:class:`NoValid`.

    Policy v1, in order:

    1. **Confidence gate** — anything under ``policy.min_confidence`` is
       rejected ``LOW_CONFIDENCE`` (this is what keeps the 0.40
       ``SINGLE_NUMBER`` heuristic out on its own).
    2. **Identity** — survivors are partitioned into non-contradicting
       groups. The largest group is the consensus; the rest are rejected
       ``IDENTITY_DISAGREEMENT``. A *tie* for largest means the page gave
       two equally-supported answers about which product it is, which is
       a :class:`Conflict`, not a coin flip.
    3. **Material divergence** — if any two consensus readings differ by
       more than the policy tolerance, :class:`Conflict`. This is checked
       *before* picking a winner, on purpose: the whole point of W3.2 is
       that a confident wrong reading must not be able to win.
    4. **Freshness** — the highest :class:`SourceTier` wins, §17
       confidence breaking ties within a tier, then the canonical
       content order of :func:`_canonical_sort_key` (M2: collection order
       used to break this tie, which made the winner depend on which
       strategy happened to run first).
    """
    carried = tuple(rejections)
    all_candidates = list(candidates)

    surviving: list[Candidate] = []
    gate_rejections: list[Rejection] = []
    for candidate in all_candidates:
        if candidate.confidence < policy.min_confidence:
            gate_rejections.append(
                candidate._rejection(
                    RejectionReason.LOW_CONFIDENCE,
                    f"confidence {candidate.confidence} is below the policy "
                    f"{policy.version} minimum {policy.min_confidence}",
                )
            )
            continue
        surviving.append(candidate)

    if not surviving:
        reasons = carried + tuple(gate_rejections)
        if not reasons:
            reasons = (
                Rejection(
                    RejectionReason.NO_PRICE,
                    "no strategy produced a price candidate for this page",
                ),
            )
        return NoValid(reasons=reasons, policy_version=policy.version)

    groups = _identity_groups(surviving)
    largest = max(len(group) for group in groups)
    contenders = [group for group in groups if len(group) == largest]

    if len(contenders) > 1:
        top = tuple(sorted((group[0] for group in contenders), key=_preference, reverse=True))
        return Conflict(
            top_candidates=top,
            policy_version=policy.version,
            detail=(
                f"{len(contenders)} equally-supported identities were read from this page; "
                "no reading may be chosen without human review"
            ),
            rejections=carried + tuple(gate_rejections),
        )

    consensus = contenders[0]
    identity_rejections = [
        candidate._rejection(
            RejectionReason.IDENTITY_DISAGREEMENT,
            "identity contradicts the consensus reading of this page",
        )
        for group in groups
        if group is not consensus
        for candidate in group
    ]
    accumulated = carried + tuple(gate_rejections) + tuple(identity_rejections)

    ordered = sorted(consensus, key=_preference, reverse=True)
    prices = [candidate.price for candidate in ordered]
    if not _within_tolerance(min(prices), max(prices), policy):
        return Conflict(
            top_candidates=tuple(ordered),
            policy_version=policy.version,
            detail=(
                f"agreeing-identity readings diverge from {min(prices)} to {max(prices)}, "
                f"beyond the policy {policy.version} tolerance of "
                f"{policy.price_tolerance_ratio:%}"
            ),
            rejections=accumulated,
        )

    winner, *rest = ordered
    return Winner(
        candidate=winner,
        policy_version=policy.version,
        corroborating=tuple(rest),
        rejections=accumulated,
    )
