# Observability, SLOs and alert rules

> Closes audit risk **H5** — "Production observability and automatic stop-loss
> controls are insufficient" —
> (`CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md` §H5, §7, §8, §12).
>
> Companion document: [`RUNBOOK_STOP_DISPATCH_AND_SPEND.md`](./RUNBOOK_STOP_DISPATCH_AND_SPEND.md).
> Implementation: `libs/shared/app_shared/opsmetrics/`, `apps/api/app/routers/ops_metrics.py`.

---

## 1. The design test

This cycle found four production failures. **All four were silent.** That, not
the absence of a dashboard, is the problem being solved. Every signal below was
chosen because at least one of these would have been caught by it:

| # | Silent failure | Signal that catches it | Severity |
|---|---|---|---|
| 1 | No `2026_09` partition on any of the 4 partitioned tables — every INSERT fails at midnight 2026-09-01. Unnoticed for weeks. | `partition.next_month_missing` | CRITICAL inside 14 days |
| 2 | The daily rollup has **never** run — 0 rows against 32,231 observations. Retention can therefore never drop a partition. | `rollup.never_run` | CRITICAL |
| 3 | extra.com burned ~87,082 proxy requests / 12.99 GiB ≈ $13 this month, *after* the runaway was believed fixed. | `discovery.runs_per_domain_per_day` + `discovery.surge` | CRITICAL |
| 4 | RLS is inert in production — every service connects as a superuser that bypasses it. | `security.rls_inert` | CRITICAL |

Validating the implementation against the live database added a **fifth**:

| 5 | The pipeline had been completely silent for 2.6 days and `refresh_rules` held **zero** rows, so nothing was ever going to scrape on a schedule. | `freshness.pipeline_silent`, `freshness.no_refresh_rules` | CRITICAL / HIGH |

Failure 5 is the important one methodologically. Every *threshold* rule passes
trivially on an empty window: a system that has stopped producing numbers looks
identical to a healthy one. Liveness must be its own rule.

---

## 2. Metrics approach — decision and trade-offs

### What Railway can and cannot do (investigated 2026-08-15, not assumed)

| Capability | Available? | Evidence |
|---|---|---|
| Scrape a Prometheus `/metrics` endpoint | **No** | No such feature in Railway's metrics docs; the only data sources are CPU, memory, network, disk, logs, project usage. |
| Alert on log content | **No** | Monitors cover "CPU, RAM, disk usage, or network egress" only. The observability dashboard *filters* logs into widgets but does not alert on them. |
| HTTP endpoint health-check alerting | **No** | Monitors have no endpoint-probe type. |
| Alert on custom application metrics | **No** | Same limitation. |
| Alert on CPU / RAM / disk / egress thresholds | **Yes** (Pro plan) | Railway Monitors. |
| Webhook on deployment state change + monitor alerts | **Yes** | Railway project webhooks. |
| Third-party (Datadog / Grafana / OTel collector) | Yes, but requires **running another service** | Railway's own guidance for anything beyond the above. |

### The decision

**Compute the metrics and evaluate the rules *inside the product*, deliver via
(a) a pull-based ops endpoint and (b) the structured JSON log stream that
already exists.** No new runtime dependency, no new service, no new table.

Concretely:

- `app_shared.opsmetrics.snapshot.collect_snapshot(session)` — one read-only SQL
  pass over tables that already exist.
- `app_shared.opsmetrics.rules.evaluate(snapshot)` — pure functions, thresholds
  in code, fully unit-tested.
- `app_shared.opsmetrics.emit.emit_snapshot(...)` — one `ops.snapshot` JSON line
  plus one `ops.alert` line per firing rule, per `contracts/observability.md`.
- `GET /ops/metrics` — JSON (or Prometheus text) for a human or a poller.

### Why not the alternatives

| Option | Why rejected |
|---|---|
| **Prometheus / OTel / StatsD client library** | Nothing in this deployment would scrape or receive it. It would add a dependency and a code surface whose only observable effect is a slightly larger image. Cheap-and-deployed beats sophisticated-and-aspirational. |
| **Run a Prometheus + Grafana (or OTel collector) service on Railway** | A ninth and tenth service, their own memory and disk, their own retention, their own failure modes — against a **$9.08/month** platform floor. The monitoring stack would cost more than the thing it monitors. Revisit if the catalog or the customer count grows an order of magnitude. |
| **Sentry** | Solves error aggregation, which is not the gap. None of the five failures raised an exception anywhere. |
| **Lean purely on log-based alerting** | Railway cannot alert on log content. Logs remain the *record*; they cannot be the *trigger*. |
| **A periodic computed-metrics snapshot table in Postgres** | Would give history and trivial rate-of-change, but adds a write path, retention, and — given failure #1 — probably a partition. Rate-of-change is instead computed **statelessly** (see §3), which removes the reason to have it. Worth adding later *only* if trend charts become a product requirement. |

### The honest cost of this choice

1. ~~**Nothing polls the endpoint yet.**~~ **Closed.** The scheduler now runs
   `emit_snapshot(collect_snapshot(session))` every
   `OPS_SNAPSHOT_INTERVAL_SECONDS` (default 900) on the BYPASSRLS system
   session — `_run_ops_snapshot_tick` in
   `apps/scheduler/app/scheduler/scheduler_app.py`. The rules are therefore
   evaluated on a cadence rather than only when a human curls the endpoint,
   and every firing rule lands in the log stream as an `ops.alert` line.
   `GET /ops/metrics` remains the pull surface for ad-hoc inspection.
2. **No history.** Every number is computed from the durable tables at request
   time. There are no trend charts and no retention of past snapshots. Windowed
   comparisons (§3) recover most of the value; charts are not recovered.
3. **No push.** Nothing wakes a human at 03:00. The realistic delivery path is a
   scheduled task that POSTs firing CRITICAL alerts to a Slack/Discord webhook —
   also a handoff, deliberately not built here because the operator's
   destination is a deployment decision, not a code one.
4. **The Prometheus rendering is unconsumed.** `?format=prometheus` exists
   because it costs ~60 lines and zero dependencies, and it makes the "no TSDB"
   decision reversible in an afternoon. Emitting it is not a claim that anything
   reads it.

---

## 3. Rate-of-change without storing state

The audit is explicit that absolute values alone are insufficient, and the
extra.com incident is why: month-to-date spend looked survivable throughout,
while the rate was the tell.

Rather than a metrics-history table, every rate signal compares a window against
**a baseline drawn from the same immutable append-only table in the same SQL
pass**:

| Signal | Current | Baseline | Note |
|---|---|---|---|
| `cost.spend_acceleration` | proxied attempts, trailing 24h | the 24h *before* that | |
| `cost.month_end_forecast` | trailing 1h and 24h rate | extrapolated over the seconds left in the calendar month | same arithmetic the proxy breaker trips on |
| `discovery.surge` | discovery runs, trailing 24h | daily mean of the **preceding 6 days** | today is excluded from its own baseline |

The last row is a correction found by backtesting against the real incident.
Dividing by the full 7-day mean puts today inside its own denominator and caps
the ratio at exactly **7.0** — every surging domain reported an identical `7.0x`,
so a 22x runaway and a 7x drift were indistinguishable. Excluding the current
window makes the ratio unbounded (and `inf` when a domain goes from a standstill
to activity, which is a real transition, not a divide error).

**Every rate rule carries an absolute floor** (e.g. ≥2,000 paid attempts,
≥30 discovery runs). A ratio computed on tiny numbers is noise, and an alerting
system that cries wolf gets muted — which is how you end up back at silent
failures.

Conversely, **rate rules alone are not enough**: fqtoners.com ran ~1,430
discovery runs/day for ten consecutive days. Its rate of change is flat. Only the
absolute-volume rule catches it. Both kinds are required, which is why both exist.

---

## 4. Service level objectives

Grounded in measured production figures. Where a figure is aspirational rather
than currently met, it says so.

### 4.1 Cost

| SLO | Objective | Basis | Currently |
|---|---|---|---|
| Cost per full catalog refresh | ≤ $2.50 all-in | measured $2.00 for 4,587 links (2026-08-12) | Met |
| Fixed platform cost | ≤ $12/month | measured $9.08/month | Met |
| Proxied requests per unique link | ≤ 4.0 fleet-wide; ≤ 8.0 per domain | healthy amazon 2.48 req/URL (2026-08-11) | **Breached** — 30d: stech 12.72, amazon 8.44, noon 7.41 |
| Paid attempts producing no observation | ≤ 20% per domain | — (target; the audit's "% of spend producing no observation") | **Breached** — 30d: amazon 43.5%, noon 48.6% |
| Month-end proxy spend forecast | ≤ 80% of the binding ceiling | binding ceiling = min(breaker 250,000, provider ledger 60,000) | Met at rest; **breached on 2026-08-11** (forecast 275,861) |
| Spend per domain per day | ≤ $1.00 | a whole-catalog refresh is $2.00 | Met (pipeline idle) |
| Discovery runs per domain per day | ≤ 50 | `PROXY_BREAKER_MAX_DISCOVERY_RUNS_PER_DOMAIN_PER_DAY` | **Breached repeatedly** — extra.com 7,186/day; fqtoners ~1,430/day × 10 days |

### 4.2 Reliability

| SLO | Objective | Basis | Currently |
|---|---|---|---|
| Per-domain success, direct sites | ≥ 95% | measured pcpalace 98.7%, rawand 99.0%, rowadalahbar 98.2% | Met |
| Per-domain success, proxied sites | ≥ 85% | measured healthy amazon 74.2% / noon 89.2% / stech 97.3% | **Partly breached** — 30d amazon 51.7%, noon 49.3%, stech 56.9% |
| Fleet success rate | ≥ 90% | 2026-08-11 full scrape: 4,502/4,588 priced (98.1%) | Varies by cohort |
| Time since last scrape attempt | ≤ 24h | daily-cadence intent | **Breached** — 53h and counting |
| Enabled refresh rules | ≥ 1 | trivially | **Breached** — zero |
| Oldest PENDING target age | ≤ 1h | — | Met |
| Oldest non-terminal (DEFERRED) target age | ≤ 24h | `DEFERRED` is meant to be re-dispatched promptly | **Breached** — 35 days |

### 4.3 Data integrity

| SLO | Objective | Currently |
|---|---|---|
| Next month's partition exists on every partitioned table | always, ≥14 days ahead | **Breached** — 0 of 4 |
| Daily rollup lag | ≤ 2 days | **Breached** — has never run |
| DEAD outbox messages | 0 | Not deployed |
| Oldest PENDING outbox message | ≤ 15 min | Not deployed |

### 4.4 Controls

| SLO | Objective | Currently |
|---|---|---|
| Redis `maxmemory-policy` | `noeviction`, 0 evicted keys | Unverified from this process |
| Proxy circuit breaker reachable and evaluated | ≤ 50 min since last evaluation | **Breached** — table not deployed |
| ACTIVE proxy providers with a `monthly_budget_limit` | all | Met (1 of 1, 60,000) |
| Connected DB role is subject to RLS | always | **Breached** — `postgres`, superuser **and** BYPASSRLS |

---

## 5. Alert rules

Full definitions in `libs/shared/app_shared/opsmetrics/rules.py`. Every rule
carries a machine-readable `justification` naming the measurement its threshold
comes from; none were invented. Tunable via `Thresholds`.

Severity contract: **CRITICAL** = money burning, data being lost, or writes
failing (or failing on a known date) → page. **HIGH** = a control is inert or an
SLO is breached → same business day. **WARNING** = leading indicator → next
working day.

### 5.1 Cost (the audit's four named primary alarms)

| Rule | Threshold | Justification |
|---|---|---|
| `cost.requests_per_url` | HIGH ≥ 8.0, WARNING ≥ 4.0 req/unique URL (≥100 attempts) | HIGH is **the same number** as `PROXY_BREAKER_MAX_REQUESTS_PER_URL`, so the warning precedes the stop-loss instead of following it. WARNING is ~1.6× the measured healthy 2.48. |
| `cost.proxied_per_url` | HIGH ≥ 6.0 paid req/unique URL | Paid subset of the above; the runaway-loop signature. |
| `cost.spend_per_domain_per_day` | WARNING ≥ $1.00, HIGH ≥ $3.00 / domain / 24h | A refresh of the **entire** 4,587-link catalog is $2.00. One domain at $3/day exceeds a whole catalog refresh. |
| `cost.wasted_paid_rate` | HIGH ≥ 40% of paid attempts failing (≥200 paid) | Measured amazon 43.5%, noon 48.6%. Cost per *usable price* is unbounded as success → 0. |
| `cost.month_end_forecast` | CRITICAL ≥ binding ceiling, WARNING ≥ 75% | Forecast from 1h and 24h velocity. Uses `min(breaker ceiling, provider ledger cap)` — see §5.5. |
| `cost.spend_acceleration` | HIGH ≥ 4× day-over-day, floor 2,000 paid attempts | Rate-of-change. Floor ≈ $0.25 — enough to matter, small enough to catch early. |
| `cost.provider_without_budget` | HIGH if any ACTIVE provider has `monthly_budget_limit IS NULL` | `incr_and_check_monthly_budget` short-circuits before Redis when `limit is None`: that provider is never metered and never denied. |
| `discovery.runs_per_domain_per_day` | HIGH ≥ 50, CRITICAL ≥ 500 | HIGH is the breaker's own ceiling. CRITICAL is 10×; production hit 7,186. |
| `discovery.surge` | HIGH ≥ 3× the preceding 6-day daily mean, floor 30 runs | Catches a runaway still under any absolute ceiling. |
| `breaker.unavailable` / `.never_evaluated` / `.evaluator_stale` | HIGH | An undeployed or un-evaluated breaker cannot stop anything, silently. Stale = 10 missed 300 s evaluation intervals. |
| `breaker.open` | CRITICAL | Informational-critical: paid work **is** stopped and recovery is deliberately manual. |

### 5.2 Data integrity

| Rule | Threshold | Justification |
|---|---|---|
| `partition.current_month_missing` | CRITICAL | Inserts are failing now. |
| `partition.next_month_missing` | CRITICAL ≤ 14 days of fuse, else HIGH | 14 days = two weekly ops cycles. A registered-but-absent parent (`webhook_events` before its migration) is skipped, not alarmed. |
| `rollup.never_run` | CRITICAL | 0 rollup rows with observations present. Blocks retention *and* destroys the historical price series the product sells. |
| `rollup.stale` | HIGH > 2 days lag | The rollup runs daily; >2 days means consecutive runs were missed. |

### 5.3 Delivery

| Rule | Threshold | Justification |
|---|---|---|
| `outbox.dead` | HIGH ≥ 1 | DEAD is terminal. Any non-zero count is permanently lost work. |
| `outbox.backlog` | WARNING ≥ 500, HIGH ≥ 5,000 pending | |
| `outbox.oldest_pending_age` | HIGH > 15 min | The dispatcher polls in seconds; 15 min of un-dispatched backlog means it is not running. |

### 5.4 Reliability and infra

| Rule | Threshold | Justification |
|---|---|---|
| `reliability.domain_success_rate` | WARNING < 90%, HIGH < 70% (≥50 attempts) | 90% floor from the healthy direct sites (98–99%); 70% sits below every currently-healthy domain and above none. |
| `queue.pending_target_age` | HIGH > 1h | A target PENDING for an hour is not queued, it is lost. |
| `queue.started_target_age` | HIGH > 30 min | `MATCH_LOCK_BROWSER_TTL_SECONDS` — the longest a healthy in-flight target can hold its lock. |
| `queue.deferred_target_age` | WARNING > 24h | `DEFERRED` is non-terminal by design; a day old means nothing re-dispatched it. |
| `freshness.pipeline_silent` | HIGH > 24h, CRITICAL > 48h | See §1 failure 5. |
| `freshness.no_refresh_rules` | HIGH if 0 enabled | Nothing is scraped on a schedule. |
| `redis.policy_violation` | CRITICAL | An evicting Redis simultaneously duplicates paid work, erases the spend ledger, and drops throttles. |
| `redis.policy_unknown` | WARNING | `CONFIG GET` may be ACL-blocked; not fatal, but must be visible. |
| `redis.evictions` | CRITICAL ≥ 1 | Threshold is **zero**: one evicted key can drop a match lock *and* a spend counter. |
| `redis.memory` | HIGH ≥ 85% of maxmemory | Eviction pressure precedes eviction. |
| `scrapyd.saturation` | WARNING ≥ 2,000 in-flight targets | Scrapers are capped at 8 concurrent slots at both levels. |
| `optimizer.churn` | WARNING ≥ 50% of profiles rewritten in 24h (≥5 profiles) | The documented `learned-profile-overrides-policy` failure mode; churn also changes which requests are paid. |
| `security.rls_inert` | CRITICAL | Audit C3. |
| `*.unavailable` | HIGH | A blind section is indistinguishable from a passing one. |

### 5.5 Two ceilings, and which one binds

Production has **two independently configured** monthly proxy ceilings:

- `PROXY_BREAKER_MONTHLY_PROXIED_REQUESTS` = **250,000** (the circuit breaker), and
- `proxy_providers.monthly_budget_limit` = **60,000** (the Redis ledger, enforced per request).

They are an order of magnitude apart. `cost.month_end_forecast` therefore
forecasts against `min(...)` — the one that binds first. Against the breaker's
number, month-to-date spend reads **8.4% of budget**; against the enforced
number it is **34.8%**. Reporting only the larger would have been reassuring and
wrong.

---

## 6. Endpoint

```
GET /ops/metrics                      # JSON: snapshot + firing alerts
GET /ops/metrics?format=prometheus    # Prometheus text exposition
GET /ops/metrics?emit=true            # also write ops.snapshot / ops.alert log lines
Authorization: Bearer $SAAS_SERVICE_TOKEN
```

**Auth posture.** `/health` and `/version` are unauthenticated because what they
reveal is nothing or already-public build provenance. This endpoint is different
in kind: it reports fleet-wide aggregates across every workspace — per-domain
spend, month-to-date proxy cost, competitor hostnames, catalog-scale volumes —
and breaker state tells an attacker how much paid work they can induce before a
stop-loss fires. It is therefore guarded by `require_service_token`, the **same**
fail-closed static bearer as `/v1/admin/*`, for the same reason: it is a
cross-workspace operator surface, not a tenant one. It is excluded from the
public OpenAPI spec.

It runs on `get_auth_session()` (BYPASSRLS) deliberately: a workspace-scoped
session would show one tenant's slice of a fleet metric and **under-report
spend**, which is worse than useless.

It emits no secrets, no workspace ids, no URLs, and no tenant row data — only
counts, rates, enum names and public competitor hostnames. Collector failures are
reported as an exception class and message, truncated; never a traceback. All of
this is enforced by tests in `tests/unit/test_ops_metrics_endpoint.py`.

It always returns **200** when the endpoint itself worked, even with CRITICAL
alerts firing. A non-200 would conflate "the monitor is broken" with "the system
is unhealthy" — exactly the confusion H5 is about. Severity is in the body.

### Structured log events

| Event | Level | Meaning |
|---|---|---|
| `ops.snapshot` | INFO | Full snapshot, one line. |
| `ops.alert` | ERROR (CRITICAL/HIGH) / WARNING | One firing rule, one line — so `event=ops.alert severity=CRITICAL` is a usable Railway log filter. |
| `ops.all_clear` | INFO | A snapshot with zero firing alerts. |

Namespaced `ops.*` so they never collide with the existing `rate_limit.*`,
`dedup.*`, `proxy_ledger.*`, `proxy_breaker.*` and `redis.policy.*` families.

---

## 7. Handoffs

Not done here, deliberately, and owned elsewhere:

1. ~~**Nothing calls the endpoint on a schedule.**~~ **Done.**
   `_run_ops_snapshot_tick` calls `emit_snapshot(collect_snapshot(session))`
   every `OPS_SNAPSHOT_INTERVAL_SECONDS` (900). Note it is *not* a Celery beat
   task as sketched here — the scheduler is a custom tick loop, so this is one
   more interval accumulator alongside the refresh poll, the durable cadence
   poll and the health tick, and it inherits their posture: system session,
   read-only, errors logged and swallowed so an observability probe can never
   take down the process it observes.
2. **No push notification.** A task that POSTs firing CRITICAL alerts to a
   Slack/Discord webhook. Destination is a deployment decision.
3. **Scheduler partition/rollup ERROR events.** The scheduler work adds
   partition-missing and rollup-stale ERROR events. Their names should be added
   to the `ops.*` table above so the two systems agree on vocabulary; the
   snapshot already carries both signals independently.
4. **Scrapyd `daemonstatus.json`.** `collect_snapshot(scrapyd_status=...)`
   accepts a pre-fetched body; this module never opens a network connection of
   its own. Wiring the probe would fill in true daemon pending/running counts
   alongside the DB-derived in-flight count.
5. **Redis posture from the API process.** `redis_policy.last_report()` returns
   `None` until something in that process probes Redis, so the API currently
   reports `redis.unavailable` (WARNING) rather than a real posture. Calling
   `enforce_redis_memory_policy` at API startup would fix it.
