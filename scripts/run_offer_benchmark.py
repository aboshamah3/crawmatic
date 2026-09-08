#!/usr/bin/env python3
"""Labeled offer benchmark + repricing release gate (Task W3.3, READY-012).

Scores the W3.2 ranked-extraction machinery
(``app_shared.strategy.candidate_ranking`` via
``scrape_core.extraction.pipeline.collect_extraction_candidates``/
``extract_ranked``) against the labeled corpus in
``tests/benchmarks/offer_truth/`` (see that directory's ``CORPUS.md`` for
provenance/labeling governance), and reports precision/recall + error
magnitude **separately** per field: identity, current price, old price,
currency, stock, seller, shipping, coupon/member conditions, landed
total (task step 2). Money comparisons go through
``app_shared.money.parse_money`` — the engine's one Decimal money
boundary — never floats (task step 2's explicit requirement).

Two domains (Noon, S-Tech) have no HTML extraction strategy at all —
their real price data is a JSON API response, not a page a
``scrape_core.extraction`` strategy parses. This script therefore
carries two small, **local, benchmark-only** shims
(:func:`_extract_noon_platform_json`, :func:`_extract_shopify_json`)
that read those payloads directly. They are deliberately simple — real
production resolution for S-Tech variants is the full algebra in
``scrape_core.adapters.variant_resolution`` (B4), which this script does
not re-implement or re-test; it only exercises the identity-matching
*shape* of that algebra (typed selector -> Resolved/Ambiguous) needed to
score this corpus's `variant`-category cases honestly. See
``tests/benchmarks/offer_truth/CORPUS.md``'s "Known, documented coverage
gaps" section for exactly what is and is not measured.

Usage::

    uv run python scripts/run_offer_benchmark.py [--domain amazon|noon|stech]
        [--category CATEGORY] [--json-out PATH] [--quiet]

Exit code: 0 if every case ran without a scorer-internal error (a
correctly-detected miss/conflict/adversarial-rejection is NOT an error —
it's a scored outcome); 1 on a scorer-internal exception. The
release-gate PASS/FAIL decision is a separate, explicit concern — see
:mod:`tests/benchmarks/test_offer_benchmark_gate` and :class:`GateThresholds`
below; this script's exit code is about the benchmark run succeeding,
not about whether the system is ready to reprice.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app_shared.enums import ExtractionMethod, StockStatus  # noqa: E402
from app_shared.money import parse_money  # noqa: E402
from app_shared.strategy.candidate_ranking import (  # noqa: E402
    POLICY_V1,
    Conflict,
    NoValid,
    RankingPolicy,
    RejectionReason,
    Winner,
    search_bounded,
)
from scrape_core.extraction.pipeline import extract_ranked  # noqa: E402
from scrape_core.money_text import normalize_price_text  # noqa: E402

from tests.benchmarks.offer_truth.schema import (  # noqa: E402
    SCORED_FIELDS,
    BenchmarkCase,
    load_corpus,
)

#: The Noon PLATFORM_JSON payload shape carries no currency field at
#: all (see CORPUS.md gap #3) — every real B5b capture was __cr.sa
#: (Saudi) proxy-targeted, so this is the domain-level assumption the
#: shim makes explicit rather than silently guessing per-hit.
_NOON_DEFAULT_CURRENCY = "SAR"

#: S-Tech is a single-merchant storefront — the seller is always the
#: store itself for any resolved variant. A genuine, trivial, always-true
#: fact (not a guess), unlike the 0%-coverage seller gap everywhere else.
_STECH_SELLER = "S-Tech"


# ---------------------------------------------------------------------------
# Actual-value shape (what a case's extraction path produced)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActualFields:
    """What the extraction path under test actually produced for a case.

    Mirrors :class:`~tests.benchmarks.offer_truth.schema.ExpectedFields`'
    field set (money fields are ``Decimal | None``, never ``float``).
    ``outcome`` is ``"winner"``/``"conflict"``/``"no_valid"`` — the
    ranked-extraction result shape, scored separately from the 9 fields
    (see :func:`score_case`).
    """

    outcome: str
    identity_id: str | None = None
    identity_title: str | None = None
    current_price: Decimal | None = None
    old_price: Decimal | None = None
    currency: str | None = None
    stock: str | None = None
    seller: str | None = None
    shipping: Decimal | None = None
    coupon: str | None = None
    landed_total: Decimal | None = None
    detail: str = ""


def _safe_decimal(value: Any) -> Decimal | None:
    """Parse through the exact §19 boundary; ``None`` on anything unparseable.

    Never floats, never a silent zero for garbage input — an
    extraction path that cannot produce a trustworthy number must
    produce nothing, not a wrong number (the same rule ``money.py``,
    ``normalize_price_text``, and B4's currency mapping all follow).

    A clean ``Decimal``/``int``/exact-numeric-``str`` goes straight
    through ``parse_money``. A string that is not already exact (a
    thousands separator, a stray symbol) gets one pass through
    ``scrape_core.money_text.normalize_price_text`` first — the same
    normalizer the HTML extraction path already runs before its own
    ``parse_money`` call — so a defensively-formatted API string is held
    to the same real, single normalization boundary the rest of the
    engine uses, not a second divergent parser invented here.
    """
    if value is None:
        return None
    try:
        return parse_money(value)
    except (TypeError, ValueError, InvalidOperation):
        pass
    if isinstance(value, str):
        normalized = normalize_price_text(value)
        if normalized is not None:
            try:
                return parse_money(normalized)
            except (TypeError, ValueError, InvalidOperation):
                return None
    return None


# ---------------------------------------------------------------------------
# HTML path (Amazon + any synthetic html_* case) — real pipeline
# ---------------------------------------------------------------------------


class _Profile:
    """Turns a case's ``input.profile`` dict into what the real strategies read.

    ``scrape_core.extraction.{css,jsonld,regex}`` read plain attributes
    off whatever ``profile`` object is handed in (``getattr(profile,
    "price_selector", None)``, ...) — a real ``ScrapeProfile`` in
    production, an attribute-bag here. No behavior is reimplemented;
    this only adapts the case file's dict into that attribute shape.
    """

    def __init__(self, raw: dict[str, Any] | None) -> None:
        self._raw = raw or {}

    def __getattr__(self, name: str) -> Any:
        return self._raw.get(name)


def _run_html_case(case: BenchmarkCase, html: str, *, policy: RankingPolicy) -> ActualFields:
    profile = _Profile(case.input.get("profile"))
    preferred_raw = case.input.get("preferred_method")
    preferred = ExtractionMethod(preferred_raw) if preferred_raw else None

    result = extract_ranked(html, profile, preferred_method=preferred, policy=policy)

    if isinstance(result, Conflict):
        return ActualFields(
            outcome="conflict",
            detail=result.detail,
        )
    if isinstance(result, NoValid):
        reasons = ", ".join(f"{r.reason}: {r.detail}" for r in result.reasons)
        return ActualFields(outcome="no_valid", detail=reasons)

    assert isinstance(result, Winner)
    candidate = result.candidate
    source = candidate.source  # the original ExtractionCandidate, per pipeline.py
    raw_title = getattr(source, "raw_title", None)
    stock = getattr(source, "stock", None)
    return ActualFields(
        outcome="winner",
        identity_id=None,  # no HTML strategy in this codebase surfaces a typed product id
        identity_title=raw_title,
        current_price=candidate.price,
        old_price=None,  # structural gap — see CORPUS.md gap #1
        currency=candidate.currency,
        stock=stock.value if isinstance(stock, StockStatus) else stock,
        seller=None,  # structural gap — see CORPUS.md gap #2
        shipping=None,
        coupon=None,
        landed_total=None,
        detail=f"method={candidate.method}",
    )


def _run_redos_direct_case(case: BenchmarkCase) -> ActualFields:
    """The one non-extraction case kind: exercises ``search_bounded`` directly."""
    pattern = case.input["pattern"]
    text = case.input["text"]
    match, rejection = search_bounded(pattern, text, policy=POLICY_V1)
    if rejection is not None and rejection.reason is RejectionReason.REDOS_PATTERN_REJECTED:
        return ActualFields(outcome="no_valid", detail=f"refused: {rejection.detail}")
    # Not refused: whatever the (safe) pattern matched is not a price at all
    # in this direct test — there is no candidate either way.
    return ActualFields(outcome="no_valid", detail="pattern was not refused (unexpected)")


# ---------------------------------------------------------------------------
# Noon PLATFORM_JSON shim (no HTML strategy exists for this domain)
# ---------------------------------------------------------------------------


def _extract_noon_platform_json(case: BenchmarkCase, payload: dict[str, Any]) -> ActualFields:
    sku_query = case.input["sku_query"]
    market = case.input.get("market")
    hits = payload.get("hits") or []

    exact = [hit for hit in hits if hit.get("sku") == sku_query]
    if exact:
        matches = exact
    else:
        # No hit's own (variant-precise) sku matches — fall back to a
        # catalog-level match. A query that only names the base
        # catalog_sku (not a specific sku_config/variant) is exactly the
        # ambiguous case checked below: it is not evidence about *which*
        # variant was meant, so multiple catalog-level matches are never
        # narrowed by picking one.
        matches = [hit for hit in hits if hit.get("catalog_sku") == sku_query]
    if not matches:
        return ActualFields(outcome="no_valid", detail=f"no hit matched sku_query {sku_query!r}")

    distinct_identities = {hit.get("sku") for hit in matches}
    if len(distinct_identities) > 1:
        return ActualFields(
            outcome="conflict",
            detail=f"query {sku_query!r} matched {len(distinct_identities)} distinct variant "
            f"identities ({sorted(distinct_identities)}) — ambiguous which was meant",
        )

    # Same sku appearing more than once with materially different prices
    # (or a sale_price/price pairing so corrupt the "discount" is inverted)
    # is a genuine ambiguity — never averaged, never "first wins".
    prices: list[Decimal] = []
    for hit in matches:
        current = _resolve_noon_current_price(hit)
        if current is not None:
            prices.append(current)
    if len(prices) >= 2:
        distinct = {p for p in prices}
        if len(distinct) > 1 and (max(distinct) - min(distinct)) / min(distinct) > POLICY_V1.price_tolerance_ratio:
            return ActualFields(
                outcome="conflict",
                detail=f"sku {sku_query!r} resolved to {len(distinct)} materially different prices: {sorted(distinct)}",
            )

    hit = matches[0]
    current_price = _resolve_noon_current_price(hit)
    if current_price is None:
        return ActualFields(outcome="no_valid", detail="hit price(s) unparseable or invalid")

    old_price = _resolve_noon_old_price(hit, current_price)
    # Adversarial data quality: a "discount" where sale_price > price is
    # an inverted/untrustworthy pairing — decline rather than assert it.
    price_raw = _safe_decimal(hit.get("price"))
    sale_raw = _safe_decimal(hit.get("sale_price"))
    if price_raw is not None and sale_raw is not None and sale_raw > price_raw:
        return ActualFields(
            outcome="conflict",
            detail=f"sale_price {sale_raw} exceeds price {price_raw} — untrustworthy discount pairing",
        )

    is_buyable = hit.get("is_buyable")
    if is_buyable is True:
        stock = StockStatus.IN_STOCK.value
    elif is_buyable is False:
        stock = StockStatus.OUT_OF_STOCK.value
    else:
        stock = StockStatus.UNKNOWN.value

    currency = _NOON_DEFAULT_CURRENCY
    if market is not None and market != "SA":
        # Known limitation (CORPUS.md gap #3): the shim cannot see a
        # per-hit currency, so it always answers SAR. Left as-is
        # (not "fixed" here) so the scored report shows the true gap.
        currency = _NOON_DEFAULT_CURRENCY

    return ActualFields(
        outcome="winner",
        identity_id=hit.get("sku"),
        identity_title=hit.get("name"),
        current_price=current_price,
        old_price=old_price,
        currency=currency,
        stock=stock,
        seller=None,
        shipping=None,
        coupon=None,
        landed_total=None,
        detail="PLATFORM_JSON",
    )


def _resolve_noon_current_price(hit: dict[str, Any]) -> Decimal | None:
    sale_price = _safe_decimal(hit.get("sale_price"))
    if sale_price is not None:
        if sale_price < 0:
            return None
        return sale_price
    price = _safe_decimal(hit.get("price"))
    if price is not None and price < 0:
        return None
    return price


def _resolve_noon_old_price(hit: dict[str, Any], current_price: Decimal) -> Decimal | None:
    sale_price = _safe_decimal(hit.get("sale_price"))
    price = _safe_decimal(hit.get("price"))
    if sale_price is not None and price is not None and price != current_price:
        return price
    return None


# ---------------------------------------------------------------------------
# S-Tech Shopify JSON shim (no HTML strategy exists for this domain either)
# ---------------------------------------------------------------------------


def _shopify_available(value: Any) -> str | None:
    """Defensive bool coercion — a string ``"true"``/``"false"`` must not
    fall through Python's plain truthiness trap (``bool("false") is True``)."""
    if isinstance(value, bool):
        return StockStatus.IN_STOCK.value if value else StockStatus.OUT_OF_STOCK.value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return StockStatus.IN_STOCK.value
        if lowered == "false":
            return StockStatus.OUT_OF_STOCK.value
    return None


def _shopify_price(variant: dict[str, Any], key: str) -> Decimal | None:
    """Shopify prices are integer minor units (cents) — Decimal(cents)/100,
    never a float division, so no binary-fraction rounding drift."""
    if key not in variant:
        return None
    raw = variant.get(key)
    if raw is None:
        return None
    cents = _safe_decimal(raw)
    if cents is None or cents < 0:
        return None
    return (cents / Decimal(100)).quantize(Decimal("0.01"))


def _extract_shopify_json(case: BenchmarkCase, payload: dict[str, Any]) -> ActualFields:
    variants = payload.get("variants") or []
    if not variants:
        return ActualFields(outcome="no_valid", detail="empty variants[]")

    selector = case.input.get("variant_selector")

    if selector is None:
        if len(variants) == 1:
            candidates = variants
        else:
            # B4's rule: the sole-variant fallback only applies to a
            # single-variant product. Several variants with nothing to
            # disambiguate is genuinely ambiguous.
            return ActualFields(
                outcome="conflict",
                detail=f"{len(variants)} variants, no identifier to disambiguate",
            )
    else:
        field_name = selector["type"]
        value = str(selector["value"])
        candidates = [v for v in variants if str(v.get(field_name)) == value]
        if not candidates:
            return ActualFields(
                outcome="conflict",
                detail=f"selector {field_name}={value!r} matched no variant (stale identifier)",
            )
        if len(candidates) > 1:
            return ActualFields(
                outcome="conflict",
                detail=f"selector {field_name}={value!r} matched {len(candidates)} variants (data collision)",
            )

    variant = candidates[0]
    if "price" not in variant:
        return ActualFields(outcome="no_valid", detail="variant has no price key")

    current_price = _shopify_price(variant, "price")
    if current_price is None:
        return ActualFields(outcome="no_valid", detail="variant price missing/negative/unparseable")

    old_price = _shopify_price(variant, "compare_at_price")
    if old_price is not None and old_price == 0:
        old_price = None  # Shopify convention: 0 means "not set"

    stock = _shopify_available(variant.get("available"))

    # Prefer whichever identifier the selector itself named (the typed
    # identifier that actually resolved this variant, per B4's "typing
    # is a fact derived from evidence" rule) — falling back to sku, then
    # barcode, then the raw variant id when resolution was the sole-
    # variant fallback (no selector at all).
    if selector is not None and variant.get(selector["type"]) is not None:
        identity_id = str(variant.get(selector["type"]))
    else:
        identity_id = variant.get("sku") or variant.get("barcode") or (
            str(variant.get("id")) if variant.get("id") is not None else None
        )

    return ActualFields(
        outcome="winner",
        identity_id=identity_id,
        identity_title=payload.get("title"),
        current_price=current_price,
        old_price=old_price,
        currency="SAR",  # domain-level assumption, same as Noon — see CORPUS.md
        stock=stock,
        seller=_STECH_SELLER,
        shipping=None,
        coupon=None,
        landed_total=None,
        detail="shopify_json",
    )


# ---------------------------------------------------------------------------
# Case dispatch
# ---------------------------------------------------------------------------


def run_case(case: BenchmarkCase, *, policy: RankingPolicy = POLICY_V1) -> ActualFields:
    kind = case.input["kind"]
    if kind == "html_file":
        html = (REPO_ROOT / case.input["path"]).read_text(encoding="utf-8", errors="replace")
        return _run_html_case(case, html, policy=policy)
    if kind == "html_inline":
        return _run_html_case(case, case.input["html"], policy=policy)
    if kind == "redos_direct":
        return _run_redos_direct_case(case)
    if kind == "platform_json_file":
        payload = json.loads((REPO_ROOT / case.input["path"]).read_text(encoding="utf-8"), parse_float=Decimal)
        return _extract_noon_platform_json(case, payload)
    if kind == "platform_json_inline":
        return _extract_noon_platform_json(case, case.input["json"])
    if kind == "shopify_json_file":
        payload = json.loads((REPO_ROOT / case.input["path"]).read_text(encoding="utf-8"), parse_float=Decimal)
        return _extract_shopify_json(case, payload)
    if kind == "shopify_json_inline":
        return _extract_shopify_json(case, case.input["json"])
    raise ValueError(f"{case.source_path}: unrecognized input.kind {kind!r}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class FieldTally:
    """Confusion-style counts for one scored field across the corpus.

    * ``correct`` — both expected and actual present, and they agree.
    * ``incorrect`` — both present, but they disagree (the dangerous
      case: a value was asserted and it was WRONG). ``error_magnitudes``
      holds ``abs(expected - actual)`` for money fields.
    * ``missed`` — expected present, actual absent (a real fact the
      system failed to surface).
    * ``extra`` — expected absent (no ground-truth claim), actual
      present (the system asserted something with nothing to check it
      against — reported, never silently dropped, but not scored as
      wrong since there is no ground truth to contradict it).
    * ``correct_absence`` — both absent (correctly claimed nothing).
    """

    correct: int = 0
    incorrect: int = 0
    missed: int = 0
    extra: int = 0
    correct_absence: int = 0
    error_magnitudes: list[Decimal] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.correct + self.incorrect + self.extra
        return (self.correct / denom) if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.correct + self.incorrect + self.missed
        return (self.correct / denom) if denom else None

    @property
    def mean_error_magnitude(self) -> str | None:
        if not self.error_magnitudes:
            return None
        return str(sum(self.error_magnitudes) / len(self.error_magnitudes))

    @property
    def max_error_magnitude(self) -> str | None:
        if not self.error_magnitudes:
            return None
        return str(max(self.error_magnitudes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "incorrect": self.incorrect,
            "missed": self.missed,
            "extra": self.extra,
            "correct_absence": self.correct_absence,
            "precision": self.precision,
            "recall": self.recall,
            "mean_error_magnitude": self.mean_error_magnitude,
            "max_error_magnitude": self.max_error_magnitude,
        }


_MONEY_FIELDS = frozenset({"current_price", "old_price", "landed_total", "shipping"})


def _field_values(case: BenchmarkCase, actual: ActualFields, field_name: str) -> tuple[Any, Any]:
    if field_name == "identity":
        expected_id = case.expected.identity.primary_id
        expected_title = case.expected.identity.title_contains
        if expected_id is None and expected_title is None:
            return None, None
        actual_id = actual.identity_id
        actual_title = actual.identity_title
        if actual_id is None and actual_title is None:
            return (expected_id or expected_title), None

        # The two ground-truth signals are independent, not a fallback
        # chain: a code-typed expectation (ASIN/SKU) is checked against
        # whatever typed id the path produced; a title-substring
        # expectation is checked against whatever title text the path
        # produced. Either one matching is enough — an HTML strategy
        # that never surfaces a typed code (no strategy in this codebase
        # does) must not be scored wrong on identity when its title is
        # exactly right, and vice versa.
        ok = False
        if expected_id is not None and actual_id is not None and actual_id == expected_id:
            ok = True
        if not ok and expected_title is not None and actual_title is not None and expected_title.lower() in actual_title.lower():
            ok = True

        actual_repr = actual_id or actual_title
        return (expected_id or expected_title), (actual_repr if ok else f"MISMATCH:{actual_repr}")
    expected = getattr(case.expected, field_name)
    actual_value = getattr(actual, field_name)
    return expected, actual_value


def score_field(case: BenchmarkCase, actual: ActualFields, field_name: str, tally: FieldTally) -> None:
    expected, actual_value = _field_values(case, actual, field_name)

    if expected is None and actual_value is None:
        return  # no claim, nothing produced — not scored either way
    if expected is None:
        tally.extra += 1
        return
    if actual_value is None:
        tally.missed += 1
        return

    if field_name in _MONEY_FIELDS:
        expected_dec = _safe_decimal(expected)
        actual_dec = actual_value if isinstance(actual_value, Decimal) else _safe_decimal(actual_value)
        if expected_dec is not None and actual_dec is not None and expected_dec == actual_dec:
            tally.correct += 1
        else:
            tally.incorrect += 1
            if expected_dec is not None and actual_dec is not None:
                tally.error_magnitudes.append(abs(expected_dec - actual_dec))
        return

    if field_name == "identity":
        if isinstance(actual_value, str) and actual_value.startswith("MISMATCH:"):
            tally.incorrect += 1
        else:
            tally.correct += 1
        return

    if str(expected).strip().casefold() == str(actual_value).strip().casefold():
        tally.correct += 1
    else:
        tally.incorrect += 1


@dataclass
class CaseResult:
    case_id: str
    domain: str
    category: str
    synthetic: bool
    outcome_correct: bool
    silently_wrong_conflict: bool
    actual: ActualFields
    error: str | None = None


@dataclass
class BenchmarkReport:
    generated_at: str
    policy_version: str
    field_tallies: dict[str, FieldTally]
    case_results: list[CaseResult]
    domains: tuple[str, ...]

    def financial_agreement_rate(self) -> float | None:
        """§15.3's "financial-field agreement" — current price, old price,
        currency, landed total — pooled correct / (correct+incorrect+missed+extra)."""
        fields = ("current_price", "old_price", "currency", "landed_total")
        correct = sum(self.field_tallies[f].correct for f in fields)
        total = sum(
            self.field_tallies[f].correct
            + self.field_tallies[f].incorrect
            + self.field_tallies[f].missed
            + self.field_tallies[f].extra
            for f in fields
        )
        return (correct / total) if total else None

    def unresolved_ambiguity_count(self) -> int:
        """Cases where the page genuinely had an unresolved
        currency/variant/marketplace ambiguity and the system answered
        confidently anyway instead of flagging it — the dangerous
        silent-wrong-answer case §15.3's "zero unresolved ambiguity" targets."""
        return sum(1 for r in self.case_results if r.silently_wrong_conflict)

    def summary(self) -> dict[str, Any]:
        total = len(self.case_results)
        outcome_correct = sum(1 for r in self.case_results if r.outcome_correct)
        errored = sum(1 for r in self.case_results if r.error is not None)
        return {
            "generated_at": self.generated_at,
            "policy_version": self.policy_version,
            "domains": list(self.domains),
            "total_cases": total,
            "outcome_correct": outcome_correct,
            "outcome_correct_rate": (outcome_correct / total) if total else None,
            "scorer_errors": errored,
            "unresolved_ambiguity_count": self.unresolved_ambiguity_count(),
            "financial_field_agreement_rate": self.financial_agreement_rate(),
            "fields": {name: tally.to_dict() for name, tally in self.field_tallies.items()},
        }


def run_benchmark(
    *, domain: str | None = None, category: str | None = None, policy: RankingPolicy = POLICY_V1
) -> BenchmarkReport:
    cases = load_corpus(domain=domain, category=category)
    tallies: dict[str, FieldTally] = {name: FieldTally() for name in SCORED_FIELDS}
    results: list[CaseResult] = []
    domains_seen: set[str] = set()

    for case in cases:
        domains_seen.add(case.domain)
        try:
            actual = run_case(case, policy=policy)
        except Exception as exc:  # noqa: BLE001 - a scorer bug must not kill the whole run
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    domain=case.domain,
                    category=case.category,
                    synthetic=case.synthetic,
                    outcome_correct=False,
                    silently_wrong_conflict=False,
                    actual=ActualFields(outcome="error"),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        expected_outcome = (
            "conflict"
            if case.expected.expect_conflict
            else ("no_valid" if case.expected.expect_no_price else "winner")
        )
        outcome_correct = actual.outcome == expected_outcome
        silently_wrong = (
            case.expected.expect_conflict
            and actual.outcome == "winner"
            and case.category in {"marketplace", "variant", "locale_format", "multi_product"}
        )

        if not case.expected.expect_conflict and not case.expected.expect_no_price:
            # Only score the 9 fields when the case expects a clean,
            # committed answer — a Conflict/NoValid case's fields are
            # legitimately unresolved by design (scored via outcome_correct
            # and silently_wrong_conflict instead, not field-by-field).
            for field_name in SCORED_FIELDS:
                score_field(case, actual, field_name, tallies[field_name])

        results.append(
            CaseResult(
                case_id=case.case_id,
                domain=case.domain,
                category=case.category,
                synthetic=case.synthetic,
                outcome_correct=outcome_correct,
                silently_wrong_conflict=silently_wrong,
                actual=actual,
            )
        )

    return BenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        policy_version=policy.version,
        field_tallies=tallies,
        case_results=results,
        domains=tuple(sorted(domains_seen)),
    )


# ---------------------------------------------------------------------------
# §15.3 release gate — PENDING OWNER REVIEW
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateThresholds:
    """Release-gate thresholds for offer data (plan §15.3).

    **PENDING OWNER REVIEW (§15.3): the exact numbers below are the
    plan's own stated starting values, not a ratified decision.** They
    are wired up and enforced so the gate mechanism is real and
    CI-invokable today, and are fully configurable (construct a
    different :class:`GateThresholds` and pass it to
    :func:`decide_repricing_gate`) so the owner can tune them without a
    code change once §15.3 is settled.
    """

    #: Lower bar — monitoring/display only, never an autonomous action.
    monitoring_min_financial_agreement: float = 0.90
    #: §15.3's stated number for autonomous repricing.
    auto_reprice_min_financial_agreement: float = 0.99
    #: "Zero unresolved currency/variant/seller ambiguity" — a hard 0, not tunable
    #: in spirit, but exposed for owner override (e.g. a phased rollout).
    auto_reprice_max_unresolved_ambiguity: int = 0
    #: "Fresh evidence" — plan text names no exact number; 24h is this
    #: script's placeholder pending §15.3.
    auto_reprice_max_evidence_age_hours: float = 24.0


@dataclass(frozen=True)
class GateDecision:
    allowed_for_monitoring: bool
    allowed_for_auto_reprice: bool
    reasons: tuple[str, ...]
    financial_agreement_rate: float | None
    unresolved_ambiguity_count: int


def check_evidence_freshness(observed_at: datetime, *, max_age_hours: float, now: datetime | None = None) -> bool:
    """Pure freshness predicate — "fresh evidence" (§15.3), tested independently
    of any specific corpus case's real capture timestamp."""
    reference = now if now is not None else datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return (reference - observed_at) <= timedelta(hours=max_age_hours)


def decide_repricing_gate(
    report: BenchmarkReport,
    thresholds: GateThresholds = GateThresholds(),
    *,
    rollback_wired: bool,
    human_approval_wired: bool,
    evidence_fresh: bool,
) -> GateDecision:
    """§15.3: monitoring display at a lower bar; automatic repricing requires
    >=99% financial-field agreement, zero unresolved currency/variant/seller
    ambiguity, fresh evidence, and a rollback + human-approval policy.

    ``rollback_wired``/``human_approval_wired``/``evidence_fresh`` are
    deployment-context facts this benchmark cannot observe on its own
    (they describe the release pipeline, not the corpus) — the caller
    (CI, or a human release check) supplies them explicitly rather than
    this function assuming either is true by default.
    """
    agreement = report.financial_agreement_rate()
    ambiguity = report.unresolved_ambiguity_count()

    reasons: list[str] = []

    monitoring_ok = agreement is not None and agreement >= thresholds.monitoring_min_financial_agreement
    if not monitoring_ok:
        reasons.append(
            f"monitoring gate: financial-field agreement {agreement!r} is below "
            f"{thresholds.monitoring_min_financial_agreement:.0%}"
        )

    auto_reprice_ok = True
    if agreement is None or agreement < thresholds.auto_reprice_min_financial_agreement:
        auto_reprice_ok = False
        reasons.append(
            f"auto-reprice gate: financial-field agreement {agreement!r} is below "
            f"{thresholds.auto_reprice_min_financial_agreement:.0%} "
            "(PENDING OWNER REVIEW §15.3)"
        )
    if ambiguity > thresholds.auto_reprice_max_unresolved_ambiguity:
        auto_reprice_ok = False
        reasons.append(
            f"auto-reprice gate: {ambiguity} unresolved currency/variant/seller "
            f"ambiguity case(s), allowed {thresholds.auto_reprice_max_unresolved_ambiguity}"
        )
    if not evidence_fresh:
        auto_reprice_ok = False
        reasons.append(
            f"auto-reprice gate: evidence is not fresh (max age "
            f"{thresholds.auto_reprice_max_evidence_age_hours}h, PENDING OWNER REVIEW §15.3)"
        )
    if not rollback_wired:
        auto_reprice_ok = False
        reasons.append("auto-reprice gate: no rollback policy wired for this deployment")
    if not human_approval_wired:
        auto_reprice_ok = False
        reasons.append("auto-reprice gate: no human-approval policy wired for this deployment")

    if monitoring_ok and auto_reprice_ok:
        reasons.append("all gates satisfied")
    elif monitoring_ok:
        reasons.append("monitoring gate satisfied; auto-reprice gate is NOT — display-only")

    return GateDecision(
        allowed_for_monitoring=monitoring_ok,
        allowed_for_auto_reprice=auto_reprice_ok,
        reasons=tuple(reasons),
        financial_agreement_rate=agreement,
        unresolved_ambiguity_count=ambiguity,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(report: BenchmarkReport) -> None:
    summary = report.summary()
    print("=" * 78)
    print("OFFER BENCHMARK — scored report")
    print("=" * 78)
    print(f"generated_at:            {summary['generated_at']}")
    print(f"policy_version:          {summary['policy_version']}")
    print(f"domains:                 {', '.join(summary['domains'])}")
    print(f"total_cases:             {summary['total_cases']}")
    print(f"outcome_correct:         {summary['outcome_correct']} / {summary['total_cases']} "
          f"({summary['outcome_correct_rate']:.1%})" if summary['outcome_correct_rate'] is not None else "outcome_correct: n/a")
    print(f"scorer_errors:           {summary['scorer_errors']}")
    print(f"unresolved_ambiguity:    {summary['unresolved_ambiguity_count']}")
    fin = summary["financial_field_agreement_rate"]
    print(f"financial_field_agreement_rate: {fin:.1%}" if fin is not None else "financial_field_agreement_rate: n/a")
    print("-" * 78)
    print(f"{'field':<16}{'correct':>8}{'incorrect':>10}{'missed':>8}{'extra':>7}{'precision':>11}{'recall':>9}  mean_err")
    for name in SCORED_FIELDS:
        t = summary["fields"][name]
        prec = f"{t['precision']:.0%}" if t["precision"] is not None else "n/a"
        rec = f"{t['recall']:.0%}" if t["recall"] is not None else "n/a"
        err = t["mean_error_magnitude"] or "-"
        print(
            f"{name:<16}{t['correct']:>8}{t['incorrect']:>10}{t['missed']:>8}{t['extra']:>7}"
            f"{prec:>11}{rec:>9}  {err}"
        )
    print("=" * 78)
    errored = [r for r in report.case_results if r.error]
    if errored:
        print(f"SCORER ERRORS ({len(errored)}):")
        for r in errored:
            print(f"  {r.case_id}: {r.error}")


# ---------------------------------------------------------------------------
# EPA C5 (F19): the shadow-events view of the same question
# ---------------------------------------------------------------------------
#
# The corpus benchmark above answers "is the ranker right?" against
# LABELED pages. `--from-shadow-events` answers the other half, which no
# corpus can: "how often does the ranker disagree with the chain on the
# pages we actually scrape?" Both numbers gate the C11 owner decision on
# `EXTRACTION_RANKING_POLICY`, and they fail in opposite directions -- a
# ranker that is right on every labeled case and disagrees with
# production on 30% of pages is not ready, and neither is one that never
# disagrees but is wrong whenever it does.
#
# Input is a JSONL EXPORT of `extraction_shadow_events`, not a live
# connection. Three reasons, in order: the gate is evaluated by a human
# reading a report, the export is the artifact that gets attached to the
# decision, and a script that opens a production database to compute a
# release number is a script that has to be trusted with a production
# database. Export with:
#
#   \copy (SELECT * FROM extraction_shadow_events
#          WHERE observed_at >= now() - interval '7 days')
#     TO 'shadow.jsonl' (FORMAT text)
#
# ...or any equivalent that emits one JSON object per line.


#: The two numbers the plan names for the C11 flip, together:
#:   "the shadow disagreement rate is < 1% on the C4 labeled sets AND
#:    the ranker wins >= 99% of labeled conflicts"
@dataclass(frozen=True)
class ShadowGateThresholds:
    max_disagreement_rate: float = 0.01
    min_ranker_win_rate: float = 0.99


@dataclass(frozen=True)
class ShadowReport:
    """What a window of shadow events says about the ranker."""

    events: int
    #: Denominator for the rate. NOT derivable from the events file --
    #: only disagreements are recorded -- so the caller supplies the
    #: count of observations over the same window. `None` means the rate
    #: is unknown, which is reported as unknown rather than assumed
    #: acceptable.
    observations: int | None
    by_kind: dict[str, int]
    by_domain: dict[str, int]
    #: Labeled conflicts: disagreements where a C4 labeled offer for the
    #: same URL says which side was right.
    labeled_conflicts: int
    ranker_wins: int
    first_hit_wins: int
    #: Neither side matched the label -- counted separately because a
    #: case where BOTH paths are wrong is evidence about the page, not
    #: about the ranker, and folding it into either win count would
    #: distort the ratio the gate reads.
    both_wrong: int

    def disagreement_rate(self) -> float | None:
        if not self.observations:
            return None
        return self.events / self.observations

    def ranker_win_rate(self) -> float | None:
        if self.labeled_conflicts == 0:
            return None
        return self.ranker_wins / self.labeled_conflicts


def load_shadow_events(path: Path) -> list[dict[str, Any]]:
    """One JSON object per line; blank lines skipped, bad lines refused.

    A malformed line raises rather than being skipped: a gate computed
    over "whatever parsed" is a gate whose denominator nobody can
    reproduce.
    """
    events: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} is not a JSON object: {exc}") from exc
    return events


def load_labeled_prices(paths: list[Path]) -> dict[str, Decimal]:
    """`url -> labeled price` from the C4 labeled-offer fixtures.

    Only the price is read. Currency/availability disagreements are
    reported by kind but not adjudicated here, because the C4 fixtures'
    own README records availability as UNKNOWN on 29 of 30 amazon rows --
    scoring against that would measure the fixture's gaps, not the
    ranker.
    """
    labels: dict[str, Decimal] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            url = row.get("url")
            price = row.get("price")
            if not url or price is None:
                continue
            try:
                labels[str(url)] = parse_money(str(price))
            except (TypeError, ValueError, InvalidOperation):
                continue
    return labels


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return parse_money(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None


def summarize_shadow_events(
    events: list[dict[str, Any]],
    *,
    labels: dict[str, Decimal] | None = None,
    observations: int | None = None,
) -> ShadowReport:
    """Reduce a window of shadow events to the two C11 gate numbers."""
    labels = labels or {}
    by_kind: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    labeled_conflicts = 0
    ranker_wins = 0
    first_hit_wins = 0
    both_wrong = 0

    for event in events:
        kind = str(event.get("disagreement_kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        domain = str(event.get("domain") or "unknown")
        by_domain[domain] = by_domain.get(domain, 0) + 1

        url = event.get("url")
        truth = labels.get(str(url)) if url else None
        if truth is None:
            continue
        labeled_conflicts += 1
        first_hit = _decimal_or_none(event.get("first_hit_price"))
        ranked = _decimal_or_none(event.get("ranked_price"))
        if ranked is not None and ranked == truth:
            ranker_wins += 1
        elif first_hit is not None and first_hit == truth:
            first_hit_wins += 1
        else:
            both_wrong += 1

    return ShadowReport(
        events=len(events),
        observations=observations,
        by_kind=by_kind,
        by_domain=by_domain,
        labeled_conflicts=labeled_conflicts,
        ranker_wins=ranker_wins,
        first_hit_wins=first_hit_wins,
        both_wrong=both_wrong,
    )


def decide_shadow_gate(
    report: ShadowReport, thresholds: ShadowGateThresholds = ShadowGateThresholds()
) -> tuple[bool, tuple[str, ...]]:
    """`(allowed, reasons)` for flipping `EXTRACTION_RANKING_POLICY` to v1.

    **Unknown is never a pass.** An unknown disagreement rate (no
    observation count supplied) and an unknown win rate (no labeled
    conflict in the window) both REFUSE, because the gate's job is to
    require evidence, and "we have no evidence of a problem" is the
    sentence this function exists to reject.

    Returns a recommendation. The flip itself is an OWNER decision at
    C11 -- nothing in this repository changes the flag.
    """
    reasons: list[str] = []
    allowed = True

    rate = report.disagreement_rate()
    if rate is None:
        allowed = False
        reasons.append(
            "disagreement rate unknown: pass --observations <count over the same "
            "window> (only disagreements are recorded, so the denominator cannot "
            "come from the events file)"
        )
    elif rate > thresholds.max_disagreement_rate:
        allowed = False
        reasons.append(
            f"disagreement rate {rate:.2%} exceeds "
            f"{thresholds.max_disagreement_rate:.2%}"
        )

    win_rate = report.ranker_win_rate()
    if win_rate is None:
        allowed = False
        reasons.append(
            "ranker win rate unknown: no shadow event in this window matched a "
            "labeled offer (pass --labels with the C4 fixtures covering these URLs)"
        )
    elif win_rate < thresholds.min_ranker_win_rate:
        allowed = False
        reasons.append(
            f"ranker wins {win_rate:.2%} of labeled conflicts, below "
            f"{thresholds.min_ranker_win_rate:.2%}"
        )

    if allowed:
        reasons.append(
            "both C11 thresholds met -- the flip to EXTRACTION_RANKING_POLICY=v1 "
            "is an OWNER decision, not this script's"
        )
    return allowed, tuple(reasons)


def _print_shadow_report(report: ShadowReport, allowed: bool, reasons: tuple[str, ...]) -> None:
    print("=" * 78)
    print("EXTRACTION SHADOW EVENTS — C11 gate input")
    print("=" * 78)
    print(f"disagreement events:     {report.events}")
    print(f"observations in window:  {report.observations if report.observations is not None else 'n/a'}")
    rate = report.disagreement_rate()
    print(f"disagreement rate:       {rate:.3%}" if rate is not None else "disagreement rate:       n/a")
    print("-" * 78)
    for kind, count in sorted(report.by_kind.items(), key=lambda item: -item[1]):
        print(f"  kind {kind:<12}{count:>8}")
    for domain, count in sorted(report.by_domain.items(), key=lambda item: -item[1])[:20]:
        print(f"  domain {domain:<30}{count:>8}")
    print("-" * 78)
    print(f"labeled conflicts:       {report.labeled_conflicts}")
    print(f"  ranker correct:        {report.ranker_wins}")
    print(f"  first-hit correct:     {report.first_hit_wins}")
    print(f"  both wrong:            {report.both_wrong}")
    win = report.ranker_win_rate()
    print(f"ranker win rate:         {win:.2%}" if win is not None else "ranker win rate:         n/a")
    print("=" * 78)
    print(f"C11 RECOMMENDATION: {'THRESHOLDS MET' if allowed else 'NOT MET'}")
    for reason in reasons:
        print(f"  - {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=["amazon", "noon", "stech"], default=None)
    parser.add_argument("--category", default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    # --- EPA C5 (F19) ---
    parser.add_argument(
        "--from-shadow-events",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "score a JSONL export of extraction_shadow_events instead of the "
            "labeled corpus (the C11 gate input for EXTRACTION_RANKING_POLICY)"
        ),
    )
    parser.add_argument(
        "--labels",
        type=Path,
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "C4 labeled-offer fixture(s) to adjudicate shadow disagreements "
            "against; repeatable. Only meaningful with --from-shadow-events"
        ),
    )
    parser.add_argument(
        "--observations",
        type=int,
        default=None,
        help=(
            "how many observations were recorded over the same window as the "
            "shadow export -- the disagreement rate's DENOMINATOR. Without it "
            "the rate is reported as unknown and the gate refuses"
        ),
    )
    args = parser.parse_args(argv)

    if args.from_shadow_events is not None:
        shadow = summarize_shadow_events(
            load_shadow_events(args.from_shadow_events),
            labels=load_labeled_prices(list(args.labels or [])),
            observations=args.observations,
        )
        allowed, reasons = decide_shadow_gate(shadow)
        if not args.quiet:
            _print_shadow_report(shadow, allowed, reasons)
        if args.json_out is not None:
            args.json_out.write_text(
                json.dumps(
                    {
                        "events": shadow.events,
                        "observations": shadow.observations,
                        "disagreement_rate": shadow.disagreement_rate(),
                        "by_kind": shadow.by_kind,
                        "by_domain": shadow.by_domain,
                        "labeled_conflicts": shadow.labeled_conflicts,
                        "ranker_wins": shadow.ranker_wins,
                        "first_hit_wins": shadow.first_hit_wins,
                        "both_wrong": shadow.both_wrong,
                        "ranker_win_rate": shadow.ranker_win_rate(),
                        "c11_thresholds_met": allowed,
                        "reasons": list(reasons),
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        # Exit 0 either way: like the corpus mode above, this script's exit
        # code says the ANALYSIS ran, not that the system passed. A gate
        # decision that shows up as a non-zero exit invites somebody to
        # "fix" it by not running the script.
        return 0

    report = run_benchmark(domain=args.domain, category=args.category)

    if not args.quiet:
        _print_report(report)

    if args.json_out is not None:
        args.json_out.write_text(json.dumps(report.summary(), indent=2, default=str), encoding="utf-8")
        if not args.quiet:
            print(f"\nwrote {args.json_out}")

    return 1 if any(r.error for r in report.case_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
