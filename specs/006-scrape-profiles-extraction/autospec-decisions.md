# Autospec Decisions — SPEC-06 Scrape Profiles & Extraction Rules

Auto-answered questions (doc-first) during the spec-kit pipeline. Format:
`- [step] Q: <question> → A: <answer> (source: doc §<section> | default)`

## specify

- [specify] Q: Scrape-profile resolution order? → A: match override → domain-strategy preferred → competitor default → workspace default → global default (`workspace_id IS NULL`) (source: doc §9 "Scrape profile resolution")
- [specify] Q: Do the assignment reference columns already exist? → A: Yes — `workspaces.default_scrape_profile_id`, `competitors.default_scrape_profile_id`, `competitor_product_matches.scrape_profile_id` created nullable by SPEC-03/05; this spec supplies the referenced table (source: doc §22 + SPEC-03/05 state)
- [specify] Q: Caching / N+1 avoidance for resolution? → A: batch-resolve per `(competitor_id, url_pattern)`, cache resolved profile in Redis short TTL keyed by `(workspace_id, competitor_id, url_pattern)`, invalidate on relevant writes or via TTL (source: doc §9 "Resolution caching")
- [specify] Q: Result when nothing is assigned anywhere? → A: global default is terminal fallback; if none, explicit "no profile resolved" result, not an error (source: doc §9)
- [specify] Q: Is any scraping/extraction executed here? → A: No — config store + resolve only; selectors/regex validated for syntax/shape, executed in SPEC-07+ (source: doc §35 "No real scraping execution yet")
- [specify] Q: Cookie guardrail? → A: only non-identifying technical cookies (currency/locale); session/auth cookies rejected at write time (source: doc §30 + §22 config guardrails)
- [specify] Q: Regex safety? → A: `*_regex` must compile, screen for catastrophic backtracking; reject un-compilable/obviously-catastrophic at write time (source: doc §16 + §7 correctness)
- [specify] Q: Bulk create/upsert of profiles? → A: set-based reject-and-report keyed by `(workspace_id, name)`, as catalog/matches (source: doc §24 + SPEC-04/05 precedent)
- [specify] Q: Global vs workspace-owned profiles? → A: `workspace_id` rows RLS-isolated; `workspace_id IS NULL` rows are read-only global defaults, managed only via a platform/global path (source: doc §32 + §9)
- [specify] Q: Money constraints in rules bundles? → A: Decimal/NUMERIC(18,4), reject NaN/Infinity and over-scale (>4 dp) values, never round (source: doc §19)
- [specify] Q: Domain-strategy chain step, given its table isn't built yet? → A: optional no-op step; skip cleanly when `domain_strategy_profiles` absent, proceed to competitor default (source: doc §14/§35 scoping — SPEC-12 builds it)
- [specify] Q: Which acceptance items are deferred (no live PG/Redis)? → A: live CRUD, RLS row denial, cross-workspace, Redis TTL/invalidation, migration run, e2e deferred; DB/Redis-independent logic unit-tested here (source: no-docker-daemon project constraint + SPEC-03/05 precedent)

Scope boundaries confirmed from doc §35: access_policies/proxy_providers/domain_access_rules → SPEC-10; spider execution/price_observations → SPEC-07; domain_strategy_profiles → SPEC-12. Not built in SPEC-06.

## clarify

## analyze

speckit-analyze: 0 CRITICAL, 0 HIGH. 100% FR/SC coverage (23/23 FR, 8/8 SC), constitution-aligned, factual claims verified against codebase (single head f4c8a391d5c9, assignment columns pre-exist, parse_money not yet present). 5 LOW/MEDIUM polish findings — all remediated in-place (analyze is read-only, orchestrator applied edits):
- [analyze] G1 (MEDIUM, coverage): edge case "empty-extraction profile is accepted" lacked a positive unit assertion → added to T017 in tasks.md.
- [analyze] C2 (LOW, mapping): SC-002 coverage row omitted T032/T033 → added.
- [analyze] D1 (LOW, duplication): FR-008/FR-022 money overlap → FR-022 now states it is the general §19 rule FR-008 specializes (one shared validator).
- [analyze] A1 (LOW, ambiguity): FR-011 "single-number 0.40/reject" → reworded to "single bare number 0.40 — below the default minimum, so rejected".
- [analyze] M1 (LOW, terminology): normalized `matches.scrape_profile_id` shorthand → real column `competitor_product_matches.scrape_profile_id` in clarifications/US2/Key Entities.
No CRITICAL/HIGH fixed, so no analyze re-run required.

## clarify

- [clarify] Ambiguity scan across the full taxonomy found no critical spec-level ambiguities: all high-impact questions were already resolved doc-first in the `## Clarifications` section, and every residual gap (cache-TTL value, FK promotion vs soft ref, delete policy block-vs-null, ReDoS-detection depth, cookie deny heuristic, global-profile seeding mechanism) is explicitly plan-level and traceable to PROJECT_SPEC §9/§16-20/§22. No stakeholder question required; no spec edits made. Requirements checklist remains 16/16. (source: doc §9/§16-20/§22 + prior-spec precedent)
