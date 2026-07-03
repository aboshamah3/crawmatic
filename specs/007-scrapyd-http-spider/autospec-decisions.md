# Autospec Decisions — SPEC-07 Scrapyd HTTP Spider MVP

Auto-answered questions and assumptions, with sources. Format:
`- [step] Q: <question> → A: <answer> (source: doc §<section> | default)`

## specify

- [specify] Q: Which extraction strategies are in scope for this MVP? → A: JSON-LD, CSS selector, regex (JSON-LD first). Embedded-JSON/XPath/Playwright deferred. (source: doc §35.07 "Covers" + §16 order)
- [specify] Q: Does the spider compute alerts/variant states/webhooks? → A: No — spider stops at persistence; price_analysis task (SPEC-09) owns that. (source: doc §8 "The spider stops at persistence")
- [specify] Q: Which access method does this slice use? → A: DIRECT_HTTP only; no proxies/access-policy/browser/rate-limiter/dedup (later specs). (source: doc §35.07 scope + §22 access methods)
- [specify] Q: How is fetch-time SSRF enforced? → A: Reuse/extend existing save-time validate_competitor_url with resolved-IP-at-connection check + per-redirect-hop re-validation. (source: doc §8, §11; repo libs/shared/app_shared/url_safety.py)
- [specify] Q: Default minimum accepted confidence? → A: 0.75 (tunable via DB); defaults JSON-LD 0.95 / CSS 0.85 / regex 0.75 / single-number 0.40. (source: doc §17)
- [specify] Q: Are price_observations / request_attempts models pre-existing? → A: No — created here, partitioned monthly from birth (scraped_at / created_at), PK includes partition key. (source: doc §22; repo grep found no such models)
- [specify] Q: match_current_prices ownership? → A: Schema per §22; spider writes/upserts it (unique workspace_id,match_id); created here if not already present. (source: doc §22)
- [specify] Q: Reactor-safety approach (async driver vs deferToThread)? → A: Left as a plan-phase decision but mandated to be decided ONCE in libs/scrape-core; spec requires only that DB calls are non-blocking on the reactor. (source: doc §8 Reactor safety)
- [specify] Q: How is the feature demonstrated without real sites? → A: Local fixture HTML pages only; tests make zero real-competitor network calls. (source: doc §35.07 "Use fixture pages first")
- [specify] Q: Scrapyd dispatch auth? → A: Basic auth on scraping service; worker authenticates every schedule.json; idempotent dispatch guard. (source: doc §4 scrapyd-http-service, §8 Idempotent dispatch)

## clarify

No questions relayed to the user — all resolved doc-first / from repo context. Integrated into spec §Clarifications (Session 2026-07-03):

- [clarify] Q: Reactor-safe DB mechanism — async driver or deferToThread? → A: sync SQLAlchemy wrapped in deferToThread, decided once in libs/scrape-core; reuse existing sync repos. (source: doc §3 stack is sync SQLAlchemy, no async driver; §8 leaves choice open → best-practice/lowest-risk)
- [clarify] Q: Default batched-flush thresholds? → A: every 50 items or 2s, whichever first, + final flush at close; config-tunable. (source: default; §8 mandates batching, values unspecified)
- [clarify] Q: How do loopback fixtures coexist with fetch-time loopback deny? → A: injectable resolver/allowlist seam; happy-path tests inject a public IP / allowlist local server; deny path tested separately; prod validates real resolved IP, no allowlist. (source: default derived from §8/§11 fetch-time SSRF + testability)

## plan

- [plan] Q: FR-015 update scrape job target state — where does the backing table live? → A: Deferred as a seam. scrape_job_targets belongs to the later orchestration spec (SPEC-08); spec Assumptions + master-doc table enumeration place it outside this slice. Recorded as a documented, scoped Constitution-Check deviation, not silently dropped. (source: spec Assumptions + doc §22/§35.08)
- [plan] Reactor-safe DB seam realized as sync SQLAlchemy in deferToThread reusing SPEC-02 session/RLS; extraction pure parsel/stdlib; migration down_revision a4f205e8d7de (single head). (source: plan.md)

## checklist

Generated checklists/security.md (28 requirements-quality items, release-gate). 26/28 passed as-written; 2 items surfaced real artifact gaps, remediated in spec.md before checking:

- [checklist] CHK008 (RLS gap): DB-level RLS on the 3 new tables was only in plan, not spec → added FR-023 (RLS enabled+forced, fail-closed) to spec. (source: constitution II NON-NEGOTIABLE + plan Constitution Check)
- [checklist] CHK028 (spec/plan conflict): FR-015 said spider MUST update scrape_job_targets, but plan defers that table to SPEC-08 → reworded FR-015 to "record terminal outcome via attempts/observations; dedicated scrape_job_targets write deferred". Resolves the conflict analyze would flag. (source: plan Complexity Tracking + spec Assumptions)

Both spec checklists now fully checked (requirements.md 16/16, security.md 28/28).

## analyze

speckit-analyze: 0 CRITICAL, 0 HIGH, 1 MEDIUM, 4 LOW. 100% FR→task coverage (23/23). All findings remediated in artifacts (analyze is read-only; orchestrator applied edits); no re-run required (nothing CRITICAL/HIGH):

- [analyze] I1 (MEDIUM): US5 Independent Test + Acceptance Scenario 2 still said "async driver or deferToThread" → reconciled to "sync SQLAlchemy in deferToThread" (matches FR-017 + Clarifications). (source: spec Clarifications)
- [analyze] A1 (LOW): spec hedged match_current_prices "may already exist" — verified it does NOT exist in repo → reworded to "this spec creates it". (source: repo grep)
- [analyze] C1 (LOW): plan mislabeled Principles VII & VIII "(NON-NEGOTIABLE)"; constitution marks only II/V/VI → removed the tag on VII/VIII. (source: constitution v1.0.1)
- [analyze] U1 (LOW): `mode` arg semantics unspecified → contracts/spider-args.md now states mode is reserved/pass-through (only HTTP⇒DIRECT_HTTP honored; other modes are later specs). (source: plan scope)
- [analyze] U2 (LOW): redirect-hop vs request_attempt count unreconciled → FR-013 now states a redirect chain = one attempt (one request_attempt row), url = originally requested URL. (source: spec FR-013/US2)

## implement

All 9 phases implemented via sequential sonnet subagents (one per phase); 52/52 tasks [X]; unit suite 891 passed; integration 6 files collect+skip cleanly; migration renders (single head 2db33dea5e14); `scrapy list` shows generic_price_spider. Per-phase commits Phase 1..9.

Follow-ups surfaced during implement for converge to reconcile:
- [implement] SafeResolver DNS-rebinding rejection is wrapped by Twisted into CannotResolveHostError → classify_exception returns UNKNOWN_ERROR, not BLOCKED. Contradicts FR-005/US2 acceptance scenario 1 (SSRF rejection must record BLOCKED). Only the SsrfGuardMiddleware path guarantees BLOCKED. → CONVERGE must fix classification so resolver-path SSRF rejection surfaces as BLOCKED. (Phase 8)
- [implement] generic_price_spider does not yet attach the resolved robots_policy to request.meta, so RobotsPolicyMiddleware defaults to RESPECT for every request. → CONVERGE should wire robots_policy from the resolved competitor/profile onto request.meta. (Phase 4)
- [implement] apps/scrapers duplicates SPEC-06's resolution_cache_key format at the call site (cannot import apps/api). Works (shares the warm cache) but is a maintainability risk — consider hoisting the cache-key helper into libs/shared. → CONVERGE to assess. (Phase 3)
