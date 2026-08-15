# Runbook: stopping dispatch and proxy spend without corrupting in-flight jobs

> Closes the audit §12 promotion criterion: *"On-call runbooks identify how to
> stop dispatch and proxy spending without corrupting in-flight jobs."*
>
> Companion: [`OBSERVABILITY_SLO_AND_ALERTS.md`](./OBSERVABILITY_SLO_AND_ALERTS.md).
> Every alert emitted by `app_shared.opsmetrics` carries a `runbook` anchor into
> this file.

**Production database access is READ-ONLY for diagnosis.** Every write action
below is an explicit, deliberate operator step.

---

## 0. First 60 seconds

```bash
curl -sS -H "Authorization: Bearer $SAAS_SERVICE_TOKEN" \
  https://api-production-7193.up.railway.app/ops/metrics | jq '{
    worst: .worst_severity,
    alerts: [.alerts[] | {rule_id, severity, subject, message}],
    spend: .snapshot.spend,
    breaker: .snapshot.breaker
  }'
```

This is the single command that answers "what is wrong and is it costing money".
Then pick the section below matching the firing `rule_id`.

---

## 1. Stop proxy spend now {#stop-proxy-spend-now}

**Fires from:** `cost.requests_per_url`, `cost.proxied_per_url`,
`cost.spend_per_domain_per_day`, `cost.wasted_paid_rate`,
`cost.month_end_forecast`, `cost.spend_acceleration`,
`cost.provider_without_budget`, `breaker.unavailable`, `spend.unavailable`.

### 1.1 Why this is safe

Read `libs/shared/app_shared/access/breaker.py` before acting. The gate
(`paid_requests_allowed`) is consulted **only while deciding the next attempt**,
inside `_prepare_dispatch`, before any request is built. Therefore:

- an already-dispatched fetch runs to completion;
- its result persists normally through the single `mark_target` writer;
- its match lock releases normally;
- an OPEN breaker degrades a proxied plan exactly the way an exhausted budget
  does (`proxy_budget_exhausted=True`) — **direct** if the strategy has a direct
  step, otherwise a clean `LIMIT_REACHED` skip that the job can finalize on.

Nothing is corrupted, nothing is orphaned, and no job is left un-finalizable.
Propagation is bounded by `PROXY_BREAKER_STATE_CACHE_SECONDS` (**30 s**) — a trip
stops paid work fleet-wide within one cache generation.

### 1.2 Preferred: trip the circuit breaker manually

> **Precondition (as of 2026-08-15 this is NOT met in production):** the table
> `proxy_circuit_breakers` must exist. The deployed migration head is
> `b7d02a41c9e3`, which is behind the code — the breaker is **not deployed**, and
> `ops/metrics` reports `breaker.unavailable` accordingly. Until the migration
> lands, skip to §1.3.

> **Use `railway ssh`, not `railway run`.** `railway run` executes the command
> **on your local machine** with the service's environment variables pulled
> down — and `DATABASE_URL` points at a `*.railway.internal` hostname that does
> **not resolve** outside the platform, so it will simply fail to connect.
> `railway ssh` executes inside the running container, where it does.

```bash
cd /srv/crawmatic/crawmatic
railway ssh --service api --environment production -- python - <<'PY'
from app_shared.database import get_system_session
from app_shared.access.breaker import trip_breaker
from app_shared.models.proxy_breaker import ProxyBreakerTrip

with get_system_session() as s:
    trip_breaker(
        s,
        reason=ProxyBreakerTrip.MANUAL,
        detail="operator stop-loss: <incident id / reason>",
    )
    s.commit()
print("breaker OPEN; new paid requests denied within 30s")
PY
```

If SSH is unavailable, the equivalent can be done over the public database URL
(`railway variables --service postgres --kv | grep DATABASE_PUBLIC_URL`) with a
direct statement — the breaker row is a plain table and `trip_breaker` writes
nothing exotic:

```sql
INSERT INTO proxy_circuit_breakers
    (id, scope_key, state, trip_reason, detail, tripped_at, cleared_at,
     trip_count, evaluated_at, created_at, updated_at)
VALUES (gen_random_uuid(), 'global', 'OPEN', 'MANUAL',
        'operator stop-loss: <incident id>', now(), NULL, 1,
        now(), now(), now())
ON CONFLICT (scope_key) DO UPDATE
SET state = 'OPEN', trip_reason = 'MANUAL',
    detail = EXCLUDED.detail, tripped_at = now(),
    cleared_at = NULL, updated_at = now();
```

Verify: `curl .../ops/metrics | jq '.snapshot.breaker'` → `state: "OPEN"`.

`trip_breaker` is **idempotent**: re-tripping an already-OPEN breaker refreshes
the reason and detail but does not bump `trip_count` (that counter measures
distinct incidents, not evaluation passes).

### 1.3 Fallback while the breaker is undeployed: clamp the provider ledger

The Redis budget ledger *is* live and *is* enforced per paid request. Lowering
an ACTIVE provider's `monthly_budget_limit` below its month-to-date usage makes
`incr_and_check_monthly_budget` deny every subsequent paid request.

Read the current position first:

```sql
-- read-only
SELECT id, name, status, monthly_budget_limit FROM proxy_providers WHERE status = 'ACTIVE';
```

Then, deliberately:

```sql
UPDATE proxy_providers SET monthly_budget_limit = 1, updated_at = now()
WHERE name = 'dataimpulse-residential';
```

Caveats, in order of importance:

- **`monthly_budget_limit = NULL` is not "unlimited-safe", it is unmetered.**
  `incr_and_check_monthly_budget` short-circuits and returns allowed *before
  touching Redis* when `limit is None`. Never clear the column to "remove the
  cap"; that removes the *accounting*, which is the `cost.provider_without_budget`
  alert.
- Takes effect on the next paid request. In-flight fetches are unaffected, same
  as §1.1.
- Restore by setting the real limit back (production value: **60,000**).

### 1.4 Blunt instrument: stop the scrapers

Only if §1.2/§1.3 are unavailable. See §2 — stopping the scrapers leaves targets
`STARTED`, which needs the recovery step.

### 1.5 Do NOT do this

- **Do not set `PROXY_LEDGER_FAIL_OPEN=true` during a spend incident.** It does
  the opposite of what its name suggests in this context: it restores the
  historical *fail-open* behaviour so that when the Redis ledger is unreachable,
  paid requests are **allowed** instead of denied. It exists solely to keep
  proxied scraping alive during a Redis outage at knowing financial risk. Default
  is `false` (fail-closed) and should stay there. See §4.
- **Do not delete `request_attempts` rows** to "reset" a velocity alarm. That
  table is the breaker's only durable input; deleting it blinds the stop-loss.

---

## 2. Stop dispatch safely {#stop-dispatch-safely}

**Fires from:** `queue.pending_target_age`, `queue.started_target_age`,
`queue.deferred_target_age`, `queue.pending_job_age`, `queue.unavailable`,
`scrapyd.saturation`.

Stopping *dispatch* is different from stopping *spend*. Do it in this order —
the order is what keeps in-flight work intact.

### 2.1 Stop the source of new work first

```bash
cd /srv/crawmatic/crawmatic          # the Railway-linked working copy
railway status                       # confirm project/environment before touching anything

# Scale to zero replicas in the deployed region (production runs in `sfo`,
# which is a us-west region). NOTE the argument form: `railway scale` takes
# positional REGION=REPLICAS pairs, NOT a --replicas flag.
railway scale --service scheduler --environment production us-west=0
```

The scheduler is what claims due refresh rules and creates jobs. With it stopped,
no new `scrape_jobs` rows are created. Everything already queued continues.

### 2.2 Drain, do not kill

Watch until in-flight reaches zero:

```bash
watch -n 30 'curl -sS -H "Authorization: Bearer $SAAS_SERVICE_TOKEN" \
  https://api-production-7193.up.railway.app/ops/metrics \
  | jq ".snapshot.queue | {targets_pending, targets_started, targets_deferred}"'
```

A target in `STARTED` holds a Scrapyd slot **and** a Redis match lock
(`MATCH_LOCK_HTTP_TTL_SECONDS` 600 / `MATCH_LOCK_BROWSER_TTL_SECONDS` 1800).
Draining lets both release cleanly through `mark_target`. Budget ~30 minutes for
a browser-heavy batch.

### 2.3 Only then stop the workers/scrapers

```bash
railway scale --service worker           --environment production us-west=0
railway scale --service scrapers         --environment production us-west=0
railway scale --service scrapers-browser --environment production us-west=0
```

### 2.4 If you had to kill mid-flight

Targets are left `STARTED` with a `started_at` and no terminal status, and their
match locks expire on TTL rather than being released. They are **not corrupted** —
they are stalled. After the services are back:

```sql
-- read-only: what is actually stuck?
SELECT status, count(*), min(started_at) AS oldest_started
FROM scrape_job_targets
WHERE status IN ('PENDING','STARTED','DEFERRED')
GROUP BY status;
```

Reset only rows older than the longest lock TTL (1800 s) so you never race a
still-running fetch:

```sql
UPDATE scrape_job_targets
SET status = 'PENDING', started_at = NULL, locked_at = NULL
WHERE status = 'STARTED' AND started_at < now() - interval '30 minutes';
```

> **Known trap** (`railway-migration` note): a wedged spider blocks all 8
> concurrent slots and nothing reports it. The fix is a GraphQL
> `deploymentRestart` on the scrapers service, **then** resetting the affected
> job to `PENDING`. Restarting alone leaves the job stuck.
>
> **Known trap** (`scrapers-deploy-trap`): every scrapers deploy wipes the
> scrapyd egg. Re-register with `apps/scrapers/register_egg.py` or all scraping
> silently stalls.

### 2.5 Restart order

Reverse of shutdown: scrapers → scrapers-browser → worker → scheduler. Confirm
`freshness.seconds_since_attempt` starts falling before re-enabling the
scheduler.

---

## 3. Close the breaker {#close-the-breaker}

**Fires from:** `breaker.open`.

Recovery is **deliberately manual**. An auto-closing spend breaker re-arms the
same runaway it just stopped; whatever evaporated a month's budget in an hour
needs a human. Note that `evaluate_and_persist` only ever *trips* — a passing
evaluation on an OPEN breaker leaves it OPEN.

1. Read *why* it tripped — the measurement snapshot is stored on the row:

   ```bash
   curl -sS -H "Authorization: Bearer $SAAS_SERVICE_TOKEN" \
     https://api-production-7193.up.railway.app/ops/metrics \
     | jq '.snapshot.breaker, .snapshot.spend, .snapshot.discovery'
   ```

2. **Fix the cause.** `MONTHLY_SPEND` / `VELOCITY_*` → raise the ceiling only if
   the spend is genuinely intended. `REQUESTS_PER_URL` → a retry or dedup loop
   (consider `SCRAPE_URL_DEDUP`). `DISCOVERY_RUNS_PER_DOMAIN` → §5.

3. Close it:

   ```bash
   railway ssh --service api --environment production -- python - <<'PY'
   from app_shared.database import get_system_session
   from app_shared.access.breaker import close_breaker
   with get_system_session() as s:
       close_breaker(s)
       s.commit()
   print("breaker CLOSED")
   PY

   # SQL equivalent, if SSH is unavailable:
   #   UPDATE proxy_circuit_breakers
   #   SET state='CLOSED', trip_reason=NULL, detail=NULL,
   #       cleared_at=now(), updated_at=now()
   #   WHERE scope_key='global';
   ```

4. Paid work resumes within `PROXY_BREAKER_STATE_CACHE_SECONDS` (30 s). Watch
   `cost.spend_acceleration` for the next hour.

If it re-trips immediately, the cause is not fixed. Do not raise thresholds to
silence it.

---

## 4. Redis recovery {#redis-recovery}

**Fires from:** `redis.policy_violation`, `redis.policy_unknown`,
`redis.evictions`, `redis.memory`, `redis.unavailable`.

One Redis carries the Celery broker **and** dispatch sentinels, in-flight match
locks, webhook dedup, token buckets/semaphores, per-domain rate ceilings,
cooldowns, the monthly proxy budget ledger, defer budgets and strategy caches. An
eviction here simultaneously duplicates paid work, erases the spend ledger and
drops throttles.

### 4.1 `redis.policy_violation` (CRITICAL)

The server reports an eviction-capable `maxmemory-policy`. This is a static
deployment misconfiguration; it cannot repair itself.

```bash
railway ssh --service api --environment production -- \
  redis-cli -u "$REDIS_URL" CONFIG GET maxmemory-policy
railway ssh --service api --environment production -- \
  redis-cli -u "$REDIS_URL" CONFIG SET maxmemory-policy noeviction
```

Persist it in the Redis service configuration, not just at runtime. Note that
processes with `PROXY_REDIS_REQUIRE_NOEVICTION=true` (the default) **refuse to
start** on a confirmed violation — a crash loop is the intended, loud,
trivially-reversible signal. The escape hatch
(`PROXY_REDIS_REQUIRE_NOEVICTION=false`) downgrades every outcome to a warning
and should be temporary.

### 4.2 `redis.evictions` (CRITICAL) — keys were actually evicted

Assume the following are now untrustworthy, in this order:

1. **The monthly budget ledger** (`proxybudget:*`) — spend may be under-counted.
   Reconcile against `request_attempts` (the durable record) via
   `.snapshot.spend.proxied_month_to_date`, not against Redis.
2. **Match locks** (`lock:scrape:*`) — duplicate paid fetches may have occurred.
   Check `cost.requests_per_url` for the affected window.
3. **Rate ceilings / cooldowns** — a domain may have been over-fetched; check
   `reliability.domain_success_rate` for `HTTP_429` / `BLOCKED` spikes.

Fix the policy (§4.1), then raise `maxmemory` or reduce key pressure. The
`request_attempts` table is authoritative for spend; Redis never is.

### 4.3 `redis.policy_unknown` (WARNING)

`CONFIG GET` was refused, renamed, or ACL-blocked. Deliberately **not** fatal —
refusing to boot because we could not *ask* converts a permissions quirk into a
total outage. Verify the policy out-of-band once and move on.

### 4.4 Redis is down entirely

Default posture is **fail-closed**: `incr_and_check_monthly_budget` denies paid
requests when Redis errors, and the caller degrades the attempt to direct where
the strategy allows. This is correct — a Redis incident must not remove the cost
brake.

To knowingly accept financial risk and keep proxied scraping alive during a
prolonged outage:

```bash
railway variable set --service scrapers --environment production \
  PROXY_LEDGER_FAIL_OPEN=true
```

Setting a variable triggers a redeploy of that service (pass `--skip-deploys`
to stage it without one). A scrapers redeploy **wipes the scrapyd egg** — see
the `scrapers-deploy-trap` note in §2.4 and re-register it afterwards.

**Set a reminder to revert it.** With this flag on and Redis down, there is no
budget accounting anywhere; the Postgres circuit breaker (§1.2) is the only
remaining stop-loss, so do not enable it while the breaker is undeployed.

---

## 5. Runaway discovery {#runaway-discovery}

**Fires from:** `discovery.runs_per_domain_per_day`, `discovery.surge`,
`discovery.unavailable`.

This is the 2026-08-12 incident's signature, and the reason the breaker exists:
every individual request was legitimate, no gate was violated, and the run was on
course for ~$325/month.

1. Confirm scale and shape (read-only):

   ```sql
   SELECT date_trunc('day', created_at)::date AS day, domain, count(*) AS runs
   FROM strategy_discovery_runs
   WHERE created_at >= now() - interval '14 days'
   GROUP BY 1, 2
   HAVING count(*) > 50
   ORDER BY runs DESC;
   ```

2. Is it costing money? Discovery on a **direct** domain has zero proxy cost but
   still burns DB writes, request volume and wall time:

   ```sql
   SELECT count(*) FILTER (WHERE access_method IN ('PROXY_HTTP','PLAYWRIGHT_PROXY')) AS paid,
          count(*) AS total
   FROM request_attempts
   WHERE created_at >= now() - interval '24 hours'
     AND url LIKE '%<domain>%';
   ```

   If `paid > 0`, do §1 first.

3. Stop the loop. Disable the offending domain's strategy profile — **status
   `DISABLED`, not an edit to `access_policies`**: the optimizer rewrites manual
   policy edits, so an edit may silently do nothing (the documented
   `learned-profile-overrides-policy` failure mode).

4. Look for a normalization mismatch. The 2026-08-12 root cause was a `www`-prefix
   mismatch in rediscovery condition 8 (fixed in `36fd624`); the same class of bug
   makes the system believe a domain has never been discovered and re-discover it
   forever. Compare the `domain` values in `strategy_discovery_runs` against
   `domain_strategy_profiles`.

5. Note that **both** rules matter: extra.com spiked (caught by `discovery.surge`)
   while fqtoners.com ran ~1,430/day flat for ten days (caught only by the
   absolute rule). Do not disable one in favour of the other.

---

## 6. Partition or rollup alert {#partition-or-rollup-alert}

**Fires from:** `partition.next_month_missing`, `partition.current_month_missing`,
`rollup.never_run`, `rollup.stale`, `partition.unavailable`, `rollup.unavailable`.

### 6.1 Missing partition — this is a dated outage, not a risk

A monthly RANGE-partitioned table with no partition for month *M* rejects every
INSERT from the first instant of *M*. `days_until_next_month` in the alert is the
fuse length.

Confirm (read-only):

```sql
SELECT parent.relname AS parent, child.relname AS partition,
       pg_get_expr(child.relpartbound, child.oid) AS bounds
FROM pg_inherits i
JOIN pg_class parent ON parent.oid = i.inhparent
JOIN pg_class child  ON child.oid  = i.inhrelid
WHERE parent.relname IN ('price_observations','request_attempts',
                         'price_alert_events','webhook_events')
ORDER BY 1, 2;
```

Fix by running the partition-creation maintenance job, which is idempotent
(`CREATE TABLE ... PARTITION OF` with existence checks) and self-heals the
current **and** next month:

```bash
railway ssh --service worker --environment production -- python - <<'PY'
from datetime import UTC, datetime
from app_shared.database import get_system_session
from app_shared.maintenance.partitions import create_missing_partitions
with get_system_session() as s:
    report = create_missing_partitions(
        s, now_utc=datetime.now(UTC), lookahead_months=1
    )
    s.commit()
print(report)
PY
```

`lookahead_months=1` covers offset 0 (current month, self-healing) and offset 1
(next month). A registered-but-absent parent is recorded in
`tables_skipped_absent` rather than raising.

Re-verify with the query above, then with `/ops/metrics`. **Then find out why the
scheduled job was not running** — a manual fix that leaves the schedule broken
just resets the fuse.

### 6.2 Rollup never ran / is stale

Consequences, both of which are silent:

1. `price_observations` retention drops a partition only after verifying
   daily-rollup coverage (`registry.feeds_rollups`). No rollup ⇒ no partition is
   ever released ⇒ unbounded growth.
2. The daily rollup **is** the long-term price history the product sells.
   Observations age out; rollups are what survive. A rollup that never ran means
   the history is being lost as partitions eventually age.

Verify:

```sql
SELECT (SELECT count(*) FROM variant_price_daily_rollups) AS rollup_rows,
       (SELECT max(date) FROM variant_price_daily_rollups) AS max_rollup_date,
       (SELECT count(*) FROM price_observations)          AS observation_rows,
       (SELECT max(scraped_at) FROM price_observations)   AS max_observation;
```

Backfilling is owned by the rollup/retention module — do **not** hand-write
INSERTs into `variant_price_daily_rollups`; the unique arbiter is
`(workspace_id, product_variant_id, date)` and a hand-built row will collide with
or shadow the real job's output.

**Do not run retention until the rollup is healthy and has backfilled.** Retention
drops partitions; a rollup that has not covered them means the data is gone.

---

## 7. Pipeline silent {#pipeline-silent}

**Fires from:** `freshness.pipeline_silent`, `freshness.no_refresh_rules`,
`freshness.no_attempts_ever`, `freshness.unavailable`.

Nothing is being scraped. Work outward:

1. **Is anything scheduled at all?**
   ```sql
   SELECT count(*) FROM refresh_rules;
   ```
   Zero means nothing will ever run on a schedule, regardless of service health.
   This was the 2026-08-15 production state. Creating refresh rules is a product
   action, not an ops one.

2. **Are the services up?** `railway status` — check `scheduler`, `worker`,
   `scrapers`, `scrapers-browser`.

3. **Is the queue empty or stuck?** `/ops/metrics` → `.snapshot.queue`. All-zero
   pending/started with no recent attempts means nothing is being *created*
   (→ step 1). Non-zero with old timestamps means dispatch is wedged (→ §2.4).

4. **Is the scrapyd egg registered?** Every scrapers deploy wipes it; without it
   scraping stalls silently with no error. Re-register via
   `apps/scrapers/register_egg.py`.

5. **Is the breaker OPEN?** An OPEN breaker denies *paid* requests only — direct
   domains should still be producing attempts. Total silence is not the breaker.

---

## 8. RLS is inert {#rls-inert}

**Fires from:** `security.rls_inert`.

The connected role is a superuser or has `BYPASSRLS`, so every
`FORCE ROW LEVEL SECURITY` policy in the schema provides **zero** isolation. This
is audit **C3** and is a release gate, not an incident.

Confirm (read-only):

```sql
SELECT current_user, rolsuper, rolbypassrls
FROM pg_roles WHERE rolname = current_user;
```

Production on 2026-08-15 returns `postgres | t | t` — superuser **and**
`BYPASSRLS`.

Remediation is owned by the RLS role work (`scripts/rls_*`): provision the
ordinary application role as non-owner, non-superuser, non-`BYPASSRLS`, point
`DATABASE_URL` at it, and keep `AUTH_DATABASE_URL`/`SYSTEM_DATABASE_URL` as the
narrow documented BYPASSRLS seams. Re-check this endpoint afterwards — it is the
live proof, and it is cheap to re-run.

There is no safe partial mitigation. Until the role changes, treat cross-workspace
isolation as unenforced by the database and enforced only by application code.

---

## 9. Outbox backlog {#outbox-backlog}

**Fires from:** `outbox.dead`, `outbox.backlog`, `outbox.oldest_pending_age`,
`outbox.unavailable`.

- `outbox.unavailable` with `UndefinedTable` means the outbox migration is not
  deployed — the durable-delivery guarantee (audit H1) simply is not in effect,
  and async work can be lost on worker/broker failure. This is the current
  production state. Escalate as a deploy gap, not an incident.
- `outbox.oldest_pending_age` > 15 min with a non-trivial backlog means the
  dispatcher is not running. Check the worker service and the `maintenance` queue.
- `outbox.dead` is **terminal**: those messages will never publish. Inspect
  `last_error`, fix the cause, and re-queue deliberately. Never bulk-reset DEAD to
  PENDING without reading `last_error` first — one poison message will re-starve
  the queue.

---

## 10. Per-domain success drop {#per-domain-success-drop}

**Fires from:** `reliability.domain_success_rate`, `optimizer.churn`,
`domains.unavailable`.

A success-rate drop is a **cost** event as much as a quality one: cost per usable
price is spend ÷ successes, so it rises sharply before raw spend looks alarming.
Check `cost.wasted_paid_rate` for the same domain immediately.

1. Classify the failures:
   ```sql
   SELECT error_code, count(*)
   FROM request_attempts
   WHERE created_at >= now() - interval '24 hours' AND NOT success
   GROUP BY 1 ORDER BY 2 DESC;
   ```
   `BLOCKED` / `HTTP_403` / `HTTP_429` → anti-bot or rate limiting.
   `PRICE_NOT_FOUND` / `INVALID_PRICE_FORMAT` → the site changed; the extraction
   profile needs updating. `TIMEOUT` / `PROXY_FAILED` → transport.

2. If the domain is **proxied** and success is below ~50%, the honest move is to
   stop paying for it until extraction is fixed. Failed paid attempts are pure
   loss: on 2026-08-15, amazon.sa and noon.com had burned 4,156 and 2,845 wasted
   paid attempts respectively over 30 days.

3. `optimizer.churn` alongside a success drop suggests the optimizer is chasing
   noise. Pin the profile (`status = DISABLED` on the learned profile) rather
   than editing `access_policies`, which the optimizer will overwrite.
