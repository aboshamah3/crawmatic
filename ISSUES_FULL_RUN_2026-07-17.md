# Production Full-Run Test — Issues To Fix (2026-07-17)

Context: first real full-catalog scrape in production (Railway project
*Crawmatic*), job `019f70c6-15bc-78f0-8caa-65581bec1813`, WORKSPACE scope,
30 active matches over 11 products / 3 competitors (Jarir, Noon, Amazon).
Triggered by inserting a one-shot WORKSPACE `refresh_rules` row (deleted
after firing). DataImpulse residential proxy seeded as global provider
`dataimpulse-residential` and linked to the global `global_default`
access policy (DIRECT_THEN_PROXY, use_proxy_on_retry, browser fallback,
rotate-per-request, SA). Final state: the job stalled permanently at ok=1 fail=1 skip=1 with 27
targets PENDING — root cause is Issue 7 below. The job row was left
RUNNING (no cancel path); the fixing session should finalize or delete it
after fixing dispatch (`scrape_jobs` id above, plus its Redis
`dispatched:019f70c6…:*` guard keys).

## Issue 1 — FIXED, needs codification: scheduler missing AUTH_DATABASE_URL

Every 30s refresh pass raised
`RuntimeError: SYSTEM_DATABASE_URL (or its AUTH_DATABASE_URL fallback) is required…`
(`app_shared/database.py:201` via `scheduler_app.py:181`), so **no
scheduled scrape had ever fired in production**. Root cause: the
`scheduler` Railway service never had `AUTH_DATABASE_URL` set; only the
api service did.

Fixed manually on 2026-07-17 by copying `AUTH_DATABASE_URL` from the api
service to the scheduler service and redeploying. **Remaining work:**
add the variable to `.railway/railway.ts` for the scheduler (and any
other service that needs the BYPASSRLS role) so the fix survives infra
re-applies; consider a startup fail-fast so a misconfigured scheduler
crashes loudly instead of logging every 30s.

## Issue 2 — strategy_stats_flush crashes: numeric overflow in NUMERIC(5,4)

`maintenance.strategy_stats_flush` fails every minute once real traffic
flows:

```
psycopg.errors.NumericValueOutOfRange: numeric field overflow
DETAIL: A field with precision 5, scale 4 must round to an absolute value less than 10^1.
```

A rate/confidence column declared `Numeric(5,4)` (candidates:
`libs/shared/app_shared/models/strategy.py:128,131,176,180`; also
`competitors_matches.py:206`, `observations.py:108/193`) is being written
a value ≥ 10 — i.e. something is writing a count/ms value into a
[0,1]-rate column, or a rate is computed unclamped. Strategy stats have
NOT been persisting. Find the flush write path
(`apps/workers/.../tasks_maintenance.py`, strategy stats flush →
`strategy` models), clamp/fix the computation, and add a unit test that
flushes a stat > 1.0.

## Issue 3 — match lock blocks the proxy retry (the escalation never runs)

Observed on Noon: attempt 1 (DIRECT_HTTP) exhausted Scrapy's retries with
TIMEOUT, then attempt 2 (labelled DIRECT_HTTP_RETRY) immediately failed
`LOCKED_ALREADY_RUNNING — match lock already held, another attempt is in
flight`. The lock taken by attempt 1 for fetch→persist was still held
when the same spider's own retry started, so the DIRECT_THEN_PROXY
escalation (the entire reason the proxy exists) never executed, and the
target ended SKIPPED.

Where to look: `libs/scrape-core/scrape_core/targets.py` (attempt loop,
`match_lock_key`/`match_lock_token` handling, SPEC-11 US2) and the
spider's retry dispatch in
`apps/scrapers/price_monitor/spiders/generic_price_spider.py`. The retry
of the *same* target within the same spider run must either reuse the
held lock token or wait for release — not treat itself as a concurrent
runner. Evidence log: spider job `4d41b93281f711f19045a2aa9b369eae`
(`/app/apps/scrapers/logs/price_monitor/generic_price_spider/…` on the
`scrapers` service).

## Issue 4 — access-policy timeout_ms not applied to Scrapy

`global_default.timeout_ms = 30000`, but the Noon fetch ran to Scrapy's
default `DOWNLOAD_TIMEOUT = 180` seconds, ×3 retries ≈ 9 minutes on one
protected URL (response_time_ms=545760 recorded). The policy timeout is
never translated into the request's `download_timeout` meta. Wire
`AttemptPlan`/policy `timeout_ms` into the Request meta in
`generic_price_spider._dispatch` (and browser spider equivalent), and cap
Scrapy's own retry middleware for these managed fetches (the access
engine owns retry semantics, not Scrapy's RetryMiddleware).

## Issue 5 — sticky sessions never reach the proxy vendor (pre-existing gap)

`generic_price_spider.py:384-387` sends `ProxyProvider.username` verbatim
in `Proxy-Authorization`; the engine's `assign_proxy` sticky_key is
computed but never appended. For DataImpulse the sticky syntax is a
username suffix `;sessid.<id>` (verified working by hand; country
targeting `__cr.sa` is already baked into the stored username). Without
it, Cloudflare-style challenge flows can't hold an IP. Small patch:
when the assignment carries a sticky key and the provider is
DataImpulse-style, append `;sessid.<key>` to the username before
base64-encoding the header.

## Issue 6 — Amazon PRICE_NOT_FOUND

First Amazon.sa target fetched a page but extracted no price
(`PRICE_NOT_FOUND`). Could be a bot-interstitial (needs the proxy/browser
path — blocked today by Issue 3) or selector mismatch for Amazon's
markup. Diagnose after Issues 3/4 land: re-run one Amazon match, inspect
the fetched HTML in the spider log, then decide selector fix vs.
access-method fix.

## Issue 7 — CRITICAL: multi-match batches collapse to ONE match at Scrapyd

The stall's root cause. `ScrapydDispatchClient._post_schedule`
(`libs/shared/app_shared/scrapyd/client.py` ~line 170) passes
`"match_ids": match_ids` (a Python **list**) into `requests.post(data=…)`.
requests form-encodes a list as repeated `match_ids=a&match_ids=b&…`
fields, and Scrapyd's `schedule.json` collapses repeated fields into a
single spider argument — so a batch of N matches launches a spider that
scrapes **one** match. The spider side (`_parse_match_ids`,
`libs/scrape-core/scrape_core/targets.py:252`) expects a
*comma-separated string* (or JSON list string).

Effect in this run: 30 targets → domain batches → each batch's spider
processed 1 match → 3 resolved, **27 targets orphaned PENDING forever**,
because the Redis `dispatched:{job}:{batch_index}` NX guard records the
batch as successfully dispatched, so even a re-enqueued `dispatch_job`
refuses to re-POST. Every multi-match dispatch since the feature shipped
has had this behavior.

Fix: `"match_ids": ",".join(str(m) for m in match_ids)` in
`_post_schedule` (matches `_parse_match_ids`'s primary format). Add an
integration-shaped test that round-trips a 2-match batch through a stub
Scrapyd and asserts both matches reach the spider. Then clear the job's
Redis guard keys and re-dispatch (or create a fresh job).

## Issue 8 — recover_stalled_batches is never scheduled by anything

`maintenance.recover_stalled_batches` (`tasks_jobs.py:452`) is registered
but **no code enqueues it**: the worker runs plain `celery worker` (no
beat, no `beat_schedule` in `celery_app.py`), and the scheduler's loop
never sends it. So when Issue 7 orphaned 27 targets, the designed
self-healing pass simply did not exist at runtime. (`finalize_jobs` DID
run — find who enqueues it and give `recover_stalled_batches` the same
cadence; grep found no sender in-repo, so check the scheduler tick list
printed at startup and the `dispatch_job` chain.)

## Ops notes for the fixing session (hard-won)

* **Prod DB access:** `railway connect postgres` is broken —
  `password authentication failed for user "postgres"` (the running
  volume's password predates the current env var). Working path:
  `railway ssh --service postgres -- env -u PGHOST -u PGPORT psql -U postgres -d railway -tA`
  (unix socket, peer auth; note the DB is named **`railway`**, not
  `crawmatic`). Fix properly by resetting the postgres password to match
  the service env (`ALTER USER postgres PASSWORD …` with the env value),
  which also un-breaks `scripts/seed_proxy.sh`/`run_full_scrape_test.sh`.
* **Bootstrap admin:** `BOOTSTRAP_ADMIN_EMAIL/PASSWORD` are set on no
  service; the seed was run some other way. API-token flows need those
  creds (or a password reset) — workspace/user exist (1 each).
* **Proxy:** DataImpulse creds in local `.env` (git-ignored);
  `proxy_providers` row `dataimpulse-residential` (global, RESIDENTIAL,
  `gw.dataimpulse.com:823`, username suffixed `__cr.sa`, password
  Fernet-encrypted key v1, budget 60000 req/mo) is live and linked to
  `global_default`.
* **Catalog reality:** prod has 11 products / 30 active matches only.
  The ~1,200-product catalog is not imported (bulk-upsert via API is the
  intended path).
* **Cost:** worker runs 32+ Celery prefork processes (~2.7 GB RSS ≈
  $27/mo of a ~$31/mo Railway bill). Capping concurrency to 4–8 is a
  one-line saving after the fixes.
