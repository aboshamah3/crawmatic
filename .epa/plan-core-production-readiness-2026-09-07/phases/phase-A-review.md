# Phase A Review: Stage A — Contain risk and establish a trustworthy baseline
Verdict: FAIL
Base SHA: d824f31   Reviewed: main tree (branch `epa/plan-core-production-readiness-2026-09-07`, uncommitted)

## Integration command

`cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"` → exit 0

```
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
4202 passed, 19 skipped, 6 deselected, 63 warnings in 527.94s (0:08:47)
```

`cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0

```
check_single_head: OK — exactly 1 head.
b6f1c40a97d2 (head)
```

Migration chain verified linear from the pre-phase head:
`c8d2e3f4a5b6 → a2f0217c9d43 (A2) → 193ac27f0dc2 (A3) → b6f1c40a97d2 (A5)`.

Docker builds not run (disk, per ASSUMPTIONS answer 4 and BLOCKERS 2026-09-07 15:50).
Secret scan over `git diff d824f31` plus every untracked non-`.epa/` file: no hits.

## Per-task

| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| A1 Browser egress guard (F01, P0) | **Partial** | Strong on the direct leg, **absent on the proxied leg** | Connection-time enforcement is real and correct for unproxied contexts: `--proxy-server=http://127.0.0.1:<port>` + `--proxy-bypass-list=<-loopback>` are set at settings import (`price_monitor_browser/settings.py:127-155`), the guard reuses `validate_competitor_url`/`_reject_ip`, rejects when *any* resolved address is non-public, and dials `addrs[0]` with no second lookup (`egress_guard.py:568-640`) — rebinding genuinely closed. The plan's 3 unit tests are present (asyncio-driven, deviation 1, acceptable). 5/5 of the in-image scenarios passed natively against real Chromium. **But the proxied-context path is both unverified and, on inspection, non-functional — see Finding 1.** `service_workers` kwarg, `browser` marker registration and the `ssrf.py` docstring/sub-resource DNS work are all correct. |
| A2 Regex hard deadline (F02) | Yes | Strong | `regex` engine with `timeout=` per node plus a cumulative `4 × timeout` per-page budget (`extraction/regex.py:293-350`); `RegexDeadlineExceeded` is swallowed at `extract_regex` so a blown deadline costs one reading, not the page, and is surfaced to the spider through a scoped `ContextVar` (deviation 2 — the only way to signal across three pinned public signatures; sound). Quarantine skip lives at the extraction seam and fails **open** on pre-migration rows (`_regex_quarantined`, correct). Write-time gate compiles on both engines + a 100 ms probe, so a pattern the execution engine rejects can never be stored. `preflight_regex_profiles.py` exits 2 on offenders; offline smoke shown verbatim. Table-name deviation (`scrape_profiles`, not the plan's non-existent `strategy_profiles`) is correct and evidenced. |
| A3 Component grants (F03) | Yes, with the two logged gaps | Strong (real compose Postgres, 141 tests, run twice incl. from-zero) | Manifest + `verify_grants.py` + explicit per-role/per-table REVOKE+GRANT match plan intent. The two deviations from plan text are correctly evidenced, not laziness: `crawmatic_auth` keeps S/I/U on `network_operations` because `netledger/recorder.py`'s idempotent close reads it on the system session; `crawmatic_app` keeps SELECT only because `admin_usage.py:~252` joins `NetworkOperation` directly — I/U/D *were* removed, so 3 of 4 privileges closed. Both are recorded in BLOCKERS.md as the owner decision at A10 §2.6 with option (b) named. `crawmatic_scraper` created by `provision_db_roles.sql`, not the migration: correct — `crawmatic_migrate` is NOSUPERUSER/no CREATEROLE, so a migration `CREATE ROLE` would fail in production and only appear to work under compose. The partition-children grant bug the worker found and fixed is exactly the class of regression this task existed to prevent. |
| A4 Advisory triage + pinned build tools | Yes | Adequate | `advisory_triage.yaml` replaces `known_critical_advisories.txt` (deletion is the task's own design, CI updated in the same change); `scrapyd-client==2.0.3` pinned in both Dockerfiles + dev group + lock; 14 new gate tests. Merged cleanly with A1's earlier Dockerfile layer rather than reverting it. |
| A5 Lifecycle truth | Yes | Strongest in the phase | Verified against a real throwaway PostgreSQL 17.5: full chain up, `downgrade -1` back to 0 columns, re-up; then a data-bearing check showing PENDING+DEFERRED → STARTED (2), a second call a no-op (0), COMPLETED not resurrected, and the three phase p95s computing. Batch `mark_targets_started` instead of N × `mark_target` is the right call for `load_targets` and `mark_target` still emits the plan's exact single conditional UPDATE. `attempt_uuid` volatile-default rewrite: **acceptable, not required to be staged** — see Follow-ups. NULL-means-not-measured is honoured throughout; the metric SQL filters NULLs rather than fabricating zeros. |
| A6 Canary calculator | Yes | Adequate | Parent-page denominator via `parent_operation_id IS NULL`, children attached by `parent_operation_id`, provider dimension added, `price_ok` joined through `request_attempts.success`; refuses (exit 1) below 50 parents / above 5% unknowns / on mismatched arms. Old API kept for the existing test file. |
| A7 Ledger coverage + wire bytes | Yes | Adequate | New `scrape_core/middlewares/` package (the path CONTEXT.md flagged as missing) with `WireBytesMiddleware` at 585, registered in the HTTP project's `DOWNLOADER_MIDDLEWARES`; browser bytes stay on the existing `ByteAccumulator`. |
| A8 Cost artifacts + watchdog | Yes | Adequate | Hard-coded `PROJECT_ID`/`SERVICE_NAMES` removed in favour of `RAILWAY_PROJECT_ID` + a live `project.services` query; egress added; hourly-sum-vs-window reconciliation alerts above 2%. Ceiling-rounding of the proxy byte unit is the ASSUMPTIONS-sanctioned choice. |
| A9 Query statistics | Yes | Adequate | `--from-pg-stat-statements --window-minutes N` delta path added with the arithmetic extracted pure and unit-tested; default probe path unchanged. Owner step (ALTER SYSTEM + restart + CREATE EXTENSION) correctly deferred into A10 §2.5.1. |
| A10 Release 1 (OWNER GATE) | Yes | Strong | Step 1 genuinely executed, not deferred: `rehearse_upgrade.sh` restored a real 190 MB production dump into `postgres:18-alpine` (major read from the manifest, never assumed), provisioned, migrated, ran the ReDoS preflight and grants verifier — 10/10 PASS, container and volume torn down. It found two real deploy-day defects: (i) grants on migration-created tables are silently skipped by a single pre-migrate provision run (`control_plane_rules` came out with zero grants and `verify_grants.py` correctly FAILed), fixed as a mandatory two-pass sequence in §2.6; (ii) `provision_db_roles.sql` §8 adopts TABLE/SEQUENCE ownership but not FUNCTION ownership, which broke `alembic upgrade head` on a fresh restore — handled as a read-only pre-flight query plus a one-line `ALTER FUNCTION` in §2.6 rather than a silent workaround. **The doc is executable in order** (§2.1 disk/tag/manifest → 2.2 SaaS migration → 2.3 quiesce → 2.4 drain → 2.5 variables → 2.6 provision/verify → 2.7 deploy order → 2.8 post-deploy → 2.9 image certification → 2.10 smoke → 2.11 rollback rule → §3 sign-off), commands are copy-pasteable, and every owner item from BLOCKERS.md is carried forward with its exact command. |

## Findings (ordered by severity)

1. **`libs/scrape-core/scrape_core/browser/egress_guard.py:733-735` and `:455-497` — proxied browser
   contexts silently egress DIRECT from the fleet IP instead of through DataImpulse.**
   The guard selects the upstream leg only when the incoming request carries
   `Proxy-Authorization: Basic <leg=proxy>:<token>` (`_wants_upstream_leg` → `_proxy_credentials`).
   Chromium does not send proxy credentials preemptively: a per-context Playwright
   `proxy={"server","username","password"}` is delivered only in response to a
   `407 Proxy Authentication Required` challenge (Playwright answers Chromium's
   `Fetch.authRequired`). The guard never issues a 407 — there is no `407` or
   `Proxy-Authenticate` anywhere in the module — so the header never arrives,
   `_wants_upstream_leg()` returns `False`, and `_serve()` falls straight through to the
   direct-dial branch and counts `ALLOWED_DIRECT`. The spider meanwhile still stamps the leg
   as proxied, so `is_proxied(meta)` and the cost ledger keep booking it as PROXY. Net effect
   on the browser path (the expensive Amazon/Noon path): the datacenter IP reaches the target,
   the residential leg is paid for but unused, and the ledger's "proxied" view is false — a
   regression against pre-A1 behaviour, where the context pointed at DataImpulse directly and
   DataImpulse's own 407 made Playwright supply the credential.
   The guard's own docstring promises the opposite ("refusing rather than silently downgraded"),
   but that branch is unreachable for the same reason.
   **This is entirely unverified either way**, which is independently disqualifying for a P0
   task: no test in `tests/unit/test_browser_egress_guard.py` or
   `tests/integration/test_browser_egress_guard_image.py` references `UpstreamProxy`,
   `register_upstream` or `PROXY_LEG_USERNAME`; `reports/A1.md` itself lists `_serve_via_upstream`
   under "Untested path"; and the image build that would have exercised a real browser is
   deferred on disk. The packet's acceptance criterion "proxied contexts route through the
   guard, which forwards CONNECT to DataImpulse after the same validation" therefore has no
   evidence behind it. The worker labelled this deviation MINOR; it is MATERIAL.
   **The fix must** make leg selection independent of browser proxy authentication, because the
   single shared guard port serves both unproxied (no credentials at all) and proxied contexts,
   so an unconditional 407 would break the former. Preferred shape: give each registered
   `UpstreamProxy` its **own** loopback listener port (`register_upstream` returns a port, not a
   token) and have the spider set
   `proxy={"server": f"http://127.0.0.1:{upstream_port}"}` with no username/password — the leg
   is then determined by which listener accepted the connection, and no credential ever crosses
   the browser boundary (still satisfying deviation 3's actual goal). Whatever shape is chosen,
   it must come with (a) a unit test driving a loopback fake upstream proxy end-to-end and
   asserting `GuardDecision.ALLOWED_UPSTREAM` plus the forwarded `CONNECT`/`Proxy-Authorization`
   line, and (b) a proxied case added to `tests/integration/test_browser_egress_guard_image.py`
   so a real Chromium proves the leg is taken. Until then a proxied browser leg must fail
   closed rather than dial direct.

## Files to commit

(For the orchestrator once Finding 1 is fixed — the exact set outside `.epa/`, from
`git status --porcelain`. `libs/scrape-core/scrape_core/middlewares/__pycache__/` is
gitignored and excluded. `scripts/security/known_critical_advisories.txt` is a **deletion**,
stage with `git rm`.)

.github/workflows/ci.yml
alembic/versions/193ac27f0dc2_tenant_usage_view_and_scraper_role.py
alembic/versions/a2f0217c9d43_scrape_profile_regex_quarantine.py
alembic/versions/b6f1c40a97d2_target_lifecycle_timestamps.py
apps/api/app/routers/admin.py
apps/api/app/schemas/admin.py
apps/scrapers-browser/Dockerfile
apps/scrapers-browser/price_monitor_browser/settings.py
apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py
apps/scrapers/Dockerfile
apps/scrapers/price_monitor/settings.py
apps/scrapers/price_monitor/spiders/generic_price_spider.py
docs/ops/QUERY_STATS.md
docs/ops/RELEASE_1_2026-09.md
docs/ops/SECRETS_BY_COMPONENT.md
libs/scrape-core/pyproject.toml
libs/scrape-core/scrape_core/browser/egress_guard.py
libs/scrape-core/scrape_core/browser/ssrf.py
libs/scrape-core/scrape_core/extraction/regex.py
libs/scrape-core/scrape_core/items.py
libs/scrape-core/scrape_core/middlewares/__init__.py
libs/scrape-core/scrape_core/middlewares/wire_bytes.py
libs/scrape-core/scrape_core/netledger_middleware.py
libs/scrape-core/scrape_core/pipelines.py
libs/scrape-core/scrape_core/targets.py
libs/shared/app_shared/config.py
libs/shared/app_shared/costauth/pricing.py
libs/shared/app_shared/enums.py
libs/shared/app_shared/jobs/targets.py
libs/shared/app_shared/models/jobs.py
libs/shared/app_shared/models/observations.py
libs/shared/app_shared/models/scrape_profiles.py
libs/shared/app_shared/opsmetrics/emit.py
libs/shared/app_shared/opsmetrics/rules.py
libs/shared/app_shared/profiles/repository.py
libs/shared/app_shared/profiles/validation.py
libs/shared/pyproject.toml
pyproject.toml
scripts/analyze_hot_query_plans.py
scripts/canary_document_only_browser.py
scripts/dr/rehearse_upgrade.sh
scripts/preflight_regex_profiles.py
scripts/provision_db_roles.py
scripts/provision_db_roles.sql
scripts/railway_cost_watchdog.py
scripts/security/advisory_triage.yaml
scripts/security/image_inventory.py
scripts/security/known_critical_advisories.txt
scripts/security/pip_audit_gate.py
scripts/sql/grants_expected.yaml
scripts/verify_grants.py
tests/integration/test_browser_egress_guard_image.py
tests/integration/test_grants_manifest.py
tests/unit/test_browser_egress_guard.py
tests/unit/test_browser_ssrf.py
tests/unit/test_canary_document_only_calculator.py
tests/unit/test_cost_watchdog_config.py
tests/unit/test_extraction_regex_timeout.py
tests/unit/test_grants_manifest_schema.py
tests/unit/test_models_control_plane.py
tests/unit/test_netledger_child_duration.py
tests/unit/test_ops_metrics_baseline.py
tests/unit/test_opsmetrics_ledger_coverage.py
tests/unit/test_pip_audit_gate.py
tests/unit/test_pipeline_target_terminalization.py
tests/unit/test_pricing_unit_setting.py
tests/unit/test_profile_regex_validation.py
tests/unit/test_query_stats_delta.py
tests/unit/test_regex_live_path_bounds.py
tests/unit/test_targets_started_transition.py
tests/unit/test_wire_bytes_middleware.py
uv.lock

## Follow-ups (non-blocking)

- **A5 `attempt_uuid` volatile-default rewrite: acceptable as written, do NOT stage it.**
  The `ADD COLUMN ... DEFAULT gen_random_uuid()` does rewrite every `request_attempts`
  partition under `ACCESS EXCLUSIVE`, but A10's rehearsal ran the whole chain against a real
  190 MB production-shaped dump in **3 s**, and migrations run as a one-shot job with no
  concurrent writer (`contracts/migration-job.md`) behind the §2.3 quiesce. The staged
  alternative (add NULL → per-partition backfill → SET DEFAULT → SET NOT NULL) is already
  written into the revision's docstring for the day the table is big enough to matter.
  Cheap improvement: carry that 3 s rehearsal figure and a one-line
  `SELECT count(*) FROM request_attempts;` sanity check into `RELEASE_1_2026-09.md` §2.7
  step 1, so the operator sees the lock is expected and bounded rather than discovering it.
- **Grants (A3/A10 §2.6)**: recommend shipping option (a) as logged (no regression vs. today),
  and scheduling the two reader migrations (`netledger/recorder.py`,
  `admin_usage.py` → `workspace_usage_v`) as an explicit Stage B/C task rather than leaving them
  in `docs/ops/SECRETS_BY_COMPONENT.md`'s "Known gaps" — otherwise the tightening never gets a
  slot. `crawmatic_scraper` never had that SELECT, so A10's DSN cutover is unaffected either way.
- **`provision_db_roles.sql` §8** should adopt `pg_proc` ownership alongside `pg_class`/sequences,
  which retires A10 §2.6's function-ownership pre-flight query permanently (A10's own follow-up
  note; A3 owns the file).
- **`docker-compose.yml` postgres pin** is `17.5-bookworm` while the real production major is
  **18** (established from the backup manifest during A10's rehearsal). Bump it so integration
  tests stop exercising a different major than production.
- **A2 per-page budget arithmetic**: `4 × timeout` is per *pattern*, so a profile with four regex
  rules can spend `4 × 4 × 0.25 s = 4 s` on a pathological page. Bounded and non-catastrophic,
  but worth stating in the tuning doc for `EXTRACTION_REGEX_TIMEOUT_SECONDS`.
- **A2 global profiles are never auto-quarantined** (`record_regex_timeout` is workspace-scoped).
  Conservative and correct as a tenant-isolation choice, but a bad *global* regex then needs
  operator action; decide whether a system-sweep seam is wanted.
- **A1 unit-suite side effect**: importing `price_monitor_browser/settings.py` now starts a daemon
  thread and a loopback listener, and two parametrised tests exec that module. Harmless today
  (idempotent, one per process) but it makes the unit suite hold a socket; consider gating the
  import-time start behind an env flag the test env sets to `false`.
- **A1 image build** remains deferred on disk (BLOCKERS.md). It must succeed before A10 §2.9,
  and after Finding 1 is fixed it is also the only thing that will prove the proxied leg.
