#!/usr/bin/env python3
"""railway_cost_watchdog.py — daily Railway memory/CPU/cost telemetry + leak alarm.

Pulls the last 24 hours of per-service ``MEMORY_USAGE_GB`` and
``CPU_USAGE`` from Railway's ``usage`` GraphQL API, compares each
service's **average resident memory** against a hand-measured baseline,
prints a table plus ALERT lines, and appends a dated one-line summary
(total resident GB + estimated $/day) to
``~/.crawmatic/railway_cost_watchdog.log``.

Why it exists: on 2026-08-03 the ``scrapers`` service leaked to a
7.65 GB plateau and held it for ~10 hours *while idle* before anyone
noticed — roughly $2.5/day of memory burn on a $10-12/mo baseline. The
in-container guards (``app_shared.memory_watchdog``, Scrapy's
``MemoryUsage``, Celery's ``worker_max_memory_per_child``) now stop that
automatically; this script is the out-of-band check that the guards are
actually working, and the place a *slow* cost regression shows up.

**Leak signature**: high resident memory with almost no CPU. A real
~3k-product run burns both, so memory alone is not evidence; memory
above 3× baseline while the service spent under
``LEAK_CPU_CEILING_VCPU_MIN`` vCPU-minutes across the whole day is the
idle-plateau shape of the 2026-08-03 incident, and is reported
separately from the plain >2× "heavy" alert.

**Read-only**: issues nothing but ``usage`` queries; it never mutates a
Railway resource.

Query shape matters: Railway's ``usage`` aggregate under-reports by
~3.7× when a long window is requested with ``groupBy: [SERVICE_ID]``
(measured 2026-08-03 against per-hour sums), so this queries **one hour
at a time** and sums client-side. Do not "optimise" it into a single
24-hour call.

Auth comes from the Railway CLI's own credentials
(``~/.railway/config.json`` → ``user.accessToken``); the API rejects the
default urllib User-Agent, hence the explicit header.

Usage::

    python3 scripts/railway_cost_watchdog.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAILWAY_CONFIG_PATH = Path.home() / ".railway" / "config.json"
RAILWAY_API_URL = "https://backboard.railway.com/graphql/v2"
USER_AGENT = "crawmatic-cost-watchdog/1.0"

PROJECT_ID = "76296e99-f8d6-4001-b6ac-10b9d2e20ea1"

LOG_PATH = Path.home() / ".crawmatic" / "railway_cost_watchdog.log"

SERVICE_NAMES = {
    "2658aa25-3ad6-46f6-b0d0-b0cb1b1b453f": "scrapers",
    "2cbe7c21-3cc4-4438-adcd-fa65db210338": "postgres",
    "5cdd7fc5-9d30-441e-a120-b900b615ab50": "api",
    "6fb3959d-40ba-4657-b2e7-2a01560d8f73": "pgbouncer",
    "7b9f7c1b-b006-41be-9c07-16f1b60b8d9f": "scheduler",
    "82783a46-cecb-4de9-98ec-e0deb0c50efb": "redis",
    "cbfcc0c3-45c1-4285-8b89-7ab69da45745": "worker",
    "dfcc8c8a-76ea-4772-9413-b5f1b313d786": "migrate",
    "e0115ef8-17da-4eac-a2a5-7c19768ed10c": "scrapers-browser",
}

# Hand-measured healthy steady-state resident memory, GB (2026-08-02,
# after the concurrency=4 fix). Deliberately *not* the observed peak: a
# service that doubles its own baseline is worth looking at even if the
# absolute number is small.
BASELINES_GB = {
    "worker": 0.6,
    "scrapers": 0.5,
    "scrapers-browser": 0.5,
    "api": 0.2,
    "scheduler": 0.15,
    "postgres": 0.2,
    "redis": 0.1,
    "pgbouncer": 0.05,
}

ALERT_MULTIPLIER = 2.0
LEAK_MULTIPLIER = 3.0
# vCPU-minutes over the whole 24h window below which a service counts as
# "idle" — a busy scrape run is orders of magnitude above this.
LEAK_CPU_CEILING_VCPU_MIN = 5.0

# Railway list prices: $10 per GB-month of memory, $20 per vCPU-month.
# Usage arrives in GB-minutes / vCPU-minutes, and 43200 = minutes/month.
MINUTES_PER_MONTH = 43200
MEMORY_RATE_PER_GB_MINUTE = 10 / MINUTES_PER_MONTH
CPU_RATE_PER_VCPU_MINUTE = 20 / MINUTES_PER_MONTH

HOURS = 24
MINUTES_PER_DAY = HOURS * 60

USAGE_QUERY = """
query($p:String!,$s:DateTime!,$e:DateTime!){
  usage(projectId:$p,measurements:[MEMORY_USAGE_GB,CPU_USAGE],
        startDate:$s,endDate:$e,groupBy:[SERVICE_ID]){
    measurement value tags{serviceId}
  }
}
"""


def _read_token() -> str:
    """Return the Railway CLI's access token, or exit with a clear error."""
    try:
        config = json.loads(RAILWAY_CONFIG_PATH.read_text())
    except OSError as exc:
        sys.exit(f"cannot read {RAILWAY_CONFIG_PATH}: {exc} (run `railway login`)")
    token = (config.get("user") or {}).get("accessToken")
    if not token:
        sys.exit(f"no user.accessToken in {RAILWAY_CONFIG_PATH} (run `railway login`)")
    return token


def _graphql(token: str, query: str, variables: dict) -> dict:
    """POST one GraphQL query; return the decoded body (never raises)."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        RAILWAY_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Railway 4xxs urllib's default UA.
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"httperror": exc.code, "body": exc.read().decode()[:500]}
    except OSError as exc:
        return {"error": str(exc)}


def fetch_last_24h(token: str) -> tuple[dict[str, dict[str, float]], list[str], str]:
    """Return ``({service: {measurement: total}}, failed_hours, window)``.

    One query per hour (see the module docstring on why), summed
    client-side. Hours that never returned data are reported rather than
    silently counted as zero.
    """
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(hours=HOURS)
    hours = [start + dt.timedelta(hours=i) for i in range(HOURS)]

    def pull(hour: dt.datetime) -> tuple[str, list]:
        nxt = hour + dt.timedelta(hours=1)
        result: dict = {}
        for attempt in range(5):
            result = _graphql(
                token,
                USAGE_QUERY,
                {
                    "p": PROJECT_ID,
                    "s": hour.strftime("%Y-%m-%dT%H:00:00Z"),
                    "e": nxt.strftime("%Y-%m-%dT%H:00:00Z"),
                },
            )
            if result.get("data"):
                break
            time.sleep(2 + 3 * attempt)
        return hour.strftime("%Y-%m-%d %H:00"), (result.get("data") or {}).get("usage")

    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    failed: list[str] = []
    with ThreadPoolExecutor(3) as pool:
        for label, usage in pool.map(pull, hours):
            if usage is None:
                failed.append(label)
                continue
            for entry in usage:
                name = SERVICE_NAMES.get(
                    entry["tags"]["serviceId"], entry["tags"]["serviceId"]
                )
                totals[name][entry["measurement"]] += entry["value"]

    window = f"{start:%Y-%m-%d %H:00}Z..{now:%Y-%m-%d %H:00}Z"
    return {name: dict(values) for name, values in totals.items()}, failed, window


def analyse(totals: dict[str, dict[str, float]]) -> tuple[list[str], list[str], float, float]:
    """Return ``(report_rows, alerts, total_resident_gb, cost_per_day)``."""
    rows: list[str] = []
    alerts: list[str] = []
    total_gb = 0.0
    cost = 0.0

    for name in sorted(totals, key=lambda n: -totals[n].get("MEMORY_USAGE_GB", 0.0)):
        gb_minutes = totals[name].get("MEMORY_USAGE_GB", 0.0)
        vcpu_minutes = totals[name].get("CPU_USAGE", 0.0)
        # Usage is GB-minutes accumulated over the window; dividing by
        # the window's minutes gives average resident GB.
        avg_gb = gb_minutes / MINUTES_PER_DAY
        total_gb += avg_gb
        cost += (
            gb_minutes * MEMORY_RATE_PER_GB_MINUTE
            + vcpu_minutes * CPU_RATE_PER_VCPU_MINUTE
        )

        baseline = BASELINES_GB.get(name)
        ratio = avg_gb / baseline if baseline else 0.0
        rows.append(
            f"  {name:<18}{avg_gb:8.3f} GB{'':2}"
            f"{(f'{ratio:5.1f}x' if baseline else '    -'):>8} baseline"
            f"{vcpu_minutes:9.2f} vCPU-min/day"
        )
        if not baseline:
            continue
        if ratio >= LEAK_MULTIPLIER and vcpu_minutes < LEAK_CPU_CEILING_VCPU_MIN:
            alerts.append(
                f"ALERT LEAK  {name}: {avg_gb:.2f} GB avg resident "
                f"({ratio:.1f}x baseline {baseline} GB) on only "
                f"{vcpu_minutes:.2f} vCPU-min/day — idle-plateau signature "
                "(2026-08-03); check WATCHDOG_MEMORY_LIMIT_MB is set on this service"
            )
        elif ratio >= ALERT_MULTIPLIER:
            alerts.append(
                f"ALERT MEM   {name}: {avg_gb:.2f} GB avg resident "
                f"({ratio:.1f}x baseline {baseline} GB), "
                f"{vcpu_minutes:.2f} vCPU-min/day"
            )

    return rows, alerts, total_gb, cost


def append_log(window: str, total_gb: float, cost: float, alerts: list[str]) -> None:
    """Append the dated summary line (+ any alerts) to the ops log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"{stamp} window={window} total_resident={total_gb:.2f}GB "
        f"est=${cost:.2f}/day (${cost * 30.4:.2f}/mo) alerts={len(alerts)}"
    ]
    lines += [f"{stamp}   {alert}" for alert in alerts]
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    token = _read_token()
    totals, failed, window = fetch_last_24h(token)
    if not totals:
        print("no usage data returned — check credentials/network", file=sys.stderr)
        return 1

    rows, alerts, total_gb, cost = analyse(totals)
    print(f"Railway usage {window} (project {PROJECT_ID})")
    print(f"  {'service':<18}{'avg resident':>11}{'ratio':>10}{'':10}{'CPU':>9}")
    print("\n".join(rows))
    if failed:
        print(f"  (!) no data for {len(failed)} hour(s): {', '.join(failed)}")
    print(
        f"\ntotal resident {total_gb:.2f} GB | est ${cost:.2f}/day "
        f"(${cost * 30.4:.2f}/mo, mem @$10/GB-mo + cpu @$20/vCPU-mo)"
    )
    print("\n".join(alerts) if alerts else "no alerts")

    append_log(window, total_gb, cost, alerts)
    print(f"appended to {LOG_PATH}")
    return 2 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
