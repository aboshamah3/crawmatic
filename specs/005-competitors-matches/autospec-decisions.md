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
