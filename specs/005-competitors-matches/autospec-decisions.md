# Autospec Decisions — SPEC-05 Competitors & Matches

Feature directory: `specs/005-competitors-matches`
Master doc: `/srv/crawmatic/PROJECT_SPEC.md`

## specify

- [specify] Q: Any clarifications needed? → A: No NEEDS CLARIFICATION markers; requirements fully specified by the doc (source: §11 URL safety/SSRF, §15 URL normalization+versioned pattern, §22 competitors/matches tables + unique constraints + enums + deletion semantics, §24 endpoints, §32 isolation, §35 subsection "05").
- [specify] Q: Feature short-name / directory? → A: `specs/005-competitors-matches` (sequential; matches doc §5 dir).
- [specify] Q: Scope? → A: competitors + competitor_product_matches tables + CRUD/bulk-upsert endpoints + save-time SSRF URL validation + versioned URL normalization/pattern derivation + isolation ONLY. NOT scrape-profiles (06), scraping/prices/alerts (07+), fetch-time URL re-validation (07 spider), optimizer (12), url_pattern backfill task (source: §35 05 vs 06/07/12; §15 backfill note).
- [specify] Q: SSRF save-time rules? → A: scheme http/https; reject localhost/private(10/8,172.16/12,192.168/16)/loopback/link-local(169.254/16,fe80::/10)/unique-local(fc00::/7)/metadata(169.254.169.254)/internal hostnames; reject userinfo. Applies on create+update+bulk. Fetch-time re-resolution = SPEC-07 spider (source: §11).
- [specify] Q: URL pattern derivation + versioning? → A: derive_url_pattern per §15 normalization steps (lowercase host, strip scheme/www/trailing-slash/fragment/query, preserve locale prefixes, :id for id-like segments, * for product slugs after known keys); URL_PATTERN_ALGORITHM_VERSION constant stored as url_pattern_version; never mix versions in lookups; backfill deferred (source: §15).
- [specify] Q: Unique keys? → A: competitors unique(workspace_id, domain); matches unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url). Variant → unlimited matches (source: §22).
- [specify] Q: FK workspace-consistency + soft refs? → A: composite workspace-local FKs (workspace_id, ref_id)→parent(workspace_id, id) for product/variant/competitor (add unique(workspace_id,id) to competitors; products/variants already have it from SPEC-04); current_price_id is a SOFT ref (no FK); scrape_profile_id/access_policy_id plain nullable (targets in SPEC-06/10) (source: §22/§32).
- [specify] Q: Match health fields at creation? → A: defaults (health unknown/pending, consecutive_failures=0, null success_rate/current_price/last-* timestamps); populated by SPEC-07/09+ (source: §22).
- [specify] Q: Enums? → A: legal_status(REVIEW_REQUIRED/APPROVED/DISABLED), robots_policy(RESPECT/REVIEW_REQUIRED/IGNORE_AFTER_APPROVAL), priority(LOW/NORMAL/HIGH/CRITICAL), competitor/match status active/archived, health_status string-backed (source: §22).
- [specify] Q: Live-Postgres acceptance given no daemon here? → A: DB-independent logic unit-tested here (models/naming render, RLS render, SSRF validator, URL normalization/pattern/version, bulk-upsert statement construction, pagination, scope-gating, workspace-consistency); live items (create/upsert, RLS row denial, cross-workspace, migration run, e2e) deferred to a PG host. SSRF validator + URL-pattern algorithm are the highest-value unit-tested logic (source: no-docker-daemon constraint).

## clarify

Run doc-first INLINE (context conservation). No questions to user. Doc-resolved clarifications in spec.md `## Clarifications` (Session 2026-07-03): save-time SSRF core = string/IP-literal/userinfo/internal-hostname checks (DNS resolution best-effort, authoritative at fetch-time SPEC-07); internal-hostname deny-list plan-level; URL-pattern id-thresholds plan-level (versioned); match unique key + unlimited matches; composite workspace-local FKs + soft/absent refs; bulk unsafe-URL reject-and-report; health defaults; version+backfill-deferred; live items deferred. Requirements checklist re-validated 16/16.

## plan (Opus subagent)

- [plan] Q: Match/health status enum sets? → A: §22 is authoritative over the specify-step shorthand — MatchStatus(ACTIVE/PAUSED/FAILED/ARCHIVED), HealthStatus(HEALTHY/DEGRADED/FAILING/UNKNOWN); CompetitorStatus(ACTIVE/ARCHIVED) for the archive-by-status deletion. (Reconciles the "active/archived only" note in the specify decisions, which was a simplification.) (source: §22).
- [plan] Q: Save-time DNS resolution? → A: No — save-time validator is pure string/parse + IP-literal deny-range + userinfo + internal-hostname deny-list only; authoritative DNS re-resolution deferred to SPEC-07 spider (research D2) (source: §11).
- [plan] Q: Constraint naming under Postgres 63-byte cap? → A: explicit short `cpm`-prefixed constraint names on competitor_product_matches (mirrors product_group_items precedent) (source: plan-level).
- [plan] Q: Reuse vs new code? → A: reuse WorkspaceScopedBase, emit_rls_policy, enum_column, WORKSPACE_OWNED_MODELS/scoped_select/scoped_get, SPEC-04 keyset pagination, catalog.upsert.dedup_last_wins, catalog.consistency.assert_refs_in_workspace, existing Scope.COMPETITORS_*/MATCHES_*, AST CI guard. New pure app_shared modules: url_safety.py, url_pattern.py (URL_PATTERN_ALGORITHM_VERSION=1), matches/upsert.py.
- [plan] Migration down_revision = c2987b29555e (verified current head). Artifacts: plan.md, research.md (9 decisions), data-model.md, quickstart.md, contracts/ (8 files).

## analyze (inline)

- [analyze] Report: 0 CRITICAL, 0 HIGH; 100% FR/SC coverage (25/25), 0 duplication, constitution PASS.
- [analyze] I1 (MEDIUM, applied): FR-010 reworded to split the **normalized URL** rules (keep scheme+query — it is the identity/unique-key value; lowercase, strip www/default-port/fragment/trailing-slash) from the **pattern** rules (drop scheme+query, :id/* generalization, preserve locale). Original text bundled pattern-only ops under "normalization rules" for both, risking a broken unique key. plan.md/data-model.md/T016 already encoded the correct behavior; this is a spec-text clarification only.
- [analyze] A1 (LOW, no change): id-like thresholds deliberately plan-level, pinned in T016 (len≥8 mixed-alnum; len≥4 & digit-ratio≥0.5).
- [analyze] C1 (LOW, no change): metadata literal 169.254.169.254 listed separately though inside 169.254/16 — intentional defense-in-depth call-out; T014 covers both.
