# amazon.sa labeled fixtures — governance (Task B5, READY-004 part 1)

Captured 2026-08-25, EPA run `prod-readiness-2026-08-25`, Task B5, as **direct**
fetches (no proxy, no proxy credentials) from this host, logged to
`/srv/crawmatic/evidence/b5-request-log-2026-08-25.csv` (mode 0600). Transport:
`curl_cffi` with `impersonate="chrome131"` — the same Chrome-TLS-fingerprint
handler `scrape_core.impersonate.ImpersonatingDownloadHandler` uses in
production for amazon.sa/noon.com (see that module's docstring: bare
Scrapy/httpx-style clients get a ~5.3KB CAPTCHA interstitial at HTTP 200;
`curl_cffi impersonate=chrome131` is what gets a real page). robots.txt was
checked first (`/dp/...` is not disallowed); rate limit enforced client-side
at ≤2 req/domain/s (0.6s spacing).

## Primary source note

The preserved canary evidence (`/srv/crawmatic/evidence/canary-2026-08-24/`)
covers 30 amazon.sa targets but **contains no raw page bytes** —
`request_attempts.csv`/`price_observations.csv` store only structured
extraction results (status code, price, currency, extraction_method, ...),
never response bodies. There was nothing to build an offline-extraction
fixture from in that bundle, so this set is a **fresh capture**, exactly as
Task B5's instructions anticipate for that case.

## Certified set (5 fixtures, top-level `<ASIN>/` directories)

Every one of the 30 canary ASINs was attempted at least once this session
(30 requests); direct httpx (no TLS impersonation) got through on only
5/30 (~17%, all in the ar-ae/Arabic locale — see the diagnostic set below);
retrying 6 of the blocked ones with `curl_cffi impersonate=chrome131`
recovered 6/6. Of the resulting real captures, these 5 were re-fetched with
the `/-/en/` URL prefix (see "Locale finding" below) and used for
certification — the rest were not re-fetched in English locale because the
50-request budget was exhausted at that point (30 dp attempts + 8 noon
attempts + 6 cffi-retry + 1 locale-hypothesis-test + 4 locale-forced-fixture
+ 1 initial robots.txt = 50, see the request log).

| ASIN | Outcome | Price | Currency |
|---|---|---|---|
| B0H1MXM57R | in stock | 2399.00 | SAR |
| B0DMTMK2Q5 | **currently unavailable** (genuine, verified) | — | — |
| B00FPDRB2W | **currently unavailable** (genuine, verified) | — | — |
| B001E90EPM | **currently unavailable** (genuine, verified) | — | — |
| B0DFZZHK6X | **currently unavailable** (genuine, verified) | — | — |

Each `product.html` is trimmed to exactly 3 containers
(`#productTitle`, `#corePrice_feature_div`, `#availability`), byte-for-byte
verbatim for what's kept; `expected.json` records the untrimmed page's
sha256/byte count, the trimmed file's own sha256, the source URL, and the
capture timestamp.

**Correction recorded honestly:** an earlier hand-label for B0DMTMK2Q5/
B00FPDRB2W (462.99 / 643.46 SAR) was WRONG — those `.a-offscreen` matches
sit inside a `cerberusSharedCards-widgetTypeOos` recommendation carousel
(an alternate product, shown because the target item itself has no buybox),
not the target product's own price. Caught by inspecting the DOM ancestor
chain before finalizing, and by cross-checking `#availability`'s real text
("Currently unavailable. We don't know when or if this item will be back
in stock."). Corrected before this fixture set was finalized — see the
comment in `_locale_diagnostic`-adjacent build script history for detail
(not committed, EPA session artifact).

**n=1 caveat, stated plainly:** only one fixture (B0H1MXM57R) exercises a
genuine positive price+currency+identity match end-to-end; the other four
correctly exercise the "no price, page says unavailable" path. A single
positive sample does not, by itself, prove CSS extraction robust across
amazon.sa's product-page variability — see the diagnostic set below for
2 more real positive-price captures (price side only; currency is the
known-broken half in that set).

## Locale finding (drives the profile-definition fix)

This host's IP resolves amazon.sa to `lang="ar-ae"` (Arabic, UAE) by
default. On that render, `.a-price-symbol`'s text is the Arabic word
"ريال" (Riyal), not the ISO code "SAR" the rest of the system expects
(canary `price_observations.currency = 'SAR'` on every successful
amazon.sa row). Forcing the `/-/en/` URL path segment
(`https://www.amazon.sa/-/en/dp/<ASIN>`) — verified on the SAME ASIN,
B0H1MXM57R, both ways — renders `lang="en-ae"` and `.a-price-symbol` reads
"SAR" cleanly. **No "ريال"→"SAR" mapping exists anywhere in this codebase**
(checked `app_shared.money`, `scrape_core.money_text` — both normalize
price digits only, never currency symbols/words). See
`scripts/seed_domain_playbooks.sql` for the resulting profile-definition
change and its evidence citation.

## Diagnostic set (`_locale_diagnostic/`, NOT part of the certified pass rate)

Two more real, in-stock, positive-price captures (B06XT3BC4Y, 1115.94
"ريال"; B0FSGJZDNT, 6399.00 "ريال") taken WITHOUT the `/-/en/` prefix —
i.e., this host's default locale. Price extraction is correct in both;
currency extraction reproduces the locale finding above. These back
`test_amazon_locale_currency_known_limitation` in
`test_domain_certification.py`, marked `xfail(strict=True)`, and are
excluded from the certified-method pass-rate/xfail-zero gate by design
(see that test module's `CERTIFIED_AMAZON_ASINS` allowlist and the
`assert_certified_methods_have_no_xfail` helper).

## Refresh policy

**Max fixture age: 14 days** before recapture (amazon.sa product
availability/price/DOM structure churns; a fixture older than this is not
trustworthy evidence for re-certification). Recapture must go through the
same request-log + budget discipline as this capture. If a recapture shows
`#corePrice_feature_div`/`#productTitle`/`#availability` selectors no
longer match on ≥2 fixtures, treat that as a structural regression, not a
stale-fixture problem, and re-diagnose before updating selectors again.

## No PII / no session tokens

Every fixture is a public product-detail page trimmed to price/title/
availability markup. No cookies, no session identifiers, no account state
of any kind were captured or stored (`curl_cffi` fetches were made with no
prior session — each is a cold, unauthenticated request).
