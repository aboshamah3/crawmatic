-- seed_domain_playbooks.sql — one-shot production seed of the global
-- domain playbook (2026-08-11 proxy-cost Fix 4, PLAN_PROXY_COST_REDUCTION.md).
--
-- Run by hand against the production DB AFTER the 6e4a9c8f2d10 migration
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
    id, workspace_id, name, mode, adapter_key, version, adapter_config, jsonld_enabled,
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
    gen_random_uuid(), NULL, 'amazon.sa', mode, adapter_key, 1, adapter_config, jsonld_enabled,
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
ON CONFLICT (name) WHERE workspace_id IS NULL DO UPDATE SET
    mode = EXCLUDED.mode,
    adapter_key = EXCLUDED.adapter_key,
    adapter_config = EXCLUDED.adapter_config,
    price_selector = EXCLUDED.price_selector,
    price_xpath = EXCLUDED.price_xpath,
    price_regex = EXCLUDED.price_regex,
    old_price_selector = EXCLUDED.old_price_selector,
    old_price_xpath = EXCLUDED.old_price_xpath,
    old_price_regex = EXCLUDED.old_price_regex,
    currency_selector = EXCLUDED.currency_selector,
    currency_xpath = EXCLUDED.currency_xpath,
    currency_regex = EXCLUDED.currency_regex,
    stock_selector = EXCLUDED.stock_selector,
    stock_xpath = EXCLUDED.stock_xpath,
    stock_regex = EXCLUDED.stock_regex,
    title_selector = EXCLUDED.title_selector,
    title_xpath = EXCLUDED.title_xpath,
    variant_selector_config = EXCLUDED.variant_selector_config,
    price_transform_rules = EXCLUDED.price_transform_rules,
    validation_rules = EXCLUDED.validation_rules,
    confidence_rules = EXCLUDED.confidence_rules,
    jsonld_enabled = EXCLUDED.jsonld_enabled,
    platform_patterns_enabled = EXCLUDED.platform_patterns_enabled,
    embedded_json_enabled = EXCLUDED.embedded_json_enabled,
    variant_strategy = EXCLUDED.variant_strategy,
    request_timeout_ms = EXCLUDED.request_timeout_ms,
    browser_timeout_ms = EXCLUDED.browser_timeout_ms,
    version = scrape_profiles.version + 1,
    updated_at = now()
WHERE ROW(
    scrape_profiles.mode, scrape_profiles.adapter_key, scrape_profiles.adapter_config,
    scrape_profiles.price_selector, scrape_profiles.price_xpath, scrape_profiles.price_regex,
    scrape_profiles.old_price_selector, scrape_profiles.old_price_xpath, scrape_profiles.old_price_regex,
    scrape_profiles.currency_selector, scrape_profiles.currency_xpath, scrape_profiles.currency_regex,
    scrape_profiles.stock_selector, scrape_profiles.stock_xpath, scrape_profiles.stock_regex,
    scrape_profiles.title_selector, scrape_profiles.title_xpath,
    scrape_profiles.variant_selector_config, scrape_profiles.price_transform_rules,
    scrape_profiles.validation_rules, scrape_profiles.confidence_rules,
    scrape_profiles.jsonld_enabled, scrape_profiles.platform_patterns_enabled,
    scrape_profiles.embedded_json_enabled, scrape_profiles.variant_strategy,
    scrape_profiles.request_timeout_ms, scrape_profiles.browser_timeout_ms
) IS DISTINCT FROM ROW(
    EXCLUDED.mode, EXCLUDED.adapter_key, EXCLUDED.adapter_config,
    EXCLUDED.price_selector, EXCLUDED.price_xpath, EXCLUDED.price_regex,
    EXCLUDED.old_price_selector, EXCLUDED.old_price_xpath, EXCLUDED.old_price_regex,
    EXCLUDED.currency_selector, EXCLUDED.currency_xpath, EXCLUDED.currency_regex,
    EXCLUDED.stock_selector, EXCLUDED.stock_xpath, EXCLUDED.stock_regex,
    EXCLUDED.title_selector, EXCLUDED.title_xpath,
    EXCLUDED.variant_selector_config, EXCLUDED.price_transform_rules,
    EXCLUDED.validation_rules, EXCLUDED.confidence_rules,
    EXCLUDED.jsonld_enabled, EXCLUDED.platform_patterns_enabled,
    EXCLUDED.embedded_json_enabled, EXCLUDED.variant_strategy,
    EXCLUDED.request_timeout_ms, EXCLUDED.browser_timeout_ms
);

-- Reusable adapter profiles.  These are global configuration, not customer
-- logic: any workspace may reuse them or create its own version through the
-- profile API.  Endpoint templates and exact predicates remain data-driven.
INSERT INTO scrape_profiles (
    id, workspace_id, name, mode, adapter_key, version, adapter_config,
    jsonld_enabled, platform_patterns_enabled, embedded_json_enabled,
    variant_strategy, request_timeout_ms, browser_timeout_ms, created_at, updated_at
)
VALUES
    (gen_random_uuid(), NULL, 'resilience.amazon.rendered.v1', 'BROWSER',
     'playwright_rendered', 1,
     '{"identity_rules":{"url_id_patterns":["/(?:dp|gp/product)/(?P<id>[A-Z0-9]{10})(?:[/?#]|$)"],"require_url_identifier":true,"locale_root_patterns":["/(?:[a-z]{2}(?:-[a-z]{2})?)/?"]}}'::jsonb,
     true, true, true, 'PAGE_SINGLE_PRICE', 30000, 45000, now(), now()),
    (gen_random_uuid(), NULL, 'resilience.noon.catalog-json.v1', 'HTTP',
     'public_catalog_json', 1,
     '{"endpoint_template":"https://www.noon.com/_svc/catalog/api/v3/u/search/?q={identifier_urlencoded}&page=1","identifier_sources":["competitor_variant_identifier","competitor_variant_sku"],"exact_id_paths":["sku","sku_code","uniqueId","id"],"fields":{"sale_price":["sale_price","salePrice"],"price":["price","sellingPrice"],"stock":["is_buyable","isBuyable"],"currency":["currency","currencyCode"],"title":["name","title"],"url":["url","productUrl"]},"currency":"SAR","follow_redirects":true}'::jsonb,
     false, false, false, 'PAGE_SINGLE_PRICE', 30000, NULL, now(), now()),
    (gen_random_uuid(), NULL, 'resilience.shopify.product-json.v1', 'HTTP',
     'shopify_product_json', 1,
     '{"variant_identity_paths":["id","sku"],"price_transform":{"divide_by":100},"currency":"SAR","follow_redirects":true}'::jsonb,
     false, false, false, 'PAGE_SINGLE_PRICE', 30000, NULL, now(), now()),
    (gen_random_uuid(), NULL, 'resilience.html.root-identity-guard.v1', 'HTTP',
     'default_http', 1,
     '{"follow_redirects":false,"identity_rules":{"root_paths":["/"],"locale_root_patterns":["/(?:[a-z]{2}(?:-[a-z]{2})?)/?"]}}'::jsonb,
     true, true, true, 'PAGE_SINGLE_PRICE', 30000, NULL, now(), now()),
    (gen_random_uuid(), NULL, 'resilience.jarir.exact-id-repair.v1', 'BROWSER',
     'exact_id_url_repair', 1,
     '{"repair_endpoint_template":"https://www.jarir.com/sa-en/catalogsearch/result/?q={identifier_urlencoded}","identifier_sources":["competitor_variant_identifier","competitor_variant_sku"],"response_format":"html","link_selector":"a[href*=''.html'']::attr(href)","identity_rules":{"url_id_patterns":["[-/](?P<id>[0-9]{5,})(?:\\.html)?(?:[/?#]|$)"],"require_url_identifier":true}}'::jsonb,
     false, false, false, 'PAGE_SINGLE_PRICE', 30000, 45000, now(), now()),
    (gen_random_uuid(), NULL, 'resilience.afaq.exact-id-repair.v1', 'HTTP',
     'exact_id_url_repair', 1,
     '{"repair_endpoint_template":"https://afaqalhasoob.com/?s={identifier_urlencoded}","identifier_sources":["competitor_variant_identifier","competitor_variant_sku"],"response_format":"html","link_selector":"a[href]::attr(href)","identity_rules":{"url_id_patterns":["[-/](?P<id>[A-Z0-9_-]{4,})(?:\\.html)?(?:[/?#]|$)"],"require_url_identifier":true}}'::jsonb,
     false, false, false, 'PAGE_SINGLE_PRICE', 30000, NULL, now(), now()),
    -- Public eXtra search configuration is visible in the storefront page and
    -- deliberately versioned here because the site/API keys can rotate.
    (gen_random_uuid(), NULL, 'resilience.extra.unbxd-exact.v1', 'HTTP',
     'public_catalog_json', 1,
     '{"endpoint_template":"https://search.unbxd.io/8fb45132f31d81ab46966cc135c24430/ss-unbxd-auk-extra-saudi-ar-prod11541714990564/search?q={identifier_urlencoded}&rows=24","identifier_sources":["competitor_variant_identifier","competitor_variant_sku"],"exact_id_paths":["uniqueId"],"fields":{"sale_price":["salePrice","sellingPrice"],"price":["wasPrice","price"],"stock":["available","inStockFlag"],"currency":["currency","currencyCode"],"url":["productUrl","url"]},"stock_mapping":{"true":"IN_STOCK","false":"OUT_OF_STOCK"},"currency":"SAR"}'::jsonb,
     false, false, false, 'PAGE_SINGLE_PRICE', 30000, NULL, now(), now())
ON CONFLICT (name) WHERE workspace_id IS NULL DO UPDATE SET
    mode = EXCLUDED.mode,
    adapter_key = EXCLUDED.adapter_key,
    adapter_config = EXCLUDED.adapter_config,
    jsonld_enabled = EXCLUDED.jsonld_enabled,
    platform_patterns_enabled = EXCLUDED.platform_patterns_enabled,
    embedded_json_enabled = EXCLUDED.embedded_json_enabled,
    variant_strategy = EXCLUDED.variant_strategy,
    request_timeout_ms = EXCLUDED.request_timeout_ms,
    browser_timeout_ms = EXCLUDED.browser_timeout_ms,
    version = scrape_profiles.version + 1,
    updated_at = now()
WHERE ROW(
    scrape_profiles.mode, scrape_profiles.adapter_key, scrape_profiles.adapter_config,
    scrape_profiles.jsonld_enabled, scrape_profiles.platform_patterns_enabled,
    scrape_profiles.embedded_json_enabled, scrape_profiles.variant_strategy,
    scrape_profiles.request_timeout_ms, scrape_profiles.browser_timeout_ms
) IS DISTINCT FROM ROW(
    EXCLUDED.mode, EXCLUDED.adapter_key, EXCLUDED.adapter_config,
    EXCLUDED.jsonld_enabled, EXCLUDED.platform_patterns_enabled,
    EXCLUDED.embedded_json_enabled, EXCLUDED.variant_strategy,
    EXCLUDED.request_timeout_ms, EXCLUDED.browser_timeout_ms
);

-- Snapshot the reusable profile version before the optional Amazon selector
-- clone below; if that clone changes it, the second snapshot insert records
-- the resulting next version as well.
INSERT INTO scrape_profile_revisions (
    id, workspace_id, scrape_profile_id, version, snapshot, created_at, updated_at
)
SELECT gen_random_uuid(), sp.workspace_id, sp.id, sp.version,
       to_jsonb(sp) - 'id' - 'workspace_id' - 'version' - 'created_at' - 'updated_at',
       now(), now()
FROM scrape_profiles sp
WHERE sp.workspace_id IS NULL AND sp.name LIKE 'resilience.%'
ON CONFLICT (scrape_profile_id, version) DO NOTHING;

-- Rendered Amazon keeps every proven selector/regex from the existing global
-- profile while changing only its transport adapter and identity policy.
UPDATE scrape_profiles rendered
SET jsonld_enabled = source.jsonld_enabled,
    platform_patterns_enabled = source.platform_patterns_enabled,
    embedded_json_enabled = source.embedded_json_enabled,
    price_selector = source.price_selector,
    price_xpath = source.price_xpath,
    price_regex = source.price_regex,
    old_price_selector = source.old_price_selector,
    old_price_xpath = source.old_price_xpath,
    old_price_regex = source.old_price_regex,
    currency_selector = source.currency_selector,
    currency_xpath = source.currency_xpath,
    currency_regex = source.currency_regex,
    stock_selector = source.stock_selector,
    stock_xpath = source.stock_xpath,
    stock_regex = source.stock_regex,
    title_selector = source.title_selector,
    title_xpath = source.title_xpath,
    validation_rules = source.validation_rules,
    confidence_rules = source.confidence_rules,
    version = rendered.version + 1,
    updated_at = now()
FROM scrape_profiles source
WHERE rendered.workspace_id IS NULL
  AND rendered.name = 'resilience.amazon.rendered.v1'
  AND source.workspace_id IS NULL
  AND source.name = 'amazon.sa'
  AND jsonb_build_array(
      rendered.jsonld_enabled, rendered.platform_patterns_enabled,
      rendered.embedded_json_enabled, rendered.price_selector,
      rendered.price_xpath, rendered.price_regex, rendered.old_price_selector,
      rendered.old_price_xpath, rendered.old_price_regex,
      rendered.currency_selector, rendered.currency_xpath, rendered.currency_regex,
      rendered.stock_selector, rendered.stock_xpath, rendered.stock_regex,
      rendered.title_selector, rendered.title_xpath,
      rendered.validation_rules, rendered.confidence_rules
  ) IS DISTINCT FROM jsonb_build_array(
      source.jsonld_enabled, source.platform_patterns_enabled,
      source.embedded_json_enabled, source.price_selector,
      source.price_xpath, source.price_regex, source.old_price_selector,
      source.old_price_xpath, source.old_price_regex,
      source.currency_selector, source.currency_xpath, source.currency_regex,
      source.stock_selector, source.stock_xpath, source.stock_regex,
      source.title_selector, source.title_xpath,
      source.validation_rules, source.confidence_rules
  );

INSERT INTO scrape_profile_revisions (
    id, workspace_id, scrape_profile_id, version, snapshot, created_at, updated_at
)
SELECT gen_random_uuid(), sp.workspace_id, sp.id, sp.version,
       to_jsonb(sp) - 'id' - 'workspace_id' - 'version' - 'created_at' - 'updated_at',
       now(), now()
FROM scrape_profiles sp
WHERE sp.workspace_id IS NULL
  AND (sp.name LIKE 'resilience.%' OR sp.name = 'amazon.sa')
ON CONFLICT (scrape_profile_id, version) DO NOTHING;

-- 2. The playbook itself. PROXY_HTTP: amazon/noon (TLS-fingerprint
--    blocked, need the residential proxy), stech (rate-limits direct;
--    proxy-first took it 0->100%). DIRECT_HTTP_RETRY where the learned
--    profiles landed there; DIRECT_HTTP everywhere else.
--
-- 2026-08-25 EVIDENCE (Task B5, READY-004 part 1 -- amazon.sa/noon.com
-- re-certification; full trace in tests/fixtures/{amazon,noon}_labeled/
-- FIXTURES.md and /srv/crawmatic/evidence/b5-request-log-2026-08-25.csv,
-- 50 logged direct requests, no proxy):
--
-- * amazon.sa DIAGNOSED (not yet applied here -- no live DB access from
--   the EPA sandbox to verify/update the 'amazon.sa' scrape_profiles row
--   safely; this is a finding for the release owner to apply): this
--   host's amazon.sa requests resolve to lang="ar-ae" (Arabic/UAE) by
--   default. On that render, the CSS currency node (`.a-price-symbol`)
--   reads the Arabic word "ريال" (Riyal), not the ISO code "SAR" the
--   rest of the system expects -- and no "ريال"->"SAR" mapping exists
--   anywhere in this codebase (checked app_shared.money and
--   scrape_core.money_text; both normalize price digits only). Forcing
--   the "/-/en/" URL path segment on the SAME product (verified on ASIN
--   B0H1MXM57R both ways) renders lang="en-ae" and `.a-price-symbol`
--   reads "SAR" cleanly; price extraction itself (`.a-price
--   .a-offscreen` -> `#corePrice_feature_div`) is correct in both
--   locales via scrape_core.money_text.normalize_price_text. RECOMMENDED
--   FIX: force "/-/en/" into every amazon.sa target URL at dispatch time
--   (a targets/URL-construction change, out of scope for the
--   scrape_profiles row this script touches) OR add a currency-symbol
--   normalization table if the URL can't be forced. Until one of those
--   lands, CSS-path currency on amazon.sa is locale-dependent and
--   unreliable outside the "/-/en/" URL form.
-- * noon.com CONFIRMED CORRECT, no change: 8 direct requests to
--   noon.com this session (2 bare-curl robots.txt, 1 bare-curl catalog
--   API, 5 curl_cffi impersonate="chrome131" catalog API -- the same
--   Chrome-TLS-fingerprint transport that recovers amazon.sa reliably)
--   were ALL rejected at the TLS/HTTP2 layer in 54-140ms (mean ~102ms;
--   "HTTP/2 stream reset by server", curl error 92) -- a fast reset, not
--   a slow timeout, and not fixed by fingerprint impersonation (points
--   to an IP-reputation/network-layer block). Cross-referencing canary
--   job 101bb01c-08eb-4c66-9883-50dea7d315ce: all 34 of its noon.com
--   request_attempts already used PROXY_HTTP (direct was never
--   attempted for Noon in production, matching this playbook's existing
--   priority-0 PROXY_HTTP-only design with DIRECT_HTTP/PLAYWRIGHT_DIRECT
--   correctly left QUARANTINED as canaries). The 2 observed TIMEOUT
--   rows in that job hit exactly the configured 30000ms
--   request_timeout_ms on the PROXY_HTTP hop itself (30307-30308ms) --
--   a proxy-side stall, not a Noon-direct-connectivity problem. No
--   profile change follows from this evidence; the existing
--   PROXY_HTTP-first choice for noon.com is confirmed correct.
INSERT INTO domain_playbooks
    (id, domain, preferred_access_method, scrape_profile_name, access_policy_name,
     method_templates, notes, created_at, updated_at)
VALUES
    (gen_random_uuid(), 'amazon.sa', 'PROXY_HTTP', 'amazon.sa', NULL,
     '[{"priority":0,"access_method":"PROXY_HTTP","scrape_profile_name":"amazon.sa","proof_state":"PROVEN","fallback_on":["PRICE_NOT_FOUND","HTTP_403","BLOCKED","PROTOCOL_FAILED","TLS_CONNECTION_FAILED"]},{"priority":1,"access_method":"PLAYWRIGHT_DIRECT","scrape_profile_name":"resilience.amazon.rendered.v1","proof_state":"PROVEN","enter_on":["PRICE_NOT_FOUND","HTTP_403","BLOCKED","PROTOCOL_FAILED","TLS_CONNECTION_FAILED"],"fallback_on":["HTTP_403","BLOCKED","PRICE_NOT_FOUND","PLAYWRIGHT_FAILED"]},{"priority":2,"access_method":"PLAYWRIGHT_PROXY","scrape_profile_name":"resilience.amazon.rendered.v1","proof_state":"CANDIDATE","enter_on":["HTTP_403","BLOCKED","PRICE_NOT_FOUND","PLAYWRIGHT_FAILED"]}]'::jsonb,
     '2026-08-25 (B5): CSS currency is locale-dependent -- "SAR" only via the "/-/en/" URL form, else the Arabic symbol "ريال" with no normalization in this codebase; see the evidence block above this INSERT. Raw candidates retained; rendered direct then proxy on configured outcomes', now(), now()),
    (gen_random_uuid(), 'noon.com', 'PROXY_HTTP', NULL, NULL,
     '[{"priority":0,"access_method":"PROXY_HTTP","scrape_profile_name":"resilience.noon.catalog-json.v1","extraction_method":"PLATFORM_JSON","proof_state":"PROVEN","proof_sample_size":40,"fallback_on":[]},{"priority":1,"access_method":"PROXY_HTTP","proof_state":"QUARANTINED","canary_after_seconds":600,"enter_on":["HTTP_403","TIMEOUT","PROTOCOL_FAILED"],"fallback_on":["HTTP_403","TIMEOUT","PROTOCOL_FAILED"]},{"priority":2,"access_method":"PLAYWRIGHT_DIRECT","proof_state":"QUARANTINED","canary_after_seconds":600,"enter_on":["HTTP_403","TIMEOUT","PROTOCOL_FAILED"],"fallback_on":["PROTOCOL_FAILED","PLAYWRIGHT_FAILED"]},{"priority":3,"access_method":"PLAYWRIGHT_PROXY","proof_state":"QUARANTINED","canary_after_seconds":600,"enter_on":["PROTOCOL_FAILED","PLAYWRIGHT_FAILED"]}]'::jsonb,
     '2026-08-25 (B5): direct access reconfirmed fully blocked (8/8 requests, TLS-impersonated included, all fast HTTP/2 resets, not timeouts) -- PROXY_HTTP-first is evidence-correct, no change. Exact-SKU catalog primary; product-page transports retained as canaries', now(), now()),
    (gen_random_uuid(), 'stech.ink', 'DIRECT_HTTP', NULL, NULL,
     '[{"priority":0,"access_method":"DIRECT_HTTP","scrape_profile_name":"resilience.shopify.product-json.v1","extraction_method":"PLATFORM_JSON","proof_state":"PROVEN","proof_sample_size":15,"fallback_on":["HTTP_403","TIMEOUT","CONNECTION_FAILED","PROXY_FAILED"]},{"priority":1,"access_method":"DIRECT_HTTP_RETRY","proof_state":"CANDIDATE","enter_on":["HTTP_403","TIMEOUT","CONNECTION_FAILED","PROXY_FAILED"],"fallback_on":["HTTP_403","TIMEOUT","CONNECTION_FAILED"]},{"priority":2,"access_method":"PROXY_HTTP","proof_state":"CANDIDATE","enter_on":["HTTP_403","TIMEOUT","CONNECTION_FAILED"]}]'::jsonb,
     'Direct Shopify JSON primary; HTML methods remain available', now(), now()),
    (gen_random_uuid(), 'afaqalhasoob.com', 'DIRECT_HTTP_RETRY', NULL, NULL,
     '[{"priority":0,"access_method":"DIRECT_HTTP_RETRY","proof_state":"PROVEN","fallback_on":["TIMEOUT","CONNECTION_FAILED","HTTP_404"]},{"priority":1,"access_method":"DIRECT_HTTP","scrape_profile_name":"resilience.afaq.exact-id-repair.v1","proof_state":"CANDIDATE","enter_on":["HTTP_404"]},{"priority":2,"access_method":"PLAYWRIGHT_DIRECT","proof_state":"CANDIDATE","enter_on":["TIMEOUT","CONNECTION_FAILED"]}]'::jsonb,
     'Bounded direct retry; exact repair on 404; browser on repeated transport failure', now(), now()),
    (gen_random_uuid(), 'fqtoners.com', 'DIRECT_HTTP', NULL, NULL,
     '[{"priority":0,"access_method":"DIRECT_HTTP","proof_state":"PROVEN","fallback_on":["TIMEOUT","CONNECTION_FAILED"]}]'::jsonb,
     'Reject storefront-root redirects before extraction', now(), now()),
    (gen_random_uuid(), 'alshamel.sa', 'DIRECT_HTTP', NULL, NULL,
     '[{"priority":0,"access_method":"DIRECT_HTTP","proof_state":"PROVEN","fallback_on":["TIMEOUT","CONNECTION_FAILED"]}]'::jsonb,
     'Reject storefront-root redirects before extraction', now(), now()),
    (gen_random_uuid(), 'jarir.com', 'DIRECT_HTTP_RETRY', NULL, NULL,
     '[{"priority":0,"access_method":"DIRECT_HTTP_RETRY","proof_state":"PROVEN","fallback_on":["HTTP_404"]},{"priority":1,"access_method":"PLAYWRIGHT_DIRECT","scrape_profile_name":"resilience.jarir.exact-id-repair.v1","proof_state":"CANDIDATE","enter_on":["HTTP_404"],"fallback_on":["PRICE_NOT_FOUND"]},{"priority":2,"access_method":"DIRECT_HTTP_RETRY","proof_state":"CANDIDATE","enter_on":["PRICE_NOT_FOUND"]}]'::jsonb,
     'Normal product profile; rendered exact numeric-ID repair on 404', now(), now()),
    (gen_random_uuid(), 'extra.com', 'DIRECT_HTTP', NULL, NULL,
     '[{"priority":0,"access_method":"DIRECT_HTTP","proof_state":"PROVEN","fallback_on":["HTTP_404","HTTP_403","BLOCKED"]},{"priority":1,"access_method":"DIRECT_HTTP","scrape_profile_name":"resilience.extra.unbxd-exact.v1","extraction_method":"PLATFORM_JSON","proof_state":"CANDIDATE","enabled":true,"enter_on":["HTTP_404","HTTP_403","BLOCKED"]}]'::jsonb,
     'Existing product page retained; public exact-uniqueId Unbxd fallback is versioned for key rotation', now(), now()),
    (gen_random_uuid(), 'pcpalace.com.sa',  'DIRECT_HTTP_RETRY', NULL, NULL, '[]'::jsonb, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'rawand.com.sa',    'DIRECT_HTTP_RETRY', NULL, NULL, '[]'::jsonb, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'ahbarhd.com',      'DIRECT_HTTP',       NULL, NULL, '[]'::jsonb, 'Open storefront', now(), now()),
    (gen_random_uuid(), 'amwajest.com',     'DIRECT_HTTP',       NULL, NULL, '[]'::jsonb, 'Learned 2026-08 (ACTIVE)', now(), now()),
    (gen_random_uuid(), 'rowadalahbar.com', 'DIRECT_HTTP',       NULL, NULL, '[]'::jsonb, 'Open storefront', now(), now())
ON CONFLICT (domain) DO UPDATE SET
    preferred_access_method = EXCLUDED.preferred_access_method,
    scrape_profile_name = EXCLUDED.scrape_profile_name,
    access_policy_name = EXCLUDED.access_policy_name,
    method_templates = EXCLUDED.method_templates,
    notes = EXCLUDED.notes,
    updated_at = now();

-- Materialize templates for existing workspace profiles without deleting or
-- overwriting prior methods.  Historic active rows are retained at stable
-- rollback priorities; the marker version makes this block idempotent.
WITH ranked AS (
    SELECT dsm.id, 10000 + row_number() OVER (
        PARTITION BY dsm.domain_strategy_profile_id ORDER BY dsm.priority, dsm.id
    ) AS rollback_priority
    FROM domain_strategy_methods dsm
    JOIN domain_strategy_profiles dsp ON dsp.id = dsm.domain_strategy_profile_id
    JOIN domain_playbooks dp ON dp.domain = dsp.domain
    WHERE jsonb_array_length(dp.method_templates) > 0
      AND dsm.retired_at IS NULL
      AND dsm.method_version <> 20260824
      AND NOT EXISTS (
          SELECT 1 FROM domain_strategy_methods seeded
          WHERE seeded.domain_strategy_profile_id = dsm.domain_strategy_profile_id
            AND seeded.method_version = 20260824
            AND seeded.retired_at IS NULL
      )
)
UPDATE domain_strategy_methods dsm
SET priority = ranked.rollback_priority, updated_at = now()
FROM ranked WHERE ranked.id = dsm.id;

WITH expanded AS (
    SELECT dsp.id AS strategy_profile_id, dsp.workspace_id, template,
           COALESCE(NULLIF(template->>'scrape_profile_name', ''), dp.scrape_profile_name) AS profile_name
    FROM domain_strategy_profiles dsp
    JOIN domain_playbooks dp ON dp.domain = dsp.domain
    CROSS JOIN LATERAL jsonb_array_elements(dp.method_templates) template
), resolved AS (
    SELECT expanded.*, sp.id AS scrape_profile_id, sp.version AS scrape_profile_version
    FROM expanded
    LEFT JOIN scrape_profiles sp
      ON sp.workspace_id IS NULL AND sp.name = expanded.profile_name
)
INSERT INTO domain_strategy_methods (
    id, workspace_id, domain_strategy_profile_id, scrape_profile_id,
    scrape_profile_version, access_method, extraction_method, priority,
    method_version, enter_on, fallback_on, enabled, proof_state,
    cooldown_until, next_canary_at, proof_sample_size, created_at, updated_at
)
SELECT gen_random_uuid(), workspace_id, strategy_profile_id, scrape_profile_id,
       scrape_profile_version, template->>'access_method',
       NULLIF(template->>'extraction_method', ''), (template->>'priority')::integer,
       20260824, COALESCE(template->'enter_on', '[]'::jsonb),
       COALESCE(template->'fallback_on', '[]'::jsonb),
       COALESCE((template->>'enabled')::boolean, true),
       COALESCE(template->>'proof_state', 'CANDIDATE'),
       CASE WHEN COALESCE((template->>'cooldown_seconds')::integer, 0) > 0
            THEN now() + make_interval(secs => (template->>'cooldown_seconds')::integer) END,
       CASE WHEN COALESCE((template->>'canary_after_seconds')::integer, 0) > 0
            THEN now() + make_interval(secs => (template->>'canary_after_seconds')::integer) END,
       COALESCE((template->>'proof_sample_size')::integer, 0), now(), now()
FROM resolved
WHERE (profile_name IS NULL OR scrape_profile_id IS NOT NULL)
  AND NOT EXISTS (
      SELECT 1 FROM domain_strategy_methods existing
      WHERE existing.domain_strategy_profile_id = strategy_profile_id
        AND existing.method_version = 20260824
        AND existing.priority = (template->>'priority')::integer
        AND existing.retired_at IS NULL
  );

WITH selected AS (
    SELECT DISTINCT ON (dsm.domain_strategy_profile_id)
           dsm.domain_strategy_profile_id, dsm.id, dsm.access_method,
           dsm.extraction_method
    FROM domain_strategy_methods dsm
    WHERE dsm.method_version = 20260824
      AND dsm.enabled = true
      AND dsm.proof_state = 'PROVEN'
      AND dsm.retired_at IS NULL
    ORDER BY dsm.domain_strategy_profile_id, dsm.priority
)
UPDATE domain_strategy_profiles dsp
SET preferred_method_id = selected.id,
    preferred_access_method = selected.access_method,
    preferred_extraction_method = selected.extraction_method,
    status = CASE WHEN dsp.status = 'DISABLED' THEN dsp.status ELSE 'LEARNING' END,
    updated_at = now()
FROM selected
WHERE selected.domain_strategy_profile_id = dsp.id;

-- ---------------------------------------------------------------------------
-- EPA C4 (2026-09-08, F08 / plan §11 items 2 and 5): the VERSIONED DOMAIN
-- STRATEGY columns added by migration a7c31d0f9e42.
--
-- SHAPE ONLY. Every value is deliberately left NULL for the owner to fill
-- after the labeled canary has run (docs/ops/DOMAIN_STRATEGIES_2026-09.md
-- §2 and §6): `fallback_cap_per_refresh` and `recovery_probe_fraction` are
-- decisions about money, and nothing has yet measured how often the
-- expensive rung actually rescues a target on these domains. Seeding an
-- invented number would look like evidence and would not be.
--
-- The columns and what NULL means (full table in the runbook):
--   strategy_version         INT      NOT NULL DEFAULT 1 -- bump it in the
--                                     SAME statement that changes any value
--                                     below; a shape change that keeps its
--                                     old version number makes every attempt
--                                     stamped with it a lie.
--   cheap_path               TEXT     AccessMethod tried FIRST when nothing
--                                     else pins the start. NULL = no hint.
--   fallback_path            TEXT     the EXPENSIVE AccessMethod that gets
--                                     rationed. NULL = nothing is rationed.
--   fallback_cap_per_refresh INT      uses of fallback_path per target per
--                                     refresh. NULL = uncapped (pre-C4
--                                     behaviour), 0 = never.
--   recovery_probe_fraction  NUMERIC  per-domain override of
--                                     SCRAPE_RECOVERY_PROBE_FRACTION.
--                                     NULL = use the setting.
--
-- Evidence-backed SUGGESTIONS for the two path columns (see the runbook
-- table; still not applied here):
--   amazon.sa  cheap PROXY_HTTP   fallback PLAYWRIGHT_PROXY
--   noon.com   cheap PROXY_HTTP   fallback PLAYWRIGHT_PROXY   (direct is
--              100% blocked at the TLS/HTTP2 layer -- B5, 8/8)
--   stech.ink  cheap DIRECT_HTTP  fallback PROXY_HTTP
--
-- To seed, replace the NULLs below (and ONLY then bump strategy_version):
--
--   UPDATE domain_playbooks SET
--       strategy_version         = 2,
--       cheap_path               = 'PROXY_HTTP',
--       fallback_path            = 'PLAYWRIGHT_PROXY',
--       fallback_cap_per_refresh = 1,
--       recovery_probe_fraction  = 0.05,
--       updated_at               = now()
--   WHERE domain = 'noon.com';
--
-- Idempotent as written: every row keeps the values it already has,
-- because COALESCE(<new>, <current>) with a NULL <new> is the current
-- value. Re-running this file therefore never resets a seeded strategy
-- back to NULL -- which is the whole reason it is written this way rather
-- than as a plain assignment.
UPDATE domain_playbooks SET
    strategy_version         = COALESCE(NULL::integer, strategy_version),
    cheap_path               = COALESCE(NULL::text,    cheap_path),
    fallback_path            = COALESCE(NULL::text,    fallback_path),
    fallback_cap_per_refresh = COALESCE(NULL::integer, fallback_cap_per_refresh),
    recovery_probe_fraction  = COALESCE(NULL::numeric, recovery_probe_fraction)
WHERE domain IN ('amazon.sa', 'noon.com', 'stech.ink');

COMMIT;

-- Verify:
--   SELECT domain, preferred_access_method, scrape_profile_name FROM domain_playbooks ORDER BY domain;
--   SELECT name, workspace_id FROM scrape_profiles WHERE workspace_id IS NULL;
--   SELECT domain, strategy_version, cheap_path, fallback_path,
--          fallback_cap_per_refresh, recovery_probe_fraction
--     FROM domain_playbooks ORDER BY domain;
