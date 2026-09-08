# Temporary staging environment for the fleet test (Task D1, Stage D)

**Status: OWNER GATE — NOT CREATED.** EPA prepared every artifact and every command below and
executed **none** of them. No Railway environment, service, variable or volume was created,
modified or deleted; no image was deployed; nothing was spent. Per
`.epa/plan-core-production-readiness-2026-09-07/ASSUMPTIONS.md` answer 2, the plan's D1 **Step 2**
(create the environment) is an owner gate and **Steps 3 and 4** (run the fleet test, write the
numbers into `docs/ops/FLEET_TEST_2026-09.md`) cannot start until it is done.

This environment exists to answer audit §13 for 100 stores **without buying a single external
request**: the whole fleet is pointed at `apps/fixture-origin`, a service that generates every page
from the request path. It is created for the run, and **deleted after D2 sign-off**.

Companion documents: `docs/ops/FLEET_TEST_2026-09.md` (the measurement record this environment
produces), `docs/ops/FAULT_INJECTION_2026-09.md` (D2, runs in the same environment),
`docs/ops/RELEASE_3_2026-09.md` (the release these images come from), `scripts/dr/RUNBOOK.md`.

Every `<...>` marks a value only the owner can supply — a name or an id, never a secret. **No
command in this document contains, or should ever be filled in with, a credential value.** Railway
variables are set through the Railway UI/CLI, and only their NAMES appear here.

---

## §0. The one property this environment must have

**Proxy credentials are ABSENT.** `DATAIMPULSE_*` / any proxy login variable is simply not set in
this environment. That is not a convenience — it is the containment: with no proxy credential and a
fixture origin on a Railway `*.up.railway.app` domain, there is no configuration mistake that can
turn a 592,500-check/day fleet test into real-domain traffic. Check it before the run and after any
variable change:

```bash
# Names only; never print values.
railway variables --environment staging --service worker | awk '{print $1}' | grep -iE 'proxy|impulse' || echo "OK: no proxy variable present"
```

If that grep finds anything, stop and remove it before seeding.

---

## §1. Create the environment (OWNER, Railway)

Duplicate the production environment so service topology, health checks and non-secret
configuration match what is being certified — then strip what must not be there.

```bash
railway login
railway link --project <RAILWAY_PROJECT_ID>            # the engine project
railway environment new staging --duplicate production  # duplicating copies services + variables
```

Then, in the duplicated `staging` environment:

1. **Delete every proxy variable** (see §0) from every service.
2. **Point every database/cache variable at the staging instances**, never production:
   `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `SYSTEM_DATABASE_URL`, `AUTH_DATABASE_URL`,
   `REDIS_URL`. §2 creates them.
3. **Detach any alerting destination that pages a human** (D2 deliberately kills processes;
   `docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md` names the variables) — or repoint it at a test
   channel, so the fault-injection matrix does not wake anyone up.
4. **Set the node counts of §3.**
5. Confirm no service in `staging` shares a volume with production
   (`railway volumes --environment staging`).

---

## §2. A fresh Postgres restored from the latest dump (OWNER)

The fleet test's database gates (rollup at realistic volume, retention, migration duration, pool
behaviour) are only meaningful against production-shaped data, so staging starts from a **restored
copy of the newest backup set**, not from an empty `alembic upgrade head`.

Reuse A10's rehearsal script — it already reads the PostgreSQL **major from the backup manifest**
(cross-checked against the dump's own `pg_restore -l` header) rather than assuming one, restores
into a throwaway container of that major, provisions the four component roles, runs
`alembic upgrade head` as `crawmatic_migrate`, and verifies grants:

```bash
cd /srv/crawmatic/crawmatic
sudo scripts/dr/rehearse_upgrade.sh \
  --report /srv/crawmatic/backups/dr/reports/staging-restore-$(date -u +%Y%m%d).md \
  <set-YYYYmmddTHHMMSSZ>        # the newest set under /srv/crawmatic/backups/dr/sets/
```

**Record from that report, for the D1 §3 gate table:** the manifest's `server_version`, the restore
duration, and the `alembic upgrade head` duration. As of 2026-09-08 the newest manifests say
production is **PostgreSQL 18.x** (18.4/18.6 across the two databases) — `docker-compose.yml`'s
`postgres:17.5-bookworm` pin is the stale value, not the plan text. **The staging Postgres must
therefore be created at the major the manifest names**, or the restore will be refused as a
newer-dump-into-older-server mismatch.

Then provision the Railway staging Postgres and load that restored copy:

```bash
railway add --database postgres --environment staging   # note the generated *.railway.internal host
# Load the restored copy (the rehearsal left it in a throwaway container; dump it back out):
pg_dump --format=custom --no-owner --no-privileges -d "$REHEARSAL_DSN" -f /tmp/staging-seed.dump
pg_restore --no-owner --no-privileges -d "$STAGING_DSN" /tmp/staging-seed.dump
```

`REHEARSAL_DSN` / `STAGING_DSN` are shell variables the operator exports for the length of the
session; they are credentials and must never be written into a file, a Railway variable name list,
or this document.

Redis: `railway add --database redis --environment staging`, then set `REDIS_URL` on every service
that needs it. It starts empty — the broker, locks, budget counters and dedup keys are all
per-environment state and must NOT be copied from production.

---

## §3. Node counts: 1 HTTP + 2 browser (OWNER)

```
SCRAPYD_HTTP_URLS      = http://<scrapyd-http>.railway.internal:6800
SCRAPYD_BROWSER_URLS   = http://<scrapyd-browser-1>.railway.internal:6800,http://<scrapyd-browser-2>.railway.internal:6800
```

Two browser nodes, not one: audit §11's hot-domain finding is that domain-hash placement can stop a
single hot domain from using additional replicas, and B4/F11's fix is only observable with **more
than one** browser node. One HTTP node is deliberate too — the fleet test's job is to find where
the ceiling is, and starting at the smallest fleet that can exhibit the routing behaviour makes the
derived node-count formula (D1 step 4) a measurement rather than an extrapolation from an
already-oversized fleet.

Set both variables on the services that dispatch (`worker`, `scheduler`); `SCRAPYD_USERNAME` /
`SCRAPYD_PASSWORD` are copied from the duplicated environment as names, with fresh staging values.

---

## §4. Deploy the release-3 images plus `fixture-origin` (OWNER)

Deploy exactly the images `docs/ops/RELEASE_3_2026-09.md` certifies — the point of the exercise is
to certify **the release**, not the branch tip.

```bash
railway up --environment staging --service api
railway up --environment staging --service worker
railway up --environment staging --service scheduler
railway up --environment staging --service scrapyd-http
railway up --environment staging --service scrapyd-browser
```

Then the fixture origin (its `railway.json` is `apps/fixture-origin/railway.json`; it takes **no
credential of any kind**):

```bash
railway add --service fixture-origin --environment staging
railway up --environment staging --service fixture-origin
railway domain --environment staging --service fixture-origin    # generates the PUBLIC domain
```

**The public domain is required, not a convenience.** The engine's save-time SSRF validator
(`libs/shared/app_shared/url_safety.py`, F01) rejects `*.railway.internal`, `localhost` and every
private IP literal, so matches pointing at the internal hostname could not be stored, and if they
somehow were they would not behave like real matches. Using the public domain is safe here
precisely because `fixture-origin` holds no data, has no credential and reaches nothing.

Optional per-store dials (names only, all optional):
`FIXTURE_ORIGIN_LATENCY_SCALE`, `FIXTURE_ORIGIN_PROFILE_OVERRIDES`, `FIXTURE_ORIGIN_MAX_SKU`,
`FIXTURE_ORIGIN_STORE_COUNT`, `FIXTURE_ORIGIN_WORKERS`.

Smoke it before seeding anything:

```bash
curl -sS https://<fixture-origin-public-domain>/healthz
curl -sS https://<fixture-origin-public-domain>/p/fleet-000/p000001 | head -5
curl -sS https://<fixture-origin-public-domain>/stores/fleet-007      # the resolved behaviour profile
```

---

## §5. Seed the fleet (EPA, once §1–§4 are done)

```bash
cd /srv/crawmatic/crawmatic
sudo -u mahmoud .venv/bin/python scripts/seed_synthetic_fleet.py \
  --i-know-this-is-staging \
  --database-url "$STAGING_DSN" \
  --origin-base-url https://<fixture-origin-public-domain> \
  --stores 100 --products-per-store 5000 \
  --start-at <YYYY-MM-DD>T00:00:00+00:00
```

Writes 100 workspaces, 500,000 products, 500,000 variants, **592,500 matches**, 100 daily
`WORKSPACE`-scope refresh rules staggered **14.4 minutes (864 s)** apart, and one `domain_rules`
row raising the fleet host limits for the fixture origin's domain.

Refusals are by design, and each one is a bug caught rather than an inconvenience:

* no `--i-know-this-is-staging` → refuses;
* no `--database-url` (it never reads `$DATABASE_URL` or app settings) → refuses;
* a database host not on the staging allowlist → refuses (pass `--host-allowlist-token <substring>`
  only for a genuinely-staging host under another name — never to make a production host pass);
* `RAILWAY_ENVIRONMENT_NAME=production` → refuses;
* an `--origin-base-url` the production SSRF validator rejects → refuses.

Re-running is safe: a workspace whose slug already exists is skipped, so an interrupted seed
resumes by simply repeating the command. `--dry-run` prints the plan and connects to nothing.

**Run `ANALYZE` before the first scheduled rollup — after the restore in §2 AND after this seed.**

```bash
psql "$STAGING_DSN" -c "VACUUM ANALYZE;"
```

Not hygiene: a freshly restored/bulk-seeded database still carries statistics that describe the
`price_observations` partition as empty until autoanalyze catches up, and the C7 rollup batch
statement is severely plan-sensitive to that. Measured on the build host (see
`docs/ops/FLEET_TEST_2026-09.md` §2.2): one batch of 500 variants over 100,000 freshly loaded
observations had **not finished after 18 minutes** with stale statistics, and took **0.19 s** after
`ANALYZE`. A rollup that appears to hang here is a planner problem, not a capacity result — do not
record it as one.

**About that `domain_rules` row.** Without it the entire 100-store fleet contends for
`FLEET_HOST_CONCURRENCY_DEFAULT` = 6 simultaneous requests against the one fixture host, and the
run would measure that number instead of the platform. The defaults (240 concurrent, 4,000/min)
sit roughly 2x above audit §10's 30.85 physical attempts/second, so the **host** cap is not the
binding constraint. If the intent is instead to test host protection, that is D2's
`host_limit_hold` row, which lowers the limit deliberately.

---

## §6. Tear down — after D2 sign-off (OWNER)

Delete the whole environment; do not "clean it up and keep it". A staging environment carrying a
restored copy of production data is a standing liability, and a fixture origin that outlives its
test is a service nobody owns.

```bash
railway environment delete staging          # removes services, variables and volumes together
railway volumes --environment staging       # expect: nothing (verify AFTER the delete)
```

Then confirm from the production side that nothing was touched: `railway variables --environment
production | awk '{print $1}'` should be byte-identical to the pre-test capture the owner takes in
§1, and `railway status --environment production` should show the same deployments.

**Checklist before deleting:**

- [ ] `docs/ops/FLEET_TEST_2026-09.md` has every measurement row filled in (it is the only thing
      that survives the environment).
- [ ] `scripts/fleet_test_report.py --format markdown` output is pasted into that document and its
      exit status recorded.
- [ ] D2's `docs/ops/FAULT_INJECTION_2026-09.md` result cells are filled in.
- [ ] The backup set used for the restore is still present and unmodified
      (`scripts/dr/inventory_backups.py`).
- [ ] No proxy variable was ever added (§0).
