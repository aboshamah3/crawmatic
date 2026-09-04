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
    "PROXY_BYTES_PER_PAGE_CEILING",
    "SUCCESS_RATE_TOLERANCE_POINTS",
    "Acceptance",
    "Check",
    "PageRow",
    "PhaseSummary",
    "build_parser",
    "evaluate_acceptance",
    "main",
    "page_row_from_operation",
    "percentile",
    "render_report",
    "summarize_phase",
]

CANARY_TOOL_VERSION = "1"

#: The domain the 2026-09 canary is about. Not hardcoded into the policy —
#: the policy reads whatever an operator lists; this is just what the run
#: measures, and what the runbook tells the owner to list.
DOCUMENT_ONLY_DOMAIN = "amazon.sa"

DEFAULT_SAMPLE_SIZE = 50
DEFAULT_OUT = "evidence/canary-document-only-2026-09/REPORT.md"

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


_ACCEPTANCE_RULE_TEXT = (
    "1. candidate success rate is **within 3 percentage points** of the baseline's; AND\n"
    "2. candidate proxy bytes per page are **<= 0.4 MB** "
    f"({PROXY_BYTES_PER_PAGE_CEILING:,} bytes).\n"
)


def runbook_text(*, domain: str = DOCUMENT_ONLY_DOMAIN, sample_size: int = DEFAULT_SAMPLE_SIZE) -> str:
    """The two-phase runbook. Every step here is the OWNER's to perform."""
    return f"""\
### Two-phase runbook (owner-performed — this script performs none of it)

`BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS` is read once per Scrapyd
scrapers-browser process via the cached `get_settings()` singleton, so the
candidate phase requires a real configuration change and a redeploy of that
service. There is deliberately no second, canary-only toggle.

1. **Phase A (baseline)** — confirm `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS`
   is UNSET on the scrapers-browser service, then enqueue ONE job over the
   {sample_size}-target `{domain}` set drawn by this script
   (`target_set.json`, one workspace, one job). Record its `scrape_job_id`.
2. **Phase B (candidate)** — set
   `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS={domain}` on the scrapers-browser
   service, redeploy it, and enqueue the SAME target set again. Record its
   `scrape_job_id`.
3. **Restore** — unset the variable and redeploy, whatever the outcome. The
   rule stays off until the owner decides on this report.
4. **Compare** — re-run this script with
   `--baseline-job-id <A> --candidate-job-id <B>`; it reads
   `network_operations` (transport = BROWSER) for each job and writes this
   report.

Both phases must run the same target set, and phase B must not be the first
run of a target set that phase A also warmed — enqueue A and B in that order,
close together, so the comparison is about the rule and not about the hour.
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
        f"- setting under test: `BROWSER_PROXIED_DOCUMENT_ONLY_DOMAINS={domain}`",
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
            runbook_text(domain=domain, sample_size=sample_size),
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
        runbook_text(domain=domain, sample_size=sample_size),
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

_OPERATIONS_SQL = """
SELECT failure_reason,
       response_status,
       duration_ms,
       bytes_compressed
FROM network_operations
WHERE scrape_job_id = CAST(:job_id AS uuid)
  AND transport::text = 'BROWSER'
  AND (domain = :domain OR domain LIKE :domain_suffix)
"""


def _open_session(db_url: str | None):
    """Delegates to ``run_gate_d_canary`` — one DSN-resolution/secret rule for
    both canaries, and no second place that could start printing a URL."""
    from scripts.run_gate_d_canary import _open_session as _gate_d_open_session
    from scripts.run_gate_d_canary import _resolve_db_url

    return _gate_d_open_session(_resolve_db_url(db_url))


def load_phase_rows(session, job_id: str, *, domain: str) -> list[PageRow]:
    from sqlalchemy import text

    rows = session.execute(
        text(_OPERATIONS_SQL),
        {"job_id": job_id, "domain": domain, "domain_suffix": f"%.{domain}"},
    ).mappings().all()
    return [page_row_from_operation(dict(row)) for row in rows]


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
    parser.add_argument("--n", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f"pages per phase (default {DEFAULT_SAMPLE_SIZE})")
    parser.add_argument("--domain", default=DOCUMENT_ONLY_DOMAIN,
                        help=f"domain under test (default {DOCUMENT_ONLY_DOMAIN})")
    parser.add_argument("--seed", default="b5-document-only-2026-09",
                        help="selection seed — same seed draws the same target set")
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
            domain=args.domain, sample_size=args.n,
        ))
        print(runbook_text(domain=args.domain, sample_size=args.n))
        print(f"DRY RUN — nothing measured, nothing spent. Wrote {out}")
        return 0

    if bool(args.baseline_job_id) != bool(args.candidate_job_id):
        parser.error("--baseline-job-id and --candidate-job-id must be given together")

    if args.baseline_job_id and args.candidate_job_id:
        session = _open_session(args.db_url)
        try:
            baseline_rows = load_phase_rows(session, args.baseline_job_id, domain=args.domain)
            candidate_rows = load_phase_rows(session, args.candidate_job_id, domain=args.domain)
        finally:
            session.close()

        policy_version = _policy_version()
        baseline = summarize_phase("A baseline (unlisted)", baseline_rows,
                                   policy_version=policy_version)
        candidate = summarize_phase("B document-only (listed)", candidate_rows,
                                    policy_version=policy_version)
        acceptance = evaluate_acceptance(baseline, candidate)
        _write(out, render_report(
            baseline=baseline, candidate=candidate, acceptance=acceptance,
            domain=args.domain, sample_size=args.n,
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
            "targets": targets,
        },
        indent=2, sort_keys=True,
    ) + "\n")
    print(f"targets={len(targets)} (requested {args.n})")
    print(f"Wrote {target_set_path}")
    print(runbook_text(domain=args.domain, sample_size=args.n))
    if len(targets) < args.n:
        print(
            f"UNDERSIZED: only {len(targets)} ACTIVE {args.domain} targets in this "
            "workspace — a smaller sample makes both rates noisier, say so in the report.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
