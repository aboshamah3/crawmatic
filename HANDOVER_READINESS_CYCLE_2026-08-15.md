# Handover — Production Readiness Cycle, 2026-08-15

**Branch:** `proxy-cost-reduction` · **Commit:** `4589249` · **Alembic head:** `b3e7c9a15d42`
**Tests:** 2398 passed, 0 failed · workspace scoping clean · exactly one head
**Production state:** unchanged except one variable (see §2). The deploy has **not** started.

---

## 0. READ FIRST — root session pitfalls

You said you'll continue as root. Root is needed for **exactly two things** on this box. Everything
else must run as `mahmoud`, or you will recreate problems that cost hours today.

### What actually needs root

```bash
docker system prune -a --volumes          # ~53G is stuck in root-owned space
chown -R mahmoud:mahmoud /srv/crawmatic/wt-proxy-cost/.venv   # optional, see below
```

### What must NOT run as root

| Command | Why root breaks it |
|---|---|
| `railway ...` | Credentials live in `/home/mahmoud/.railway`. Root has `/root/.railway`, which is **not logged in** — commands fail or act on the wrong account. |
| `git ...` | Creates root-owned objects inside `.git`, so `mahmoud` can no longer commit or fetch. |
| `uv` / `pytest` | This is the bug that broke today. The repo's `.venv` was built by root and symlinks into `/root/.local/share/uv/python/...`, which `mahmoud` cannot traverse. Every `uv run` failed with *Permission denied*. It also means the original audit's "2,051 passed" was a **root-only** run. |

### The safe pattern from a root shell

```bash
sudo -u mahmoud -H bash -lc 'cd /srv/crawmatic/crawmatic && railway status'
sudo -u mahmoud -H bash -lc 'cd /srv/crawmatic/wt-proxy-cost && git log --oneline -1'
```

`-H` matters — it sets `HOME=/home/mahmoud` so `railway` and `git` find their config.

### Running tests (as mahmoud, always)

The repo `.venv` is unusable. A working user-owned environment already exists — **use it**:

```bash
export UV_PROJECT_ENVIRONMENT=/srv/crawmatic/.venv-core
cd /srv/crawmatic/wt-proxy-cost
uv run pytest tests/unit -q          # expect: 2398 passed
bash scripts/check_single_head.sh    # expect: exactly 1 head, b3e7c9a15d42
uv run python scripts/check_workspace_scoping.py
```

Do **not** run `uv sync` as root in the worktree. If you want the in-repo `.venv` working, `chown` it
to `mahmoud` and re-sync **as mahmoud** with `uv sync --locked --all-packages` (plain `uv sync`
leaves workspace members uninstalled and produces ~136 collection errors).

### Disk

`/` was at 100% and broke agent work mid-flight. I freed ~3.5 G by deleting stale Claude session
scratchpads. **~53 G remains in root-owned space** (almost certainly `/var/lib/docker` from the
8-service compose builds) — that's the one genuinely useful thing to do as root. CI will need
headroom to build images.

---

## 1. What was done

All **critical and high** audit findings are closed in code, plus three production defects the audit
never found.

| ID | Finding | State |
|---|---|---|
| C1 | Price analysis rejected repeating-decimal averages | Fixed, 8 regressions |
| C2 | Release provenance uncontrolled | `/version` (SHA + code-vs-live-DB head), first CI workflow, config validation |
| C3 | RLS unverified | **Verified INERT in production**; guard + provisioning + probe built. Remediation NOT run |
| H1 | Async work can be lost | late-ack, reject-on-worker-lost, derived visibility timeout, transactional outbox |
| H2/H3 | Redis SPOF, cost brakes fail open | Policy assertion; paid work fails CLOSED; durable Postgres circuit breaker |
| H5 | No observability | Ops metrics, SLOs, alert rules, stop-spend runbook |
| M3 | Rollup non-sargable, missing indexes | Pruning proven on prod (1120 → 57 buffers); 5 hot-path indexes |
| — | **Daily maintenance dead for a month** | Two causes, both fixed (see §2) |
| — | **Discovery loop: 1,439 runs/day for 10 days** | Root cause fixed + two self-releasing bounds |
| — | **Config guard couldn't see the real misconfiguration** | Matched only `.env.example`'s role name |

### Corrections to the audit worth carrying forward

- **C2 is trivial.** `proxy-cost-reduction` is a *strict ancestor-superset* of `origin/main`,
  `origin/saas-phase2`, local `saas-phase2` and `master`. There is no divergence to reconcile —
  it is a pure fast-forward. The audit's framing made this sound risky; it isn't.
- **H2 was wrong about Redis.** Production `maxmemory-policy` is *already* `noeviction`,
  `evicted_keys: 0`. The real exposures are different: `maxmemory 0` against a 24 GB cgroup (kernel
  OOM-kill instead of clean errors) and `appendonly no` with `save 60 1` (**up to 60 s of
  acknowledged writes lost on crash**, including the spend ledger).
- **C3 is worse than stated.** Not "unverified" — measurably inert. `crawmatic_app` does not exist;
  every service connects as `postgres`, which is superuser *and* owner of all 40 tables.
- **M2 is bigger than stated.** Not 4 dead enum values but 7, plus `AdapterKey` — a `NOT NULL`
  column that is API-validated, persisted, and read by nothing.

---

## 2. The one production change already made

`SYSTEM_DATABASE_URL` was set on the Railway **worker** service (value copied from scheduler's
`AUTH_DATABASE_URL`: role `crawmatic_auth`, non-owner, direct to `postgres:5432`).

**Why:** live worker logs showed all three daily maintenance tasks arriving and immediately dying:

```
Task maintenance.partition_create[...] received
Task maintenance.daily_rollup[...]     received
Task maintenance.retention_drop[...]   received
... ERROR ... RuntimeError("SYSTEM_DATABASE_URL (or its AUTH_DATABASE_URL fallback) is required")
```

This is why `variant_price_daily_rollups` has **0 rows** against 32,231 observations, and why no
`2026_09` partition exists.

**Second, independent cause (fixed in code, not yet deployed):** `scheduler_app.py` drove daily
cadences off in-process float accumulators reset to `0.0` on every process start, against 86400 s
intervals. On Railway, any restart inside 24 h resets the countdown. Cadence state is now durable in
Postgres and partition lookahead is raised 1 → 3 months.

**Still to verify:** the September partition. The deployed code has `LOOKAHEAD=1`, and `offset=0` is
the current month / `offset=1` the next — so the next successful `partition_create` creates
September on its own, no DDL needed. The scheduler's accumulator last fired `2026-08-14 21:38 UTC`,
so the next attempt is ~21:38 nightly.

```sql
-- Expect price_observations_2026_09 to appear. If it has NOT by 2026-08-20, investigate.
SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid
JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='price_observations' ORDER BY 1;
```

**Hard deadline: 2026-09-01.** With no `2026_09` partition, every INSERT into `price_observations`,
`request_attempts`, `price_alert_events` and `webhook_events` fails — a total write outage.

**Handoff:** add `SYSTEM_DATABASE_URL` to `commonEnv` in `/srv/crawmatic/crawmatic/.railway/railway.ts`,
or the next infra apply drops it back out.

---

## 3. Deploy runbook

Railway builds from Dockerfiles via `railway up` (local directory upload) — there is **no GitHub
source wired**. So the deploy uploads whatever is in the working directory, and the push to `main`
is for release provenance only, not a deploy prerequisite.

Run everything below **as mahmoud**.

### Step 1 — release provenance

```bash
git -C /srv/crawmatic/wt-proxy-cost push origin HEAD:main
```

Re-verify FF safety first if any time has passed:

```bash
git -C /srv/crawmatic/wt-proxy-cost merge-base --is-ancestor origin/main HEAD && echo "FF safe"
```

### Step 2 — acknowledgement flag (MUST precede the code)

Production connects as `postgres`, so the new startup guard will **correctly refuse to boot**
without this. It logs at ERROR on every startup so it can't be forgotten silently.

```bash
cd /srv/crawmatic/crawmatic
for s in api worker scheduler scrapers scrapers-browser; do
  railway variables --service $s --set 'RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE=1'
done
```

> This is a deliberate, temporary acknowledgement that tenant isolation is inert. Remove it after §5.

### Step 3 — deploy, migrations first

```bash
cd /srv/crawmatic/wt-proxy-cost
railway link --project 69dc4bda-0d97-4290-a82f-822ed97d3fb8 --environment production
railway up --service migrate
```

Migrations are additive: 3 new tables (`proxy_circuit_breakers`, `outbox_messages`,
`maintenance_cadences`) plus 5 `CONCURRENTLY` indexes. No drops, no data movement. Prod head is
currently `b7d02a41c9e3`, so four migrations will apply.

If a `CONCURRENTLY` index fails midway it can leave an `INVALID` index. Check and drop/recreate:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

Then the rest, one at a time:

```bash
railway up --service worker
railway up --service scheduler
railway up --service scrapers
railway up --service scrapers-browser
railway up --service api          # api last: customer-facing surface moves last
```

### Step 4 — re-register the Scrapyd egg (DO NOT SKIP)

```bash
cd /srv/crawmatic/wt-proxy-cost && python apps/scrapers/register_egg.py
```

**Every scrapers deploy wipes the Scrapyd egg.** Skip this and *all scraping silently stalls* with no
error anywhere. This has bitten the project before.

### Step 5 — verify

```bash
curl -s https://<api-host>/version        # SHA present; code head == live DB head
```

```sql
-- New tables exist
SELECT tablename FROM pg_tables WHERE schemaname='public'
  AND tablename IN ('proxy_circuit_breakers','outbox_messages','maintenance_cadences');

-- Alembic head matches b3e7c9a15d42
SELECT version_num FROM alembic_version;
```

```bash
railway logs --service worker | grep -iE "RuntimeError|Traceback|refusing to start"   # expect none
```

---

## 4. AFTER DEPLOY — retention safety hold

**Do not leave this unattended.** Retention has been dead for a month. Once rollups populate,
`price_observations_2026_07` becomes eligible to drop, and the verify-before-drop gate will finally
clear.

Before letting retention run:

1. Confirm the configured retention age and that July is genuinely past it.
2. Confirm the rollups covering July are **correct**, not merely present.
3. Confirm the Postgres backup is current.

The client has one month of price history in that partition. A newly-working task deleting it
because it did exactly what it was told is the realistic data-loss path here.

---

## 5. C3 — the real RLS remediation (not yet run)

Everything is built and dry-run against production inside `BEGIN…ROLLBACK`; production is unchanged.

**Evidence of the problem:**

```
rolname        | rolsuper | rolbypassrls          -- crawmatic_app does not exist
crawmatic_auth | f        | t
postgres       | t        | t
```
```sql
BEGIN; SELECT set_config('app.workspace_id','00000000-...-000000000000',true);
SELECT count(*) FROM products;   --> 3494    (should be 0)
```

**Procedure:**

1. Snapshot `\du` and `pg_tables` ownership; confirm a volume backup.
2. `psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -v app_password="'<pw>'" -v auth_password="'<pw>'" -f scripts/rls_provision.sql`
3. Verify **before touching any service**:
   `RLS_VERIFY_DATABASE_URL='...crawmatic_app...' uv run python scripts/rls_verify.py` → must exit 0.
   Also verify through pgbouncer from inside the private network (`railway ssh --service api`);
   pgbouncer runs `POOL_MODE=transaction` and was empirically confirmed to forward the client
   username, but confirm for `crawmatic_app` specifically.
4. Set `DATABASE_URL` to `crawmatic_app` on api/worker/scheduler/scrapers/scrapers-browser.
   Leave `AUTH_DATABASE_URL` and `MIGRATION_DATABASE_URL` exactly as they are.
5. Restart **worker → scheduler → scrapers → scrapers-browser → api**.
6. Re-run `rls_verify.py`; smoke a login, a `/v1/products` list, one scrape job.
7. **Remove `RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE`** from all five services.

**Rollback:** set `DATABASE_URL` back to the `postgres` URL and restart in the same order. Instant,
no data change. `crawmatic_app` owns nothing, so `DROP OWNED BY crawmatic_app; DROP ROLE crawmatic_app;`
is clean if you want it gone.

**Known residual gaps:** `workspaces` and `refresh_tokens` carry no RLS by design, so
`crawmatic_app` can still read every workspace row cross-tenant. The 6 monthly partitions have
`relrowsecurity=f` (policies apply via the parent, so the app path is safe, but a direct partition
query is not filtered). `crawmatic_auth` is over-granted.

---

## 6. Remaining work

### Not started

| Item | Note |
|---|---|
| **H6** URL dedup A/B | `SCRAPE_URL_DEDUP` still unset. Amazon URLs repeat up to 6×; ~5.06 requests/link measured |
| **M1** browser batch sizing | One `http_max=200` chunks browser work that runs 2-concurrent / 1 context. Start 5–25 |
| **M2** `EMBEDDED_JSON` adapter | Specced, not built. Design in §7 below |
| **M2** delete dead vocabulary | 7 enum values + `AdapterKey`. Cheap, safe, shippable alone |
| **M4** webhooks | Still no outbound HTTP call. Decide: document as polling-only, or build signed delivery with SSRF/DNS-rebind protection + DLQ |
| **H4** canary | Rerun the 12-competitor / 134-link cohort at the deployed SHA; compare to the 2026-08-12 baseline |
| 30-day | Failure injection, load test, backup/restore + Redis recovery drills |

### Open bugs found but deliberately NOT fixed

1. **`build_recent_signals` is blind.** It joins `competitor_product_matches.url_pattern ==
   profile.url_pattern`, but under `STRATEGY_PROFILE_SCOPE="domain"` the profile key is the bare
   domain and **0 of 4,588** match rows have a bare-apex pattern. Rediscovery **conditions 3–8 are
   dead code in production.** Fixing this switches six dormant triggers on at once — it needs its own
   verification pass, which is why it was left. The new per-key bounds cap the blast radius.
2. **`apply_promotion` dead-end.** It requires `method_column != validated_name`, so a DEGRADED
   profile whose best method is unchanged can never return to ACTIVE, and discovery only runs for
   ACTIVE profiles. `fqtoners.com` is parked there now and will not self-heal.
3. **Discovery probes don't write `request_attempts`** — 646 recorded rows vs ~87,000 real proxy
   requests. Per-URL accounting and the breaker's `REQUESTS_PER_URL` condition are blind to the
   largest paid-request source. Needs an `origin` discriminator plus filters in `rollups.py` and
   `build_recent_signals`, or probe rows will themselves trip rediscovery condition 6.

### Config / infra handoffs

- `SYSTEM_DATABASE_URL` → `.railway/railway.ts` `commonEnv` (§2).
- Redis durable config — survives restarts only via the start command:
  ```bash
  railway variables --service redis --set 'RAILWAY_RUN_COMMAND=redis-server *:6379 --maxmemory-policy noeviction --maxmemory 19gb --appendonly yes --appendfsync everysec'
  ```
  Needs a restart (seconds of broker downtime; Celery reconnects, RDB reloads from volume).
- Enforcement vars on the five app services: `PROXY_REDIS_REQUIRE_NOEVICTION=true`,
  `PROXY_LEDGER_FAIL_OPEN=false`, `PROXY_BREAKER_ENABLED=true`. All have safe defaults in code.
- **DataImpulse sub-user cap** — needs the dashboard email+password to mint a reseller token
  (`api.dataimpulse.com/reseller/*`; the proxy credential only exposes `/api/stats` and `/api/usage`).
  There is **no daily/monthly rate cap** available to the sub-user credential — the only
  app-independent ceiling is the prepaid balance (**$33.68 remaining**). Recommended: a dedicated
  sub-user with ~5 GiB allocated (vs $2.00 per full refresh), scripted monthly top-up.
- Scheduler beat task calling `emit_snapshot(collect_snapshot(session))` every ~15 min — without it
  nothing evaluates the new alert rules on a schedule.
- `assert_production_safe()` is wired into the API only, **not** scheduler/worker.

---

## 7. Context worth keeping

### The discovery loop (fixed — understand it before touching strategy code)

Not a URL normalization bug, which was my first hypothesis and it was wrong. **Rediscovery conditions
1 and 2 read state the remedy never clears.** `light_recheck` runs every 60 s over ACTIVE profiles;
condition 2 reads the *lifetime* `strategy_attempt_stats` ratio. Rediscovery marks the profile
DEGRADED, discovery probes, `seed_from_discovery` sets it ACTIVE again — touching neither counter.
`_probe_sample` writes no stats at all, so a successful discovery **cannot move the number that
triggered it**. Next tick, identical inputs, re-fire.

Live proof: `fqtoners.com` preferred method at `3/6 = 0.500` against a floor of `0.80`. 13,151
COMPLETED runs, flat ~1,439/day (= one per minute) for ten days.

**extra.com was a *different* producer** — 9,159 runs across 186 distinct patterns, the condition-8
`www` mismatch already fixed by `36fd624`. The bare-apex pattern is *correct by design* under domain
scope, not a defect.

**Cost attribution:** extra.com 9,159 runs × 10 samples = 91,590 probes vs 87,082 measured
DataImpulse requests — within 5%. The access ladder escalates `DIRECT_HTTP → DIRECT_HTTP_RETRY →
PROXY_HTTP`, and extra.com sits behind a WAF, so every probe escalated to the paid proxy.
fqtoners answers directly and cost nothing — **same defect, only luck decided which one billed.**

**Billing note:** the loop has been dormant since 2026-08-12 21:56 because the whole pipeline went
silent (53+ h, zero scrape attempts, and there are **zero refresh rules configured**). Recent spend
under $2 is consistent with that. The loop is dormant, not fixed-by-luck — and deploying plus running
a canary is exactly what restarts scraping. Daily trend when it stopped: 1,387 → 3,270 → **7,477**.

### `EMBEDDED_JSON` design (M2, highest-value adapter)

New `extraction/embedded_json.py` with the same strategy signature; chain position **between JSON-LD
and CSS**. Parses rather than regexes: collect `<script>` bodies (`type="application/json"`,
`id="__NEXT_DATA__"`, `var X = {...};`), `json.loads`, resolve a JSON-pointer path from new nullable
profile columns (`price_json_path` etc., mirroring the `*_selector` / `*_regex` convention). Add one
line to `_METHOD_TO_STRATEGY` and the enum becomes real *and* learnable.

**Honest sizing:** Amazon's 1,112 failures look like the prize, but 216 of 252 affected matches are
genuinely out of stock — Amazon's win is correctness plus retiring a hardcoded Arabic out-of-stock
string sniff, **not** recovered prices. The unambiguous case is **noon: 269 failures / 59 matches /
275 wasted paid attempts**, where JSON-LD reports `"price": 0` for unavailable items while
`__NEXT_DATA__` carries the truth. Build a negative fixture for that first.

`PLATFORM_JSON` is **not** worth building: every Shopify/Zid/Salla site already extracts via JSON-LD
at 97.3–100% with zero extraction failures.

### Traps

- Every `scrapers` deploy wipes the Scrapyd egg — re-register or all scraping silently stalls.
- `*.railway.internal` hostnames **do not resolve** from this box. Use
  `railway variables --service postgres --kv | grep DATABASE_PUBLIC_URL`.
- `check_single_head.sh` is the guard that matters; the head literal in
  `test_webhook_single_head.py` was made head-agnostic this cycle — don't re-pin it.
- Wedged spiders can block all 8 scraper slots — restart via GraphQL `deploymentRestart`, then reset
  the job to PENDING.
- Production DB access should stay read-only unless deliberately remediating:
  `SET default_transaction_read_only = on;`

### Alert event names now emitted

`maintenance_partition_missing`, `maintenance_rollup_stale`, `maintenance_cadence_overdue`,
`maintenance_system_session_unavailable`, `maintenance_health_ok`, `proxy_ledger.degraded`,
`proxy_breaker.tripped`, `proxy_breaker.denied`, `proxy_breaker.unavailable`,
`redis.policy.violation` / `.unknown` / `.ok`, `strategy_rediscovery_rate_limited`, `ops.snapshot`,
`ops.alert`.

### Metrics already alarming in production

| Rule | Value |
|---|---|
| `freshness.pipeline_silent` | 53 h with zero scrape attempts |
| `rollup.never_run` | 0 rollup rows vs 32,231 observations |
| `security.rls_inert` | role `postgres`, superuser + BYPASSRLS |
| `partition.next_month_missing` ×4 | 16 days to write outage |
| `freshness.no_refresh_rules` | **zero** refresh rules — nothing is scheduled to scrape |
| `queue.deferred_target_age` | 16 DEFERRED targets, oldest 35 days |

Note the two cost ceilings differ by an order of magnitude: the breaker uses 250,000 while the
*enforced* provider ledger cap is **60,000**. The forecast rule takes `min(...)`.
