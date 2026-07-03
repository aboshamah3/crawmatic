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
