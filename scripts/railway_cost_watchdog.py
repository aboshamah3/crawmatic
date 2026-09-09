#!/usr/bin/env python3
"""railway_cost_watchdog.py — daily Railway memory/CPU/egress telemetry + leak alarm.

Pulls the last 24 hours of per-service ``MEMORY_USAGE_GB``, ``CPU_USAGE``
and ``NETWORK_TX_GB`` (egress) from Railway's ``usage`` GraphQL API for
the project named by ``RAILWAY_PROJECT_ID``, compares each service's
**average resident memory** against a hand-measured baseline, prints a
table plus ALERT lines, and appends a dated one-line summary (total
resident GB + estimated $/day + the hourly-vs-window reconciliation) to
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

**Read-only**: issues nothing but ``usage``/``project`` queries; it never
mutates a Railway resource.

**Project by environment, not by a hard-coded ID (deep dive §8.3).** The
old script pointed at a project ID and a ``{serviceId: name}`` map that
belonged to a DIFFERENT Railway project than this one — its output was
never a core-project cost read. ``RAILWAY_PROJECT_ID`` is now required
(the script refuses to run without it, loudly, rather than silently
reporting someone else's numbers), and the service ID → name map is
pulled from the API's own ``project.services`` for that project instead
of being copy-pasted and left to rot.

Query shape matters: Railway's ``usage`` aggregate was suspected of
under-reporting by ~3.7× when a long window is requested with
``groupBy: [SERVICE_ID]`` (measured 2026-08-03 against per-hour sums) —
**not reproduced on 2026-09-06** (hourly and single-window CPU/egress
sums agreed; memory differed by ~0.003%, deep dive §8.3). The per-hour
query stays (it is still the more conservative shape and costs nothing
extra), but this script now also runs the single-window query itself and
reports ``hourly_sum_vs_window_delta_pct`` for CPU/RAM/egress every day,
warning above :data:`RECONCILIATION_WARN_PCT` — so a *future*
recurrence of the discrepancy is caught fresh rather than an inherited
correction factor being trusted blindly.

Auth comes from the Railway CLI's own credentials
(``~/.railway/config.json`` → ``user.accessToken``); the API rejects the
default urllib User-Agent, hence the explicit header.

Usage::

    RAILWAY_PROJECT_ID=<project id> python3 scripts/railway_cost_watchdog.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
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

#: The core project's id, from the environment — never hard-coded (deep
#: dive §8.3: the previous constant pointed at a DIFFERENT project).
#: :func:`project_id` is the only place this is read.
RAILWAY_PROJECT_ID_ENV_VAR = "RAILWAY_PROJECT_ID"

LOG_PATH = Path.home() / ".crawmatic" / "railway_cost_watchdog.log"

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

#: Railway measurement names -> the short label this script reports them
#: under (deep dive §8.3: "CPU/RAM/egress"). ``NETWORK_TX_GB`` is
#: outbound traffic — egress is what is billed.
MEASUREMENTS = ("MEMORY_USAGE_GB", "CPU_USAGE", "NETWORK_TX_GB")
MEASUREMENT_LABELS = {
    "MEMORY_USAGE_GB": "ram",
    "CPU_USAGE": "cpu",
    "NETWORK_TX_GB": "egress",
}

#: Percent difference between the hourly-summed and single-window usage
#: totals above which the reconciliation warns (deep dive §8.3).
RECONCILIATION_WARN_PCT = 2.0

USAGE_QUERY = """
query($p:String!,$s:DateTime!,$e:DateTime!){
  usage(projectId:$p,measurements:[MEMORY_USAGE_GB,CPU_USAGE,NETWORK_TX_GB],
        startDate:$s,endDate:$e,groupBy:[SERVICE_ID]){
    measurement value tags{serviceId}
  }
}
"""

SERVICES_QUERY = """
query($p:String!){
  project(id:$p){
    services{
      edges{ node{ id name } }
    }
  }
}
"""


def project_id() -> str:
    """The core project's id from ``RAILWAY_PROJECT_ID`` — refuses to run
    without it (deep dive §8.3: a hard-coded id silently pointed this
    script at a project that was not this one)."""
    value = os.environ.get(RAILWAY_PROJECT_ID_ENV_VAR, "").strip()
    if not value:
        sys.exit(
            f"{RAILWAY_PROJECT_ID_ENV_VAR} is not set — refusing to guess which "
            "Railway project to read (deep dive §8.3: the old hard-coded project "
            "id pointed at a DIFFERENT project than this one)"
        )
    return value


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


def fetch_service_names(token: str, pid: str) -> dict[str, str]:
    """``{serviceId: name}`` for ``pid``, from the API itself.

    Replaces the old hand-maintained ``SERVICE_NAMES`` dict (deep dive
    §8.3) — a hard-coded copy belonging to a different project drifts
    silently the moment a service is added, renamed or removed; this
    reads the live truth on every run. A service id the API did not
    return a name for falls back to the raw id (never dropped from the
    report — an unnamed service is still a service being billed).
    """
    result = _graphql(token, SERVICES_QUERY, {"p": pid})
    edges = (
        ((result.get("data") or {}).get("project") or {}).get("services") or {}
    ).get("edges") or []
    names: dict[str, str] = {}
    for edge in edges:
        node = (edge or {}).get("node") or {}
        service_id = node.get("id")
        name = node.get("name")
        if service_id:
            names[service_id] = name or service_id
    return names


def _usage_totals_for_window(
    token: str, pid: str, start: dt.datetime, end: dt.datetime, service_names: dict[str, str]
) -> dict[str, dict[str, float]]:
    """One ``usage`` call over ``[start, end)`` — the single-window shape
    the 2026-08-03 incident suspected of under-reporting. Used both by
    the reconciliation check and, per-hour, by :func:`fetch_last_24h`."""
    result = _graphql(
        token,
        USAGE_QUERY,
        {
            "p": pid,
            "s": start.strftime("%Y-%m-%dT%H:00:00Z"),
            "e": end.strftime("%Y-%m-%dT%H:00:00Z"),
        },
    )
    usage = (result.get("data") or {}).get("usage")
    if usage is None:
        return {}
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for entry in usage:
        name = service_names.get(entry["tags"]["serviceId"], entry["tags"]["serviceId"])
        totals[name][entry["measurement"]] += entry["value"]
    return {name: dict(values) for name, values in totals.items()}


def fetch_last_24h(
    token: str, pid: str, service_names: dict[str, str]
) -> tuple[dict[str, dict[str, float]], list[str], str, dict[str, dict[str, float]]]:
    """Return ``({service: {measurement: total}}, failed_hours, window,
    single_window_totals)``.

    One query per hour (see the module docstring on why), summed
    client-side, PLUS one single 24h-window query — the shape a 2026-08-03
    incident suspected of under-reporting by ~3.7×, not reproduced on
    2026-09-06 (deep dive §8.3). Both are returned so :func:`main` can
    reconcile them fresh every run instead of trusting an inherited
    correction factor. Hours that never returned data are reported
    rather than silently counted as zero.
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
                    "p": pid,
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
                name = service_names.get(
                    entry["tags"]["serviceId"], entry["tags"]["serviceId"]
                )
                totals[name][entry["measurement"]] += entry["value"]

    window = f"{start:%Y-%m-%d %H:00}Z..{now:%Y-%m-%d %H:00}Z"
    window_totals = _usage_totals_for_window(token, pid, start, now, service_names)
    return {name: dict(values) for name, values in totals.items()}, failed, window, window_totals


def aggregate_measurement_totals(totals: dict[str, dict[str, float]]) -> dict[str, float]:
    """Sum every service's per-measurement total into one project-wide
    figure per measurement — what the hourly-vs-window reconciliation
    compares."""
    aggregate: dict[str, float] = defaultdict(float)
    for measurements in totals.values():
        for measurement, value in measurements.items():
            aggregate[measurement] += value
    return dict(aggregate)


def reconcile_hourly_vs_window(
    hourly_totals: dict[str, dict[str, float]],
    window_totals: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Percent difference between the hour-by-hour sum and the single
    whole-window query, per measurement label (``cpu``/``ram``/``egress``).

    This is the check that would have caught the 2026-08-03 suspected
    3.7x under-report. It was NOT reproduced on 2026-09-06 (deep dive
    §8.3: "do not apply an inherited correction factor blindly"), so
    rather than hard-coding a fixed correction, the comparison itself
    runs fresh on every invocation.
    """
    hourly_by_measurement = aggregate_measurement_totals(hourly_totals)
    window_by_measurement = aggregate_measurement_totals(window_totals)
    deltas: dict[str, float] = {}
    for measurement in MEASUREMENTS:
        label = MEASUREMENT_LABELS[measurement]
        hourly = hourly_by_measurement.get(measurement, 0.0)
        window = window_by_measurement.get(measurement, 0.0)
        if hourly == 0.0 and window == 0.0:
            deltas[label] = 0.0
        elif hourly == 0.0:
            deltas[label] = 100.0
        else:
            deltas[label] = 100.0 * abs(hourly - window) / hourly
    return deltas


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


def _format_reconciliation(deltas: dict[str, float]) -> str:
    """``cpu:0.12%,ram:0.00%,egress:1.87%`` — the log/print form of
    :func:`reconcile_hourly_vs_window`'s result, ordered CPU/RAM/egress."""
    order = ("cpu", "ram", "egress")
    return ",".join(f"{label}:{deltas.get(label, 0.0):.2f}%" for label in order)


def append_log(
    window: str,
    total_gb: float,
    cost: float,
    alerts: list[str],
    reconciliation: dict[str, float],
) -> None:
    """Append the dated summary line (+ any alerts) to the ops log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"{stamp} window={window} total_resident={total_gb:.2f}GB "
        f"est=${cost:.2f}/day (${cost * 30.4:.2f}/mo) alerts={len(alerts)} "
        f"hourly_sum_vs_window_delta_pct={_format_reconciliation(reconciliation)}"
    ]
    lines += [f"{stamp}   {alert}" for alert in alerts]
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    pid = project_id()
    token = _read_token()
    service_names = fetch_service_names(token, pid)
    totals, failed, window, window_totals = fetch_last_24h(token, pid, service_names)
    if not totals:
        print("no usage data returned — check credentials/network", file=sys.stderr)
        return 1

    rows, alerts, total_gb, cost = analyse(totals)

    reconciliation = reconcile_hourly_vs_window(totals, window_totals)
    for label, delta_pct in reconciliation.items():
        if delta_pct > RECONCILIATION_WARN_PCT:
            alerts.append(
                f"ALERT RECONCILE  {label}: hourly-sum-vs-single-window delta "
                f"{delta_pct:.2f}% exceeds the {RECONCILIATION_WARN_PCT:.0f}% ceiling "
                "(deep dive §8.3 — verify before trusting this run's cost figures)"
            )

    print(f"Railway usage {window} (project {pid})")
    print(f"  {'service':<18}{'avg resident':>11}{'ratio':>10}{'':10}{'CPU':>9}")
    print("\n".join(rows))
    if failed:
        print(f"  (!) no data for {len(failed)} hour(s): {', '.join(failed)}")
    print(
        f"\ntotal resident {total_gb:.2f} GB | est ${cost:.2f}/day "
        f"(${cost * 30.4:.2f}/mo, mem @$10/GB-mo + cpu @$20/vCPU-mo)"
    )
    print(f"hourly-sum-vs-window delta: {_format_reconciliation(reconciliation)}")
    print("\n".join(alerts) if alerts else "no alerts")

    append_log(window, total_gb, cost, alerts, reconciliation)
    print(f"appended to {LOG_PATH}")
    return 2 if alerts else 0


if __name__ == "__main__":
    raise SystemExit(main())
