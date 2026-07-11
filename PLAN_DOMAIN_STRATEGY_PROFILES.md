# Plan: Domain-Level Strategy Profiles (ease the discovery gate)

**Status:** planned, not yet implemented — written 2026-07-11 for execution in a fresh session.
**Decision (agreed with Abdul):** key strategy discovery/profiles by **competitor domain** instead of
`(competitor, url_pattern)`. URL-pattern keying stays dormant in the schema/data for a possible later
refinement; we are NOT weakening `STRATEGY_DISCOVERY_MIN_SAMPLE=3` and NOT changing the pattern
algorithm (a v2-rules alternative was prototyped and shelved — see §8).

## 1. Why (context recap)

- Discovery only proceeds with ≥3 sample URLs sharing one `(competitor, url_pattern)` key
  (`STRATEGY_DISCOVERY_MIN_SAMPLE=3`). jarir.com and noon.com embed per-product slugs in every URL,
  so under the v1 pattern algorithm every product is its own pattern (n=1) and discovery **never**
  runs for them — 0 HTTP requests ever made. See `MUSHTRYATI_SCRAPE_TEST_REPORT.md` §3.
- Anti-bot/access behavior is domain-wide in practice; per-pattern discovery multiplies live probe
  fetches by catalog size (1,000 slug-unique products ⇒ up to 1,000 discovery runs). Domain-level
  caps discovery at O(#competitors).
- Escape hatch for genuinely heterogeneous sites already exists:
  `domain_access_rules.url_pattern_override` (manual per-pattern carve-out) keeps working.
- Matches keep stamping `url_pattern`/`url_pattern_version` (v1, unchanged) so pattern-level keying
  can be re-enabled later data-driven, with history intact.

## 2. Design

New setting `STRATEGY_PROFILE_SCOPE` in `app_shared.config.Settings`:
- `"domain"` (new default): the profile lookup key passed around as `url_pattern` becomes the bare
  competitor **domain** string (e.g. `noon.com`). No schema change — `domain_strategy_profiles`
  unique key `(workspace, competitor, domain, url_pattern)` simply gets `url_pattern == domain`.
- `"url_pattern"`: legacy behavior, exact current semantics (rollback is a config change, no deploy).

A manual `url_pattern_override` from `domain_access_rules` **always wins** regardless of scope
(unchanged precedence).

## 3. Code changes (file by file)

Read each file before editing — line refs are from 2026-07-11.

1. **`libs/shared/app_shared/config.py`** — add:
   ```python
   STRATEGY_PROFILE_SCOPE: str = "domain"  # "domain" | "url_pattern"
   ```
   (follow neighboring settings' style; validate value if config has a validator convention).

2. **`libs/scrape-core/scrape_core/targets.py:~583`** (spider group resolution, `load_targets`).
   Current: `lookup_pattern = override or derive_url_pattern(group[0].competitor_url)`.
   New: `lookup_pattern = override or (domain if scope == "domain" else derive_url_pattern(...))`.
   The competitor's `domain` is already available on the group/competitor row. Keep the
   override-first precedence.

3. **`apps/workers/app/workers/tasks_strategy.py::_select_sample_urls` (~155–174)** — the gate fix.
   Current query filters `CompetitorProductMatch.competitor_id == X AND url_pattern == Y`.
   New: when scope == "domain", drop the `url_pattern ==` filter (competitor_id + workspace scoping
   already bound the sample to the domain). This removes any dependency on stored match patterns —
   **no match re-upsert is needed** for existing rows to count toward the gate.

4. **`libs/shared/app_shared/strategy/rediscovery.py:~309`** — the "observed URL pattern differs
   from profile pattern" signal re-derives via `derive_url_pattern`. When scope == "domain", compare
   the observed URL's **host/domain** to the profile key instead (the signal should effectively
   never fire on same-domain URLs).

5. **`libs/shared/app_shared/strategy/resolution.py::resolve_or_create_strategy_profile`** — no
   logic change (it keys on whatever the caller passes); update the docstring to document both
   scopes.

6. **Leave untouched:** `derive_url_pattern` itself, `URL_PATTERN_ALGORITHM_VERSION` (stays 1),
   match upsert stamping (`app_shared/matches/upsert.py`, api match routers),
   `pattern_backfill` task (scans `url_pattern_version < current`; constant unchanged ⇒ no-op).

## 4. Tests

- Add unit tests for scope="domain": `_select_sample_urls` ignores pattern; `load_targets` lookup
  key is the domain; override still wins; scope="url_pattern" preserves exact old behavior
  (regression pin).
- Existing suites to keep green (they assert v1 pattern behavior, which we're NOT changing):
  `tests/unit/test_url_pattern.py`, `tests/unit/test_url_pattern_grouping.py`,
  `tests/unit/test_strategy_router.py`, `tests/unit/test_rediscovery.py` (this one may need a
  scope fixture pin to "url_pattern" if it exercises the compare-signal),
  `tests/integration/test_consumption_seam.py`.
- Run: `uv run pytest tests/unit -q` (integration tests need Postgres — skip locally if no daemon;
  see memory note "No Docker daemon in build env").

## 5. Production data cleanup (Mushtryati workspace)

Workspace `c23797f7-5e90-4668-bfaf-9635017d8d00` has **17 stale `DISCOVERY_REQUIRED` profiles**
keyed by v1 patterns (created during the failed runs). Domain-keyed rows will be created fresh on
the next run; delete the stale ones in the same pass via the SSH tunnel:

```sql
DELETE FROM domain_strategy_profiles
WHERE workspace_id = 'c23797f7-5e90-4668-bfaf-9635017d8d00'
  AND status = 'DISCOVERY_REQUIRED';
```

Run via: `cat file.sql | railway connect postgres --ssh` (project already linked; SSH key
`railway-tunnel` is registered for this account).

## 6. Git → GitHub

- Repo state: local branch `master` tracks `origin/main`
  (`origin = https://github.com/aboshamah3/crawmatic.git`). Push with
  `git push origin master:main` (or whatever matches the tracking config after checking).
- Commit **as two separate commits**, ours only:
  1. `fix(scrapers): match Scrapyd's installed epoll reactor` — `apps/scrapers/price_monitor/settings.py`
     (already deployed live on Railway, but never committed — must land in git or the next
     `railway up` from a clean checkout regresses it).
  2. `feat(strategy): domain-level profile scope (STRATEGY_PROFILE_SCOPE, default domain)` — the
     changes in §3 + tests. Include `mushtryati_product_matches.json`,
     `MUSHTRYATI_SCRAPE_TEST_REPORT.md`, and this plan file if desired.
- **Pre-existing dirty files NOT ours — do not sweep into these commits without Abdul's OK:**
  `.dockerignore`, `.gitignore`, `alembic/versions/f4c8a391d5c9_...py`, `apps/api/Dockerfile`,
  `apps/workers/pyproject.toml`, `libs/shared/pyproject.toml`, `uv.lock`, `.agents/`, `.railway/`,
  `GAP_ANALYSIS.md`.

## 7. Railway deploy + post-deploy (order matters)

1. `railway up --service api --ci --yes` — libs/shared changed (config).
2. `railway up --service worker --ci --yes` — tasks_strategy changed.
3. `railway up --service scrapers --ci --yes` and same for `scrapers-browser` — scrape-core changed.
4. **Re-deploy the Scrapy project onto the fresh scrapers container** (no baked-in deploy step —
   known gap): `railway ssh --service scrapers -- pip install scrapyd-client`, then run the
   credential-safe egg deploy script (multipart POST to `addversion.json` using
   `$SCRAPYD_USERNAME/$SCRAPYD_PASSWORD` from the container env — never write creds to disk; a
   working copy exists at the previous session's scratchpad
   `/tmp/claude-1000/-srv-crawmatic-crawmatic/e9345327-fea9-4fc2-8d59-98736c9b1d2e/scratchpad/deploy_egg.py`).
5. Load the **updated 11-product fixture** (repo root `mushtryati_product_matches.json`, 33 matches)
   via bulk-upsert — the import scripts from the previous session are in that same scratchpad dir
   (`import_mushtryati.py`, `import_matches.py`); API key for the workspace is in
   `workspace_secrets.json` there (key name `bulk-import`, scopes incl. jobs:read/write).
   ⚠️ Before loading, fix the 2 known bad matches (see §9 caveat).
6. Trigger jobs for all matches (`trigger_jobs.py` pattern: `POST /v1/jobs/run/match/{id}`).
7. **Verify discovery actually runs this time:** worker logs should show
   `strategy_discovery.run_discovery` taking seconds (real sample fetches), not ~0.01s no-ops;
   then check `domain_strategy_profiles` — expect 3 rows (one per competitor domain), status
   `LEARNING`/`ACTIVE` with a `preferred_access_method`.
8. Trigger jobs **again** (first round seeds discovery; scraping consumes the learned profile) and
   check `price_observations` for real prices. Note: nothing schedules
   `SCRAPE_FINALIZE_JOBS`, so `ScrapeJob.status` stays RUNNING even when targets complete — judge
   by `scrape_job_targets.status` and `price_observations` rows, not the job header.

## 8. Shelved alternative (for the record)

Pattern-algorithm v2 (two generic rules: `slug-<id>.html` ⇒ `*.html`; sluggy-segment-before-`:id`
⇒ `*`) was prototyped and validated (all 3 competitors would group n=11; no category over-merge in
sanity checks) but adds version-bump/backfill complexity and doesn't cap discovery cost the way
domain scoping does. Prototype: previous session scratchpad `pattern_v2_experiment.py`. Revisit
only if domain-level proves too coarse for some competitor.

## 9. Known caveats to carry forward

- **2 confirmed bad matches in the fixture** (verified live 2026-07-11): Galaxy Tab A11+ **5G**
  is matched to jarir's **Wi-Fi-only** listing (`samsung-galaxy-tab-a11-tablet-pc-jpm1776.html`);
  Acer Nitro V15 source says 16GB RAM but jarir (live) and amazon (fixture text) both say 8GB.
  Fix or drop these before loading; noon.com/amazon.sa could not be live-verified (bot-blocked).
- `.railway/railway.ts` is stale vs live config (`AUTH_TYPE: "trust"` vs live `scram-sha-256`;
  no `AUTH_DATABASE_URL`) — root-owned file, needs chown/sudo to fix; a `railway config apply`
  before fixing it would regress production auth.
- Security follow-ups still open (Abdul deferred): rotate `RAILWAY_API_TOKEN`/`GH_TOKEN` leaked
  into an earlier transcript; `JWT_SECRET`/`SCRAPYD_PASSWORD` are unresolved `secret(...)` literal
  strings in production.
- `railway ssh` quirk: single short commands print fine; for `sh -c` pipelines echo sentinel
  markers (output sometimes swallowed without them). No `curl` in app containers — use python3
  urllib. `timeout` on BusyBox containers differs — wrap timeouts on the local side.
