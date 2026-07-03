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
