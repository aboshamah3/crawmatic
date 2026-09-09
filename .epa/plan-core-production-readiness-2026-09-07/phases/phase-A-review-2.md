# Phase A Review #2: Stage A — Contain risk and establish a trustworthy baseline
Verdict: PASS
Base SHA: d824f31   Reviewed: main tree (branch `epa/plan-core-production-readiness-2026-09-07`, uncommitted)
Scope: re-review after the A1 fix cycle (`reports/A1-fix1.md`) closing review #1's single finding.

## Integration command

`sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"` → exit 0

```
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
4207 passed, 19 skipped, 6 deselected, 63 warnings in 543.74s (0:09:03)
```

(4202 at review #1 + the 5 new proxied-leg unit tests; zero regressions.)

`cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh` → exit 0

```
check_single_head: OK — exactly 1 head.
b6f1c40a97d2 (head)
```

`cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/integration/test_browser_egress_guard_image.py -m browser -q -p no:cacheprovider` → exit 0

```
......                                                                   [100%]
6 passed in 20.87s
```

Docker builds not run (disk, per ASSUMPTIONS answer 4 and BLOCKERS 2026-09-07 15:50).
Secret scan over the five files the fix touched: no hits (the only matches are the
test-local fakes `_UPSTREAM_PASSWORD = "fake-provider-password"` and the existing
`JWT_SECRET = "test-jwt-secret"` fixture env).

## Finding 1 (review #1) — CLOSED

**Leg selection no longer depends on browser proxy authentication.**
`EgressGuard.register_upstream_async()` / `register_upstream()`
(`libs/scrape-core/scrape_core/browser/egress_guard.py:405-466`) bind a **dedicated loopback
listener per `UpstreamProxy`** and return its port; `_client_callback(upstream)` closes over the
leg, so `_serve()` receives the upstream from *the socket that accepted the connection*
(`:488-534`). The header-driven `_wants_upstream_leg()` path is gone from the selection
decision and survives only as a fail-closed guard.

Verified against each half of the finding:

- **Leg really taken.** `_serve()` calls `_validate_destination()` first, then dispatches to
  `_serve_via_upstream()` whenever `upstream is not None` (`:545-585`). The registered-upstream
  dict is keyed on the frozen dataclass, so re-registration is idempotent (asserted).
- **Fail-closed.** A request arriving with the stale `username="leg=proxy"` credential on a
  listener with **no** upstream is refused `502 / REJECTED_UPSTREAM_REFUSED`, never dialed direct
  (`egress_guard.py:549-560`, test `test_a_proxied_leg_asked_for_the_old_way_fails_closed`
  asserts `ALLOWED_DIRECT == 0` and that the origin server was never contacted).
  `register_upstream()` with no running loop raises `RuntimeError` rather than returning the
  direct port (`:443-466`, asserted). Destination validation runs *before* the upstream is
  dialed, so a privately-resolving name on a proxied leg is refused and the upstream is never
  contacted (`test_upstream_leg_still_validates_the_destination_before_forwarding` asserts
  `upstream_proxy.heads == []`).
- **Ledger booking truthful.** The spider sets `meta["playwright_context"] = f"proxy:{id}"` and
  `context_kwargs["proxy"]` inside the same branch that registers the leg
  (`generic_browser_price_spider.py:625-694`), and `register_upstream()` raises rather than
  degrading, so `netledger_middleware._is_proxied(meta)` books PROXY exactly when the bytes
  really cross the provider. Chromium is handed a bare `http://127.0.0.1:<leg_port>` with **no**
  username/password — the provider credential never crosses the browser boundary — while
  `_serve_via_upstream()` attaches the provider's own `Proxy-Authorization` on the far side
  (`egress_guard.py:586-654`). `_origin_form_request()` still strips all `Proxy-*` headers.
- **Tests are non-vacuous — independently confirmed by mutation, not taken on report.** I
  changed the dispatch at `egress_guard.py:545` to `if False and upstream is not None:` (i.e.
  the old "fell through to direct" behaviour), re-ran both suites, and restored the file from a
  scratchpad copy (`diff -q` → identical, both suites re-run green, 20 passed):
  - `tests/unit/test_browser_egress_guard.py` → **1 failed, 13 passed** —
    `test_upstream_leg_listener_forwards_connect_with_the_provider_credential` at line 363.
  - `tests/integration/test_browser_egress_guard_image.py -m browser` → **1 failed, 5 passed** —
    `test_proxied_context_really_takes_the_upstream_leg` at line 545, with
    `refusing 127.0.0.1:<port> (PRIVATE_OR_INTERNAL_IP)` in the captured log.
  Both fake-upstream harnesses are real sockets and assert the **wire contract** (the forwarded
  `CONNECT <name>:<port>` line and the base64 `Proxy-Authorization`), not a counter; the
  in-image case drives real Chromium through a per-context proxy wired exactly as the spider
  wires it, and additionally asserts the 302 hop to `http://127.0.0.1:<port>/secret` was refused
  on the *proxied* listener (`REJECTED_PRIVATE_IP_LITERAL >= 1`, `_FixtureHandler.secret_hits == []`).

## Per-task

| Task | Criteria met | Evidence quality | Notes |
|---|---|---|---|
| A1 Browser egress guard (F01, P0) | **Yes** (was Partial) | Strong on both legs now | Direct leg unchanged and still correct (launch args at `settings.py:127-155`, validate-then-dial-`addrs[0]` with no second lookup). Proxied leg reworked to one listener per upstream — see "Finding 1 — CLOSED" above. 6/6 in-image scenarios pass natively against real Chromium; 14/14 guard unit tests. `PROXY_LEG_USERNAME` deliberately retained as a fail-closed tripwire for stale callers (fix deviation 1) — correct, deleting it would let the old wiring degrade back to a silent direct dial. `EgressGuard.upstream` now means "the main listener's leg"; no caller in the repo passes it and `ensure_process_guard()` passes nothing, so the process guard's main listener stays direct (deviation 2, verified by grep: the only `register_upstream` call sites are the spider and the integration fixture). Two registration entry points (deviation 3) avoid a deadlock when called from the guard's own loop; sound. Dockerfile change is one comment block + the pre-existing gate (deviation 4) — A4's `scrapyd-client==2.0.3` pin in the same file is intact. |
| A2 Regex hard deadline (F02) | Yes | Strong | Unchanged since review #1. |
| A3 Component grants (F03) | Yes, with the two logged gaps | Strong | Unchanged since review #1. |
| A4 Advisory triage + pinned build tools | Yes | Adequate | Unchanged; the fix's Dockerfile comment did not disturb the pin (diff verified). |
| A5 Lifecycle truth | Yes | Strongest in the phase | Unchanged; migration chain still single-head at `b6f1c40a97d2`. |
| A6 Canary calculator | Yes | Adequate | Unchanged. |
| A7 Ledger coverage + wire bytes | Yes | Adequate | Unchanged. |
| A8 Cost artifacts + watchdog | Yes | Adequate | Unchanged. |
| A9 Query statistics | Yes | Adequate | Unchanged. |
| A10 Release 1 (OWNER GATE) | Yes | Strong | Unchanged. Note the fix touched no A10 artifact; `RELEASE_1_2026-09.md` §2.9 image certification is still the step that will exercise the guard gate inside the image. |

**No regression elsewhere.** The changed-path set outside `.epa/` is byte-for-byte the same
72 paths as review #1 (no new files, none removed): the fix touched only
`egress_guard.py`, `generic_browser_price_spider.py`, `tests/unit/test_browser_egress_guard.py`,
`tests/integration/test_browser_egress_guard_image.py` and one comment block in
`apps/scrapers-browser/Dockerfile`, all of which were already in the list. The full unit suite
gained exactly the 5 new tests and lost none.

## Findings

None.

## Files to commit

(The exact set outside `.epa/`, from `git status --porcelain`.
`libs/scrape-core/scrape_core/middlewares/__pycache__/` is gitignored and excluded.
`scripts/security/known_critical_advisories.txt` is a **deletion** — stage with `git rm`.)

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

New, from this review:

- **Upstream listeners are never evicted.** `register_upstream` is keyed on the frozen
  `UpstreamProxy`, and a DataImpulse sticky key changes the *username* — hence the dataclass,
  hence the port. A crawl with N live sticky sessions on one provider therefore binds N
  loopback listeners, released only when the guard stops (process exit). Bounded today by the
  number of live sticky sessions, but it is a real growth dimension: if sticky rotation is ever
  made per-request this needs an eviction policy (the worker flagged it too). Stage B note.
- **Per-provider context name vs. per-sticky-key leg.** `meta["playwright_context"]` is
  `proxy:{provider_id}`, but the leg port now varies with the sticky key; scrapy-playwright
  keys its context pool by that name, so a second sticky key on the same provider reuses the
  first context's proxy kwargs. **Pre-existing** (the old wiring had the same shape via the
  username), not a regression from this fix, but worth an explicit decision in Stage B.
- **`register_upstream_async` race branch** closes the losing server without awaiting
  `wait_closed()` (`egress_guard.py:432-435`). Harmless (the socket is closed), cosmetic only.

Carried forward unchanged from review #1:

- **A1 image build** remains deferred on disk (BLOCKERS.md). It must succeed before A10 §2.9.
  The gate command itself now passes 6/6 natively against real Chromium including the proxied
  case, so what remains unproven is only that the stage runs *inside the image*.
- **A1 unit-suite side effect**: importing `price_monitor_browser/settings.py` starts a daemon
  thread and a loopback listener; consider gating the import-time start behind an env flag.
- **A5 `attempt_uuid` volatile-default rewrite**: acceptable as written; carry A10's 3 s
  rehearsal figure and a `SELECT count(*) FROM request_attempts;` sanity check into
  `RELEASE_1_2026-09.md` §2.7 step 1.
- **Grants (A3/A10 §2.6)**: ship option (a) as logged; schedule the two reader migrations
  (`netledger/recorder.py`, `admin_usage.py` → `workspace_usage_v`) as an explicit Stage B/C task.
- **`provision_db_roles.sql` §8** should adopt `pg_proc` ownership alongside `pg_class`/sequences,
  retiring A10 §2.6's function-ownership pre-flight query.
- **`docker-compose.yml` postgres pin** is `17.5-bookworm` while production is major **18**.
- **A2 per-page budget arithmetic**: `4 × timeout` is per *pattern*; state the aggregate bound in
  the tuning doc for `EXTRACTION_REGEX_TIMEOUT_SECONDS`.
- **A2 global profiles are never auto-quarantined** (`record_regex_timeout` is workspace-scoped);
  decide whether a system-sweep seam is wanted.
