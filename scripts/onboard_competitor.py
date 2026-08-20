#!/usr/bin/env python3
"""onboard_competitor.py — competitor onboarding kit (Task 3.4, 2026-08-16
saas-core-optimization brief).

Today, onboarding a new competitor is archaeology: the right rate rule
gets discovered via 429s in production, the right access policy via a
support ticket. This script turns that into a runbook procedure (see
``docs/COMPETITOR_ONBOARDING.md``):

1. Probe a handful of real product-page URLs for the new domain,
   direct-HTTP, with realistic browser headers (never the paid proxy —
   see ``probe_urls``/``_PROBE_HEADERS``).
2. Report which extraction strategy the real chain would use
   (``scrape_core.extraction.pipeline``'s JSON-LD -> EMBEDDED_JSON -> CSS
   order) and the price/currency/availability each probe found.
3. Detect 403/429/challenge-page blocking and recommend an access-ladder
   tier (``AccessStrategy``) plus a conservative default rate rule
   (10 rpm / concurrency 1 / 2 s cooldown).
4. With ``--apply``, seed ``access_policies`` + the domain rate rule
   (``domain_access_rules``) + a strategy profile (``scrape_profiles``)
   for the new competitor, **in one transaction** — one commit, or none.

Dry-run (report only, no database writes) is the default; ``--apply``
requires ``--workspace-id`` (the tenant this competitor belongs to).

Usage::

    # 1. Dry run: probe + review the report.
    python scripts/onboard_competitor.py --domain example.com --urls urls.txt

    # 2. Once the report looks right, seed it for real.
    python scripts/onboard_competitor.py --domain example.com --urls urls.txt \\
        --apply --workspace-id <uuid>

``urls.txt`` is one URL per line (blank lines / ``#`` comments ignored);
the first 5 are probed.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from parsel import Selector
from sqlalchemy import select
from sqlalchemy.orm import Session

# `scripts/` has no __init__.py / installed entry point -- same sys.path
# convention as `scripts/seed_bootstrap.py` / `tests/unit/test_seed_bootstrap.py`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app_shared.enums import AccessStrategy, ExtractionMethod  # noqa: E402
from app_shared.models.access import AccessPolicy, DomainAccessRule  # noqa: E402
from app_shared.models.competitors_matches import Competitor  # noqa: E402
from app_shared.models.scrape_profiles import ScrapeProfile  # noqa: E402
from scrape_core.extraction.jsonld import extract_jsonld  # noqa: E402

# Deliberate private-API coupling, not an oversight: `_iter_documents` is
# `embedded_json.py`'s underscore-prefixed script-tag scanner (not in that
# module's `__all__`). Reused verbatim here rather than reimplemented so a
# "candidate" this probe reports is guaranteed resolvable the same way the
# real `EMBEDDED_JSON` strategy would resolve it (see
# `detect_embedded_json_candidate`'s docstring). This is a same-repo,
# same-commit coupling -- if `embedded_json.py`'s internal scanning shape
# ever changes, this import breaks loudly (ImportError) rather than silently
# drifting out of sync; no attempt is made here to shield against that,
# since the alternative (a second parser) is exactly the drift risk this
# avoids. Do not touch `embedded_json.py` itself to "fix" this coupling
# (Task 3.1 owns that module).
from scrape_core.extraction.embedded_json import _iter_documents  # noqa: E402

__all__ = [
    "ProbeOutcome",
    "Recommendation",
    "ExtractionConfig",
    "ApplyResult",
    "OnboardingError",
    "probe_urls",
    "probe_one",
    "recommend_access",
    "derive_extraction_config",
    "build_report",
    "apply_onboarding",
    "main",
]


# --- probing constants --------------------------------------------------------

#: Realistic browser headers (same values as
#: `apps/workers/app/workers/tasks_strategy._PROBE_HEADERS`, duplicated
#: rather than imported -- that module is a Celery task module with heavier
#: import-time dependencies this standalone script shouldn't need). Never
#: `requests`' default `python-requests/x.y` UA, a canonical bot-block
#: target.
_PROBE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}
_PROBE_TIMEOUT_SECONDS = 15.0
_MAX_PROBE_URLS = 5

#: The brief's conservative default rate rule -- always what is
#: recommended (contracts/domain_access_rules.md's shape), regardless of
#: tier. A stricter posture is a tier change (DIRECT_ONLY -> PROXY_FIRST),
#: not a tighter number here.
_DEFAULT_RATE_RPM = 10
_DEFAULT_CONCURRENCY = 1
_DEFAULT_COOLDOWN_SECONDS = 2

#: Case-insensitive substrings that mark a *200-status* body as a bot
#: challenge/interstitial page rather than real content (Cloudflare/
#: Akamai/Incapsula/PerimeterX) -- a `200` alone does not mean "safe to
#: scrape", matching the 2026-08-12 cost report's live observation that a
#: blocked amazon response still returns a page, just not the product's.
#:
#: Deliberately **multi-word, distinctive phrases only** -- a first draft
#: included the single word "captcha", which false-positived on every
#: Shopify storefront (`stech.ink` included): Shopify ships a
#: `<script id="captcha-bootstrap">` on every page for its own contact-
#: form spam guard, present whether or not *this* fetch was blocked.
#: Single ambiguous words ("access denied", "captcha") are excluded for
#: the same reason -- they appear in real page furniture too often to be
#: a reliable signal on their own.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "verify you are a human",
    "verify you are human",
    "cf-browser-verification",
    "__cf_chl_",
    "pardon our interruption",
    "sorry, you have been blocked",
    "request unsuccessful. incapsula incident id",
    "distil_r_captcha.html",
    "perimeterx",
    # Amazon's own (non-Cloudflare) bot-block page, not a JS challenge --
    # a plain 200 with this exact api-services-support@amazon.com contact
    # line and no product content. Verified live 2026-08-17 against
    # amazon.sa; matches the block the 2026-08-12 cost report's §4.1
    # measured separately (25 proxied attempts, 0 successes).
    "automated access to amazon data",
)

#: Common price-bearing CSS selectors, cheapest/most-specific first. A
#: "candidate" only -- unlike JSON-LD/EMBEDDED_JSON this is a hint for a
#: human to configure `ScrapeProfile.price_selector`, not a claim that
#: `extract_css` would already fire (it needs that selector configured
#: on a profile first).
_CSS_PRICE_SELECTORS: tuple[str, ...] = (
    '[itemprop="price"]',
    'meta[property="product:price:amount"]::attr(content)',
    '[data-price]',
    ".price-amount",
    ".product-price",
    ".current-price",
    "span.price",
    ".price",
    "#price",
)

#: Key-name hints (lower-cased) for the embedded-JSON candidate scan.
_PRICE_KEY_HINTS: tuple[str, ...] = (
    "price",
    "amount",
    "saleprice",
    "offerprice",
    "finalprice",
    "currentprice",
    "sellingprice",
    "value",
)
_CURRENCY_KEY_HINTS: tuple[str, ...] = ("currency", "pricecurrency", "currencyiso", "currencycode")
_EMBEDDED_JSON_SCAN_MAX_NODES = 5000
_EMBEDDED_JSON_SCAN_MAX_DEPTH = 8


class OnboardingError(RuntimeError):
    """Raised for a user-facing onboarding failure (bad input, missing
    --workspace-id, an explicit --competitor-id that doesn't exist, ...).
    Never raised for a probe failure -- that is reported, not fatal."""


# --- probing -------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeOutcome:
    """One URL's probe result -- report step (a)/(b)/(c)."""

    url: str
    status_code: int | None
    error: str | None
    is_blocked: bool
    block_reason: str | None
    extraction_method: str | None  # "JSON_LD" | "EMBEDDED_JSON_CANDIDATE" | "CSS_CANDIDATE" | None
    price: str | None
    currency: str | None
    detail: str | None  # selector_used / json pointer / css selector


def _detect_block(status_code: int, text: str) -> str | None:
    """Return a human reason string if this response looks blocked/
    challenged, else `None`. Checked for *every* status code, not just
    403/429 -- a `200` Cloudflare interstitial is still a block (§ intro)."""
    if status_code in (403, 429):
        return f"HTTP {status_code}"
    lowered = text[:20_000].lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return f"challenge page (matched {marker!r})"
    return None


def _walk_json_for_price_key(
    document: Any, *, pointer: str = "", depth: int = 0, budget: list[int] | None = None
) -> tuple[str, Any] | None:
    """Depth/node-bounded recursive scan for the first dict key whose
    lower-cased name matches `_PRICE_KEY_HINTS` and whose value is a
    numeric-looking scalar. Returns `(json_pointer, value)` or `None`.
    Bounded (`_EMBEDDED_JSON_SCAN_MAX_NODES`/`_MAX_DEPTH`) -- a
    pathological embedded blob must not hang the probe."""
    if budget is None:
        budget = [_EMBEDDED_JSON_SCAN_MAX_NODES]
    if budget[0] <= 0 or depth > _EMBEDDED_JSON_SCAN_MAX_DEPTH:
        return None
    budget[0] -= 1

    if isinstance(document, dict):
        for key, value in document.items():
            if (
                isinstance(key, str)
                and key.lower() in _PRICE_KEY_HINTS
                and isinstance(value, int | float | str)
                and not isinstance(value, bool)
            ):
                text = str(value).strip()
                if text and re.fullmatch(r"\d+(\.\d+)?", text):
                    child_pointer = f"{pointer}/{key}"
                    return child_pointer, value
        for key, value in document.items():
            hit = _walk_json_for_price_key(
                value, pointer=f"{pointer}/{key}", depth=depth + 1, budget=budget
            )
            if hit is not None:
                return hit
    elif isinstance(document, list):
        for index, item in enumerate(document):
            hit = _walk_json_for_price_key(
                item, pointer=f"{pointer}/{index}", depth=depth + 1, budget=budget
            )
            if hit is not None:
                return hit
    return None


def _sibling_currency(document: Any, price_pointer: str) -> str | None:
    """Best-effort: resolve a currency-shaped sibling key next to the
    price hit, e.g. `.../pricing/amount` -> `.../pricing/currency`."""
    parent_pointer = price_pointer.rsplit("/", 1)[0]
    current = document
    for token in [t for t in parent_pointer.split("/") if t]:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return None
    if not isinstance(current, dict):
        return None
    for key, value in current.items():
        if isinstance(key, str) and key.lower() in _CURRENCY_KEY_HINTS and isinstance(value, str):
            return value
    return None


def detect_embedded_json_candidate(html: str) -> tuple[str, str, str | None] | None:
    """Scan the page's embedded JSON blobs (same script-tag priority order
    as the real `EMBEDDED_JSON` strategy, reusing its scanner) for a
    price-shaped key. Returns `(json_pointer, raw_value, currency)` — a
    *candidate* pointer for a human to configure as
    `ScrapeProfile.price_json_path`, not a claim the pipeline would
    already extract it (that strategy is strictly opt-in, per-profile)."""
    selector = Selector(text=html, type="html")
    if selector.type != "html":
        return None
    for document in _iter_documents(selector):
        hit = _walk_json_for_price_key(document)
        if hit is not None:
            pointer, value = hit
            currency = _sibling_currency(document, pointer)
            return pointer, str(value), currency
    return None


def detect_css_candidate(html: str) -> tuple[str, str] | None:
    """First common price selector that matches non-empty numeric-looking
    text. Returns `(css_selector, raw_text)` -- a candidate for
    `ScrapeProfile.price_selector`, same caveat as the embedded-JSON scan."""
    selector = Selector(text=html, type="html")
    if selector.type != "html":
        return None
    for css_query in _CSS_PRICE_SELECTORS:
        matches = selector.css(css_query)
        if not matches:
            continue
        raw = matches.get()
        if raw is None:
            continue
        text = raw.strip() if css_query.endswith("::attr(content)") else None
        if text is None:
            node_text = matches[0].xpath("string(.)").get()
            text = node_text.strip() if node_text else None
        if text and re.search(r"\d", text):
            return css_query, text
    return None


def probe_one(url: str, *, session: requests.Session | None = None) -> ProbeOutcome:
    """One probe attempt: direct HTTP, realistic headers, never the paid
    proxy (self-review requirement — dry-run/probe never touches
    `app_shared.access`). Never raises; every failure mode becomes a
    `ProbeOutcome` field."""
    getter = session.get if session is not None else requests.get
    try:
        response = getter(
            url, headers=_PROBE_HEADERS, timeout=_PROBE_TIMEOUT_SECONDS, allow_redirects=True
        )
    except requests.RequestException as exc:
        return ProbeOutcome(
            url=url,
            status_code=None,
            error=str(exc),
            is_blocked=False,
            block_reason=None,
            extraction_method=None,
            price=None,
            currency=None,
            detail=None,
        )

    block_reason = _detect_block(response.status_code, response.text)
    if block_reason is not None:
        return ProbeOutcome(
            url=url,
            status_code=response.status_code,
            error=None,
            is_blocked=True,
            block_reason=block_reason,
            extraction_method=None,
            price=None,
            currency=None,
            detail=None,
        )

    if not response.ok:
        return ProbeOutcome(
            url=url,
            status_code=response.status_code,
            error=f"HTTP {response.status_code}",
            is_blocked=False,
            block_reason=None,
            extraction_method=None,
            price=None,
            currency=None,
            detail=None,
        )

    jsonld = extract_jsonld(response.text)
    if jsonld is not None:
        return ProbeOutcome(
            url=url,
            status_code=response.status_code,
            error=None,
            is_blocked=False,
            block_reason=None,
            extraction_method=ExtractionMethod.JSON_LD.value,
            price=jsonld.raw_price_text,
            currency=jsonld.currency,
            detail=jsonld.selector_used,
        )

    embedded = detect_embedded_json_candidate(response.text)
    if embedded is not None:
        pointer, value, currency = embedded
        return ProbeOutcome(
            url=url,
            status_code=response.status_code,
            error=None,
            is_blocked=False,
            block_reason=None,
            extraction_method="EMBEDDED_JSON_CANDIDATE",
            price=value,
            currency=currency,
            detail=pointer,
        )

    css = detect_css_candidate(response.text)
    if css is not None:
        css_selector, text = css
        return ProbeOutcome(
            url=url,
            status_code=response.status_code,
            error=None,
            is_blocked=False,
            block_reason=None,
            extraction_method="CSS_CANDIDATE",
            price=text,
            currency=None,
            detail=css_selector,
        )

    return ProbeOutcome(
        url=url,
        status_code=response.status_code,
        error=None,
        is_blocked=False,
        block_reason=None,
        extraction_method=None,
        price=None,
        currency=None,
        detail=None,
    )


def probe_urls(urls: list[str]) -> list[ProbeOutcome]:
    """Probe up to `_MAX_PROBE_URLS` URLs, one `requests.Session` (keep-alive,
    still direct HTTP only -- never a proxy)."""
    with requests.Session() as session:
        return [probe_one(url, session=session) for url in urls[:_MAX_PROBE_URLS]]


# --- recommendation --------------------------------------------------------------


@dataclass(frozen=True)
class Recommendation:
    access_strategy: AccessStrategy
    tier_label: str
    rationale: str
    max_requests_per_minute: int = _DEFAULT_RATE_RPM
    max_concurrent_requests: int = _DEFAULT_CONCURRENCY
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS


def recommend_access(outcomes: list[ProbeOutcome]) -> Recommendation:
    """Access-ladder tier from the probe sample. The rate rule is always
    the brief's conservative default (10 rpm / conc 1 / 2 s cooldown) --
    only the *tier* (whether to proxy at all, and from which attempt)
    varies with what the probes actually saw."""
    total = len(outcomes)
    blocked = sum(1 for o in outcomes if o.is_blocked)
    clean_ok = sum(1 for o in outcomes if o.status_code == 200 and not o.is_blocked)

    if total == 0:
        return Recommendation(
            access_strategy=AccessStrategy.DIRECT_ONLY,
            tier_label="DIRECT_ONLY (no probes run)",
            rationale="No URLs were probed -- defaulting to the least-committal tier. "
            "Run the probe before trusting this.",
        )

    no_strategy_fired = all(o.extraction_method is None for o in outcomes)

    if blocked == 0 and clean_ok == total:
        caveat = ""
        if no_strategy_fired:
            # A known marker (Cloudflare/Akamai/Amazon's own apology page/...)
            # never matched, but every single probe also failed to extract
            # anything -- for 5 different real product URLs that is unusual
            # enough to be worth a human's eyes: it can mean an unrecognized
            # localized/custom block page (`_CHALLENGE_MARKERS` is not
            # exhaustive) as easily as "this domain only has CSS pricing".
            caveat = (
                " CAUTION: none of JSON-LD/embedded-JSON/CSS fired on ANY probe -- "
                "for 5 real product pages that is unusual. Open one probe URL by hand "
                "before trusting DIRECT_ONLY; this could be an unrecognized block page "
                "(_CHALLENGE_MARKERS is a known-marker list, not exhaustive)."
            )
        return Recommendation(
            access_strategy=AccessStrategy.DIRECT_ONLY,
            tier_label="DIRECT_ONLY",
            rationale=f"All {total}/{total} probes returned 200 with no block/challenge "
            f"signal -- direct HTTP looks sufficient.{caveat}",
        )

    if blocked >= (total + 1) // 2:
        return Recommendation(
            access_strategy=AccessStrategy.PROXY_FIRST,
            tier_label="PROXY_FIRST",
            rationale=f"{blocked}/{total} probes were blocked or hit a challenge page -- "
            "this domain wants a proxy from the first attempt, direct is a wasted round-trip.",
        )

    return Recommendation(
        access_strategy=AccessStrategy.DIRECT_THEN_PROXY,
        tier_label="DIRECT_THEN_PROXY",
        rationale=f"{blocked}/{total} probes were blocked/failed but {clean_ok}/{total} "
        "succeeded direct -- direct-first with a proxy retry looks like the right compromise.",
    )


# --- extraction config derivation -------------------------------------------------


@dataclass(frozen=True)
class ExtractionConfig:
    method: ExtractionMethod | None
    price_selector: str | None = None
    price_json_path: str | None = None
    currency_json_path: str | None = None
    notes: str = ""


def derive_extraction_config(outcomes: list[ProbeOutcome]) -> ExtractionConfig:
    """First-hit-wins across the probe sample, same chain order as
    production (`scrape_core.extraction.pipeline._STRATEGIES`): a domain
    where any probe found real JSON-LD needs no profile config at all
    (`ScrapeProfile.jsonld_enabled` defaults `True`); EMBEDDED_JSON/CSS
    candidates need the operator to confirm and copy the pointer/selector
    in — this is a *starting* config, not a guarantee."""
    for outcome in outcomes:
        if outcome.extraction_method == ExtractionMethod.JSON_LD.value:
            return ExtractionConfig(
                method=ExtractionMethod.JSON_LD,
                notes="JSON-LD fired directly -- no profile fields needed "
                "(jsonld_enabled defaults True).",
            )
    for outcome in outcomes:
        if outcome.extraction_method == "EMBEDDED_JSON_CANDIDATE":
            return ExtractionConfig(
                method=ExtractionMethod.EMBEDDED_JSON,
                price_json_path=outcome.detail,
                currency_json_path=None,
                notes=f"Embedded-JSON candidate pointer {outcome.detail!r} found at "
                f"{outcome.url} -- CONFIRM by hand before relying on it.",
            )
    for outcome in outcomes:
        if outcome.extraction_method == "CSS_CANDIDATE":
            return ExtractionConfig(
                method=ExtractionMethod.CSS,
                price_selector=outcome.detail,
                notes=f"CSS candidate selector {outcome.detail!r} found at {outcome.url} -- "
                "CONFIRM by hand before relying on it.",
            )
    return ExtractionConfig(
        method=None,
        notes="No strategy fired on any probed URL -- inspect a probe URL by hand "
        "before onboarding; --apply will seed a profile with no extraction config.",
    )


# --- report ------------------------------------------------------------------------


def build_report(
    *,
    domain: str,
    outcomes: list[ProbeOutcome],
    recommendation: Recommendation,
    extraction_config: ExtractionConfig,
) -> str:
    lines = [f"# Onboarding probe report — {domain}", ""]
    lines.append(f"Probed {len(outcomes)} URL(s), direct HTTP only (no proxy).")
    lines.append("")
    for i, outcome in enumerate(outcomes, start=1):
        lines.append(f"## [{i}] {outcome.url}")
        if outcome.error is not None:
            lines.append(f"  ERROR: {outcome.error}")
        elif outcome.is_blocked:
            lines.append(f"  status={outcome.status_code} BLOCKED: {outcome.block_reason}")
        else:
            lines.append(f"  status={outcome.status_code}")
            if outcome.extraction_method is not None:
                lines.append(
                    f"  strategy={outcome.extraction_method} "
                    f"price={outcome.price!r} currency={outcome.currency!r} "
                    f"detail={outcome.detail!r}"
                )
            else:
                lines.append("  strategy=NONE (JSON-LD / embedded-JSON / CSS all missed)")
        lines.append("")

    lines.append("## Access-ladder recommendation")
    lines.append(f"  tier: {recommendation.tier_label}")
    lines.append(f"  rationale: {recommendation.rationale}")
    lines.append(
        "  rate rule: "
        f"{recommendation.max_requests_per_minute} rpm / "
        f"concurrency {recommendation.max_concurrent_requests} / "
        f"{recommendation.cooldown_seconds}s cooldown"
    )
    lines.append("")

    lines.append("## Extraction strategy")
    lines.append(f"  method: {extraction_config.method.value if extraction_config.method else 'NONE'}")
    if extraction_config.price_selector:
        lines.append(f"  price_selector: {extraction_config.price_selector!r}")
    if extraction_config.price_json_path:
        lines.append(f"  price_json_path: {extraction_config.price_json_path!r}")
    lines.append(f"  notes: {extraction_config.notes}")
    lines.append("")

    return "\n".join(lines)


# --- apply (one transaction) --------------------------------------------------------
#
# Escalation on re-apply (2026-08-17 review finding, Important): a first
# version's `_get_or_create_*` helpers returned `existing, False` and
# silently discarded the new run's recommendation whenever a row already
# existed -- so re-running `--apply` against an already-onboarded domain
# after real-world blocking (exactly the scenario
# `docs/COMPETITOR_ONBOARDING.md` §7 tells the operator to do: "go back to
# §3 with a PROXY_FIRST/DIRECT_THEN_PROXY tier") printed "APPLIED (one
# transaction)" while changing nothing. Fixed by choosing (a) from the
# review's two options: `--update-existing` makes a reused row's mutable
# fields track the new recommendation (still one transaction; the diff is
# always computed and always printed, whether or not it was applied), with
# an explicit `--allow-downgrade` guard so an operator can never *silently*
# loosen an access tier or rate rule by re-running with a stale/regressed
# report -- `apply_onboarding` raises before mutating anything if a
# downgrade is detected and `--allow-downgrade` wasn't passed (one commit
# or none still holds: the raise happens before any `session.add`/attribute
# mutation for that call). Without `--update-existing` at all, behavior is
# unchanged (existing rows are reused as-is) but no longer silent: the diff
# is still computed and surfaced, and `main()` prints an unmissable
# "EXISTING ROW NOT UPDATED" block naming exactly what changed and which
# flag closes the gap.


#: Coarse "how committed to a proxy is this tier" ordering, used only to
#: detect a downgrade on re-`--apply` (never to pick a tier -- `recommend_access`
#: alone does that). `RESIDENTIAL_ONLY`/`BROWSER_FALLBACK` outrank
#: `PROXY_FIRST` even though `recommend_access` never recommends either --
#: a human may have hand-set one, and a probe-driven re-apply must not
#: quietly walk that back either.
_ACCESS_STRATEGY_RANK: dict[AccessStrategy, int] = {
    AccessStrategy.DIRECT_ONLY: 0,
    AccessStrategy.DIRECT_THEN_PROXY: 1,
    AccessStrategy.PROXY_FIRST: 2,
    AccessStrategy.RESIDENTIAL_ONLY: 3,
    AccessStrategy.BROWSER_FALLBACK: 4,
}


@dataclass(frozen=True)
class FieldDiff:
    """One field's old -> new value, surfaced whether or not it was applied
    (`ApplyResult.*_updated` says which)."""

    field: str
    old: Any
    new: Any


def _access_policy_diff(existing: AccessPolicy, recommendation: Recommendation) -> tuple[FieldDiff, ...]:
    if existing.strategy == recommendation.access_strategy:
        return ()
    return (
        FieldDiff(
            "strategy",
            existing.strategy.value if existing.strategy is not None else None,
            recommendation.access_strategy.value,
        ),
    )


def _access_policy_diff_is_downgrade(diff: tuple[FieldDiff, ...]) -> bool:
    for field_diff in diff:
        if field_diff.field != "strategy":
            continue
        old_rank = _ACCESS_STRATEGY_RANK[AccessStrategy(field_diff.old)]
        new_rank = _ACCESS_STRATEGY_RANK[AccessStrategy(field_diff.new)]
        if new_rank < old_rank:
            return True
    return False


def _rule_diff(existing: DomainAccessRule, recommendation: Recommendation) -> tuple[FieldDiff, ...]:
    diffs: list[FieldDiff] = []
    if existing.max_requests_per_minute != recommendation.max_requests_per_minute:
        diffs.append(
            FieldDiff(
                "max_requests_per_minute",
                existing.max_requests_per_minute,
                recommendation.max_requests_per_minute,
            )
        )
    if existing.max_concurrent_requests != recommendation.max_concurrent_requests:
        diffs.append(
            FieldDiff(
                "max_concurrent_requests",
                existing.max_concurrent_requests,
                recommendation.max_concurrent_requests,
            )
        )
    if existing.cooldown_seconds != recommendation.cooldown_seconds:
        diffs.append(
            FieldDiff("cooldown_seconds", existing.cooldown_seconds, recommendation.cooldown_seconds)
        )
    return tuple(diffs)


def _rule_diff_is_downgrade(diff: tuple[FieldDiff, ...]) -> bool:
    """A rate-rule change is a downgrade if it moves toward *faster/less
    safe* on any single field -- higher rpm, higher concurrency, or a
    shorter cooldown than what's already there."""
    for field_diff in diff:
        if field_diff.field in ("max_requests_per_minute", "max_concurrent_requests"):
            if field_diff.new > field_diff.old:
                return True
        elif field_diff.field == "cooldown_seconds":
            if field_diff.new < field_diff.old:
                return True
    return False


def _profile_diff(
    existing: ScrapeProfile, extraction_config: ExtractionConfig
) -> tuple[FieldDiff, ...]:
    """Gap-filling only -- never proposes overwriting an already-populated
    field. A configured `price_selector`/`price_json_path` may be a human's
    hand-tuned confirmation (`docs/COMPETITOR_ONBOARDING.md` §2's "CONFIRM
    by hand" step); a fresh probe run finding nothing (or a different
    candidate) must never silently clobber it. No downgrade concept
    applies here -- `--allow-downgrade` is never required for a profile
    diff."""
    if extraction_config.method is None:
        return ()
    diffs: list[FieldDiff] = []
    for field, new_value in (
        ("price_selector", extraction_config.price_selector),
        ("price_json_path", extraction_config.price_json_path),
        ("currency_json_path", extraction_config.currency_json_path),
    ):
        old_value = getattr(existing, field)
        if old_value is None and new_value is not None:
            diffs.append(FieldDiff(field, old_value, new_value))
    return tuple(diffs)


@dataclass(frozen=True)
class ApplyResult:
    competitor_id: uuid.UUID
    competitor_created: bool
    access_policy_id: uuid.UUID
    access_policy_created: bool
    access_policy_updated: bool
    access_policy_diff: tuple[FieldDiff, ...]
    domain_access_rule_id: uuid.UUID
    domain_access_rule_created: bool
    domain_access_rule_updated: bool
    domain_access_rule_diff: tuple[FieldDiff, ...]
    scrape_profile_id: uuid.UUID
    scrape_profile_created: bool
    scrape_profile_updated: bool
    scrape_profile_diff: tuple[FieldDiff, ...]


def _get_or_create_competitor(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    domain: str,
    name: str,
    competitor_id: uuid.UUID | None,
) -> tuple[Competitor, bool]:
    if competitor_id is not None:
        existing = session.execute(
            select(Competitor).where(
                Competitor.workspace_id == workspace_id, Competitor.id == competitor_id
            )
        ).scalar_one_or_none()
        if existing is None:
            raise OnboardingError(
                f"--competitor-id {competitor_id} does not exist in workspace {workspace_id}"
            )
        return existing, False

    existing = session.execute(
        select(Competitor).where(Competitor.workspace_id == workspace_id, Competitor.domain == domain)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    competitor = Competitor(workspace_id=workspace_id, name=name, domain=domain)
    session.add(competitor)
    session.flush()
    return competitor, True


def _get_or_create_or_update_access_policy(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    domain: str,
    recommendation: Recommendation,
    update_existing: bool,
    allow_downgrade: bool,
) -> tuple[AccessPolicy, bool, bool, tuple[FieldDiff, ...]]:
    """Returns `(policy, created, updated, diff)`. `diff` is always the
    (possibly empty) difference between the stored row and `recommendation`
    -- computed even when `update_existing` is `False`, so the caller can
    still surface "this row is stale" without having touched it."""
    name = f"{domain}-access-policy"
    existing = session.execute(
        select(AccessPolicy).where(AccessPolicy.workspace_id == workspace_id, AccessPolicy.name == name)
    ).scalar_one_or_none()
    if existing is None:
        strategy = recommendation.access_strategy
        policy = AccessPolicy(
            workspace_id=workspace_id,
            name=name,
            strategy=strategy,
            use_proxy_on_first_attempt=strategy
            in (AccessStrategy.PROXY_FIRST, AccessStrategy.RESIDENTIAL_ONLY),
            use_proxy_on_retry=strategy != AccessStrategy.DIRECT_ONLY,
        )
        session.add(policy)
        session.flush()
        return policy, True, False, ()

    diff = _access_policy_diff(existing, recommendation)
    if not diff or not update_existing:
        return existing, False, False, diff

    if not allow_downgrade and _access_policy_diff_is_downgrade(diff):
        raise OnboardingError(
            f"--update-existing would downgrade access policy {name!r} "
            f"({diff[0].old} -> {diff[0].new}) -- pass --allow-downgrade to permit this. "
            "Refusing before writing anything."
        )

    strategy = recommendation.access_strategy
    existing.strategy = strategy
    existing.use_proxy_on_first_attempt = strategy in (
        AccessStrategy.PROXY_FIRST,
        AccessStrategy.RESIDENTIAL_ONLY,
    )
    existing.use_proxy_on_retry = strategy != AccessStrategy.DIRECT_ONLY
    session.flush()
    return existing, False, True, diff


def _get_or_create_or_update_domain_access_rule(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    domain: str,
    access_policy_id: uuid.UUID,
    recommendation: Recommendation,
    update_existing: bool,
    allow_downgrade: bool,
) -> tuple[DomainAccessRule, bool, bool, tuple[FieldDiff, ...]]:
    existing = session.execute(
        select(DomainAccessRule).where(
            DomainAccessRule.workspace_id == workspace_id,
            DomainAccessRule.competitor_id == competitor_id,
            DomainAccessRule.domain == domain,
            DomainAccessRule.url_pattern.is_(None),
        )
    ).scalar_one_or_none()
    if existing is None:
        rule = DomainAccessRule(
            workspace_id=workspace_id,
            competitor_id=competitor_id,
            domain=domain,
            url_pattern=None,
            access_policy_id=access_policy_id,
            max_concurrent_requests=recommendation.max_concurrent_requests,
            max_requests_per_minute=recommendation.max_requests_per_minute,
            cooldown_seconds=recommendation.cooldown_seconds,
        )
        session.add(rule)
        session.flush()
        return rule, True, False, ()

    diff = _rule_diff(existing, recommendation)
    if not diff or not update_existing:
        return existing, False, False, diff

    if not allow_downgrade and _rule_diff_is_downgrade(diff):
        readable = ", ".join(f"{d.field} {d.old} -> {d.new}" for d in diff)
        raise OnboardingError(
            f"--update-existing would loosen the domain_access_rule for {domain!r} "
            f"({readable}) -- pass --allow-downgrade to permit this. "
            "Refusing before writing anything."
        )

    existing.access_policy_id = access_policy_id
    existing.max_concurrent_requests = recommendation.max_concurrent_requests
    existing.max_requests_per_minute = recommendation.max_requests_per_minute
    existing.cooldown_seconds = recommendation.cooldown_seconds
    session.flush()
    return existing, False, True, diff


def _get_or_create_or_update_scrape_profile(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    domain: str,
    extraction_config: ExtractionConfig,
    update_existing: bool,
) -> tuple[ScrapeProfile, bool, bool, tuple[FieldDiff, ...]]:
    name = f"{domain}-profile"
    existing = session.execute(
        select(ScrapeProfile).where(ScrapeProfile.workspace_id == workspace_id, ScrapeProfile.name == name)
    ).scalar_one_or_none()
    if existing is None:
        profile = ScrapeProfile(
            workspace_id=workspace_id,
            name=name,
            price_selector=extraction_config.price_selector,
            price_json_path=extraction_config.price_json_path,
            currency_json_path=extraction_config.currency_json_path,
        )
        session.add(profile)
        session.flush()
        return profile, True, False, ()

    diff = _profile_diff(existing, extraction_config)
    if not diff or not update_existing:
        return existing, False, False, diff

    # Gap-filling only (`_profile_diff` never proposes overwriting a
    # populated field) -- no downgrade guard applies.
    for field_diff in diff:
        setattr(existing, field_diff.field, field_diff.new)
    session.flush()
    return existing, False, True, diff


def apply_onboarding(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    domain: str,
    competitor_name: str,
    recommendation: Recommendation,
    extraction_config: ExtractionConfig,
    competitor_id: uuid.UUID | None = None,
    update_existing: bool = False,
    allow_downgrade: bool = False,
) -> ApplyResult:
    """Get-or-create-or-update the competitor, access policy, domain rate
    rule, and strategy profile, wiring the competitor's
    `default_access_policy_id`/`default_scrape_profile_id` to the
    (possibly freshly created) rows.

    Idempotent (safe to re-run) and **does not commit** — mirrors
    `scripts/seed_bootstrap.run_seed`'s convention: the caller controls the
    transaction boundary, so a probe/DB error (or a refused downgrade)
    partway through never leaves a partial write (self-review requirement:
    one commit, or none, for the whole onboarding).

    `update_existing=False` (the default): an already-onboarded domain's
    access policy / rate rule / profile are reused completely unchanged,
    even if `recommendation`/`extraction_config` now disagrees with what's
    stored — but the disagreement is never silent: `ApplyResult.*_diff`
    always carries it, for `main()` to print as an "EXISTING ROW NOT
    UPDATED" warning (2026-08-17 review finding — a prior version discarded
    this silently, which broke exactly the escalation workflow
    `docs/COMPETITOR_ONBOARDING.md` §7 tells an operator to run).

    `update_existing=True`: a reused row's mutable fields are written to
    match the new recommendation (still inside this one transaction), and
    `*_updated`/`*_diff` report what changed. `allow_downgrade=False` (the
    default) refuses the *entire* apply — raising before any row is
    mutated — the moment either the access policy's tier or the rate rule
    would move to a less-safe value than what's already stored; pass
    `allow_downgrade=True` to permit that deliberately.
    """
    competitor, competitor_created = _get_or_create_competitor(
        session,
        workspace_id=workspace_id,
        domain=domain,
        name=competitor_name,
        competitor_id=competitor_id,
    )
    policy, policy_created, policy_updated, policy_diff = _get_or_create_or_update_access_policy(
        session,
        workspace_id=workspace_id,
        domain=domain,
        recommendation=recommendation,
        update_existing=update_existing,
        allow_downgrade=allow_downgrade,
    )
    rule, rule_created, rule_updated, rule_diff = _get_or_create_or_update_domain_access_rule(
        session,
        workspace_id=workspace_id,
        competitor_id=competitor.id,
        domain=domain,
        access_policy_id=policy.id,
        recommendation=recommendation,
        update_existing=update_existing,
        allow_downgrade=allow_downgrade,
    )
    profile, profile_created, profile_updated, profile_diff = _get_or_create_or_update_scrape_profile(
        session,
        workspace_id=workspace_id,
        domain=domain,
        extraction_config=extraction_config,
        update_existing=update_existing,
    )

    competitor.default_access_policy_id = policy.id
    competitor.default_scrape_profile_id = profile.id

    return ApplyResult(
        competitor_id=competitor.id,
        competitor_created=competitor_created,
        access_policy_id=policy.id,
        access_policy_created=policy_created,
        access_policy_updated=policy_updated,
        access_policy_diff=policy_diff,
        domain_access_rule_id=rule.id,
        domain_access_rule_created=rule_created,
        domain_access_rule_updated=rule_updated,
        domain_access_rule_diff=rule_diff,
        scrape_profile_id=profile.id,
        scrape_profile_created=profile_created,
        scrape_profile_updated=profile_updated,
        scrape_profile_diff=profile_diff,
    )


# --- CLI -----------------------------------------------------------------------------


def _read_urls(path: Path) -> list[str]:
    urls = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        urls.append(stripped)
    return urls


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--domain", required=True, help="Bare competitor domain, e.g. example.com")
    parser.add_argument(
        "--urls", required=True, type=Path, help="Path to a file of sample product URLs, one per line"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Seed access_policies + the domain rate rule + a strategy profile "
        "(one transaction). Without this flag, the script only probes and reports.",
    )
    parser.add_argument("--workspace-id", type=uuid.UUID, help="Required with --apply.")
    parser.add_argument(
        "--competitor-id",
        type=uuid.UUID,
        default=None,
        help="Reuse an existing competitor row instead of get-or-create-by-domain.",
    )
    parser.add_argument(
        "--name", default=None, help="Competitor display name (default: --domain's value)."
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="With --apply against an already-onboarded domain: write the new "
        "recommendation's access-policy tier / rate rule / extraction config onto "
        "the existing rows instead of leaving them untouched. Still one transaction. "
        "Without this flag, an existing row is reused as-is and any disagreement with "
        "the new recommendation is only reported, never applied.",
    )
    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Only meaningful with --update-existing: permit an update that would "
        "loosen the access tier (e.g. PROXY_FIRST -> DIRECT_ONLY) or the rate rule "
        "(higher rpm/concurrency, shorter cooldown) versus what's currently stored. "
        "Without it, --update-existing refuses the entire apply the moment it detects "
        "a downgrade -- before writing anything.",
    )
    return parser.parse_args(argv)


def _print_field_row(label: str, row_id: uuid.UUID, created: bool, updated: bool, diff: tuple[FieldDiff, ...]) -> None:
    print(f"  {label:<24}= {row_id} (created={created})")
    if updated:
        for field_diff in diff:
            print(f"      escalated: {field_diff.field}: {field_diff.old!r} -> {field_diff.new!r}")
    elif diff:
        print("      *** EXISTING ROW NOT UPDATED *** -- the new recommendation disagrees:")
        for field_diff in diff:
            print(f"          {field_diff.field}: {field_diff.old!r} -> {field_diff.new!r}")
        print(
            "      Re-run with --apply --update-existing (add --allow-downgrade too "
            "if this is a downgrade) to escalate."
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    urls = _read_urls(args.urls)
    if not urls:
        print(f"error: no URLs found in {args.urls}", file=sys.stderr)
        return 2

    outcomes = probe_urls(urls)
    recommendation = recommend_access(outcomes)
    extraction_config = derive_extraction_config(outcomes)
    report = build_report(
        domain=args.domain,
        outcomes=outcomes,
        recommendation=recommendation,
        extraction_config=extraction_config,
    )
    print(report)

    if not args.apply:
        print("DRY RUN — no database writes. Re-run with --apply --workspace-id <uuid> to seed.")
        return 0

    if args.workspace_id is None:
        print("error: --apply requires --workspace-id", file=sys.stderr)
        return 2

    if args.allow_downgrade and not args.update_existing:
        print(
            "note: --allow-downgrade has no effect without --update-existing "
            "(nothing is written to an existing row either way).",
            file=sys.stderr,
        )

    from app_shared.database import get_session, set_workspace_context  # deferred: no DB import for dry-run

    with get_session() as session:
        set_workspace_context(session, args.workspace_id)
        try:
            result = apply_onboarding(
                session,
                workspace_id=args.workspace_id,
                domain=args.domain,
                competitor_name=args.name or args.domain,
                recommendation=recommendation,
                extraction_config=extraction_config,
                competitor_id=args.competitor_id,
                update_existing=args.update_existing,
                allow_downgrade=args.allow_downgrade,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise

    print("APPLIED (one transaction):")
    print(f"  competitor_id           = {result.competitor_id} (created={result.competitor_created})")
    _print_field_row(
        "access_policy_id", result.access_policy_id, result.access_policy_created,
        result.access_policy_updated, result.access_policy_diff,
    )
    _print_field_row(
        "domain_access_rule_id", result.domain_access_rule_id, result.domain_access_rule_created,
        result.domain_access_rule_updated, result.domain_access_rule_diff,
    )
    _print_field_row(
        "scrape_profile_id", result.scrape_profile_id, result.scrape_profile_created,
        result.scrape_profile_updated, result.scrape_profile_diff,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
