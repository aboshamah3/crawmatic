# Contract: Discovery orchestration + profile seeding (FR-016..FR-019, US3)

**Task**: `STRATEGY_DISCOVERY_RUN = "strategy_discovery.run_discovery"` on the `strategy_discovery`
queue (§26). Worker module `apps/workers/app/workers/tasks_strategy.py`. Both the automatic trigger
(D5, new key → `DISCOVERY_REQUIRED` → enqueue) and the operator trigger (`contracts/api.md`) enqueue
**this same task** with the same payload shape (spec Clarification #3, FR-016).

## Payload

`{workspace_id, competitor_id, domain, url_pattern, sample_urls: [str], triggered_by: "AUTO"|"OPERATOR"}`.
`sample_urls` is 3–10 matched URLs for the key (operator-supplied, or selected from
`competitor_product_matches` for the key when auto/absent).

## Lifecycle (`strategy_discovery_runs.status`)

1. **Validate** `STRATEGY_DISCOVERY_MIN_SAMPLE ≤ len(sample_urls) ≤ STRATEGY_DISCOVERY_MAX_SAMPLE`
   (3..10). Out of bounds → record a `FAILED` run (or reject at the API with a 422 before enqueue) and
   stop (FR-019, US3 AS2). Each sample URL is `validate_competitor_url`'d (SSRF guard, Constitution VI).
2. Insert a `strategy_discovery_runs` row: `sample_size = len(sample_urls)`, `status = PENDING` → set
   `RUNNING` (FR-017, US3 AS1).
3. **Probe** — the one path allowed to try multiple methods (small sample, §14): drive the sample URLs
   through the **existing** fetch/extract pipeline (reuse SPEC-07/10 dispatch + spider + SPEC-11
   limiter/lock), testing candidate **access methods** (the internal ladder only — `DIRECT_HTTP` →
   `DIRECT_HTTP_RETRY` → `PROXY_HTTP` → `PLAYWRIGHT_PROXY`, Constitution VI) then **extraction methods**
   (§16 order). No external unlocker APIs; only public pages.
4. **Select winner**: the `(access_method, extraction_method)` combination that yields a valid numeric
   price + valid currency-when-required + confidence ≥ `STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD` on the
   most sample URLs (ties broken by cheapest access method then earliest extraction-order method).
5. **Complete**:
   - winner found → `status = COMPLETED`, set `winning_access_method` / `winning_extraction_method` /
     `completed_at` (US3 AS1).
   - no working combination → `status = NO_WINNER`, `completed_at` set, `winning_* = NULL` (US3 AS4).
   - unexpected error → `status = FAILED`.

## Profile seeding (shared seed helper `app_shared/strategy/seed.py::seed_from_discovery`)

On `COMPLETED` (FR-018, US3 AS3): upsert the `domain_strategy_profiles` row for the key (unique
`(workspace, competitor, domain, url_pattern)`), set `preferred_access_method` /
`preferred_extraction_method` (+ confidences from the sample), `last_discovery_at`, and move it **out of
`DISCOVERY_REQUIRED`**:
- → `ACTIVE` if the sample already satisfies the 3-confirmation rule (≥3 qualifying successes across ≥3
  distinct URLs — reuses `evaluate_promotion`),
- → `LEARNING` otherwise (winner seeded, awaiting live 3-confirmation).

On `NO_WINNER`: the profile **stays `DISCOVERY_REQUIRED`** and the run records the outcome (US3 AS4 —
"profile status reflects that discovery did not succeed"). Runs against an existing `DEGRADED`/`LEARNING`
profile (rediscovery, `contracts/rediscovery.md`) update it in place through the same helper.

Reactor-safety: discovery is a Celery task (worker), fully off-reactor; it uses the existing
enqueue/dispatch seams, never imports the reactor, and never blocks a spider (Constitution V).
