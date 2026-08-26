#!/usr/bin/env python3
"""Browser saturation (EPA W5.5-GA item B, report §10).

NOT RUNNABLE HERE. This dev box has no browser fleet (`apps/scrapers-
browser`'s Playwright/Chromium pool is deployed as its own Railway
service in production — see `docker-compose.yml`'s `scrapers-browser`
service and `apps/scrapers-browser/Dockerfile`); there is nothing to
saturate on this box beyond whatever a single local headless Chromium
instance could show, which would not be a meaningful signal for a
concurrency-saturation scenario. This script therefore does not launch
anything — it always reports SKIPPED-here and documents the scenario a
real browser fleet should run, so the shape is specified once rather than
reconstructed from scratch whenever a fleet becomes available.

## The scenario to run against a real fleet

Drive N concurrent `apps/scrapers-browser/price_monitor_browser` jobs
(N sweeping past the fleet's configured `scrapers-browser` replica count x
per-instance Playwright context limit) against a fixture target set (never
production domains — reuse the existing loopback-fixture-server pattern
`tests/integration/_scrapyd_spider_live_support.py::serve_fixture_pages`
already established for live spider tests, so this needs no new safety
seam). Measure, per concurrency level:

* p50/p95/p99 time-to-first-byte and time-to-extracted-result per browser
  job;
* queueing/rejection behaviour once concurrency exceeds the fleet's
  configured capacity (does it queue with backpressure, or does memory/
  CPU degrade the whole fleet — i.e. does one saturated pass affect
  OTHER tenants' HTTP-mode jobs sharing the same worker fleet?);
* browser process memory high-water-mark per concurrent context, to
  correlate an observed OOM/restart against a specific concurrency
  level (Playwright's per-context overhead is documented as
  non-trivial; that number needs measuring on the real fleet's instance
  size, not guessed here).

## Parameters this scenario should sweep (fill in on a real fleet)

| Parameter | Suggested sweep | Notes |
|---|---|---|
| Concurrent browser contexts | 1, 2, 4, 8, 16, 32 | Compare against the fleet's per-instance/replica-count budget |
| Target page weight | light (small HTML) / heavy (image-and-script-heavy PDP) | Byte-accounting already exists (`request_attempt_byte_accounting`, READY-004) — reuse it to label "heavy" objectively |
| Duration | 5, 15, 60 minutes sustained | Distinguishes a burst spike from a sustained-saturation memory leak |

This is intentionally left as a specification, not a script that fakes a
result — a fabricated browser-saturation number would be worse than an
honest gap for a GA sign-off decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import report_and_print  # noqa: E402


def main() -> int:
    report_and_print(
        "browser_saturation",
        {
            "status": "SKIPPED-here",
            "reason": (
                "No browser fleet reachable on this dev box. See this file's "
                "module docstring for the fully-parameterized scenario to run "
                "against a real apps/scrapers-browser fleet."
            ),
            "finding": (
                "GAP, honestly labelled: browser saturation is un-measured for "
                "GA. Not a false pass — this scenario deliberately produces no "
                "numeric result rather than simulating one."
            ),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
