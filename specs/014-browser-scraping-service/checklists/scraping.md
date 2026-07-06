# Browser Scraping Correctness & Safety Checklist: Browser Scraping Service

**Purpose**: Validate that the requirements for the browser (JS-rendering) scrape path are complete, clear, consistent, and measurable before implementation — focused on scraping correctness, SSRF/URL safety, timeout bounding, variant-selection, proxy routing, failure-code coverage, dispatch routing, and shared-seam reuse.
**Created**: 2026-07-06
**Feature**: [spec.md](../spec.md)
**Audience**: Implementer + Reviewer

## Requirement Completeness

- [x] CHK001 Are requirements defined for extraction running against the *rendered* DOM (post-JS) rather than initial HTML, for every extraction strategy the HTTP spider supports? [Completeness, Spec §FR-001]
- [x] CHK002 Are the browser spider's dispatch input arguments explicitly enumerated and matched to the HTTP spider's contract? [Completeness, Spec §FR-002]
- [x] CHK003 Are requirements specified for the no-`wait_for_selector` case (proceed on load/network-settle) distinctly from the selector-present case? [Completeness, Spec §FR-003, Edge Cases]
- [x] CHK004 Are requirements defined for a safe default browser timeout when `browser_timeout_ms` is unset? [Completeness, Spec §FR-003/FR-018, Edge Cases]
- [x] CHK005 Is the `variant_selector_config` JSON shape (which element, what value, how keyed off the match variant) fully specified somewhere binding (plan/contract)? [Completeness, Spec §FR-004, Assumptions]
- [x] CHK006 Are requirements defined for browser session/context release on crash, page error, or timeout so slots are reclaimed (no leaked browser processes)? [Completeness, Spec §FR-018, Edge Cases]
- [x] CHK007 Is the `price_analysis` handoff requirement (exactly one task per affected variant, deduped per variant per job) stated with the same dedup scope as the HTTP spider? [Completeness, Spec §FR-006]
- [x] CHK008 Are the deployment prerequisites (separate image, browser runtime baked at build, project baked in, basic auth) enumerated as requirements rather than assumed from scaffold? [Completeness, Spec §FR-013]
- [x] CHK009 Are the concurrency-bound knobs (`max_proc`, `CONCURRENT_REQUESTS`, `PLAYWRIGHT_MAX_CONTEXTS`) tied to a requirement that this feature owns their correctness? [Completeness, Spec §FR-014, Assumptions]

## Requirement Clarity

- [x] CHK010 Is "a deliberately low level" / "a small number of concurrent browser jobs" quantified or bounded to an objectively checkable value? [Clarity, Spec §FR-014/SC-004]
- [x] CHK011 Is "wait for the page to settle" (after variant selection or load) defined with an objective condition (selector present / network-idle / change), not left subjective? [Clarity, Spec §FR-004, US3 AS-1]
- [x] CHK012 Are the browser-failure error codes named and enumerated (timeout, variant-not-found, playwright-failed, proxy-failed, blocked) rather than referred to as "an appropriate error code"? [Clarity, Spec §FR-003/FR-005, US1 AS-2]
- [x] CHK013 Is "off the reactor thread" made concrete (which calls must be offloaded and by what mechanism) consistently with the HTTP spider's rule? [Clarity, Spec §FR-007]
- [x] CHK014 Is the batched-flush trigger quantified ("every N items or T seconds") with the same N/T semantics as the HTTP spider? [Clarity, Spec §FR-010]
- [x] CHK015 Is "the same idempotency guard" specified with its exact key form (`dispatched:{scrape_job_id}:{batch_index}`) so it is unambiguous? [Clarity, Spec §FR-016]

## Requirement Consistency

- [x] CHK016 Do the reuse requirements (persistence, extraction/validation, SSRF, robots, proxy, locks, rate-limit) consistently state "reuse as-is, no divergence" without contradicting any browser-specific requirement? [Consistency, Spec §FR-006/§FR-007/§FR-008/§FR-009/§FR-011/§FR-012, Assumptions]
- [x] CHK017 Is the SSRF re-validation requirement consistent between the functional requirement and the edge case (every hop / every redirect against the resolved IP)? [Consistency, Spec §FR-008, Edge Cases, US4 AS-2]
- [x] CHK018 Are the "no new schema / no migration" requirement and the "consumes existing SPEC-06 fields" statements mutually consistent across FR, Key Entities, and Assumptions? [Consistency, Spec §FR-017, Key Entities, Assumptions]
- [x] CHK019 Is the robots-handling requirement (custom middleware, not `ROBOTSTXT_OBEY`) consistent with how the HTTP spider is described so the two paths do not diverge? [Consistency, Spec §FR-009]
- [x] CHK020 Is the "no in-run HTTP→browser escalation; routing decided at dispatch by resolved mode" position stated consistently across US2, FR-015, and Assumptions with no conflicting escalation language? [Consistency, Spec §FR-015, Assumptions]

## Acceptance Criteria Quality (Measurability)

- [x] CHK021 Can SC-002 (100% browser targets to browser service / 0% to HTTP, and vice-versa) be objectively measured from dispatch records? [Measurability, Spec §SC-002]
- [x] CHK022 Can SC-004 (never exceeds low concurrency; no page beyond timeout) be objectively observed under load? [Measurability, Spec §SC-004]
- [x] CHK023 Can SC-005 (0% internal-address fetches complete; refused before body read) be verified against a redirect-to-internal case? [Measurability, Spec §SC-005]
- [x] CHK024 Can SC-006 (proxy routed, attempt records `PLAYWRIGHT_PROXY`, password in no log) be objectively checked, including the negative log assertion? [Measurability, Spec §SC-006]
- [x] CHK025 Can SC-007 (far fewer than N commits, no DB call on reactor thread) be measured with a concrete commit-count / thread assertion? [Measurability, Spec §SC-007]
- [x] CHK026 Can SC-008 (retried batch runs exactly once on exactly one node) be verified deterministically? [Measurability, Spec §SC-008]

## Scenario Coverage

- [x] CHK027 Are requirements defined for the primary flow (JS page → wait → extract → one observation/current-price/attempt + one price_analysis)? [Coverage, Spec §US1, FR-001/FR-006]
- [x] CHK028 Are requirements defined for the browser-mode-but-static-HTML case (browser mode as a superset of static extraction)? [Coverage, Spec §US1 AS-4]
- [x] CHK029 Are mixed-mode dispatch requirements (one job, both modes, no batch mixes modes) fully covered? [Coverage, Spec §US2 AS-1, FR-015, Edge Cases]
- [x] CHK030 Are requirements defined for the all-HTTP-domain case (no batch sent to browser pool)? [Coverage, Spec §US2 AS-3]
- [x] CHK031 Are variant-present and variant-absent flows both covered (perform selection vs. never attempt interaction)? [Coverage, Spec §US3 AS-1/AS-2, FR-004]

## Edge Case Coverage

- [x] CHK032 Are requirements defined for `wait_for_selector` never appearing within timeout (bounded timeout failure, failed attempt recorded, no bogus observation)? [Edge Case, Spec §FR-003, US1 AS-2, Edge Cases]
- [x] CHK033 Are requirements defined for a variant target that is missing/uninteractable (clean failure, no partially-interacted price persisted)? [Edge Case, Spec §FR-005, US3 AS-3, Edge Cases]
- [x] CHK034 Are requirements defined for a public URL that 302-redirects to an internal host (refused before body, on every hop)? [Edge Case, Spec §FR-008, US4 AS-2, Edge Cases]
- [x] CHK035 Are requirements defined for "proxy assigned but browser cannot use it" (recorded as proxy failure, not a silent direct fetch that misrepresents transport)? [Edge Case, Spec §FR-011, Edge Cases]
- [x] CHK036 Are requirements defined for browser crash / page error mid-scrape (failed attempt + session released)? [Edge Case, Spec §FR-018, Edge Cases]
- [x] CHK037 Are requirements defined for an already-held in-flight match lock (respect lock/rate-limit, no duplicate concurrent scrape)? [Edge Case, Spec §FR-012, US4 AS-5]

## Non-Functional Requirements (Safety, Scale, Compliance)

- [x] CHK038 Are reactor-safety requirements specified with the same rigor as the HTTP spider (no sync DB/blocking Redis on the reactor)? [Non-Functional, Spec §FR-007]
- [x] CHK039 Are the SSRF scheme-allowlist + private/loopback/link-local/internal deny rules specified against the resolved IP at connection time? [Non-Functional/Security, Spec §FR-008]
- [x] CHK040 Is the compliance boundary (public product pages only; JS rendering added, no anti-bot/stealth/authenticated-session) stated as a hard requirement? [Non-Functional/Compliance, Spec §FR-019]
- [x] CHK041 Is the proxy-password non-disclosure requirement stated as a non-functional (logging/security) constraint, not only implied? [Non-Functional/Security, Spec §FR-011, SC-006]

## Dependencies & Assumptions

- [x] CHK042 Are the reused upstream capabilities (SPEC-06 fields, SPEC-07 persistence/extraction, SPEC-08 dispatch/idempotency/node-selection, SPEC-09 handoff, SPEC-10 proxy, SPEC-11 locks/rate-limit) documented as validated dependencies? [Dependency, Assumptions]
- [x] CHK043 Is the assumption that the `scrapers-browser` deployment scaffold (image, `scrapyd.conf`, entrypoint, `SCRAPYD_BROWSER_URLS`) already exists validated against the actual repo state? [Assumption, Spec §Assumptions]
- [x] CHK044 Is the assumption that the resolver already yields per-target `mode` (routing key) validated so browser routing has a real signal? [Assumption, Spec §Assumptions]

## Ambiguities & Conflicts

- [x] CHK045 Is there a resolved position (no ambiguity) on where `variant_selector_config` schema validation happens and what an invalid config does at runtime? [Ambiguity, Spec §FR-004/FR-005]
- [x] CHK046 Is the known dispatch defect (browser batches currently scheduled with the HTTP project/spider) captured as an explicit requirement to fix, with no conflicting statement that routing already works? [Conflict, Spec §FR-015/FR-016]
- [x] CHK047 Is the browser retry semantics (one browser attempt per target vs. an in-run ladder) unambiguously stated so implementers do not add an unintended in-process fallback? [Ambiguity, Spec §Assumptions]
