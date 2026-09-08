#!/usr/bin/env python3
"""canary_document_only_browser.py — EPA B5: does document-only actually pay?

WHAT THIS SCRIPT IS — AND WHAT IT DELIBERATELY IS NOT
=====================================================

B5 shipped a resource-blocking rule that is OFF: for a domain an operator
lists in ``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS``, and only on a leg that
actually crossed the paid proxy, a browser fetch carries the document and
nothing else (``app_shared.profiles.browser_resource_policy``). The setting
defaults to ``()``; nothing changes for anyone until somebody lists a domain.

This script is what decides whether that is worth doing for ``amazon.sa``. It
draws the target set, prints the two-phase runbook, and — once the owner has
actually run both phases — computes the comparison and writes the report.

**It never spends money and never touches a Playwright browser.** It does not
enqueue a job, does not deploy, does not write to Railway, and does not talk
to Amazon or any other target. Every live action in the sequence belongs to
the owner and is printed as a runbook step, because the flag flip in phase B
is a production configuration change on a service this script has no business
making. With ``--dry-run`` it does not even open a database session: it
writes the runbook and the acceptance rule so a reviewer can read what the
live run will do before authorizing it.

WHY TWO PHASES AND A REDEPLOY, NOT AN IN-PROCESS TOGGLE
======================================================

``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS`` is read through
``app_shared.config.get_settings()``, which is an ``lru_cache``d singleton
parsed once per process from the environment. The process that reads it is
the Scrapyd **scrapers-browser** worker on Railway — not this one. There is
therefore no way for a local script to flip the setting for a remote run
without either restarting that service or shipping a second, weaker
configuration path into production for the benefit of a one-off measurement.
The measurement is not worth that: a second toggle mechanism that only the
canary uses is exactly the sort of thing that survives the canary.

So the toggle is the ordinary one, and the script's job is to make the
comparison honest across it:

* **Phase A (baseline)** runs with the variable unset — today's behaviour,
  ``policy_version`` 2 deciding exactly as v1 did.
* **Phase B (candidate)** runs after the owner sets
  ``BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS=amazon.sa`` on the scrapers-browser
  service and redeploys it, then unsets it again when the run is done.

Each phase is one enqueued job over the SAME target set, and the two are told
apart by their ``scrape_job_id`` — passed to this script as
``--baseline-job-id`` / ``--candidate-job-id``. (``network_operations`` has
no ``policy_version`` column to tag rows with; the constant travels in the
report header instead, so a reader can tell which rule-set produced the
numbers without having to date the rows.)

THE NUMBERS
===========

All three come from ``network_operations`` rows with ``transport = BROWSER``
for the phase's job — the same rows the C1 ledger prices, so the canary
cannot disagree with the bill:

* **success rate** — a row with no ``failure_reason`` and a 2xx (or absent)
  ``response_status``;
* **p50/p95 wall** — ``duration_ms``, nearest-rank;
* **proxy bytes per page** — ``bytes_compressed`` summed over the phase and
  divided by the pages measured.

ACCEPTANCE (printed in the report, both rules must hold)
========================================================

1. Candidate success rate is within :data:`SUCCESS_RATE_TOLERANCE_POINTS`
   (3) percentage points of the baseline's — a cheaper page nobody can parse
   is not cheaper, it is broken.
2. Candidate proxy bytes per page ≤ :data:`PROXY_BYTES_PER_PAGE_CEILING`
   (0.4 MB) — the point of the exercise.

USAGE
=====

Dry (no DB, no network — safe and unattended)::

    python scripts/canary_document_only_browser.py --dry-run

Draw the target set + print the runbook (reads the DB, spends nothing)::

    python scripts/canary_document_only_browser.py \\
        --workspace 01a020de-871c-7760-8273-59f3b67d9c18 --n 50

Compare, after the owner has run BOTH phases::

    python scripts/canary_document_only_browser.py \\
        --baseline-job-id <phase A job> --candidate-job-id <phase B job>

SECRET DISCIPLINE
=================

No DSN, password or provider credential is ever printed or written to the
report — same rule as ``scripts/run_gate_d_canary.py``, whose ``--db-url`` /
environment resolution this script reuses rather than re-implementing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

# `scripts/` has no __init__.py; the sibling-module import below needs the
# repository root on the path when this file is run as a script.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = [
    "CANARY_TOOL_VERSION",
    "DEFAULT_OUT",
    "DEFAULT_SAMPLE_SIZE",
    "DOCUMENT_ONLY_DOMAIN",
    "MAX_UNKNOWN_PCT",
    "MIN_COMPARABLE_PARENTS",
    "PROXY_BYTES_PER_PAGE_CEILING",
    "PROXY_CANARY_USERNAME_ENV",
    "SUCCESS_RATE_TOLERANCE_POINTS",
    "Acceptance",
    "CalculatorSummary",
    "Check",
    "CoverageVerdict",
    "PageOperation",
    "PageRow",
    "PhaseSummary",
    "build_parser",
    "calculator_summary_to_phase_summary",
    "evaluate_acceptance",
    "evaluate_coverage",
    "main",
    "page_operation_from_mapping",
    "page_row_from_operation",
    "percentile",
    "proxy_canary_username_configured",
    "randomized_execution_order",
    "render_report",
    "summarize",
    "summarize_phase",
]

CANARY_TOOL_VERSION = "2"

#: The domain the 2026-09 canary is about. Not hardcoded into the policy —
#: the policy reads whatever an operator lists; this is just what the run
#: measures, and what the runbook tells the owner to list.
DOCUMENT_ONLY_DOMAIN = "amazon.sa"

DEFAULT_SAMPLE_SIZE = 50
DEFAULT_OUT = "evidence/canary-document-only-2026-09/REPORT.md"

#: EPA C3 (2026-09-08): the environment variable NAME (never its value --
#: no credential is ever read, printed, or written by this script) that
#: must reference a dedicated canary-only proxy sub-account on the
#: scrapers-browser service, so canary spend/reputation never mixes into
#: production proxy billing or a production exit IP's reputation. This
#: script only checks whether the name is SET in its own process (an
#: advisory signal for `--dry-run`/the target-set draw, which run on this
#: host, not on scrapers-browser); the real, load-bearing configuration is
#: the owner setting it on the scrapers-browser service itself before
#: Step 2, exactly like `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` already is
#: (see `runbook_text`).
PROXY_CANARY_USERNAME_ENV = "PROXY_CANARY_USERNAME"

#: Acceptance rule 1: how far the candidate's success rate may fall below the
#: baseline's, in PERCENTAGE POINTS. "Within 3" includes exactly 3.
SUCCESS_RATE_TOLERANCE_POINTS = 3.0

#: Acceptance rule 2: proxy bytes per page the candidate must not exceed.
#: 0.4 MB, counted in decimal megabytes (400,000 bytes) rather than MiB —
#: the stricter of the two readings, chosen so a run that lands exactly on
#: the stated bar cannot pass on a unit technicality.
PROXY_BYTES_PER_PAGE_CEILING = 400_000


# --- pure core ----------------------------------------------------------------


@dataclass(frozen=True)
class PageRow:
    """One browser page fetch, as the ledger recorded it."""

    success: bool
    wall_ms: int
    proxy_bytes: int


@dataclass(frozen=True)
class PhaseSummary:
    label: str
    policy_version: int
    pages: int
    successes: int
    success_pct: float
    p50_wall_ms: float
    p95_wall_ms: float
    proxy_bytes_total: int
    proxy_bytes_per_page: float


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Acceptance:
    checks: tuple[Check, ...]

    @property
    def accepted(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def verdict(self) -> str:
        return "ACCEPT" if self.accepted else "REJECT"


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of `values` (order-independent).

    Nearest-rank rather than interpolated so a reported p95 is always a wall
    time some page actually took. An empty sequence yields ``0.0`` —
    "nothing measured", which the acceptance rules refuse on their own
    (:func:`evaluate_acceptance`) rather than by raising here.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def page_row_from_operation(operation: Mapping[str, Any]) -> PageRow:
    """One ``network_operations`` row -> one :class:`PageRow`.

    "Success" is the ledger's own notion, not a re-derivation: no
    ``failure_reason``, and either no ``response_status`` at all (a browser
    navigation that never produced one is judged by its failure reason) or a
    2xx. A NULL ``bytes_compressed`` counts as 0 rather than propagating a
    ``None`` that would make the whole phase's average un-computable.
    """
    status = operation.get("response_status")
    success = operation.get("failure_reason") is None and (
        status is None or 200 <= int(status) < 300
    )
    return PageRow(
        success=success,
        wall_ms=int(operation.get("duration_ms") or 0),
        proxy_bytes=int(operation.get("bytes_compressed") or 0),
    )


def summarize_phase(
    label: str, rows: Sequence[PageRow], *, policy_version: int
) -> PhaseSummary:
    """Aggregate one phase's pages into the three numbers the owner reads."""
    pages = len(rows)
    successes = sum(1 for row in rows if row.success)
    proxy_bytes_total = sum(row.proxy_bytes for row in rows)
    return PhaseSummary(
        label=label,
        policy_version=policy_version,
        pages=pages,
        successes=successes,
        success_pct=(100.0 * successes / pages) if pages else 0.0,
        p50_wall_ms=percentile([row.wall_ms for row in rows], 50),
        p95_wall_ms=percentile([row.wall_ms for row in rows], 95),
        proxy_bytes_total=proxy_bytes_total,
        proxy_bytes_per_page=(proxy_bytes_total / pages) if pages else 0.0,
    )


def evaluate_acceptance(baseline: PhaseSummary, candidate: PhaseSummary) -> Acceptance:
    """The two acceptance rules, in the order the report prints them.

    A candidate phase that measured NOTHING fails both: a zero-page run has a
    0.0 MB/page average and a 0% success rate, and neither is evidence.
    """
    measured = candidate.pages > 0 and baseline.pages > 0
    drop = baseline.success_pct - candidate.success_pct
    success_ok = measured and drop <= SUCCESS_RATE_TOLERANCE_POINTS
    bytes_ok = measured and candidate.proxy_bytes_per_page <= PROXY_BYTES_PER_PAGE_CEILING

    if not measured:
        reason = " (NO PAGES MEASURED — a phase with no rows is not evidence)"
    else:
        reason = ""

    return Acceptance(
        checks=(
            Check(
                name="success rate within 3 points of baseline",
                passed=bool(success_ok),
                detail=(
                    f"baseline {baseline.success_pct:.1f}% -> candidate "
                    f"{candidate.success_pct:.1f}% (drop {drop:.1f} pts, "
                    f"allowed {SUCCESS_RATE_TOLERANCE_POINTS:.0f}){reason}"
                ),
            ),
            Check(
                name="proxy bytes per page <= 0.4 MB",
                passed=bool(bytes_ok),
                detail=(
                    f"baseline {_mb(baseline.proxy_bytes_per_page)} -> candidate "
                    f"{_mb(candidate.proxy_bytes_per_page)} "
                    f"(ceiling {_mb(PROXY_BYTES_PER_PAGE_CEILING)}){reason}"
                ),
            ),
        )
    )


def _mb(value: float) -> str:
    return f"{value / 1_000_000:.3f} MB"


def _phase_table(summary: PhaseSummary) -> str:
    return (
        f"| {summary.label} | {summary.pages} | {summary.successes} | "
        f"{summary.success_pct:.1f}% | {summary.p50_wall_ms:.0f} ms | "
        f"{summary.p95_wall_ms:.0f} ms | {_mb(summary.proxy_bytes_per_page)} |"
    )


# --- EPA C3: randomized paired ordering + dedicated proxy sub-account --------


def randomized_execution_order(
    targets: Sequence[Mapping[str, Any]], *, seed: str
) -> list[dict[str, Any]]:
    """Shuffle `targets` deterministically from `seed`.

    "Paired" ordering: phase A and phase B both replay this SAME shuffled
    list (the seed is fixed per canary run, not per phase), so whatever
    position-dependent drift exists within a job -- proxy warm-up, a
    rate-limit window, time-of-day -- lands on the SAME targets in the
    SAME order in both phases and cancels out of the A/B comparison,
    rather than confounding it. This is deliberately a SECOND-level
    shuffle on top of `build_amazon_target_set`'s own selection-key sort:
    that sort exists so the target SET is reproducible run over run; this
    shuffle exists so the EXECUTION ORDER within one run is not the
    selection key itself (which could correlate with e.g. when a match
    was created).

    Pure and deterministic: the same `targets` + `seed` always produce the
    same order, so a re-run for review reproduces the exact plan without
    needing to persist it separately. Never mutates `targets`.
    """
    rng = random.Random(f"{seed}:execution-order")
    order = list(targets)
    rng.shuffle(order)
    return order


def proxy_canary_username_configured() -> bool:
    """Whether the dedicated canary proxy sub-account env var (see
    :data:`PROXY_CANARY_USERNAME_ENV`) is SET in this process.

    Advisory only: this process draws the target set / writes the runbook
    on this host, never the scrapers-browser Railway service that
    actually makes the proxied request -- so this check cannot itself
    guarantee Step 2 uses the dedicated sub-account (that guarantee is
    the owner's, on the service that runs the fetch). It exists so a
    `--dry-run` invocation on the SAME host the owner intends to configure
    can catch "the variable name is misspelled" or "nobody set it yet"
    before the live run, without ever reading or logging the value.
    """
    return bool(os.environ.get(PROXY_CANARY_USERNAME_ENV, "").strip())


# --- parent/child/price/provider-aware calculator (deep dive §8.1) -----------
#
# `PageRow`/`summarize_phase` above treat every `network_operations` row as
# one page: a browser navigation plus its ~99 subresource children reads as
# "100 pages" with a fabricated 0 ms latency (the children carry no
# `duration_ms`) and a 1/100th-scale bytes/page average. On the 2026-08-28
# job this over-counted 335 real pages as 1,328. The fix is not "coerce
# children to 0" — that hides the defect behind a different wrong number —
# it is to know which rows are pages, attach every other row to its page by
# `parent_operation_id` regardless of which host served it, and keep missing
# facts missing so a caller can tell "no page did this" from "we didn't
# measure this".


@dataclass(frozen=True)
class PageOperation:
    """One `network_operations` row, carrying enough identity for
    :func:`summarize` to tell a PAGE (`parent_operation_id is None`) from a
    child fetch attached to one, plus the provider/price-outcome dimensions
    the old per-row `PageRow` had no room for.

    `duration_ms`/`bytes_compressed` are `None` when unmeasured — NEVER
    coerced to `0` — so :func:`summarize` can report `unknown_durations`/
    `unknown_bytes` instead of silently manufacturing a real-looking number.
    `price_ok` is the row's terminal price-extraction outcome (only
    meaningful on a page row; a subresource has no attempt of its own).
    """

    network_request_id: str
    parent_operation_id: str | None
    provider: str | None
    duration_ms: int | None
    bytes_compressed: int | None
    price_ok: bool | None
    proxy_provider_id: str | None = None


@dataclass(frozen=True)
class CalculatorSummary:
    """The numbers :func:`summarize` produces for one phase's operations."""

    pages: int
    price_success: int
    p50_ms: float
    p95_ms: float
    bytes_per_page: float
    unknown_durations: int
    unknown_bytes: int
    providers: tuple[str, ...]


def page_operation_from_mapping(row: Mapping[str, Any]) -> PageOperation:
    """One row of :data:`_OPERATIONS_SQL` -> one :class:`PageOperation`."""

    def _opt_str(value: Any) -> str | None:
        return None if value is None else str(value)

    def _opt_int(value: Any) -> int | None:
        return None if value is None else int(value)

    return PageOperation(
        network_request_id=str(row["network_request_id"]),
        parent_operation_id=_opt_str(row.get("parent_operation_id")),
        provider=row.get("provider"),
        proxy_provider_id=_opt_str(row.get("proxy_provider_id")),
        duration_ms=_opt_int(row.get("duration_ms")),
        bytes_compressed=_opt_int(row.get("bytes_compressed")),
        price_ok=(None if row.get("price_ok") is None else bool(row["price_ok"])),
    )


def summarize(rows: Sequence[PageOperation]) -> CalculatorSummary:
    """Aggregate one phase's raw operations into page-level numbers.

    Pages are rows with no `parent_operation_id`; every other row is a
    child attached to whichever page it names, regardless of the child's
    own provider/hostname. `price_success` counts pages whose terminal
    attempt actually produced a price — a page's children have no price
    outcome of their own and are never asked for one. A `None`
    `duration_ms`/`bytes_compressed` is preserved as "not measured": it is
    counted in `unknown_durations`/`unknown_bytes`, never folded into the
    percentile or the byte total as a `0`.
    """
    parents = [row for row in rows if row.parent_operation_id is None]
    pages = len(parents)

    durations: list[int] = []
    unknown_durations = 0
    for parent in parents:
        if parent.duration_ms is None:
            unknown_durations += 1
        else:
            durations.append(parent.duration_ms)

    bytes_total = 0
    unknown_bytes = 0
    for row in rows:
        if row.bytes_compressed is None:
            unknown_bytes += 1
        else:
            bytes_total += row.bytes_compressed

    price_success = sum(1 for parent in parents if parent.price_ok)
    providers = tuple(sorted({row.provider for row in rows if row.provider}))

    return CalculatorSummary(
        pages=pages,
        price_success=price_success,
        p50_ms=percentile(durations, 50),
        p95_ms=percentile(durations, 95),
        bytes_per_page=(bytes_total / pages) if pages else 0.0,
        unknown_durations=unknown_durations,
        unknown_bytes=unknown_bytes,
        providers=providers,
    )


#: Coverage-refusal thresholds (deep dive §8.1: "reject comparisons with
#: insufficient coverage").
MIN_COMPARABLE_PARENTS = 50
MAX_UNKNOWN_PCT = 5.0


@dataclass(frozen=True)
class CoverageVerdict:
    """Whether a baseline/candidate pair is even comparable, before either
    is judged against the acceptance rule."""

    ok: bool
    reasons: tuple[str, ...]


def evaluate_coverage(
    baseline: CalculatorSummary,
    candidate: CalculatorSummary,
    *,
    baseline_target_ids: frozenset[str] = frozenset(),
    candidate_target_ids: frozenset[str] = frozenset(),
    baseline_strategy_profile_id: str | None = None,
    candidate_strategy_profile_id: str | None = None,
    min_parents: int = MIN_COMPARABLE_PARENTS,
    max_unknown_pct: float = MAX_UNKNOWN_PCT,
) -> CoverageVerdict:
    """Refuse a comparison that cannot be trusted, before computing it.

    Both arms need >= `min_parents` pages (a handful of pages is noise, not
    evidence) and <= `max_unknown_pct` unknown duration/byte records
    (unmeasured rows dilute both numbers the report is judged on). The two
    arms must also have measured the SAME target set under the SAME
    resolved strategy profile — a comparison across different targets or a
    profile the resolver swapped mid-run is not measuring the setting under
    test.
    """
    reasons: list[str] = []
    for label, summary in (("baseline", baseline), ("candidate", candidate)):
        if summary.pages < min_parents:
            reasons.append(
                f"{label} has {summary.pages} parent pages, fewer than the "
                f"required minimum of {min_parents}"
            )
        denominator = max(summary.pages, 1)
        unknown_pct = 100.0 * (summary.unknown_durations + summary.unknown_bytes) / denominator
        if unknown_pct > max_unknown_pct:
            reasons.append(
                f"{label} has {unknown_pct:.1f}% unknown duration/bytes records, "
                f"over the {max_unknown_pct:.0f}% ceiling"
            )
    if baseline_target_ids and candidate_target_ids and baseline_target_ids != candidate_target_ids:
        reasons.append("baseline and candidate did not measure the same target set")
    if (
        baseline_strategy_profile_id is not None
        and candidate_strategy_profile_id is not None
        and baseline_strategy_profile_id != candidate_strategy_profile_id
    ):
        reasons.append(
            "baseline and candidate resolved different strategy profiles "
            f"({baseline_strategy_profile_id!r} != {candidate_strategy_profile_id!r})"
        )
    return CoverageVerdict(ok=not reasons, reasons=tuple(reasons))


def calculator_summary_to_phase_summary(
    label: str, summary: CalculatorSummary, *, policy_version: int
) -> PhaseSummary:
    """Bridge the new calculator's numbers into the existing `PhaseSummary`
    shape so the acceptance rule and report renderer (which judge success
    rate and bytes/page, not raw page counts) need no changes of their own.
    """
    return PhaseSummary(
        label=label,
        policy_version=policy_version,
        pages=summary.pages,
        successes=summary.price_success,
        success_pct=(100.0 * summary.price_success / summary.pages) if summary.pages else 0.0,
        p50_wall_ms=summary.p50_ms,
        p95_wall_ms=summary.p95_ms,
        proxy_bytes_total=round(summary.bytes_per_page * summary.pages),
        proxy_bytes_per_page=summary.bytes_per_page,
    )


_ACCEPTANCE_RULE_TEXT = (
    "1. candidate success rate is **within 3 percentage points** of the baseline's; AND\n"
    "2. candidate proxy bytes per page are **<= 0.4 MB** "
    f"({PROXY_BYTES_PER_PAGE_CEILING:,} bytes).\n"
)


def runbook_text(
    *,
    domain: str = DOCUMENT_ONLY_DOMAIN,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    max_usd: float | None = None,
) -> str:
    """The two-phase runbook. Every step here is the OWNER's to perform."""
    cap_line = (
        f"Spend cap for this canary: **<= ${max_usd:.2f}** (`--max-usd`, required).\n\n"
        if max_usd is not None
        else ""
    )
    return f"""\
### Two-phase runbook (owner-performed — this script performs none of it)

{cap_line}`BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` (or its EPA C3 alias,
`BROWSER_DOCUMENT_ONLY_DOMAINS`) is read once per Scrapyd scrapers-browser
process via the cached `get_settings()` singleton, so the candidate phase
requires a real configuration change and a redeploy of that service. There
is deliberately no second, canary-only toggle for that setting.

**Dedicated proxy sub-account (EPA C3)** — before EITHER phase, set
`{PROXY_CANARY_USERNAME_ENV}` on the scrapers-browser service to a proxy
sub-account created FOR THIS CANARY, distinct from the production
sub-account. This keeps the canary's proxy spend and any resulting IP-
reputation impact from mixing into production's -- a canary that
CAPTCHAs an exit IP must not be the reason production traffic starts
seeing the same CAPTCHA. The variable's NAME is referenced here; its
VALUE is never read, printed, or written by this script.

1. **Phase A (baseline)** — confirm `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS`
   is UNSET on the scrapers-browser service, then enqueue ONE job over the
   {sample_size}-target `{domain}` set drawn by this script
   (`target_set.json`, one workspace, one job, in the RANDOMIZED PAIRED
   ORDER recorded as `execution_order` in that file). Record its
   `scrape_job_id`.
2. **Phase B (candidate)** — set
   `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS={domain}` on the scrapers-browser
   service, redeploy it, and enqueue the SAME target set again, in the
   SAME `execution_order` (paired randomization: position-dependent drift
   lands on the same targets in both phases and cancels out of the
   comparison). Record its `scrape_job_id`.
3. **Restore** — unset the variable and redeploy, whatever the outcome. The
   rule stays off until the owner decides on this report.
4. **Compare** — re-run this script with
   `--baseline-job-id <A> --candidate-job-id <B>`; it reads
   `network_operations` (transport = BROWSER) for each job and writes this
   report.

Both phases must run the same target set in the same randomized order, and
phase B must not be the first run of a target set that phase A also warmed —
enqueue A and B in that order, close together, so the comparison is about
the rule and not about the hour.
"""


def render_report(
    *,
    baseline: PhaseSummary | None,
    candidate: PhaseSummary | None,
    acceptance: Acceptance | None,
    domain: str = DOCUMENT_ONLY_DOMAIN,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    baseline_job_id: str | None = None,
    candidate_job_id: str | None = None,
    generated_at: str | None = None,
    max_usd: float | None = None,
) -> str:
    """The evidence file. `None` phases render the DRY RUN plan instead."""
    stamp = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    dry = baseline is None or candidate is None or acceptance is None

    lines = [
        f"# Canary — document-only proxied browser ({domain})",
        "",
        f"- tool: `scripts/canary_document_only_browser.py` v{CANARY_TOOL_VERSION}",
        f"- generated: {stamp}",
        f"- blocklist policy_version: {_policy_version()}",
        f"- sample size: {sample_size} pages per phase",
        f"- setting under test: `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS={domain}` "
        f"(alias: `BROWSER_DOCUMENT_ONLY_DOMAINS={domain}`)",
        f"- spend cap: <= ${max_usd:.2f}" if max_usd is not None else "- spend cap: (not set)",
        f"- dedicated proxy sub-account env `{PROXY_CANARY_USERNAME_ENV}` set on THIS host: "
        f"{proxy_canary_username_configured()} (advisory only -- the load-bearing check is "
        "on the scrapers-browser service, see the runbook)",
        "",
        "## Acceptance rule",
        "",
        "The rule is adopted for this domain only if BOTH hold:",
        "",
        _ACCEPTANCE_RULE_TEXT,
    ]

    if dry:
        lines += [
            "## DRY RUN — nothing was measured",
            "",
            "No database session was opened, no job was enqueued, no page was",
            "fetched and nothing was spent. This file records what the live run",
            "will do and the bar it will be judged against.",
            "",
            runbook_text(domain=domain, sample_size=sample_size, max_usd=max_usd),
        ]
        return "\n".join(lines) + "\n"

    assert baseline is not None and candidate is not None and acceptance is not None
    lines += [
        "## Measured",
        "",
        f"- phase A (baseline) job: `{baseline_job_id or 'n/a'}`",
        f"- phase B (candidate) job: `{candidate_job_id or 'n/a'}`",
        "",
        "| phase | pages | ok | success | p50 wall | p95 wall | proxy bytes/page |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        _phase_table(baseline),
        _phase_table(candidate),
        "",
        "## Verdict",
        "",
        f"**{acceptance.verdict}**",
        "",
    ]
    for check in acceptance.checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"- [{mark}] {check.name} — {check.detail}")
    lines += [
        "",
        f"Proxy bytes saved per page: "
        f"{_mb(baseline.proxy_bytes_per_page - candidate.proxy_bytes_per_page)}.",
        "",
        runbook_text(domain=domain, sample_size=sample_size, max_usd=max_usd),
    ]
    return "\n".join(lines) + "\n"


def _policy_version() -> int:
    """The live ``BLOCKLIST_VERSION``, so the report names the rule-set that
    produced its numbers (``network_operations`` carries no such column).
    Imported lazily: the dry path must not require the engine's settings."""
    try:
        from app_shared.profiles.browser_resource_policy import BLOCKLIST_VERSION

        return int(BLOCKLIST_VERSION)
    except Exception:  # noqa: BLE001 - a report is still readable without it
        return 0


# --- database shell (never reached by --dry-run) ------------------------------

# Fixed per deep dive §8.1. `page_ops` finds the PARENT pages on the target
# domain (the denominator); the outer query then pulls every operation that
# either IS one of those pages or names one as `parent_operation_id` —
# regardless of the child's OWN domain, so other-host CDN/subresource
# traffic that is part of the page is no longer silently excluded. `price_ok`
# joins the terminal `request_attempts` row for the operation (by
# `network_request_id`, C1's operation identity) — `request_attempts.success`
# IS the price outcome (FR-013: exactly one row per attempted target,
# `success=False` on e.g. `PRICE_NOT_FOUND`/`EXTRACTION_FAILED`, not merely a
# transport failure) — so a page whose browser fetch transported fine but
# never yielded a price is correctly `price_ok=False`, not folded into
# transport success the way the old predicate did.
_OPERATIONS_SQL = """
WITH page_ops AS (
    SELECT network_request_id
    FROM network_operations
    WHERE scrape_job_id = CAST(:job_id AS uuid)
      AND transport::text = 'BROWSER'
      AND parent_operation_id IS NULL
      AND (domain = :domain OR domain LIKE :domain_suffix)
)
SELECT n.network_request_id,
       n.parent_operation_id,
       n.provider,
       n.duration_ms,
       n.bytes_compressed,
       a.proxy_provider_id,
       a.success AS price_ok
FROM network_operations n
LEFT JOIN request_attempts a ON a.network_operation_id = n.network_request_id
WHERE n.scrape_job_id = CAST(:job_id AS uuid)
  AND transport::text = 'BROWSER'
  AND (
        n.network_request_id IN (SELECT network_request_id FROM page_ops)
        OR n.parent_operation_id IN (SELECT network_request_id FROM page_ops)
      )
"""

#: The dimensions a comparison must agree on across arms (deep dive §8.1:
#: "require the same target set and resolved profile"). `request_attempts`
#: has no `strategy_profile_id` column — the nearest real analog is
#: `scrape_profile_id`, the resolved profile a target's fetch ran under.
_DIMENSIONS_SQL = """
SELECT DISTINCT a.match_id, a.scrape_profile_id
FROM request_attempts a
JOIN network_operations n ON n.network_request_id = a.network_operation_id
WHERE n.scrape_job_id = CAST(:job_id AS uuid)
  AND n.transport::text = 'BROWSER'
  AND n.parent_operation_id IS NULL
  AND (n.domain = :domain OR n.domain LIKE :domain_suffix)
"""


def _open_session(db_url: str | None):
    """Delegates to ``run_gate_d_canary`` — one DSN-resolution/secret rule for
    both canaries, and no second place that could start printing a URL."""
    from scripts.run_gate_d_canary import _open_session as _gate_d_open_session
    from scripts.run_gate_d_canary import _resolve_db_url

    return _gate_d_open_session(_resolve_db_url(db_url))


def load_phase_rows(session, job_id: str, *, domain: str) -> list[PageRow]:
    """Legacy per-row loader, kept only for callers still on the old
    (over-counting) `PageRow`/`summarize_phase` path. Live comparisons use
    :func:`load_phase_operations` + :func:`summarize` instead (deep dive
    §8.1) — see :func:`main`."""
    from sqlalchemy import text

    rows = session.execute(
        text(
            """
            SELECT failure_reason, response_status, duration_ms, bytes_compressed
            FROM network_operations
            WHERE scrape_job_id = CAST(:job_id AS uuid)
              AND transport::text = 'BROWSER'
              AND (domain = :domain OR domain LIKE :domain_suffix)
            """
        ),
        {"job_id": job_id, "domain": domain, "domain_suffix": f"%.{domain}"},
    ).mappings().all()
    return [page_row_from_operation(dict(row)) for row in rows]


def load_phase_operations(session, job_id: str, *, domain: str) -> list[PageOperation]:
    """Pages + attached children (regardless of hostname) for one phase,
    as :class:`PageOperation` rows ready for :func:`summarize`."""
    from sqlalchemy import text

    rows = session.execute(
        text(_OPERATIONS_SQL),
        {"job_id": job_id, "domain": domain, "domain_suffix": f"%.{domain}"},
    ).mappings().all()
    return [page_operation_from_mapping(dict(row)) for row in rows]


def load_phase_dimensions(
    session, job_id: str, *, domain: str
) -> tuple[frozenset[str], str | None]:
    """The target set (distinct `match_id`) and resolved profile (the
    single `scrape_profile_id` the phase's pages ran under, or `None` if
    the phase mixed more than one / recorded none) for the coverage check
    (:func:`evaluate_coverage`)."""
    from sqlalchemy import text

    rows = session.execute(
        text(_DIMENSIONS_SQL),
        {"job_id": job_id, "domain": domain, "domain_suffix": f"%.{domain}"},
    ).mappings().all()
    target_ids = frozenset(str(row["match_id"]) for row in rows if row.get("match_id") is not None)
    profile_ids = {str(row["scrape_profile_id"]) for row in rows if row.get("scrape_profile_id") is not None}
    strategy_profile_id = next(iter(profile_ids)) if len(profile_ids) == 1 else None
    return target_ids, strategy_profile_id


def build_amazon_target_set(
    session, *, workspace_id: str, size: int, domain: str, seed: str
) -> list[dict[str, Any]]:
    """The canary's explicit target set, drawn with Gate D's own builder.

    Reuses ``run_gate_d_canary``'s workspace-scoped candidate pool
    (``build-sample --workspace``, commit eb23dff) rather than a second
    query, for the reason that flag exists at all: every production
    job-creation entry point is workspace-scoped, so a target set that spans
    workspaces cannot be enqueued as ONE job — and a canary whose phases are
    two jobs each is not measuring one thing. Restricted here to `domain`
    (suffix match, so `www.amazon.sa` counts) and truncated deterministically
    by the same seeded selection key, so re-running the builder draws the
    same set.
    """
    from scripts.run_gate_d_canary import _selection_key, load_candidate_pool

    pool = load_candidate_pool(
        session,
        classifications=_classifications(session),
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
        {"match_id": candidate.match_id, "domain": candidate.domain,
         "competitor_url": candidate.competitor_url}
        for candidate in on_domain[:size]
    ]


def _classifications(session) -> dict[str, str]:
    from scripts.run_gate_d_canary import load_classifications

    return load_classifications(session, None)


# --- CLI ----------------------------------------------------------------------


def _workspace_uuid(raw: str) -> str:
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"not a UUID: {raw!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canary_document_only_browser.py",
        description=(
            "EPA B5 canary: measure success rate, wall time and proxy bytes per "
            "page with and without the document-only rule. Spends nothing; every "
            "live step is printed as an owner runbook."
        ),
    )
    parser.add_argument("--workspace", type=_workspace_uuid, default=None,
                        help="workspace UUID the target set is drawn from (one job, one workspace)")
    parser.add_argument("--targets", "--n", dest="n", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f"pages per phase (default {DEFAULT_SAMPLE_SIZE}); "
                             "`--n` is the pre-C3 spelling, kept as an alias")
    parser.add_argument("--max-usd", type=float, default=None, dest="max_usd",
                        help="hard spend cap in USD for the live two-phase run -- REQUIRED "
                             "for every invocation except --dry-run")
    parser.add_argument("--domain", default=DOCUMENT_ONLY_DOMAIN,
                        help=f"domain under test (default {DOCUMENT_ONLY_DOMAIN})")
    parser.add_argument("--seed", default="b5-document-only-2026-09",
                        help="selection seed — same seed draws the same target set AND "
                             "the randomized paired execution order")
    parser.add_argument("--dry-run", action="store_true",
                        help="no database, no network: write the runbook and the acceptance rule")
    parser.add_argument("--baseline-job-id", default=None,
                        help="phase A scrape_job_id (variable UNSET)")
    parser.add_argument("--candidate-job-id", default=None,
                        help="phase B scrape_job_id (variable set to the domain)")
    parser.add_argument("--db-url", default=None,
                        help="database URL; falls back to run_gate_d_canary's env resolution. Never printed.")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"report path (default {DEFAULT_OUT})")
    return parser


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Path(args.out)

    if args.dry_run:
        _write(out, render_report(
            baseline=None, candidate=None, acceptance=None,
            domain=args.domain, sample_size=args.n, max_usd=args.max_usd,
        ))
        print(runbook_text(domain=args.domain, sample_size=args.n, max_usd=args.max_usd))
        if not proxy_canary_username_configured():
            print(
                f"NOTE: {PROXY_CANARY_USERNAME_ENV} is not set on this host. This host "
                "only draws the target set; the load-bearing setting is on the "
                "scrapers-browser service before the live run (see the runbook).",
                file=sys.stderr,
            )
        print(f"DRY RUN — nothing measured, nothing spent. Wrote {out}")
        return 0

    # Every non-dry-run invocation is spend-capable (it either draws a real
    # target set for a live two-phase run, or reads a completed live run's
    # rows) -- EPA C3 / ASSUMPTIONS.md answer 3: every money-capable script
    # ships with a required cap even while the run itself stays deferred.
    if args.max_usd is None:
        parser.error("--max-usd is required (omit only together with --dry-run)")

    if bool(args.baseline_job_id) != bool(args.candidate_job_id):
        parser.error("--baseline-job-id and --candidate-job-id must be given together")

    if args.baseline_job_id and args.candidate_job_id:
        session = _open_session(args.db_url)
        try:
            baseline_ops = load_phase_operations(session, args.baseline_job_id, domain=args.domain)
            candidate_ops = load_phase_operations(session, args.candidate_job_id, domain=args.domain)
            baseline_targets, baseline_profile = load_phase_dimensions(
                session, args.baseline_job_id, domain=args.domain
            )
            candidate_targets, candidate_profile = load_phase_dimensions(
                session, args.candidate_job_id, domain=args.domain
            )
        finally:
            session.close()

        baseline_calc = summarize(baseline_ops)
        candidate_calc = summarize(candidate_ops)
        coverage = evaluate_coverage(
            baseline_calc,
            candidate_calc,
            baseline_target_ids=baseline_targets,
            candidate_target_ids=candidate_targets,
            baseline_strategy_profile_id=baseline_profile,
            candidate_strategy_profile_id=candidate_profile,
        )
        if not coverage.ok:
            reasons = "\n".join(f"- {reason}" for reason in coverage.reasons)
            _write(
                out,
                "# Canary — comparison REFUSED\n\n"
                "The comparison did not meet the minimum coverage bar (deep dive "
                "§8.1) and was not computed:\n\n"
                f"{reasons}\n",
            )
            for reason in coverage.reasons:
                print(f"REFUSED: {reason}", file=sys.stderr)
            print(f"Wrote {out}")
            return 1

        policy_version = _policy_version()
        baseline = calculator_summary_to_phase_summary(
            "A baseline (unlisted)", baseline_calc, policy_version=policy_version
        )
        candidate = calculator_summary_to_phase_summary(
            "B document-only (listed)", candidate_calc, policy_version=policy_version
        )
        acceptance = evaluate_acceptance(baseline, candidate)
        _write(out, render_report(
            baseline=baseline, candidate=candidate, acceptance=acceptance,
            domain=args.domain, sample_size=args.n, max_usd=args.max_usd,
            baseline_job_id=args.baseline_job_id, candidate_job_id=args.candidate_job_id,
        ))
        for summary in (baseline, candidate):
            print(
                f"{summary.label}: pages={summary.pages} success={summary.success_pct:.1f}% "
                f"p50={summary.p50_wall_ms:.0f}ms p95={summary.p95_wall_ms:.0f}ms "
                f"proxy_bytes_per_page={_mb(summary.proxy_bytes_per_page)}"
            )
        print(_ACCEPTANCE_RULE_TEXT)
        for check in acceptance.checks:
            print(f"[{'PASS' if check.passed else 'FAIL'}] {check.name} — {check.detail}")
        print(f"verdict={acceptance.verdict}")
        print(f"Wrote {out}")
        return 0 if acceptance.accepted else 1

    if not args.workspace:
        parser.error("--workspace is required to draw the target set (or use --dry-run)")

    session = _open_session(args.db_url)
    try:
        targets = build_amazon_target_set(
            session, workspace_id=args.workspace, size=args.n,
            domain=args.domain, seed=args.seed,
        )
    finally:
        session.close()

    # EPA C3: the SET is drawn deterministically above (selection-key sort,
    # reproducible run over run); the EXECUTION order both phases must
    # replay is a separate, randomized-but-paired shuffle of that same set
    # (see `randomized_execution_order`'s docstring for why).
    execution_order = randomized_execution_order(targets, seed=args.seed)

    target_set_path = out.parent / "target_set.json"
    _write(target_set_path, json.dumps(
        {
            "record_type": "b5_document_only_target_set",
            "tool_version": CANARY_TOOL_VERSION,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_filter": args.workspace,
            "domain": args.domain,
            "selection_seed": args.seed,
            "requested_size": args.n,
            "sample_size": len(targets),
            "max_usd": args.max_usd,
            "proxy_canary_username_env": PROXY_CANARY_USERNAME_ENV,
            "targets": targets,
            "execution_order": execution_order,
        },
        indent=2, sort_keys=True,
    ) + "\n")
    print(f"targets={len(targets)} (requested {args.n})")
    print(f"Wrote {target_set_path}")
    print(runbook_text(domain=args.domain, sample_size=args.n, max_usd=args.max_usd))
    if not proxy_canary_username_configured():
        print(
            f"NOTE: {PROXY_CANARY_USERNAME_ENV} is not set on this host. This host only "
            "draws the target set; the load-bearing setting is on the scrapers-browser "
            "service before the live run (see the runbook).",
            file=sys.stderr,
        )
    if len(targets) < args.n:
        print(
            f"UNDERSIZED: only {len(targets)} ACTIVE {args.domain} targets in this "
            "workspace — a smaller sample makes both rates noisier, say so in the report.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
