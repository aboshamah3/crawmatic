# Autospec Decisions Log — SPEC-14 Browser Scraping Service

Questions auto-answered from the master doc (`/srv/crawmatic/PROJECT_SPEC.md`) and the existing
codebase, rather than asked of the user. Format:
`- [step] Q → A (source)`

## specify

- [specify] Q: Is the browser service a separate deployment or a mode of the HTTP service? → A:
  Separate Scrapyd service/image with its own node pool and low concurrency. (source: doc §4
  scrapyd-browser-service, §35.14 "Browser service runs separately")
- [specify] Q: What decides that a target goes to the browser service? → A: Resolved scrape profile
  `mode == BROWSER`; dispatch already batches by `(domain, mode)`. The "domain strategy learned
  browser is needed" path flips future dispatch via SPEC-12, not an in-run switch. (source: doc §8
  "Used when scrape_profiles.mode = BROWSER…", §26 batching by domain/mode; code
  `libs/shared/app_shared/jobs/batching.py`)
- [specify] Q: Does the browser spider do in-process HTTP→browser escalation (access ladder attempt
  4)? → A: No. HTTP and browser are separate processes (HTTP node has no browser), so PLAYWRIGHT_PROXY
  is realized as browser-mode routing at dispatch time; in-run escalation is out of scope. (source:
  doc §4/§8 separate services; §35.14 out-of-scope "automatic browser-fallback escalation beyond the
  ladder/strategy")
- [specify] Q: Are new DB fields/migrations required? → A: No. `scrape_profiles.mode`,
  `wait_for_selector`, `browser_timeout_ms`, `variant_selector_config` already exist from SPEC-06.
  (source: `libs/shared/app_shared/models/scrape_profiles.py` lines 84–119)
- [specify] Q: What existing machinery is reused vs. built new? → A: Reuse SPEC-07 persistence/
  extraction/SSRF/robots pipeline, SPEC-08 dispatch/batching/node-selection/idempotency, SPEC-10
  proxy assignment + attempt logging + PLAYWRIGHT_PROXY, SPEC-11 locks + rate limiting, SPEC-09
  price_analysis handoff. New: `generic_browser_price_spider`, scrapy-playwright wait/variant/proxied
  context, and the dispatch routing that sends BROWSER batches to the browser project/spider. (source:
  doc §8, §26; code survey of existing seams)
- [specify] Q: Gap found in current dispatch? → A: `apps/workers/app/workers/tasks_jobs.py` already
  routes BROWSER batches to `SCRAPYD_BROWSER_URLS` but schedules the HTTP project/spider
  (`price_monitor`/`generic_price_spider`) for all batches; SPEC-14 must schedule the browser project
  (`price_monitor_browser`) + `generic_browser_price_spider` for BROWSER batches. Captured as FR-015/016.
  (source: code `tasks_jobs.py` lines 53–57, 247–260, 445–458)

## clarify

- [clarify] Q: Any critical ambiguities requiring resolution before planning? → A: None.
  Coverage scan: all taxonomy categories Clear. The three open items
  (`variant_selector_config` JSON shape, browser-failure error-code names, browser retry
  semantics) are plan-level design decisions the master doc is silent on — deferred to
  `/speckit-plan`, not asked of the user. (source: doc §8/§34 silent on these; spec Assumptions
  already flag them as plan-defined)

## analyze

speckit-analyze report: 0 CRITICAL, 1 HIGH, 2 MEDIUM, 2 LOW. No user pause required
(no CRITICAL). Remediations applied to artifacts by the orchestrator (analyze is read-only):

- [analyze] C1 (HIGH, Constitution §VI NON-NEGOTIABLE): tasks.md labeled US1+US2 the "deployable
  MVP", but the browser SSRF guard (per-hop `PLAYWRIGHT_ABORT_REQUEST` T030 + pre-fetch
  `SsrfGuardMiddleware`/`DNS_RESOLVER` wiring T031) only landed in US4. Shipping the MVP would run
  the browser path with NO SSRF enforcement — a Principle VI violation. → FIX: redefined the
  deployable MVP to include T030/T031 as a hard safety gate; the browser path MUST NOT be
  dispatched in production until T030/T031 complete. Updated T014 forward-ref, Phase 4 checkpoint,
  Phase 6 note, and the Implementation Strategy MVP section. (tasks.md)
- [analyze] I1 (MEDIUM, signature drift): data-model.md §2 stated the interpreter as
  `parse_variant_config(config, match) -> list[PageMethod]` (one fn taking the match), but the
  design (research R2 + variant-selection.md + tasks T024) splits it into
  `resolve_variant_values(config, match)` (off-reactor) + `parse_variant_config(config,
  resolved_values)` (pure). → FIX: corrected data-model.md §2 to the two-function signature.
- [analyze] G1 (MEDIUM, coverage): FR-013 (browser service basic-auth on every `schedule.json`)
  had no dedicated verification task. → FIX: extended T023 to assert the dispatch client
  authenticates to the browser node (basic auth), noting the deployment scaffold itself is
  SPEC-01's (R12).
- [analyze] X1 (LOW, redundant wording): T021 bundled `nodes` into the "change" tuple though node
  routing is already mode-branched in `tasks_jobs.py`. → FIX: trimmed T021 wording to clarify only
  the project/spider constants change; node selection already mode-branched, left unchanged.
- [analyze] N1 (LOW/MEDIUM, test): FR-007/Principle V reactor safety asserted only in the
  env-skipped live test T036. → FIX: added a lightweight unit guard to Polish (T037) asserting the
  spider's DB/Redis entry points route through `run_in_thread` (runs in this container-less env).

Re-run (after C1/HIGH remediation): 0 CRITICAL, 0 HIGH, 0 MEDIUM, 4 LOW. C1 confirmed mitigated
(now informational S1 — flagged in 3 places + pulled into MVP gate). Second-pass LOW findings:

- [analyze] I1b (LOW, plan wording): plan.md said `page.py` builds "proxied context kwargs", but
  T032 stamps the proxied `playwright_context` in the spider's `_browser_request_for`, not `page.py`.
  → FIX: corrected plan.md structure comment for `page.py`.
- [analyze] B1 (LOW, FR-003 unset-selector default): T013 only appended a wait PageMethod "if set";
  the "normal load/network settle" default for the `wait_for_selector`-unset path was implicit.
  → FIX: T013 now appends an explicit `wait_for_load_state("networkidle")` (bounded) when unset.
- [analyze] U1 (LOW, negative reqs): FR-017 (no-migration) / FR-019 (public-pages/no-stealth) have
  no dedicated verification task. → ACCEPTED as reuse/constraint-guaranteed (no new schema is
  structurally impossible here; FR-019 is a design constraint honored by reusing the locked access
  methods). Not worth a task; noted.
- [analyze] S1 (LOW, informational): SSRF authored two phases after US1 settings.py — already
  mitigated by the C1 fix (MVP gate). No further action.
