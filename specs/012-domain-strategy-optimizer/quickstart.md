# Quickstart & Validation: Domain Strategy Optimizer (SPEC-12)

Validation/run guide. Pure-logic checks run everywhere (no infra). Live checks (RLS zero-rows under
real Postgres, discovery driving a real Scrapyd sample, end-to-end flush→promote) are **integration
tests that skip cleanly** when Postgres/Redis/Scrapyd are absent (SPEC-05..11 convention — no Docker
daemon in this build env). Details live in `data-model.md` and `contracts/`; do not duplicate code here.

## Prerequisites

```bash
uv sync --all-packages        # always --all-packages (workspace members)
uv run alembic heads          # expect a single head after the new migration
uv run scripts/check_single_head.sh
```

## Run the suites

```bash
uv run pytest tests/unit                       # all pure-logic checks (below) — no infra
uv run pytest tests/integration -q             # live checks — skip cleanly without infra
uv run scripts/check_workspace_scoping.py      # CI guard: profiles/runs in WORKSPACE_OWNED_MODELS
uv run pytest tests/unit/test_reactor_safety_grep.py   # FR-025/SC-007 static proof still green
```

## Scenario 1 — Learn & promote (US1, FR-010/FR-011) — unit

1. Seed a `DomainStrategyProfile` in `LEARNING` for a `(workspace, competitor, domain, url_pattern)`.
2. Feed a sequence of qualifying successes (confidence ≥ 0.85, valid `Decimal` price, valid currency)
   across **3 distinct URLs** of the same pattern for one access method and one extraction method.
3. Assert `evaluate_promotion` returns `promote=True`; after apply, the profile has
   `preferred_access_method` + `preferred_extraction_method` (+ confidences), `confirmed_success_count`
   bumped, `status = ACTIVE` (AS1, AS5).
4. Negative: 3 successes across only **2** distinct URLs → `promote=False` (AS2). A below-threshold /
   invalid-price / missing-required-currency success never counts (AS3).
5. Assert `derive_url_pattern("https://www.example.com/products/red-shoe-123")` and
   `.../products/blue-shoe-999?ref=x#frag` both → `example.com/products/*` at `URL_PATTERN_ALGORITHM_VERSION`
   (AS4) — reuses the shipped `app_shared.url_pattern` (research D10).

## Scenario 2 — Consume learned start (US2, FR-013/FR-015) — unit

1. `resolve_strategy_start(active_profile_with_PROXY_HTTP_and_CSS, algorithm_version=1)` →
   `(PROXY_HTTP, CSS)` (AS1).
2. `None` profile → `None` (caller falls back to the default ladder; AS2).
3. `DISABLED` profile → `None` (AS3). `url_pattern_version != 1` → `None` (AS4, never mix versions).

## Scenario 3 — Discovery over a sample (US3, FR-016..FR-019) — unit + integration

1. Unit: `sample_size` 2 or 11 → rejected (422 at API / `FAILED` run) (AS2). 3..10 accepted.
2. Integration (skip-clean): trigger `STRATEGY_DISCOVERY_RUN` for a key with 5 sample URLs; assert a
   `strategy_discovery_runs` row with `sample_size=5` progresses `PENDING→RUNNING→COMPLETED`, records
   `winning_access_method`/`winning_extraction_method`/`completed_at`, and the profile leaves
   `DISCOVERY_REQUIRED` → `LEARNING`/`ACTIVE` (AS1/AS3). No-winner path → `NO_WINNER`, profile stays
   `DISCOVERY_REQUIRED` (AS4).

## Scenario 4 — Rediscovery (US4, FR-020/FR-021) — unit

1. `recent_failure_count = 3` on an `ACTIVE` profile → `evaluate_rediscovery.trigger=True`; apply →
   `DEGRADED` + `STRATEGY_DISCOVERY_RUN` enqueued (AS1).
2. Combined `success_rate = 0.79` (persisted + pending) → trigger (AS2). Repeated low confidence /
   empty selector / 403-429 / currency-gone / unrealistic price / template-change → trigger (AS3).
3. Healthy signals (rate ≥ 0.80, no consecutive failures, confidence ≥ 0.75) → `trigger=False` (stays
   `ACTIVE`). Light re-check task detects degradation without a full failed batch (AS4).

## Scenario 5 — Buffered atomic stats (US5, FR-022..FR-025) — unit + integration

1. Unit (fake/real Redis): `record_attempt` N times for one `(profile, method_type, method_name)` →
   the `stratstat:…` HASH accumulates via `HINCRBY`; **no** primary-store write occurs (AS1, SC-003).
2. `drain` + flush → exactly one `count = count + delta` UPSERT per key (AS2); a second flush with no
   new activity writes nothing.
3. `read_pending` before a flush → promotion/rediscovery see persisted + pending (AS3, FR-024).
4. Static: `tests/unit/test_reactor_safety_grep.py` stays green — the stats recorder is only reachable
   from `scrape_core.pipelines._flush_batch` (already an off-reactor `run_in_thread` entry point), so no
   `time.sleep`/sync-Redis on the reactor (AS4, SC-007).

## Scenario 6 — Workspace isolation (FR-026, SC-005) — integration (skip-clean)

1. With no `app.workspace_id` GUC set, a select on each of the three tables returns **0 rows**
   (`domain_strategy_profiles`/`strategy_discovery_runs` via `emit_rls_policy`;
   `strategy_attempt_stats` via `emit_fk_transitive_rls_policy` EXISTS-subquery).
2. Workspace A cannot read workspace B's profile/run/stats (cross-workspace denied).
3. Rendered-DDL unit test asserts the three RLS statements per table (including the transitive EXISTS
   policy) — runs with no infra.
