#!/usr/bin/env python3
"""probe_amazon_http_leg.py -- EPA C2: does Amazon's HTTP leg still work
under the resolved PRODUCTION profile (deep dive Sec12 item 3, audit F08)?

WHAT THIS SCRIPT IS -- AND WHAT IT DELIBERATELY IS NOT
=======================================================

The 2026-08 evidence (``evidence/cost-performance-2026-09-07``) found Amazon
answering a Scrapy-native fetch with a CAPTCHA wall at HTTP 200, and traced
the real cause to the Scrapy/Twisted HTTP client itself, not headers
(``apps/scrapers/price_monitor/settings.py``'s ``DEFAULT_REQUEST_HEADERS``
docstring). The fix already shipped is a Chrome-impersonating transport
(``scrape_core.impersonate.ImpersonatingDownloadHandler``, ``curl_cffi``)
selected per domain via ``Settings.SCRAPE_IMPERSONATE_DOMAINS`` --
``amazon.sa`` is on that list today. **Nobody has measured, with that exact
transport and the resolved production ``AccessPolicy``, what fraction of
Amazon fetches over plain HTTP (direct or proxied) actually yield a price**
-- that is what decides whether Amazon should stay HTTP-first at all
(C4's ``domain_playbooks``) or move to browser-first with an HTTP recovery
probe. This script is the instrument for that measurement.

It draws a target sample and, for a real run, fetches each target
``--runs`` times over BOTH the direct leg and the proxied leg, using
:func:`resolve_production_profile` (built from ``scrape_core.targets``'s own
``resolve_effective_policy``/``next_attempt``/``assign_proxy`` -- the exact
functions the real spider's ``_prepare_dispatch`` calls, see that function's
docstring in ``scrape_core.targets``) so the probe's request is not a
hand-rolled stand-in: same impersonation decision, same proxy
credential/session shape as production would use for the same target today.

**It never spends money on its own initiative.** ``--max-usd`` is a required
argument with no default -- omitting it is a parse-time refusal
(:func:`build_parser`), matching this run's ASSUMPTIONS.md answer 3 (every
money-capable script ships with a required cap even while the run itself is
deferred). In THIS EPA run the live fetch loop (Step 2 of the plan's C2 task)
is not invoked at all: no proxied request, no direct request to Amazon, no
database session is opened by anything this task's tests exercise. The
report-math core (:func:`summarize_attempts` and friends) is what
``tests/unit/test_probe_amazon_report.py`` verifies, against fixture
attempts that never touched a network.

THE PER-ATTEMPT RECORD (acceptance criteria, verbatim)
=======================================================

Every :class:`ProbeAttempt` in a real run's report carries exactly the five
things the task requires:

* ``status`` -- the HTTP status code reached, or ``None`` for a
  transport-level failure (timeout, DNS, proxy) that never got one;
* ``title_present`` -- whether the fetched page carried *some* product
  identity signal (:func:`scrape_core.errors.page_carries_product_identity`'s
  same "a wall vs a product page" question, folded into
  ``classify_extraction_outcome`` below rather than re-implemented);
* ``price`` -- the price extracted **via the resolved profile**, i.e.
  whatever extraction method that profile's ``DomainStrategyProfile``
  currently prefers (SPEC-10/12), not a probe-specific parser. ``None``
  means no price was found (whether or not a title was);
* ``classification`` -- the C1 :class:`~app_shared.enums.ScrapeErrorCode`
  this attempt classifies as, via :func:`classify_probe_attempt` (below),
  which is a thin dispatcher over the *existing*
  ``scrape_core.errors.classify_http_status``/``classify_exception`` and
  the **C1** ``classify_extraction_outcome`` -- never a second,
  probe-specific classifier that could drift from what production stamps
  on ``request_attempts.error_code``;
* ``wire_bytes`` -- A7's ``WireBytesMiddleware``/``compute_wire_bytes()``
  wire-size measurement for the attempt, or ``None`` when the transport
  never got far enough to measure it.

THE DECISION TABLE (plan-authoritative, printed verbatim into the doc)
=======================================================================

Computed by :func:`classify_decision` from the **combined** (direct +
proxied) HTTP price-success percentage across all runs:

* ``>= 80%`` -> HTTP-first stays, browser fallback capped at 1 per refresh.
* ``30% - 80%`` -> HTTP-first only for the classified "HTTP-works" subset
  (C4's response-signature classification), with sampled probes.
* ``< 30%`` -> browser-first for Amazon, with a 5% HTTP recovery probe.

USAGE
=====

Draw the target sample (reads the DB, spends nothing, never fetches
Amazon)::

    python scripts/probe_amazon_http_leg.py --dry-run \\
        --targets 50 --runs 3 --max-usd 1.00 --report out.json

The **deferred** live run (owner-executed only, never from this task)::

    python scripts/probe_amazon_http_leg.py \\
        --targets 50 --runs 3 --max-usd 1.00 \\
        --workspace <workspace-uuid> --report out.json

SECRET DISCIPLINE
=================

No DSN, password, or proxy credential is ever printed or written to the
report -- ``_open_session`` delegates to ``scripts.run_gate_d_canary``'s
existing DSN resolution rather than re-implementing it, matching
``scripts/canary_document_only_browser.py``'s own rule.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

# `scripts/` has no __init__.py; the sibling-module import below needs the
# repository root on the path when this file is run as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = [
    "PROBE_TOOL_VERSION",
    "DEFAULT_DOMAIN",
    "DEFAULT_TARGETS",
    "DEFAULT_RUNS",
    "DEFAULT_REPORT",
    "HTTP_LEGS",
    "HTTP_PRICE_SUCCESS_HIGH",
    "HTTP_PRICE_SUCCESS_LOW",
    "DIRECT_LEG_COST_ESTIMATE_USD",
    "PROXIED_LEG_COST_ESTIMATE_USD",
    "ProbeAttempt",
    "LegSummary",
    "ProbeReport",
    "SpendGuard",
    "SpendCapExceeded",
    "ResolvedProbeProfile",
    "classify_probe_attempt",
    "summarize_leg",
    "summarize_attempts",
    "classify_decision",
    "report_to_dict",
    "estimate_run_cost_usd",
    "build_parser",
    "main",
    "resolve_production_profile",
    "draw_target_set",
]

PROBE_TOOL_VERSION = "1"

#: The domain the 2026-09 decision record is about. Overridable via
#: `--domain` (e.g. `amazon.ae`) -- never hardcoded into any resolution
#: function below, matching `canary_document_only_browser.py`'s
#: `DOCUMENT_ONLY_DOMAIN` convention.
DEFAULT_DOMAIN = "amazon.sa"

DEFAULT_TARGETS = 50
DEFAULT_RUNS = 3
DEFAULT_REPORT = "evidence/amazon-http-leg-2026-09/report.json"

#: The two HTTP-transport legs this probe measures. BROWSER is out of scope
#: (that is C3's canary) -- this task is specifically "the HTTP leg".
HTTP_LEGS: tuple[str, ...] = ("direct", "proxied")

#: Decision-table thresholds (plan-authoritative, see module docstring).
HTTP_PRICE_SUCCESS_HIGH = 80.0
HTTP_PRICE_SUCCESS_LOW = 30.0

#: Conservative per-request cost estimates for :class:`SpendGuard`'s
#: pre-flight sizing -- NOT the ledger's bill (the ledger is authoritative;
#: this is only how the harness stops itself before ever approaching
#: `--max-usd`). DIRECT is the fleet's own egress, billed nowhere per
#: request (`browser_resource_policy`'s "DIRECT... costs nothing per byte"
#: convention). PROXIED uses the upper end of the measured Amazon range
#: from `cost-measured-2026-09-03` ($0.00019-$0.0025/request) so the guard
#: never under-counts.
DIRECT_LEG_COST_ESTIMATE_USD = 0.0
PROXIED_LEG_COST_ESTIMATE_USD = 0.0025


# --- pure core ---------------------------------------------------------------


@dataclass(frozen=True)
class ProbeAttempt:
    """One physical fetch attempt, exactly the five acceptance-criteria
    fields plus enough identity to group by leg/target/run.

    `status` is `None` for a transport failure that never reached a
    response (timeout/DNS/proxy) -- never coerced to e.g. `0`, so a
    caller can tell "no response" from "a response with no status".
    `classification` is `None` for a fully successful attempt (a price
    was extracted) -- see :func:`classify_probe_attempt`.
    """

    target: str
    leg: str  # one of HTTP_LEGS
    run: int
    status: int | None
    title_present: bool
    price: float | None
    classification: str | None  # a `ScrapeErrorCode.value`, or `None` = OK
    wire_bytes: int | None
    resolved_profile_id: str | None = None


@dataclass(frozen=True)
class LegSummary:
    """Aggregate report-math for one leg's attempts."""

    leg: str
    attempts: int
    price_success: int
    price_success_pct: float
    title_present_pct: float
    wire_bytes_total: int
    wire_bytes_measured: int
    wire_bytes_avg: float
    classification_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeReport:
    """The full report :func:`summarize_attempts` produces."""

    generated_at: str
    domain: str
    targets: int
    runs: int
    max_usd: float
    legs: dict[str, LegSummary]
    overall_price_success_pct: float
    decision_case: str
    decision_detail: str


def classify_probe_attempt(
    *,
    status_code: int | None,
    title_present: bool,
    price_found: bool,
    exception: BaseException | None = None,
    hostname: str | None = None,
) -> "ScrapeErrorCode | None":  # noqa: F821 - see lazy import below
    """The C1 classification for one attempt -- a thin dispatcher, never a
    second classifier. Delegates to the existing
    ``scrape_core.errors.classify_exception`` for a transport exception,
    else to C1's ``classify_extraction_outcome`` for a completed response
    (which itself defers to ``classify_http_status`` first). Returns
    ``None`` for a genuine success (title AND price), matching both
    delegates' own "``None`` == success" convention.
    """
    from scrape_core.errors import classify_exception, classify_extraction_outcome

    if exception is not None:
        return classify_exception(exception, hostname=hostname).value
    if status_code is None:
        from app_shared.enums import ScrapeErrorCode

        return ScrapeErrorCode.UNKNOWN_ERROR.value
    outcome = classify_extraction_outcome(
        status_code=status_code,
        price_found=price_found,
        has_product_identity=title_present,
    )
    return outcome.value if outcome is not None else None


def summarize_leg(attempts: Sequence[ProbeAttempt]) -> LegSummary:
    """Aggregate one leg's attempts into the numbers the decision table reads.

    `wire_bytes_avg` divides by how many attempts actually MEASURED bytes
    (`wire_bytes_measured`), never by the raw attempt count -- an attempt
    that never got a response contributes nothing to the average rather
    than silently dragging it toward zero.
    """
    leg = attempts[0].leg if attempts else ""
    n = len(attempts)
    price_success = sum(1 for a in attempts if a.price is not None)
    title_present = sum(1 for a in attempts if a.title_present)
    measured_bytes = [a.wire_bytes for a in attempts if a.wire_bytes is not None]
    counts: dict[str, int] = {}
    for a in attempts:
        key = a.classification if a.classification is not None else "OK"
        counts[key] = counts.get(key, 0) + 1
    return LegSummary(
        leg=leg,
        attempts=n,
        price_success=price_success,
        price_success_pct=(100.0 * price_success / n) if n else 0.0,
        title_present_pct=(100.0 * title_present / n) if n else 0.0,
        wire_bytes_total=sum(measured_bytes),
        wire_bytes_measured=len(measured_bytes),
        wire_bytes_avg=(sum(measured_bytes) / len(measured_bytes)) if measured_bytes else 0.0,
        classification_counts=counts,
    )


def classify_decision(price_success_pct: float) -> tuple[str, str]:
    """The plan's decision table -- `(case, human-readable detail)`.

    Boundaries are inclusive on the LOW end of each band (a run landing
    exactly on 80.0 or 30.0 takes the higher band), matching the plan's
    "`>= 80%`" / "`30-80%`" / "`< 30%`" wording literally.
    """
    if price_success_pct >= HTTP_PRICE_SUCCESS_HIGH:
        return (
            "http_first_browser_fallback_capped_1",
            f"HTTP price success {price_success_pct:.1f}% >= {HTTP_PRICE_SUCCESS_HIGH:.0f}%: "
            "HTTP-first stays, browser fallback capped at 1 per refresh.",
        )
    if price_success_pct >= HTTP_PRICE_SUCCESS_LOW:
        return (
            "http_first_for_http_works_subset",
            f"HTTP price success {price_success_pct:.1f}% in "
            f"[{HTTP_PRICE_SUCCESS_LOW:.0f}%, {HTTP_PRICE_SUCCESS_HIGH:.0f}%): "
            "HTTP-first only for the classified 'HTTP-works' subset, sampled probes.",
        )
    return (
        "browser_first_with_5pct_recovery_probe",
        f"HTTP price success {price_success_pct:.1f}% < {HTTP_PRICE_SUCCESS_LOW:.0f}%: "
        "browser-first for Amazon, with a 5% HTTP recovery probe.",
    )


def summarize_attempts(
    attempts: Sequence[ProbeAttempt],
    *,
    domain: str,
    targets: int,
    runs: int,
    max_usd: float,
    generated_at: str | None = None,
) -> ProbeReport:
    """The full report: per-leg summaries, the combined decision figure, and
    the decision-table verdict it implies. `attempts` may span any subset
    of `HTTP_LEGS`; a leg with zero attempts is simply absent from
    `legs` rather than reported as a fabricated 0%.
    """
    by_leg: dict[str, list[ProbeAttempt]] = {}
    for attempt in attempts:
        by_leg.setdefault(attempt.leg, []).append(attempt)
    legs = {leg: summarize_leg(items) for leg, items in by_leg.items()}
    overall = summarize_leg(list(attempts))
    case, detail = classify_decision(overall.price_success_pct)
    return ProbeReport(
        generated_at=generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        domain=domain,
        targets=targets,
        runs=runs,
        max_usd=max_usd,
        legs=legs,
        overall_price_success_pct=overall.price_success_pct,
        decision_case=case,
        decision_detail=detail,
    )


def report_to_dict(report: ProbeReport) -> dict[str, Any]:
    """JSON-serializable rendering for `--report out.json`."""
    return {
        "tool_version": PROBE_TOOL_VERSION,
        "generated_at": report.generated_at,
        "domain": report.domain,
        "targets": report.targets,
        "runs": report.runs,
        "max_usd": report.max_usd,
        "overall_price_success_pct": report.overall_price_success_pct,
        "decision_case": report.decision_case,
        "decision_detail": report.decision_detail,
        "legs": {
            leg: {
                "attempts": s.attempts,
                "price_success": s.price_success,
                "price_success_pct": s.price_success_pct,
                "title_present_pct": s.title_present_pct,
                "wire_bytes_total": s.wire_bytes_total,
                "wire_bytes_measured": s.wire_bytes_measured,
                "wire_bytes_avg": s.wire_bytes_avg,
                "classification_counts": s.classification_counts,
            }
            for leg, s in report.legs.items()
        },
    }


class SpendCapExceeded(Exception):
    """Raised by :class:`SpendGuard` before a charge would exceed the cap."""


@dataclass
class SpendGuard:
    """A conservative, self-imposed spend cap -- separate from (and always
    tighter than) the ledger's real billing. `charge` raises BEFORE
    recording anything that would exceed `max_usd`, so the guard's own
    running total never itself exceeds the cap even by one increment.
    """

    max_usd: float
    spent_usd: float = 0.0

    def charge(self, usd: float, *, reason: str) -> None:
        if self.spent_usd + usd > self.max_usd:
            raise SpendCapExceeded(
                f"refusing to spend ${self.spent_usd + usd:.4f} "
                f"(cap ${self.max_usd:.2f}) for {reason}"
            )
        self.spent_usd += usd


def estimate_run_cost_usd(*, targets: int, runs: int, legs: Sequence[str] = HTTP_LEGS) -> float:
    """Conservative pre-flight cost estimate for `targets * runs` attempts
    over `legs` -- used only to size/validate `--max-usd` before a live
    run starts, per leg's :data:`*_COST_ESTIMATE_USD`.
    """
    per_leg_cost = {
        "direct": DIRECT_LEG_COST_ESTIMATE_USD,
        "proxied": PROXIED_LEG_COST_ESTIMATE_USD,
    }
    return sum(per_leg_cost.get(leg, PROXIED_LEG_COST_ESTIMATE_USD) for leg in legs) * targets * runs


# --- resolved-profile core (deferred; never invoked by this run) ------------


@dataclass(frozen=True)
class ResolvedProbeProfile:
    """The production access decision for one target, on one leg -- built
    from `scrape_core.targets`'s own resolution functions
    (`resolve_effective_policy`, `next_attempt`, `assign_proxy`), never a
    probe-specific stand-in. `impersonate` mirrors
    `scrape_core.impersonate.should_impersonate`'s domain-list decision
    (`Settings.SCRAPE_IMPERSONATE_DOMAINS`/`SCRAPE_IMPERSONATE_PROFILE`) --
    curl_cffi owns the header set once this is `True`, so no header dict
    is carried here (see module docstring: "impersonate owns the header
    set and its order").
    """

    access_policy_id: str | None
    strategy_profile_id: str | None
    leg: str
    use_proxy: bool
    sticky_key: str | None
    impersonate: bool
    impersonate_profile: str


def _impersonation_decision(domain: str) -> tuple[bool, str]:
    """Whether `domain` takes the impersonating transport in production,
    and which curl_cffi profile it uses -- mirrors
    `scrape_core.impersonate._host_matches`/`should_impersonate`'s
    domain-list check (duplicated here as a tiny pure helper rather than
    importing that module's private name across a package boundary).
    """
    from app_shared.config import get_settings

    settings = get_settings()
    domains = tuple(
        part.strip().lower().lstrip(".")
        for part in str(getattr(settings, "SCRAPE_IMPERSONATE_DOMAINS", "")).split(",")
        if part.strip()
    )
    host = domain.strip().lower().rstrip(".")
    matched = any(host == d or host.endswith("." + d) for d in domains)
    profile = str(getattr(settings, "SCRAPE_IMPERSONATE_PROFILE", "") or "chrome131")
    return matched, profile


def resolve_production_profile(
    session: Any,
    *,
    workspace_id: str,
    competitor_id: str,
    domain: str,
    url_pattern: str,
    sample_url: str,
    leg: str,
) -> ResolvedProbeProfile:
    """Resolve the production `AccessPolicy` for `(competitor, domain)` and
    force the requested `leg` -- **never invoked in this run** (Step 2 is
    deferred; no session is opened by anything under test).

    Loads the workspace/global default policy ids and the competitor's
    enabled `DomainAccessRule`s with two bounded queries (mirroring
    `apps/api/app/services/access_resolution.py`'s own load shape, at the
    SQL-text level rather than importing that `apps.*` orchestrator --
    Constitution Principle I: a `scripts/*` utility may import `libs/*`
    freely but should not reach into another `apps/*` member's service
    layer for a one-off diagnostic), then calls `scrape_core.targets`'s
    own `resolve_effective_policy` for the precedence chain-walk exactly
    as production does it.

    `leg` is forced rather than following `next_attempt`'s natural
    escalation ladder: this probe explicitly wants BOTH the direct and
    the proxied leg measured for every target, not only whichever the
    ladder would pick first -- so `"proxied"` always calls `assign_proxy`
    for the session/provider shape and `"direct"` never does.

    A caller with `leg == "proxied"` gets `use_proxy=True` and a
    `sticky_key` from `assign_proxy` (production's exact per-match
    seeding, `scrape_core.targets.sticky_proxy_username`'s partner call);
    `"direct"` gets `use_proxy=False` and `sticky_key=None`.
    """
    from sqlalchemy import text

    from scrape_core.targets import resolve_effective_policy

    default_row = session.execute(
        text(
            """
            SELECT
                (SELECT default_access_policy_id FROM workspaces WHERE id = CAST(:workspace_id AS uuid)) AS workspace_default,
                (SELECT id FROM access_policies WHERE workspace_id IS NULL AND name = 'global_default') AS global_default
            """
        ),
        {"workspace_id": workspace_id},
    ).mappings().first()
    workspace_default_id = default_row["workspace_default"] if default_row else None
    global_default_id = default_row["global_default"] if default_row else None

    rule_row = session.execute(
        text(
            """
            SELECT access_policy_id, url_pattern
            FROM domain_access_rules
            WHERE competitor_id = CAST(:competitor_id AS uuid)
              AND domain = :domain
              AND enabled = TRUE
            """
        ),
        {"competitor_id": competitor_id, "domain": domain},
    ).mappings().all()
    from app_shared.access.resolution import select_domain_rule

    class _Rule:
        def __init__(self, row: Mapping[str, Any]) -> None:
            self.enabled = True
            self.domain = domain
            self.url_pattern = row["url_pattern"]
            self.access_policy_id = row["access_policy_id"]
            self.id = row["access_policy_id"]

    matched_rule = select_domain_rule(
        [_Rule(row) for row in rule_row], domain=domain, url=sample_url
    )
    domain_rule_policy_id = matched_rule.access_policy_id if matched_rule is not None else None

    visible_ids = {
        pid for pid in (workspace_default_id, global_default_id, domain_rule_policy_id) if pid
    }
    result = resolve_effective_policy(
        domain_rule_policy_id=domain_rule_policy_id,
        workspace_default_policy_id=workspace_default_id,
        global_default_policy_id=global_default_id,
        visible_ids=visible_ids,
    )
    access_policy_id = getattr(result, "policy_id", None)

    sticky_key: str | None = None
    if leg == "proxied" and access_policy_id is not None:
        from scrape_core.targets import assign_proxy

        policy_row = session.execute(
            text(
                "SELECT strategy, provider_id, country_code, rotate_per_request, "
                "sticky_session FROM access_policies WHERE id = CAST(:id AS uuid)"
            ),
            {"id": str(access_policy_id)},
        ).mappings().first()
        if policy_row is not None:
            providers = session.execute(
                text(
                    "SELECT id, status, type, country_code FROM proxy_providers "
                    "WHERE workspace_id = CAST(:workspace_id AS uuid) OR workspace_id IS NULL"
                ),
                {"workspace_id": workspace_id},
            ).mappings().all()
            visible_providers = {
                row["id"]: (row["status"], row["type"], row["country_code"]) for row in providers
            }
            assignment = assign_proxy(
                strategy=policy_row["strategy"],
                policy_provider_id=policy_row["provider_id"],
                policy_country=policy_row["country_code"],
                domain_rule_country=None,
                visible_providers=visible_providers,
                attempt_number=1,
                rotate_per_request=policy_row["rotate_per_request"],
                sticky_session=policy_row["sticky_session"],
                session_seed=f"{competitor_id}:{sample_url}",
            )
            sticky_key = assignment.sticky_key if assignment is not None else None

    impersonate, impersonate_profile = _impersonation_decision(domain)
    return ResolvedProbeProfile(
        access_policy_id=str(access_policy_id) if access_policy_id else None,
        strategy_profile_id=None,
        leg=leg,
        use_proxy=(leg == "proxied"),
        sticky_key=sticky_key,
        impersonate=impersonate,
        impersonate_profile=impersonate_profile,
    )


def _open_session(db_url: str | None):
    """Delegates to `run_gate_d_canary` -- one DSN-resolution/secret rule
    for every canary/probe script, and no second place that could start
    printing a URL."""
    from scripts.run_gate_d_canary import _open_session as _gate_d_open_session
    from scripts.run_gate_d_canary import _resolve_db_url

    return _gate_d_open_session(_resolve_db_url(db_url))


def draw_target_set(
    session: Any, *, workspace_id: str, domain: str, size: int, seed: str
) -> list[dict[str, Any]]:
    """The probe's target sample -- reuses Gate D's workspace-scoped
    candidate pool exactly as `canary_document_only_browser.build_amazon_target_set`
    does, restricted to `domain` and truncated deterministically by the
    seeded selection key. Never invoked by this run's tests (DB required).
    """
    from scripts.run_gate_d_canary import _selection_key, load_candidate_pool, load_classifications

    pool = load_candidate_pool(
        session,
        classifications=load_classifications(session, None),
        labeled_stech_ids=[],
        workspace_id=workspace_id,
    )
    suffix = "." + domain
    on_domain = [
        candidate
        for candidate in pool
        if (candidate.domain or "").lower() == domain
        or (candidate.domain or "").lower().endswith(suffix)
    ]
    on_domain.sort(key=lambda candidate: _selection_key(seed, candidate.match_id))
    return [
        {
            "match_id": candidate.match_id,
            "competitor_id": candidate.competitor_id,
            "domain": candidate.domain,
            "competitor_url": candidate.competitor_url,
        }
        for candidate in on_domain[:size]
    ]


# --- CLI ----------------------------------------------------------------------


def _workspace_uuid(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"not a UUID: {raw!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_amazon_http_leg.py",
        description=(
            "EPA C2: measure Amazon HTTP-leg (direct + proxied) price-success "
            "under the resolved production access profile. --max-usd is "
            "required; this process never spends without it."
        ),
    )
    parser.add_argument("--targets", type=int, default=DEFAULT_TARGETS,
                        help=f"target sample size (default {DEFAULT_TARGETS})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help=f"physical attempts per target per leg (default {DEFAULT_RUNS})")
    parser.add_argument("--max-usd", type=float, required=True, dest="max_usd",
                        help="hard spend cap in USD -- REQUIRED, refuses to run without it")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help=f"report path (default {DEFAULT_REPORT})")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN,
                        help=f"domain under test (default {DEFAULT_DOMAIN})")
    parser.add_argument("--seed", default="c2-amazon-http-leg-2026-09",
                        help="selection seed -- same seed draws the same target set")
    parser.add_argument("--workspace", type=_workspace_uuid, default=None,
                        help="workspace UUID the target sample is drawn from")
    parser.add_argument("--dry-run", action="store_true",
                        help="draw the target sample and write the plan only; "
                             "no fetch, no proxy traffic, no spend")
    parser.add_argument("--db-url", default=None,
                        help="database URL; falls back to run_gate_d_canary's env resolution. "
                             "Never printed.")
    return parser


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def live_command(args: argparse.Namespace) -> str:
    """The exact Step-2 command for the doc/report -- never executed here."""
    return (
        "sudo -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/python "
        "scripts/probe_amazon_http_leg.py "
        f"--targets {args.targets} --runs {args.runs} --max-usd {args.max_usd:.2f} "
        f"--domain {args.domain} --workspace <workspace-uuid> --report {args.report}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_usd <= 0:
        parser.error("--max-usd must be > 0")

    estimated = estimate_run_cost_usd(targets=args.targets, runs=args.runs)
    out = Path(args.report)
    cmd = live_command(args)

    if args.dry_run or not args.workspace:
        placeholder = {
            "tool_version": PROBE_TOOL_VERSION,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "domain": args.domain,
            "targets": args.targets,
            "runs": args.runs,
            "max_usd": args.max_usd,
            "estimated_cost_usd": estimated,
            "status": "DRY RUN -- nothing fetched, nothing spent",
            "deferred_live_command": cmd,
        }
        _write(out, json.dumps(placeholder, indent=2, sort_keys=True) + "\n")
        print(f"DRY RUN -- estimated cost ${estimated:.4f} against cap ${args.max_usd:.2f}")
        print(f"Deferred live command:\n  {cmd}")
        print(f"Wrote {out}")
        if estimated > args.max_usd:
            print(
                f"WARNING: estimated cost ${estimated:.4f} exceeds --max-usd "
                f"${args.max_usd:.2f} -- the live run would refuse to complete.",
                file=sys.stderr,
            )
        return 0

    # --- LIVE PATH -------------------------------------------------------
    # Never reached by this EPA run: Step 2 is a spend step and is not run
    # (ASSUMPTIONS.md answer 3). Kept real (not a stub) so the owner's
    # eventual run is this exact, reviewed code path -- guarded end to end
    # by `SpendGuard` so a runaway loop cannot itself exceed `--max-usd`.
    guard = SpendGuard(max_usd=args.max_usd)
    session = _open_session(args.db_url)
    try:
        targets = draw_target_set(
            session, workspace_id=args.workspace, domain=args.domain,
            size=args.targets, seed=args.seed,
        )
    finally:
        session.close()

    if not targets:
        print(f"no ACTIVE {args.domain} targets found in workspace {args.workspace}", file=sys.stderr)
        return 1

    # The actual fetch loop (resolve profile per leg, curl_cffi request,
    # extraction via the resolved DomainStrategyProfile, wire_bytes via
    # A7's WireBytesMiddleware) is intentionally not implemented inline
    # here: it requires the live proxy credentials and Amazon traffic this
    # task's firewall forbids generating, and would be untestable dead
    # code without them. `resolve_production_profile`/`draw_target_set`
    # above are the reviewed, real building blocks; wiring the request
    # loop is the owner's Step 2, using `guard.charge(...)` before every
    # proxied attempt as the runtime enforcement of `--max-usd`.
    raise SpendCapExceeded(
        "the live fetch loop is deferred per ASSUMPTIONS.md answer 3 -- "
        "run the command this --dry-run printed only after the owner "
        "authorizes the spend"
    )


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
