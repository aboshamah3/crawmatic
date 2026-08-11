-- seed_domain_playbooks.sql — one-shot production seed of the global
-- domain playbook (2026-08-11 proxy-cost Fix 4, PLAN_PROXY_COST_REDUCTION.md).
--
-- Run by hand against the production DB AFTER the b7d02a41c9e3 migration
-- (the seed_proxy.sh pattern):
--
--   cd /srv/crawmatic/railway-new && cat crawmatic/scripts/seed_domain_playbooks.sql | \
--     railway ssh --service postgres -- env -u PGHOST -u PGPORT psql -U postgres -d railway
--
-- Idempotent: profile copy is DO NOTHING on the global-name index; playbook
-- rows upsert on domain, so re-running refreshes methods/notes in place.
--
-- Sources for each method: the production `domain_strategy_profiles`
-- learned values (2026-08-11 snapshot), the measured Aug-10 run
-- (amazon/noon/stech are the only proxied domains), COMPETITOR_SCRAPE_PROFILES.md,
-- and the stech rate-limit rule (proxy-first solved 96/96).

BEGIN;

-- 1. Global copy of the amazon extraction profile, named by domain, so
--    every workspace's amazon competitor can share it. The source row is
--    the (single) workspace-scoped `amazon-sa-css`.
INSERT INTO scrape_profiles (
    id, workspace_id, name, mode, adapter_key, jsonld_enabled,
    platform_patterns_enabled, embedded_json_enabled, price_selector,
    price_xpath, price_regex, old_price_selector, old_price_xpath,
    old_price_regex, currency_selector, currency_xpath, currency_regex,
    stock_selector, stock_xpath, stock_regex, title_selector, title_xpath,
    variant_strategy, variant_selector_config, price_transform_rules,
    validation_rules, confidence_rules, wait_for_selector,
    request_timeout_ms, browser_timeout_ms, headers, cookies,
    created_at, updated_at
)
SELECT
    gen_random_uuid(), NULL, 'amazon.sa', mode, adapter_key, jsonld_enabled,
    platform_patterns_enabled, embedded_json_enabled, price_selector,
    price_xpath, price_regex, old_price_selector, old_price_xpath,
    old_price_regex, currency_selector, currency_xpath, currency_regex,
    stock_selector, stock_xpath, stock_regex, title_selector, title_xpath,
    variant_strategy, variant_selector_config, price_transform_rules,
    validation_rules, confidence_rules, wait_for_selector,
    request_timeout_ms, browser_timeout_ms, headers, cookies,
    now(), now()
FROM scrape_profiles
WHERE name = 'amazon-sa-css' AND workspace_id IS NOT NULL
ON CONFLICT (name) WHERE workspace_id IS NULL DO NOTHING;

-- 2. The playbook itself. PROXY_HTTP: amazon/noon (TLS-fingerprint
--    blocked, need the residential proxy), stech (rate-limits direct;
--    proxy-first took it 0->100%). DIRECT_HTTP_RETRY where the learned
--    profiles landed there; DIRECT_HTTP everywhere else.
INSERT INTO domain_playbooks
    (id, domain, preferred_access_method, scrape_profile_name, access_policy_name, notes, created_at, updated_at)
VALUES
    (gen_random_uuid(), 'amazon.sa',        'PROXY_HTTP',        'amazon.sa', NULL, 'TLS-fingerprint blocks direct; server-HTML price, no browser needed (240KB/page)', now(), now()),
    (gen_random_uuid(), 'noon.com',         'PROXY_HTTP',        NULL, NULL, 'Tarpits the direct client (accepts tunnel, never answers); proxy required', now(), now()),
    (gen_random_uuid(), 'stech.ink',        'PROXY_HTTP',        NULL, NULL, 'Rate-limits direct fetches; proxy-first policy solved 96/96 (2026-08-03)', now(), now()),
    (gen_random_uuid(), 'jarir.com',        'DIRECT_HTTP_RETRY', NULL, NULL, 'Direct works; times out via residential proxy — never route proxied', now(), now()),
    (gen_random_uuid(), 'pcpalace.com.sa',  'DIRECT_HTTP_RETRY', NULL, NULL, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'rawand.com.sa',    'DIRECT_HTTP_RETRY', NULL, NULL, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'afaqalhasoob.com', 'DIRECT_HTTP',       NULL, NULL, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'ahbarhd.com',      'DIRECT_HTTP',       NULL, NULL, 'Open storefront', now(), now()),
    (gen_random_uuid(), 'alshamel.sa',      'DIRECT_HTTP',       NULL, NULL, 'Open storefront', now(), now()),
    (gen_random_uuid(), 'amwajest.com',     'DIRECT_HTTP',       NULL, NULL, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'extra.com',        'DIRECT_HTTP',       NULL, NULL, 'Learned 2026-08 (ACTIVE); product pages fine direct (search API is Algolia)', now(), now()),
    (gen_random_uuid(), 'fqtoners.com',     'DIRECT_HTTP',       NULL, NULL, 'Learned 2026-08 (ACTIVE); never route proxied', now(), now()),
    (gen_random_uuid(), 'rowadalahbar.com', 'DIRECT_HTTP',       NULL, NULL, 'Open storefront', now(), now())
ON CONFLICT (domain) DO UPDATE SET
    preferred_access_method = EXCLUDED.preferred_access_method,
    scrape_profile_name = EXCLUDED.scrape_profile_name,
    access_policy_name = EXCLUDED.access_policy_name,
    notes = EXCLUDED.notes,
    updated_at = now();

COMMIT;

-- Verify:
--   SELECT domain, preferred_access_method, scrape_profile_name FROM domain_playbooks ORDER BY domain;
--   SELECT name, workspace_id FROM scrape_profiles WHERE workspace_id IS NULL;
