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
