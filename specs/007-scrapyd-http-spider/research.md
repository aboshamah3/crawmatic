# Phase 0 Research: Scrapyd HTTP Spider MVP

All NEEDS CLARIFICATION resolved. Each decision cites the master doc (§), the spec Clarifications, and existing repo code being reused. No new third-party stack is introduced; everything is a build-out of the locked stack (Scrapy/Scrapyd, SQLAlchemy/Alembic, Postgres, Redis) already present in SPEC-01→06.

---

## D1 — Reactor-safe DB seam (decided once in `scrape-core`)

**Decision**: Synchronous SQLAlchemy wrapped in Twisted `deferToThread`, defined **once** in `libs/scrape-core/scrape_core/db.py`, reusing `app_shared.database.get_session()` + `set_workspace_context()` and the existing per-process pool through PgBouncer. Shape:

- `run_in_thread(fn, *a, **kw) -> Deferred` — `twisted.internet.threads.deferToThread(fn, ...)`; the single sanctioned way any pipeline/middleware touches the DB.
- `workspace_txn(workspace_id)` — a context manager that opens `get_session()`, calls `set_workspace_context(session, workspace_id)` (so RLS is active), yields, commits/rolls back. Executed **inside** the thread offloaded by `run_in_thread`, never on the reactor.
- Pool stays small (`DB_POOL_SIZE`, existing default 5) with `prepare_threshold=None` (already set in `app_shared.database`) for PgBouncer transaction pooling.

**Rationale**: Spec Clarification (Session 2026-07-03) + master resolved decision #1: the §3 stack is sync SQLAlchemy with **no** async driver, so a parallel async DB stack is higher risk for zero MVP benefit; §8 requires only that no DB call blocks the reactor. Reusing the SPEC-02 session/RLS seam avoids a second connectivity implementation and keeps `SET LOCAL app.workspace_id` semantics identical.

**Alternatives rejected**: (a) async driver (`asyncpg`/SQLAlchemy async) bound to the reactor — contradicts the locked sync stack and duplicates repositories; (b) synchronous commits in the pipeline — blocks the reactor, forbidden by Principle V.

---

## D2 — Fetch-time SSRF: resolver/allowlist seam + per-redirect-hop re-validation

**Decision**: Extend, do not replace, the save-time `app_shared.url_safety.validate_competitor_url`. Add `scrape_core.safety.fetch.validate_resolved_target(url, *, resolver, allowlist=None)`:

1. Run the pure save-time checks (scheme allow-list, userinfo rejection, IP-literal deny) by calling `validate_competitor_url` (reuse, no duplication).
2. Resolve the host via the **injected** `resolver` (a callable `host -> list[ip_str]`).
3. For each resolved IP, reject with the existing `_reject_ip` predicate unless it is in the explicit `allowlist`.

Two enforcement points in the Scrapy project:
- `scrape_core.safety.resolver.SafeResolver` — a Twisted resolver wrapper installed via the `DNS_RESOLVER` setting; it resolves then **refuses to hand back an unsafe IP**, so the connection literally cannot proceed to an internal address (this is what defeats DNS rebinding at connect time, not just at request-build time).
- `scrape_core.safety.middleware.SsrfGuardMiddleware` — `process_request` re-checks scheme/userinfo before fetch; redirect handling re-validates **every** hop (Scrapy's `RedirectMiddleware` re-emits each redirect as a new request that passes back through `process_request`, and each new host re-resolves through `SafeResolver`). A rejection short-circuits to a flagged failure item (no body download).

**Injectable seam** (spec Clarification #3): the `resolver`/`allowlist` are parameters. Happy-path fixture tests inject a resolver returning a **public** IP (or pass an explicit `allowlist` for the loopback fixture server) so the loopback-served fixtures don't trip the deny rule; the deny path is tested separately with private/loopback/redirect cases. **Production wiring passes the real system resolver and `allowlist=None`** — prod always validates the real resolved IP with no allowlist.

**Rationale**: §8 "URL fetch safety" + §11 "at fetch time" mandate resolved-IP-at-connection-time validation + per-redirect re-validation; the existing save-time validator already encodes the deny ranges (`_reject_ip`) and scheme/userinfo rules — reusing it keeps one source of truth (Principle VI/VII). The seam reconciles loopback fixtures with the loopback deny rule without weakening production.

**Alternatives rejected**: (a) validate only the URL literal (no DNS) — cannot catch a public host that resolves/rebinds to a private IP; (b) a global allowlist env toggle — risks shipping an allowlist to prod; the parameter seam keeps prod allowlist-free by construction.

---

## D3 — Partitioned-table convention (first partitioned tables in the repo)

**Decision**: `price_observations` and `request_attempts` are created **partitioned from birth** (§22/§29). No prior migration created a partitioned table, so this spec establishes the convention:

- **ORM** (`app_shared.models.observations`): declare the model on `WorkspaceScopedBase`, add the partition column (`scraped_at` / `created_at`) as `primary_key=True` **in addition** to `id`, giving a composite `PRIMARY KEY (id, <partition_col>)` (Postgres requires the partition key in the PK / every unique constraint). Set `__table_args__ = {"postgresql_partition_by": "RANGE (<partition_col>)"}` (SQLAlchemy PostgreSQL dialect table option) so the parent renders `CREATE TABLE ... PARTITION BY RANGE (...)`.
- **Migration**: create the two parents (SQLAlchemy emits `PARTITION BY RANGE`), then `op.execute` monthly `CREATE TABLE <t>_YYYY_MM PARTITION OF <t> FOR VALUES FROM ('YYYY-MM-01') TO ('<next>-01')` for at least **current + next** month (a small helper computes the month bounds). RLS via `emit_rls_policy` on the **parent** (Postgres propagates the ENABLE/FORCE + policy to all partitions; new partitions inherit it). Indexes on the parent (`workspace_id`, `match_id`, partition col) propagate to partitions.
- Naming: `price_observations_2026_07`, etc. All names stay < 63 bytes.
- `match_current_prices.observation_id` and `competitor_product_matches.current_price_id` remain **soft** references (no FK) so a dropped old partition can dangle harmlessly (§22).

**Rationale**: §22 "Partition monthly (created partitioned from birth; PK includes the partition key)" + §29 retention-by-partition-drop + Principle VIII. `postgresql_partition_by` keeps the ORM the source of truth for autogenerate/offline render; raw-DDL partitions cover what the dialect doesn't emit.

**Alternatives rejected**: (a) non-partitioned "for now" — §29 requires partitioned from the first real-data load (this is it); retrofitting needs a full table rewrite. (b) FK from observations into `competitor_product_matches` — cpm has no `unique(workspace_id, id)` to back a composite FK, and FKs onto/among partitioned tables complicate retention; soft references match §22.

---

## D4 — Extraction order + pure `parsel` parsing

**Decision**: For this MVP the ordered strategy chain is **JSON-LD → CSS selector → regex** (first hit wins; else `PRICE_NOT_FOUND`), a subset of the §16 full chain (platform/embedded-JSON/XPath/Playwright deferred per master decision #4). Implement each extractor as a **pure** function over the response body using `parsel.Selector` (Scrapy's own parsing lib, no reactor) + stdlib `json`/`re`, in `scrape_core/extraction/`. Each returns an `ExtractionCandidate(raw_price_text, currency, method, confidence, selector_used, raw_title, stock, matched_text)`. Default confidences come from `app_shared.profiles.confidence.DEFAULT_CONFIDENCE_RULES` (JSON-LD/`jsonld` 0.95, CSS 0.85, regex 0.75, single-number 0.40) — never hardcoded literals in the extractor.

`parsel` is added to `scrape-core`'s dependencies so extraction is unit-testable against fixture HTML strings without booting Scrapy/Twisted.

**Rationale**: §16 order + §17 confidences; spec FR-007/FR-008 + US3. Purity keeps the whole extraction path off-reactor and covered by fixture-only unit tests (SC-003, SC-007). Consuming the shared confidence defaults keeps §17 DB-tunable (Principle IV/VII).

**Alternatives rejected**: BeautifulSoup/lxml directly — `parsel` already wraps lxml, is the Scrapy-native selector, and gives identical CSS/XPath semantics to what the spider uses.

---

## D5 — Price validation + confidence gate (reuse the money + confidence boundaries)

**Decision**: `scrape_core.validation.validate_candidate(candidate, validation_rules, confidence_cfg) -> Accepted | Rejected(error_code)` applies, in order:

1. `app_shared.money.parse_money(raw_price_text)` → exact `Decimal`, rejecting float/NaN/Infinity/over-scale/non-positive (do **not** round). Failure → `INVALID_PRICE_FORMAT`.
2. `> 0` guard (parse_money already forbids over-scale/non-finite; add the positivity/`>0` check).
3. Currency: if `validation_rules.required_currency` (or the client variant currency) is set and differs → mark `comparable=false` + `CURRENCY_MISMATCH` (still saved, excluded from comparison — no FX).
4. `min_price`/`max_price` bounds from `validation_rules`.
5. `reject_if_text_contains` against the candidate's surrounding `matched_text` (old/installment/discount/"save X"/shipping) → reject.
6. Confidence gate: `candidate.confidence >= confidence_cfg["min_accepted_confidence"]` (default 0.75 via `resolve_confidence_rules`) else `LOW_CONFIDENCE_PRICE`. A single-number candidate (0.40) fails this by default.

Rejections yield a `success=false` observation with the error code; `match_current_prices` is **not** updated with a rejected price.

**Rationale**: §18 validation list + §17 confidence + §19 money; spec FR-008/FR-009/FR-010/FR-011 + US3. Reusing `parse_money` (the single §19 boundary) and `resolve_confidence_rules` (the single §17 source) satisfies Principle VII with no duplicate money/confidence logic. `validation_rules`/`confidence_rules` come from the resolved profile (Principle IV).

**Alternatives rejected**: a fresh Decimal parser — would fork the §19 boundary and risk float leakage.

---

## D6 — Batched persistence pipeline

**Decision**: `scrape_core.pipelines.BatchedPersistencePipeline` (a Scrapy item pipeline) buffers incoming `ScrapeResult` items and flushes when **either** the buffer reaches `SCRAPE_FLUSH_MAX_ITEMS` (default 50) **or** `SCRAPE_FLUSH_INTERVAL_SECONDS` (default 2.0) elapses (a Twisted `LoopingCall` drives the time-based flush), plus a **final flush** in `close_spider`. Each flush is **one** `deferToThread` transaction (via the D1 seam) that: bulk-inserts the batch's `price_observations` + `request_attempts` and upserts each `match_current_prices` row (`insert(...).on_conflict_do_update` on `unique(workspace_id, match_id)`). Thresholds are read from `Settings` (config-tunable, not constants).

**Rationale**: §8 "Persistence batching" + spec Clarification #2 + FR-016 + US5/SC-006: at 10k–20k targets, per-item commits serialize the pooler. Batch flush keeps commit count ≪ item count; final flush guarantees no partial-batch loss at close.

**Alternatives rejected**: one commit per item (forbidden by §8); an unbounded end-of-spider single commit (loses the streaming/backpressure benefit and risks a huge transaction).

---

## D7 — Per-request robots middleware

**Decision**: `scrape_core.robots.RobotsPolicyMiddleware` (custom downloader middleware) resolves `robots_policy` **per request** from the competitor config loaded by the spider (`RESPECT` / `REVIEW_REQUIRED` / `IGNORE_AFTER_APPROVAL`, the existing `RobotsPolicy` enum). Scrapy's process-global `ROBOTSTXT_OBEY` is set **False** in settings. On `RESPECT` with a disallowed path the target is skipped and recorded (`BLOCKED` error code); `IGNORE_AFTER_APPROVAL` fetches. The robots fetcher is injectable so fixtures can supply a robots body without a network call.

**Rationale**: §8 "Robots handling" + FR-006: `robots_policy` is per-competitor/domain (§22), so the process-global toggle is wrong; a per-request middleware reads the resolved policy.

**Alternatives rejected**: `ROBOTSTXT_OBEY=True` — process-global, cannot vary per competitor, and ignores the DB policy.

---

## D8 — Authenticated, idempotent Scrapyd dispatch client

**Decision**: `app_shared.scrapyd.client.ScrapydDispatchClient` (plain `requests`, no scrapy/twisted) exposes `schedule(project, spider, *, workspace_id, scrape_job_id, match_ids, mode, batch_index) -> jobid`. It POSTs `schedule.json` with HTTP **basic auth** from `SCRAPYD_USERNAME`/`SCRAPYD_PASSWORD` against a `SCRAPYD_HTTP_URLS` node, passing the spider args through unchanged. **Idempotency**: a stable `dispatch_key = f"dispatched:{scrape_job_id}:{batch_index}"` guarded by Redis `SET NX` (reusing `app_shared.redis_client`); if the key already exists the call is a no-op (returns the persisted jobid), so an at-least-once Celery retry never double-runs a batch. Lives in `app_shared` (which already owns `SCRAPYD_*` config + `redis_client`), consumed by a thin `apps/workers` Celery task.

**Rationale**: §4 (scrapyd-http-service requires basic auth) + §8 "Idempotent dispatch" + FR-018/FR-019 + US4. Basic auth on Scrapyd is mandatory because `addversion.json` is RCE-capable on an unauthenticated node (Principle VI). Placing the client in `app_shared` keeps it usable by workers and unit-testable without scrapy.

**Alternatives rejected**: (a) unauthenticated dispatch — RCE risk; (b) idempotency via DB unique only — the Redis `SET NX` short-circuits before the network call (cheaper, and §8 names it); the persisted jobid is the durable backstop.

---

## D9 — FR-015 scrape-job-target state (deferred seam)

**Decision**: `scrape_jobs`/`scrape_job_targets` (§22) are **not** created in this slice. `scrape_job_id` is a passed-in correlation UUID stored as a **nullable** soft reference on `price_observations`/`request_attempts` (matching §22's nullable `scrape_job_id`). The spider records each match's terminal outcome (`request_attempts.success` + `price_observations.success`) — the exact data a job-target updater consumes; the actual `scrape_job_targets` row write is a documented seam that activates when that table lands with the job-orchestration spec.

**Rationale**: The spec's Assumptions defer "job orchestration (dispatch of jobs, batching into Scrapyd calls)" to a later spec, and the master doc's resolved decision #5 enumerates the tables created here as exactly `price_observations`, `request_attempts`, `match_current_prices` — `scrape_job_targets` is not among them and is not partitioned. Building the job lifecycle/state machine + `unique(scrape_job_id, match_id)` + parent-counter aggregation (§21/§26) here would contradict the scope. Recorded in plan.md Complexity Tracking as a scoped deviation rather than silently dropped.

**Alternatives rejected**: creating a partial `scrape_job_targets` here — pulls orchestration into a spider-proof slice; violates decision #5's table enumeration.

---

## Cross-cutting: unit-vs-live split (no container engine in this env)

Per the SPEC-02→06 deferred-verification pattern and the master REPO CONTEXT: reactor/DB/network-independent logic (extraction, price validation, confidence, fetch-time URL safety with an injected resolver, batching flush boundaries, the dispatch client request shape + idempotency guard with a fake Redis, model/partition/PK/RLS DDL render via offline `alembic upgrade head --sql`) is **fully unit-tested here**. Live-stack behavior (a real spider run under Scrapyd against a loopback fixture server, actual partition routing, RLS row denial, real `schedule.json` auth) is **authored and skip-marked** for a full-stack host. No test makes a real-competitor network call (FR-021/SC-007).

## Migration head

Current Alembic head is **`a4f205e8d7de`** (`scrape_profiles_table`, SPEC-06). The new migration's `down_revision = a4f205e8d7de`; single linear head preserved (CI head guard).
