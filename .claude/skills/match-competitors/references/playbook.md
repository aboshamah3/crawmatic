# Competitor search playbook

**Deliverable = a direct, canonical product-page URL per competitor. NO price
capture — ignore prices entirely.**

> **Not this skill's job, but useful context (2026-07-27):** the *pricing* side that
> later consumes these links is documented in `matching/COMPETITOR_SCRAPE_PROFILES.md`
> (per-site access + extraction method, live-verified). It changes nothing here — this
> playbook's noon/amazon jina technique below remains the correct approach for matching.
> Current pricing reality: 12 of 13 sites price successfully; **noon is a price gap**
> (it blocks everything except jina), which is a reason to keep collecting noon *links*
> exactly as now, not a reason to skip the site.

**Competitor roster comes from `matching/competitors.json` (13 KSA sites):**
noon.com, amazon.sa, jarir.com, extra.com, ahbarhd.com, stech.ink,
pcpalace.com.sa, fqtoners.com, rowadalahbar.com, rawand.com.sa, amwajest.com,
afaqalhasoob.com, alshamel.sa. One `matches[]` entry per site, in that order.
Budget with 13 sites: ~2 searches + 1-2 fetches per site; `site:<domain>
<identifier>` is the workhorse for the small stores — if it and one on-site
search return nothing, mark that site `no_match`/`gap` and move on.

## STEP 0 FOR EVERY BATCH — build the local sitemap indexes first (2026-08-07)

Before spawning any matcher, download the competitors' full product-URL lists once and
flatten them to one URL per line. Agents then **grep locally instead of searching**. This
is the single biggest speed and quality win found so far.

```bash
cd <scratchpad>/sitemaps
for i in 1 2 3 4; do curl -sL -A "Mozilla/5.0" \
  "https://www.jarir.com/media/sitemap/sitemap_sa_ar_product_$i.xml" -o jarir_$i.xml & done; wait
curl -sL -A "Mozilla/5.0" https://pcpalace.com.sa/sitemap_products.xml   -o pcpalace.xml
curl -sL -A "Mozilla/5.0" https://afaqalhasoob.com/sitemap_products.xml  -o afaq.xml
curl -sL -A "Mozilla/5.0" https://alshamel.sa/sitemap-1.xml              -o alshamel1.xml
curl -sL -A "Mozilla/5.0" https://alshamel.sa/sitemap-2.xml              -o alshamel2.xml
grep -oh "<loc>[^<]*</loc>" jarir_[0-9].xml | sed 's|</\?loc>||g' > jarir.urls   # ~185,553
grep -oh "<loc>[^<]*</loc>" pcpalace.xml    | sed 's|</\?loc>||g' > pcpalace.urls # ~2,020
grep -oh "<loc>[^<]*</loc>" afaq.xml        | sed 's|</\?loc>||g' > afaq.urls     # ~1,703
grep -ohE "https://alshamel\.sa/[^< \"]+" alshamel*.xml | sort -u > alshamel.urls # ~807
```

Notes: jarir's index is `https://www.jarir.com/media/sitemap/sitemap_sa_ar.xml`
(`sitemap_sa_en_product_*` 404s); its sitemap URLs are the canonical
`https://www.jarir.com/<slug>-<family>-<id>.html` form. Put these paths in every matcher
prompt.

**Why it matters:** (a) the shared WebSearch budget (200/session) runs out mid-batch —
batch 11 exhausted it two-thirds through and finished fine on greps + jina; (b) an absence
in a complete catalogue is *real* gap evidence ("grepped 185k jarir URLs, zero hits"),
which a failed search is not; (c) it settles whole families in one command — jarir carries
no ZTE phones (routers only), no itel, no Xiaomi 14 Ultra, and no plain Tecno Spark 30C/40
(only Spark 40 **Pro** and Spark 50).

**Pre-grep leads per product and paste them into the prompt** — it roughly halves agent
fetch counts. Filter capacity/spec tokens (`128gb`, `16gb`, `i7`) out of any auto-generated
lead list: they match SSDs, cables and phone cases, not the product.

**A sitemap hit is a LEAD, never a match.** Sitemaps go stale in both directions: batch 11
had an alshamel entry whose page 302s to the homepage and an extra URL returning HTTP 404,
both originally recorded as matches. **Require a rendered page with the spec block before
recording anything.**

## Lookup order (stop at first verified hit; record every strategy tried)

0. **EAN/UPC barcode** — if the product row has a `barcode` (Canon toner section),
   search it first on every competitor; it is the strongest possible key.
1. **MPN / hard identifier** — use `mpn_candidates` from the batch file. Inventory-sheet
   codes often pack several interchangeable OEM part numbers (`CE505A/280A/CRG719`) —
   try each; any of them identifies the same product family.
2. **SKU as-is** — only if it looks like a real manufacturer part number.
3. **Brand + model line** — e.g. `HP 26A CF226A black`, `Kobra 245 TS shredder`.
4. **English title** — use `english_title` from the batch file. Do not re-normalize.
5. **Arabic name** — `name` as-is; amazon.sa and noon index Arabic well.
6. **Site-restricted web search** — `site:jarir.com CF226A`, `site:noon.com <english_title>`.
   Especially effective for Jarir, whose on-site search is weak.

**STRICT SAME-BRAND RULE:** only the exact same product from the exact same brand is
a match. For our Mint (منت) products search the brand explicitly — `Mint <part
number>`, `منت <part number>`, "Mint LaserJet Toner <model>" — because marketplace
Brand fields often mislabel Mint as "Generic". Finding the OEM original (HP/Canon/
Epson) or another aftermarket brand instead is a **gap**, not a match.

Then **mandatory three-way verification** (see confidence-rubric.md): open the
candidate page and confirm brand + identifier in the page **title**, the URL
**slug**, and the **description/spec fields** (opaque slugs like `/dp/ASIN` shift
the burden to title + description). Any disagreement between the three ⇒ no match.
Canonicalize the URL. Do NOT spend fetches extracting prices.

## Fetch ladder (per page — for identity verification only)

1. `WebSearch` for discovery; `WebFetch` for pages.
2. If bot-walled/empty → `python3 <skill>/scripts/fetch.py "<url>"`
   (DataImpulse SA residential proxy; prints TITLE / visible text — enough to confirm
   brand + part number).
3. Last resort → Apify `rag-web-browser` (via ToolSearch if not loaded).
4. Two consecutive hard failures on the same competitor ⇒ mark that competitor
   `error_transient` and move on; do not burn the batch.

## amazon.sa

- Search: `https://www.amazon.sa/s?k=<query>` (also try `&language=en_AE` for English UI).
- Canonical URL: `https://www.amazon.sa/dp/{ASIN}` — ASIN is 10 chars `[A-Z0-9]`,
  extract from `/dp/` or `/gp/product/`. Strip everything else (slug, ref, query).
- `competitor_variant_identifier` = ASIN. Color/size siblings are separate ASINs —
  pin the one matching our variant; note alternates (`alt_color_asin`) if useful.
- Watch for: marketplace sellers of international versions; "compatible" cartridges
  ranked above originals; renewed/refurbished listings.

## noon.com

- Search: `https://www.noon.com/saudi-en/search/?q=<query>`.
- Canonical URL: `https://www.noon.com/saudi-en/<slug>/<Nxxxxxx…V>/p/` — keep the path,
  strip query. `competitor_variant_identifier` = the `N…V` code.
- Noon titles usually embed full specs (RAM/storage/color) — good for `high` evidence.
- Arabic search works; the `saudi-en` locale still resolves.
- **Condition test (2026-08-07): noon marks genuine refurbished stock in an explicit
  `Grade` spec field** (`Grade Refurbished`, `Grade Max`). Its **absence proves the unit is
  new** — decisive for our مجدد rows, and it rejected a listing whose seller text admitted
  "the RAM/SSD/Windows have been upgraded by the seller" (a modified *new* unit).
- **"(Upgraded Version)" in a noon title/model number = seller-modified** (`Model Number
  PV15250 Upgraded`, `250R G10/Upgraded`). Same nominal config but different provenance
  and seller — not وكيل — warranty, so **`medium` max** against our جديد بالكرتون rows.
- noon distinguishes RAM tiers honestly in the title (`4GB RAM` vs `8+8GB RAM` on sibling
  SKUs), so a lower figure is a real config difference, not extended-RAM shorthand.
- **"Free Gifts" promo pages** (bundled powerbank/case) are still the bare product for
  matching, but check whether a non-promo listing exists and prefer it.

## jarir.com

- On-site search is weak — prefer strategy 6 (`site:jarir.com <identifier or model>`).
  On-site: `https://www.jarir.com/sa-en/catalogsearch/result/?search=<query>`.
- Canonical URL: `https://www.jarir.com/sa-en/<slug>-<digits>.html` (or legacy
  `…-jpmNNNN.html`). `competitor_variant_identifier` = the trailing numeric/jpm id.
- Jarir often bundles storage/connectivity options on ONE page (variant selector) —
  that's a `medium` with `competitor_variant_options` + note, per the rubric.
- Jarir stocks mostly originals — low compatible-cartridge risk, but smaller catalog:
  expect more `gap`s here than on amazon/noon.
- Fetching: jarir times out through the residential proxy but serves fine direct —
  use `fetch.py --no-proxy` for jarir (fetch.py auto-falls-back anyway, `--no-proxy`
  just skips the wasted 60s).

## extra.com (eXtra — United Electronics)

- Major electronics retailer (laptops/phones/appliances; little to no toner).
- Search: `https://www.extra.com/en-sa/search/?text=QUERY` (also `/ar-sa/`). `?q=` 500s.
- Canonical URL: keep the full product path, strip query. Prefer the `/en-sa/` form.
- **extra does NOT render its spec table client-side** (corrected 2026-08-07 — the old
  claim cost 16 entries a wrongly-capped `medium`). The **full product JSON is in the
  served HTML** under plain `curl`: `modelNumber`, `RAM SIZE`, `Memory (internal)`,
  network and screen classifications. So extra pages support a `high` when every stated
  attribute checks out — and `modelNumber` often equals the Jarir MPN, giving free
  cross-retailer corroboration (e.g. Honor 600 Lite `5109CEHK` on both).
- extra returns a real **HTTP 404** for dead product paths (`Title: Not Found - eXtra`).
  Never record a match without seeing the product JSON — a 404 has no spec block.

## Small specialist stores (ahbarhd, stech.ink, pcpalace, fqtoners, rowadalahbar, rawand, amwajest, afaqalhasoob, alshamel)

- Mostly toner/ink and computer shops on Shopify or Salla platforms.
- **Shopify** (stech.ink, pcpalace.com.sa): product URLs `/products/<slug>`,
  on-site search `/search?q=QUERY`.
- **Salla/other Arabic storefronts** (the rest): URL patterns vary — derive the
  canonical form from the first product page you open on that site; strip query
  strings. On-site search is usually `/search?q=` or a `?s=` parameter.
- Primary strategy: `site:<domain> <MPN>` web search; secondary: one on-site search.
- These stores DO stock compatible/aftermarket brands — check the brand on the page
  exactly like everywhere else (Mint vs OEM vs other aftermarket).
- **alshamel.sa DOES stock brand Mint (منت)** (found 2026-07-22: `mint-w2071a---117a` slug,
  title "حبر طابعة منت W2071A 117A") — never skip it for Mint products; search
  `site:alshamel.sa منت <MPN>` and `site:alshamel.sa mint <MPN>` (slugs often start `mint-`).
- amazon.sa carries Mint multi-color KITS (bundles) — not matches for single-unit products.
- Toner stores won't carry laptops/phones and extra.com won't carry toner refills —
  skip obviously-irrelevant site/product combinations with a quick single search,
  don't burn budget.

## Tier notes

- **Tier A (ink/toner/accessories)**: identifier-first, but the identifier alone is
  NOT enough — the OEM part number (CF350A etc.) appears on Mint, OEM and rival
  aftermarket listings alike, so the **brand in title/slug/description decides**.
  Mint product ⇒ only a Mint (منت) listing matches; OEM or other aftermarket ⇒ gap.
  Always pin: brand, color (cyan/magenta/yellow/black), XL/high-yield vs standard,
  single vs multipack. The Arabic name states the color (احمر=red/magenta,
  اسود=black, ازرق=cyan, اصفر=yellow). Expect far more gaps than the old OEM-as-medium
  runs — that is correct behavior, not under-searching.
- **Tier B (shredders/printers/gaming accessories)**: model number usually in the code
  field (`KOBRA 245-TS`, Brother printer models). Kobra shredders: search "Kobra <model>
  shredder". Gaming accessories are often generic/no-brand imports (mouse pads, gas
  lifts, chair parts) — an identical-looking generic item under a different listing is
  NOT a verifiable match; expect many `gap`s and don't over-search: 4 strategies then gap.
- **Tier C (laptops/phones/tablets)**: never match on model name alone — pin CPU/RAM/
  storage/connectivity/color explicitly. Cross-check competitors against each other.
- **Refurbished (مجدد)**: match only refurbished/renewed listings.

---

# FIELD-PROVEN ADDENDUM (batches 1–3, validated 2026-07-24) — READ BEFORE EVERY BATCH

## A. Amazon.sa — the #1 source of missed matches (MANDATORY technique)
amazon.sa's on-site search silently omits many VALID listings — both zero-offer ("Currently
unavailable") pages AND some fully in-stock ones (e.g. Brother TN-200 B00007MCMK was live and
purchasable yet invisible to search). The deep audit recovered 20 zero-match products this way.
1. Search amazon.sa normally (fetch.py --no-proxy `s?k=`).
2. **ALWAYS also search amazon.eg and amazon.ae for the PN via jina** (`https://r.jina.ai/<url>`)
   and harvest genuine-brand ASINs — ASINs are shared across marketplaces.
3. Probe `https://r.jina.ai/https://www.amazon.sa/dp/<ASIN>` directly. A resolving page with
   matching brand+PN+color = match (zero-offer → medium, flag `zero_offer`; live local seller
   after three-way → can be high).
4. amazon dp bot-walls (bm-verify/challenge) are BYPASSABLE via jina — never record
   "unverifiable" without trying jina first.
5. Cross-marketplace ASIN quirk: the same ASIN can be a DIFFERENT variant per marketplace
   (B016EPLB8E = cyan on .ae, yellow on .sa). The .sa page is authoritative.
6. Amazon search cross-matches PNs loosely: a search "hit" for your PN may be a different
   product (106R00682 search returns a 106R01395 listing). Only the dp page's spec fields count.

## B. noon.com
- Bot-walled to curl; use jina reader for everything. SPA search grid renders ~60% of the time —
  direct product-page fetches are more reliable than search; 2 failed renders → error_transient.
- **Marketplace cap:** noon listings are `medium` max unless the page shows a brand store
  ("sold by the brand", RICOH SUPPLIES, xerox store) or a verified local seller line — those can
  be high after three-way. Seller/description legs often don't render → keep medium.
  **Sharpened 2026-08-09 (batch 15, cost 7 downgrades):** a plain `/corsair/`, `/lg/`,
  `/lexar/`, `/apple/` **brand chip is a FILTER link, not an official brand store** — with a
  third-party seller it does NOT lift the cap. The test is an official-store badge (as with
  the Mint store in D4), not the mere presence of a brand chip.
- Canonical `/saudi-en/<slug>/<Ncode>/p/`, strip `?o=` params. saudi-ar works when -en fails.
- **noon availability is episodic — probe it once at session start** before planning around
  it: `curl -s "https://r.jina.ai/https://www.noon.com/saudi-en/search/?q=galaxy%20a17" |
  grep -c "/p/"`. A healthy render returns dozens of `/p/` links. It was fully walled for
  most of batches 7–10 and fully up on 2026-08-07. When it is up, **drain the retry queue
  first** — that is where the recoverable noon coverage lives.
- Under heavy concurrency (~18 agents) noon starts refusing: batch 11 logged 48 noon
  `error_transient` legs. Keep noon-heavy work to fewer parallel agents, or accept the
  legs and sweep them later.

## C. Per-site profiles (verified across 555 products — use as priors, still verify)
- **stech.ink** — backbone (259 matched products). suggest.json (`curl -A "Mozilla/5.0"
  ".../search/suggest.json?q=X&resources%5Btype%5D=product"`); when it 404s use
  `/search?q=X&type=product`. **vendor field decides genuine** vs S-TECH house brand; CET-branded
  items sit under OEM vendor facets (aftermarket!). Suffixes -S/-M/-L on Toshiba FC-lines are
  CAPACITY TIERS, not colors. "Sold out/backordered" listings still count (links-only).
- **jarir.com** — catalogsearch false-negatives whole families. Use product sitemaps
  (`sitemap_sa_ar_product_1..3.xml`) or slug probe `<family>-ink-toners-and-ribbons-<id>.html`
  (bare `-<id>.html` 404s). fetch.py --no-proxy. Carries current HP lines; ZERO Ricoh/Toshiba/
  Xerox toner (sitemap-proven).
- **fqtoners.com** — genuine أصلي lines for HP/Brother/Toshiba/Ricoh/Xerox beside FQ compatibles.
  **Live category pages beat sitemaps** (stale both ways: dead 302/410 URLs still indexed AND
  live products missing from sitemap). TRAP: أصلي titles with متوافق descriptions = aftermarket
  (always read description; "متوافق مع الطابعات X" = printer-fitment phrasing = OK). Their yield
  figures are unreliable. Toshiba cat: /توشيبا/c1397902784; Ricoh: /ريكو/c933760966.
- **pcpalace.com.sa** — Shopify/Zid; on-site search unreliable → grep sitemap_products.xml.
  Scattered genuine Xerox/HP/Brother among hardware. Slug typos happen (CL-44 = CL-441) —
  the part number in specs decides.
- **rawand.com.sa** (Zid) — brand categories are authoritative: Toshiba /categories/636926/,
  Ricoh /categories/636923/. English SEO slugs can be misleading ("C-1810" slug = T-1810 toner;
  "Best-offers-for..." slugs) — title+description decide. Genuine Brother TN-361 set, MP C2503
  colors, MP2501.
- **rowadalahbar.com** (Salla) — genuine TN-240/TN-261 sets, M C2000 black; Xerox = 3020 only.
  TRAP: template bugs put wrong specs on some pages (83A/131A-black/203A-cyan) and their INK
  (953XL) pages have no MPNs + "recyclable/compatible" wording — treat ink authenticity as
  unconfirmed (medium max).
- **amwajest.com** (Salla) — A.W.D house brand (بديل = aftermarket). Genuine niche: Xerox
  B210/C230, OKI C612/MC363. TRAP: title color words can be wrong (أحمر on a black W2120A page;
  أصفر on a black page) — body/PN decides. Copier-ink category labeled "Xerox،Canon،Ricoh،Toshiba"
  actually contains only Canon/Sharp.
- **alshamel.sa** (Salla) — main Mint carrier (+PULI). JS-only pages; **bare /ar/p<code> URLs
  410 — full slugged URLs required**. jina renders ~half the pages (rest = store shell; if shell,
  don't conclude delisted without a second signal). Rare genuine: 131A Y/M, 150A, 307A-Y, TN-2305.
  **Dead-link test (2026-08-07):** a live alshamel product id 301-redirects to its canonical
  slug *even from a deliberately wrong slug* — so if an id instead 302s to the homepage the
  product is delisted and the sitemap entry is stale. Use that as the control test rather
  than concluding from a shell render. Also: read the store's own title, not the slug — a
  slug token `i7-240h` was a **240Hz** panel, not a "Core 7-240H" CPU.
- **extra.com** — `?text=` endpoint only (`?q=` 500s). Current HP toner only; zero Xerox/Toshiba/
  Ricoh brand presence.
- **ahbarhd.com** — 100% HD/FMQ aftermarket. 0 genuine matches in 555 products. One sitemap grep
  suffices.
- **afaqalhasoob.com** — no consumables category; near-zero. (But its amazon storefront sells
  genuine HP — found as amazon seller "AFAQ ALHASOOB".)

## D. Variant discipline (validator-enforced)
- Standard vs high-capacity/XL/H = DIFFERENT SKU → gap with variant-candidate note (never match,
  never high). This is the #1 Xerox/Toshiba pattern: competitors often stock the opposite capacity.
- Regional/pack suffixes (D/E/P/PS/C/DS-S/AC/FAT390E-vs-X) = medium max with suffix note.
- Multipacks/twin-packs/sets/kits never match singles (Epson T907x and Canon BCI-15 exist ONLY
  as bundles in KSA → gaps).
- Remanufactured ≠ genuine. "Brand OKI + genuine PN" can still say "Cartridge Type: Compatible"
  on amazon — flag, keep medium.
- Zero-offer amazon listings = matches at medium with `zero_offer` flag (export policy TBD).

## D2. Tier-C (laptops / phones / desktops) discipline — added 2026-08-07 after batch 11

- **Config drift is the #1 error class here**, replacing Tier A's brand-mixing. Our rows pin
  CPU + RAM + storage + screen + GPU; the listing must satisfy **every attribute our row
  states**. i7-1255U ≠ i7-1265U · 16GB ≠ 32GB · 512GB ≠ 1TB · 14" ≠ 16" · 4G ≠ 5G ·
  Ultra 7 155H ≠ 255H · 255H (Arrow Lake) ≠ 256V/258V (Lunar Lake).
- **Attributes our row does NOT state are not distinguishing.** Phone rows rarely give a
  colour — do not downgrade or reject over colour; pin the stated specs, record the colour
  found in `competitor_variant_options`, note sibling ASINs/N-codes.
- **Condition is a hard attribute both ways.** مجدد/Refurbished rows match ONLY renewed
  listings; جديد rows match ONLY new ones. Expect refurb rows to be near-total gaps: amazon
  Renewed is the only realistic carrier, and jarir/extra/the small stores never list refurb
  laptops.
- **Soldered-RAM CPUs make RAM a hard key.** Lunar Lake (226V/256V/258V) and most thin
  business ultrabooks have non-upgradable RAM, so a RAM difference is a different SKU.
- **The common near-miss is "right model, wrong configuration"** — the model exists locally
  but only in another spec. That is a genuine `gap`; record the near-miss in `note`.
- Vendor part numbers are the fastest route to `high`: Lenovo MTM (`21QC009PAD`,
  `83JE00GGAD`), HP SKU strings (`15-fa2002nx`, `16-r1020nia`, `818U8EA`), Asus chassis
  codes (`B1403CVA`, `E1504FA`, `AL14-32P`), Apple part numbers (MLXW3/MLY13/MLXY3/MLY03).
  Grep these in the local sitemap indexes first.
- **Middle East Version is the normal KSA retail SKU for phones — not a downgrade.** Reserve
  the "international version" medium cap for genuinely foreign-market (US/UK/India) units.
- Under-specified rows (no model number, or a model line with no configuration) cannot reach
  `high`: cap at medium, say why, and emit a `catalog_issue`. Rows naming only a family
  ("Toshiba Satellite Pro dynabook", "Lenovo IdeaPad Core i7 12th Gen") are honest gaps.
- Near-identical sibling rows are common in Tier C (five MacBook Air M2 colour/storage rows,
  four Latitude 7430s, six ThinkPad E14/E16 Gen-7s). **Tell each agent its siblings and what
  differs**, and have it flag in `note` when one listing could satisfy two of our rows —
  that is how the duplicate catalogue rows surface.

## D3. Refurbished (مجدد / "Renewed") rows — proven 2026-08-08, batch 12

**No KSA retailer lists refurbished laptops except the two marketplaces.** Batch 12 ran 24
refurb laptop rows against all 13 sites and got 7 matches, all on noon (4) and amazon.sa (3).
Sitemap-proven absences, worth citing rather than re-searching:
- **jarir** — 192,127 product URLs contain exactly **3 renewed laptops**: HP Omen (673891),
  Honor MagicBook Art (684098), Apple MacBook Air (686728). Nothing else, ever.
- **pcpalace** — 7,637 URLs, **0** renewed/refurbished/used items.
- **alshamel** — 807 URLs, **0** renewed items.
- **afaqalhasoob** — 1,702 URLs, 4 renewed items (HP Z2 G5 desktop, Huawei MateBook B3-430,
  HP LaserJet M507dn).
Put these four lines in the batch BRIEF for any refurb-heavy batch: each matcher then closes
four competitors in one sentence and spends its budget on amazon/noon. Confirming the model
exists **new** at jarir/extra is still worth one search — it belongs in `note` as the
near-miss, not as a match.

**The two condition tests that actually decide it:**
- noon: an explicit **`Grade`** spec field (`Grade Refurbished` / `Grade Max` / `Grade Basic`).
  Its absence proves NEW. Batch 12 rejected a noon "L14 Gen 2 i7-1165G7" on exactly this.
- amazon: the `(Renewed)` title marker plus a "Visit the Amazon Renewed Store" link and/or a
  `Refurbished - Excellent` condition line. All three appear in the jina render.

**Under-specified refurb rows are the norm, and they cap at medium.** Batch 12's rows gave a
model line + a CPU family and nothing else, so no entry could reach `high`. When a site has
several genuine renewed listings of the row's model that differ only in an attribute our
catalogue never states (amazon had **four** renewed Latitude 3420 i5 listings), the honest
result is `unresolved` → review-push, not a coin-flip match.

## D4. Mint (منت) sourcing + two dead-link tests — measured 2026-08-08, batch 13

**noon.com IS a genuine Mint carrier.** It has a **Mint brand store** at `/saudi-en/mint/`; a
product page whose brand chip links there satisfies the brand-store bar, so it can reach
`high` instead of noon's usual medium cap. Confirmed Mint stock: the **117A, 201A, 203A,
207A, 222A and 410A colour SETS**, plus 151A/W1510A, 80A/CF280A, 05A/CE505A, 17A/CF217A.
**THE TRAP: a noon Mint listing's SLUG usually omits the brand.** The 117A set is at
`hp-117a-ink-cartridge-set`, the 201A set at `201a-ink-cartridge-set-compatible-with-hp` —
only the *title* says "Mint". Therefore:
- **Search noon by the bare PART NUMBER and read the title + brand chip.** Searching
  `mint <PN>` or grepping slugs for "mint" WILL MISS real matches — this cost batch 13 an
  unknown number of noon legs before it was found mid-batch.
- Title + description carry the whole three-way burden there, as with an amazon `/dp/ASIN`.
- "mint" on noon is polluted by mint-flavoured cosmetics, facial toners and mint-green
  stationery — require a printer-consumable PN.
So the Mint carriers are **noon + alshamel.sa** (alshamel slugs *do* start `mint-`). Mint
depth is line-dependent: alshamel has **no Ricoh toner at all** and no Canon-067 coverage.

**Brand-field direction (settled by a validator, batch 13).** The rubric's rule holds as
written: a title stating **Mint + the PN** is a match even when the spec `Brand` field says
`Generic`, and "Compatible (not original)" is *consistent* with Mint being aftermarket, not a
contradiction. Two amazon Mint kits (`B0H242PJMX` 415A, `B0GY1RQPBD` 410A) were wrongly
gapped on `Brand: Generic` and reinstated after the pages showed `Number of Items 4` +
`COMPLETE KIT`. Only a title that **never names the brand** fails.

**fqtoners dead-link test.** A live product returns **200 under plain
`curl -sL -A "Mozilla/5.0"`** (no proxy, no jina). A **stale** sitemap entry **302s to
`https://fqtoners.com/`** — you get the store shell (nav + related-products grid, no spec
block) — and a bare `/p<id>` without the slug **410s**. The `/ar/` prefix is irrelevant.
A store-shell render ⇒ **delisted, record a gap**, not `error_transient`.

**alshamel dead-link test** (restated because it is the same shape): a live id 301s to its
canonical slug even from a deliberately wrong slug, so an id that instead 302s to the
homepage is delisted. Bare `/ar/p<code>` 410s. jina renders ~half its pages, so a shell alone
proves nothing — use the control test.

**Toner-store sitemaps now exist for all six specialists** (batch 13 built them):
`stech 5,417` · `fqtoners 2,543` · `amwaj 1,191` · `rowad 708` · `rawand 520` · `ahbarhd 299`.
Shopify gotcha for stech: parse `/sitemap.xml` for its
`sitemap_products_N.xml?from=…&to=…` parts — a bare `sitemap_products_1.xml` returns only the
homepage. **ahbarhd and afaqalhasoob returned 0 matches across batch 13's 190 rows on top of
phase 1's 555 — treat both as one-grep sites.**

## D5. House brands, brand stores, and the fetch reality — measured 2026-08-09, batch 14

**ENUMERATE THE BRAND STORE BEFORE FANNING OUT.** Twice now a mid-batch brand-store discovery
has invalidated every earlier agent's gap on that site: noon-Mint in batch 13, **noon-OMES in
batch 14**. noon exposes a brand store at `noon.com/saudi-en/<brand>/<category>/?limit=100`
and the page prints its own result count — one fetch settles the whole brand. For OMES it
returned **18 items: 11 cloth-tape, 3 shredders, 4 laminators**, which recovered **10 matches
already recorded as gaps** and simultaneously *proved* the remaining ~90 OMES gaps real (no
staplers, box files, binders, price guns, cutters or sharpeners exist there). For any batch
with a house brand or a single-vendor block, make the brand-store enumeration **step 0**, put
the result in the shared brief, and let the matchers consume it.

**OMES (أومس/اومس) is a two-carrier brand.** Grep-proven over the complete catalogues: 0 OMES
in jarir (195,493 URLs), pcpalace (7,637), stech (5,417), fqtoners (2,543), afaq (1,675),
amwaj (1,191), alshamel (807), rowad (708), ahbarhd (299). **rawand.com.sa has exactly 2**
(laminators OS-188-3F and OS-381-3F, both genuine matches); amazon.sa carries OMES shredders
and staplers; noon carries the 18 above. Everything else is an evidenced gap.

**Ugreen's model number is the 5-digit suffix of our SKU, and competitors put it in the slug**
(`alshamel …-90798/`, `…-15508/`, `jarir ugreen-robot-35605b-…`). Grepping it beats searching.
Two traps: 5-digit greps collide with jarir book ISBNs (ignore silently), and a one-digit
miss is a real gap — rowadalahbar stocks Ugreen **30848** against our **30847**.

**FETCH REALITY (supersedes the jina-first advice above for amazon):**
- **amazon.sa serves fine to plain direct curl** — `curl -sL --compressed -A "Mozilla/5.0
  (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0
  Safari/537.36"` returns the full spec block including `Brand` and `Item model number`.
  Use it for dp pages *and* `s?k=` search. Do not spend jina on amazon.
- **The DataImpulse proxy was DOWN on 2026-08-09** (curl exit 56 every attempt). `fetch.py`
  prints "proxy fetch failed … retrying direct" and the direct retry is what works — check
  the proxy before planning any batch around it.
- **jina 429s under ~15 concurrent agents and recovers in ~8s.** That is our own concurrency,
  not a block. Agents must `sleep 8` and retry ×3 before writing `error_transient`; batch 14
  lost noon coverage to agents that stalled on the first 429.

**Read `short_desc`, not just `name`.** Three batch-14 rows were left `ambiguous` between a
64GB and a 128GB listing because the matcher pinned capacity from `name` only — the capacity
was in `short_desc` all along. All three resolved to clean matches on validation.

**"(Compatible)" third-party enterprise drives** (Dell/HPE rows with an interface and form
factor but no part number and no capacity) are structurally unmatchable. Ten of them closed as
130 evidenced gaps in a single cheap agent — do not let a full-budget agent near them.

**Orchestrator bug to avoid:** when a validator *upgrades* an `ambiguous` entry to `matched`,
the chosen URL lives in `candidates`, not `competitor_url`. An apply-verdicts script that does
not lift it will trip its own "matched implies URL" assertion and silently gap the match — this
ate 6 real matches in batch 14 before being caught. Likewise, keep the checkpoint sweep loud:
a `ck.sh` that hid `state.py` failures behind `>/dev/null` + `&&` reported "0 new" while 19
finished products sat unwritten.

**Grouping beats one-agent-per-product for Tier A.** 190 rows → ~40 work units keyed by
part-number family, so colour siblings are decided by a single agent and cross-colour
attribution errors become impossible. Put sibling traps in the unit's `traps` array. Unrelated
singletons can be merged 5–7 per agent provided the prompt says "unrelated PNs — never let one
row's URL serve another".

**Two schema defects worth asserting in every checkpoint sweep** (both seen in batch 13):
agents sometimes **omit the `competitor` key** and rely on array position, and sometimes emit
`status: matched` with **no `competitor_url`**. Assert name-at-canonical-position and
matched⇒URL. Also detect SET rows on **English "SET"/"KIT"**, not just Arabic `طقم`.

## D6. THE CHEAP-CLOSE HARNESS — build it before any agent (added 2026-08-09, batch 15)

This is the consolidation of everything batches 1–14 learned about *not* spending fetches.
Ten of the thirteen competitors have a complete, downloadable product catalogue. Grep them
mechanically at the orchestrator and the matchers never touch them.

**Step 0 is now three scripts, ~4 minutes total, all in the scratchpad:**

1. `sitemaps/` — all **ten** local catalogues, not the four in the old STEP 0:
   jarir 195,493 · stech 10,834 (incl. `/ar/`) · pcpalace 7,643 · fqtoners 4,363 ·
   rowadalahbar 2,849 · amwajest 2,380 · afaq 1,686 · ahbarhd 1,234 · alshamel 807 ·
   rawand 520. Endpoints: Shopify/Salla `sitemap_products.xml`, `sitemap-N.xml`, and for
   stech the `/sitemap.xml` index → its `sitemap_products_N.xml?from=…&to=…` parts.
   **rawand's index is `/sitemap.xml` → `sitemap_products.xml`** (a bare `sitemap-N.xml`
   guess returns 54 junk URLs — that near-miss silently halved a site's evidence once).
2. `leads.py` — per-row identifier tokens greppedagainst those ten catalogues, emitting
   `leads.json` = `{pid: {tokens, hits{site:[urls]}, clean_sites[]}}`.
   **Match on delimiter-bounded URL tokens, never on substring.** Substring matching gave
   ~6× the hits, all junk: `20204` inside a jarir ISBN `5400520204257`, `s24` inside
   `thinkvision-s24-4e`, `cs24` inside a Salla CDN filename. Build an inverted index of
   `[a-z0-9]+` runs per URL (fast: 200 rows × 220k URLs in 3 s), plus a prefix mode for
   tokens ≥5 chars so `35605` still finds `ugreen-robot-35605b`. Drop CDN image URLs and
   category `/cNNNNNN` paths first. Filter capacity/spec noise (`16gb`, `750`, `ddr5`,
   `360`) out of the token list or every SSD and cable matches.
3. `brandmap.py` — **the single highest-value artifact.** For each brand in the batch ×
   each of the ten sites, the count and the URL list. **A brand with 0 URLs in a complete
   catalogue is an evidenced gap for every row of that brand on that site — the matcher
   writes it with zero fetches.** Batch 15: ESR and Boulies were 0 on all ten (closing 340
   legs outright), KLEVV existed only at pcpalace, OMES only at rawand (2). Use
   **delimiter-bounded** matching here too — plain substring reported "OMES: 256 URLs at
   jarir" from *homes*/*chromes*, which contradicts D5's grep-proven 0. That contradiction
   is the regression test: if brandmap disagrees with a previously proven absence, your
   matcher is buggy, not the finding.

**Net effect measured on batch 15:** of 2,000 local-site legs (200 rows × 10 sites), only
~109 rows had any local hit at all; the rest close on catalogue evidence. Live fetching
collapses to amazon + noon + extra plus the handful of leads.

**Brand-store enumeration is step 0b, and it now has a completeness test.** noon serves
`noon.com/saudi-en/<brand>/?limit=100` through jina. Extract `title<TAB>url` pairs and hand
the file to the matchers. **A file with exactly 100 rows is TRUNCATED — absence from it
proves nothing; under 100 is complete.** `&page=2` does *not* render through jina (0 product
links on every retry, sequential or parallel), so 100 is a hard ceiling per brand — do not
build gap evidence on a truncated store. Batch 15: corsair 100 (truncated), esr 134 (two
partial renders merged), ugreen 100, lexar 130, marvo 64 ✔, klevv 23 ✔, omes 19 ✔,
boulies 0 (store page exists, renders no products — inconclusive, search per row).
**Scrub prices when extracting** — the jina line carries `**17.95**` and `13% Off` inline.

**Fetch reality, re-verified 2026-08-09** (this changes every few days — probe, don't assume):
- amazon.sa — **plain direct curl works**, search and dp. `data-asin="[A-Z0-9]{10}"` off
  `s?k=`. No jina, no proxy.
- noon.com — **direct curl fails** (HTTP/2 stream error / silent timeout, curl exit 92).
  jina only. A 429 is our own concurrency: `sleep 8`, retry ×3.
- DataImpulse proxy — **still down** (curl exit 56), second session running. `fetch.py`'s
  direct-retry fallback is what works.
- stech.ink — always store the **`www.`** host; the bare apex 301s and that broke 281 links
  in production (~12% of the match catalogue silently returning nothing).
- extra.com — **the SEARCH grid is client-rendered; the PRODUCT page is not.** `?text=` returns
  HTTP 200 with ~111 KB and *zero* real `/p/<id>` product links (only static promo/gift-card
  paths), so judging absence from a search fetch is worthless — it cost batch 15 five legs
  recorded as `error_transient`, two of which were real matches. Discover via
  `site:extra.com <model>` web search, then curl the product URL: the full product JSON with
  `modelNumber` IS in the served HTML, which is what supports a `high`. (The older note
  "extra does not render its spec table client-side" is right about product pages and was
  never about the search grid.)

## E. Wave/checkpoint protocol (session-tested)
- **Concurrency: the harness caps subagents at 20**; launches above that bounce with
  "Concurrent subagent limit reached" and must be relaunched (no work is lost — checkpoints
  are per-product). Batch 11 ran ~15–18 sonnet matchers in parallel against jina/sitemaps
  with zero amazon blocks, so the old "4 agents/wave" ceiling applies to *amazon-search-heavy*
  work, not to jina/sitemap-driven work. Steady state that worked: keep ~15 in flight, and
  after each completion notification checkpoint everything on disk and launch replacements.
- Keep a `ck.sh`-style sweep that checkpoints **any** `rec-*.json` not yet in `state.json`
  and prints progress. It makes the session crash-safe and makes "how many left" a one-liner.
- Expect a few agents to die on transient API errors ("response stalled mid-stream");
  relaunching the same product recovers cleanly.
- 4 sonnet agents/wave (amazon bot-pressure above that). Orchestrator normalizes EVERY record:
  labels must match score bands (high .90–1.00, medium .60–.89), absences = gap/0, floor mediums
  to 0.60, cap cross-border at medium, strip `?` params, `candidates` only on ambiguous.
- Feed each wave the sibling-family knowledge from the previous wave (store leads with exact
  URLs/ASINs) — it halves agent fetch counts. But agents' sitemap greps DO miss live items:
  when two sibling agents disagree about a store, re-check the live category yourself.
- Agents sometimes emit malformed/truncated JSON (6 of 13 entries) — count entries before
  checkpointing; SendMessage the agent to finish the missing competitors.
- WebSearch budget (200/session) exhausts fast — prompts must mandate sitemap/jina/direct-fetch
  alternatives from the start.
- Validator (Opus) after each batch: mediums+ambiguous chunks of ~40 + 10% high sample; jina
  bypasses botwalls so "unverifiable" should be rare. **Group the chunks by competitor** so
  each validator stays on one site's fetch technique — batch 11 ran 4 × ~43 and returned
  121 keep / 19 upgrade / 24 downgrade / 7 reject / 0 unresolved.
- **The validator's highest-yield check is "did this page actually render?"** Three of batch
  11's seven rejects were URLs that never resolved (an extra 404, an alshamel 302-to-home,
  and a matcher that read a slug token instead of the page). Have it open every URL.
- **Links only — no prices, including inside `evidence`/`note`.** Batch 11 leaked 32 real
  price figures into evidence text. Scrub numeric amounts (`SAR 4,089`, `395.00 SAR`,
  `ريال ...`) before completing a batch; the phrase "Buy Online at Best Price in KSA" in
  amazon titles carries no figure and is fine.

## E2. `state.py checkpoint` SILENTLY DROPS REPAIRS (found 2026-08-09, batch 15)

`cmd_checkpoint` starts with *"if pid in done_product_ids: print('already checkpointed —
skipping duplicate'); return"*. So **any pass that rewrites a `rec-*.json` after its first
checkpoint — a retry sweep, an error-leg repair, an ordering fix, an applied verdict — is a
no-op against `results/batch-NNN.results.json`**, and worse, it looks like it worked if the
caller redirects stdout to `/dev/null` (which the obvious `for pid in …; do … >/dev/null &&
echo ok; done` loop does).

Batch 15 lost 11 repaired legs to this before it was caught by reconciling the results file's
status counts against the rec files. **Always reconcile after a repair pass**, and apply
corrections with a script that *replaces* the record in the results file and appends the
corrected copy to the append-only master, rather than re-calling `checkpoint`
(`scratchpad/apply_recs.py` in the batch-15 session is the reference implementation).

Same session, same shape, two more instances of the general lesson: an agent's final
`DONE` lines under-report what it actually wrote (several units reported 1 line but had all
rows on disk), and two harness restarts killed 26 in-flight agents. **Trust files on disk,
never an agent's summary** — a `gaps.sh` that diffs unit rows against `recs/*.json` is what
makes both recoverable, and telling agents to *write each record the moment it is finished*
(not batched at the end) is what turned the second restart from a total loss into a
5-rows-lost one.

## E3. THE PRICE SCRUBBER EATS PART NUMBERS (found 2026-08-09, batch 18)

`apply_verdicts.py`'s `PRICE` regex listed **bare `SR`** as a currency alternative with no
word boundary. Any `<digits>SR` or `SR<digits>` run inside a **model number** was therefore
replaced with `[price removed]` — silently, across `evidence`, `note`, `external_title`
**and the top-level `catalog_issue`**.

Damage found: batch 18 lost the `SR` from APC `SRT192BP2` / `SRTRK4` / `SRT1000XLI` /
`SRTRK2` (7 fields); **batch 16 had already been corrupted and pushed** — LG `27SR50F-W` and
`32SR50F-W` became `[price removed]50F-W` in 21 fields. `competitor_url` was never touched,
so no shipped *link* was ever wrong; only the human-readable text degraded.

Rules that follow:
- The pattern is now `\bSAR\b|\bSR\b|ر\.س|ريال` and a figure must sit adjacent. **Never put a
  bare two-letter currency code in a scrub regex** — `SR`, `SA` and `RS` all live inside real
  part numbers.
- **Scrub `catalog_issue` as well as the three match fields.** It was outside every scan and
  is where two of the three unrecoverable corruptions hid.
- **Print a before/after diff of every scrubbed field and eyeball it.** A destructive regex
  running silently over the results file is a worse outcome than a leaked price figure.
- **Recovery depends on `scratchpad/<batch>/recs/rec-*.json` still existing** — those
  per-product files are the only untouched original. That is a second reason (after restart
  safety) not to clean them up, and it is what made both repairs exact rather than guesswork.
- Words like `price` or `رسوم الاستيراد` appearing in *evidence prose* are normal — agents
  cite the import-fee check by name. **Grep for currency-adjacent digits, not for the word.**

## F. After the batch — pushing to prod
- Payload builds from `results/batch-NNN.results.json` (NOT master), so validator
  corrections applied to the results file are picked up automatically.
- **`matches.master.jsonl` is an append-only log** — retry/validator passes append a full
  corrected record. Any consumer must collapse to the **last record per `product_id`**
  first. `report.py` was fixed for this 2026-08-07; `validate_urls.py` still double-counts
  its "N URLs checked" figure after a correction pass (its reject/dup logic is unaffected).
- **The push is upsert-only — there is no delete step.** A match withdrawn after a batch was
  pushed stays live in prod until explicitly deleted. Re-pushing does not remove it.
- **The matches upsert is keyed on `(competitor_id, competitor_url)`, so two of OUR products
  claiming the SAME competitor URL collapse into one prod row** — the payload reports fewer
  `upserted` than it sent, and the losing product silently has no match in production.
  Batch 15 sent 192 and got **188**: exactly its 4 duplicate-URL groups (one amazon, two noon,
  one jarir), all of them genuine duplicate catalogue rows. **Always diff `upserted` against
  the payload row count**; if they differ, group the payload by
  `(competitor_id, competitor_url)` — the extras will account for the gap precisely. This is
  the correct outcome for duplicate catalogue rows (one URL = one match), but it means
  de-duplicating the catalogue is the only way for both rows to carry a price.
- `push_batch.sh` gotcha fixed 2026-08-07: `products/bulk-upsert` adds a fresh default
  variant with NULL `external_id` even when the product already has a variant holding that
  id (anything touched by the variant-consolidation phase). The backfill UPDATE then hits
  the unique constraint, **rolls back for the whole batch**, and every match 422s with
  `UNRESOLVED_VARIANT`. The script now guards with `NOT EXISTS` and fails loudly instead of
  printing "BATCH N PUSHED" over a total failure.

## D7. Batch 16 corrections — measured 2026-08-09 (cables/networking/printers/monitors)

**"Global Store" is a FALSE import marker on amazon.sa — stop capping confidence on it.**
The string appears inside an HTML *comment* ("Returning Global Store Items") in return-policy
boilerplate on essentially every dp page. Briefs since batch 11 have told matchers to cap at
`medium` when they see it, so it has been suppressing amazon confidence run-wide (6 rows in
batch 16 alone). **The real import signal is the `ifd_price_row` element / the Arabic
"رسوم الاستيراد" price row** — present on only 5 of ~25 dp pages a validator opened. Grep for
that, not for "Global Store". ("Currently unavailable" / "غير متوفر حالياً" zero-offer pages
are still valid matches at `medium` with a `zero_offer` note.)

**amazon bm-verify walls are OUR concurrency, not a block, and they are bypassable.**
Under ~15–20 parallel agents, plain curl hits a bm-verify meta-refresh on roughly 1 request
in 3; batch 16 lost 19 legs to `error_transient` that way while amazon answered the
orchestrator normally throughout. **Fix that worked every time: follow the meta-refresh
`bm-verify` URL once with a persisted cookie jar, then re-request.** A sequential repair pass
using it recovered 9/9 legs (2 matched, 6 evidenced gaps, 1 ambiguous, 0 still erroring).
Reference script: `scratchpad/b16/fetch/az.sh`. Prefer sequential amazon work in repair passes.

**extra.com serves per-country storefronts.** A product that renders on `en-om` (Oman) can
**404 on `en-sa`** (KSA) — a matcher nearly recorded an Oman URL as a KSA match. Only a
rendering `en-sa` product page counts. (This is on top of D6's rule that extra's search grid
is client-rendered and proves nothing about absence.)

**extra.com's product-JSON `mpn` field is AUTHORITATIVE — read it, never infer from the
internal family code.** (Batch 17, 2026-08-09.) extra's titles often omit the manufacturer
model number and expose only an internal code like `MU001-Grey` / `MU006-Black`. Two Ugreen
mice were matched by correlating colour + that code; the validator pulled the served product
JSON and found `mpn` = **90374** and **25160**, not our 90674/90673 — both rejected. The
`MU00x` prefix is a *family* code shared across several distinct models, so colour+family
correlation is not evidence. Grep the served HTML for `mpn` / `modelNumber` and require it to
equal our model number; absent a matching mpn it is a gap, not a match.

**A product IMAGE can settle a colour hard key when the page text is silent.** (Batch 17.)
rowadalahbar's Ugreen 90675 page confirms model + internal code in title, slug and spec block
but names no colour anywhere; the matcher correctly left it `ambiguous`, and the validator
resolved it to a `high` match from the product photo (unmistakably black). Try the image
before gapping a colour-keyed row — but only once brand + model already agree three ways.
Conversely, when the page's colour *contradicts* our row (63827/63845: page black/grey vs our
`أخضر`), that is **not** a downgrade-to-medium — record `ambiguous` and push to human review,
because on colour-only-named rows the likelier defect is OUR catalogue colour.

**Resuming killed agents costs ~0 fetches — `SendMessage`, never a fresh spawn.** (Batch 17
survived three process restarts.) A stopped subagent resumes from its own transcript with its
sitemap greps and page fetches intact; a newly spawned agent repeats all of them. Recovery
loop: `ck.sh` → diff `units.json` pids against `recs/` → SendMessage each incomplete unit its
own missing-pid list. This only works because recs are written per product, the moment each
row finishes — never batch rec writes to the end of a unit.

**Numeric-token sitemap leads are frequently FALSE POSITIVES — verify by fetching.**
5-digit Ugreen model numbers collide with jarir's Arabic book product IDs: token `25911`
matched `non-branded-artist-tools-259112.html`; `60820` matched a stech product whose real
model is `HD10460820`. The delimiter-bounded index from D6 reduces this but does not eliminate
it for short numeric tokens. **A sitemap hit is a lead, never a match** — this is where that
rule earns its keep.

**Model numbers often do NOT appear in amazon/noon listing TITLES even when stocked.**
For Ugreen and Ubiquiti the number lives in the spec block ("Item model number" / noon's
"Model Number"). A title-only scan under-reports matches — open the dp/product page for any
promising candidate before writing a gap. A title without the number is not absence evidence;
a spec block with a *different* number is.

**noon brand stores cap at exactly 100 rows.** Batch 16: `ugreen` 100 and `ubiquiti` 100 —
both TRUNCATED, so absence from them proves nothing. `esr` 134 and `lexar` 130 (merged partial
renders) are complete and usable as absence evidence. Restating D6's test because it decided
several legs here: **exactly 100 lines = truncated; under 100 = complete.**

**Cable/adapter families: LENGTH and VERSION are hard keys.** Consecutive Ugreen model numbers
are the same cable in a different length (`40101`/`40102`/`40103` = HD119 2m/3m/5m, confirmed
via stech titles and extra's `modelNumber` JSON). A length mismatch is a gap, not a match.
Likewise HP printer suffixes (`e` = HP+/Instant Ink vs `b` vs plain), Ubiquiti single-unit vs
5-pack and generation (U6≠U7), Apple Studio Display glass (Standard vs Nano-Texture) and stand
(Tilt vs Tilt-and-Height), Lexar CFexpress Type A vs Type B.

**Structural non-matchables to close fast** (batch 16 hit 61/200 rows with a `catalog_issue`,
~30%): WooCommerce **variable products** whose row has an EMPTY sku and spans several lengths
with nothing pinned; rows whose name is pure Arabic boilerplate with no distinguishing spec
(ESR chargers); rows carrying no manufacturer part number at all (generic shredders,
`SHRD-GEN-*`). None of these can be three-way verified — record `catalog_issue` and gap all 13
rather than burning budget or guessing.

## D8. Batch 18 — Ugreen accessory families and APC (measured 2026-08-09)

**Ugreen yield splits hard by family, and it is predictable.** Across 70 Ugreen accessory
rows the same brand behaved like three different brands:
- **Network adapters match well** (up to 5 sites on one row) — they carry a printed `CMxxx`
  model that every retailer reproduces, so the three-way check passes easily.
- **USB hubs match thinly, USB-C docks worse, HDMI adapters not at all** (one whole
  HDMI unit closed all-13 on every row). Twelve dock rows share the *identical* Arabic name
  "محطة توصيل UGREEN USB-C متعددة المنافذ" and differ only by number — there is no name
  signal at all, and KSA retailers stock only a couple of the twelve.
- Practical consequence: **spend the fetch budget on Ugreen adapters, close Ugreen
  hubs/docks fast** once the brand files and amazon/noon/extra come back empty.

**APC/Schneider is the opposite shape and was where this batch's value sat.** APC part
numbers (`SMT1500RMI2UC`, `SRT1000XLI`, `SRTRK4`, `AP9640`) are globally unique, so a bare
search on the part number resolves in one hit and supports `high` immediately. **pcpalace was
the top site of the batch (18 matches) purely on APC** — it stocks ~118 APC URLs and its
title, slug, meta description and JSON-LD product name all agree on the part number, which is
unusually clean for a site the playbook otherwise warns about (see C: pcpalace titles lie).
The accessory rows (battery packs, rail kits, network cards) match on their own part numbers
just as reliably as the UPS units — do not treat them as unmatchable accessories.

**noon's medium cap is over-firing on spec-block matches.** All three validator upgrades this
batch were noon legs capped at `medium` by the "medium unless a brand store is visible" rule
whose pages in fact carried an exact `Model Number` in the spec block (CM195/15214,
CM512/60515, 50737). **An exact model number in noon's own spec block is high-confidence
evidence on its own** — the brand-store check is a fallback for when the spec block is silent,
not a precondition. Applying this at match time would have saved a validator round-trip.

## D9. Batch 19 — colliding product NAMES are not indistinguishable PRODUCTS (2026-08-09)

53 rows in batch 19 were ESR phone/tablet cases whose Arabic names and descriptions are
**byte-identical in pairs and triples** (three separate rows all read
`غطاء ESR MagSafe لهاتف iPhone 17 Pro أسود`). The previous session concluded they were
structurally unmatchable and pre-declared them a "settled duplicate-name group". **That was
wrong, and it would have gapped ~50 legs that exist.**

- **The SKU is the key the name throws away.** Those three rows are `ESR-1A863003`,
  `ESR-1A867003`, `ESR-1A881001`. The part-number *series* (1A863xxx / 1A867xxx / 1A881xxx)
  identifies the ESR case **line** — Classic Hybrid, Classic Hybrid + Stash Stand, Cyber
  Tough — for the same phone and the same colour. Our catalogue name omits the line entirely.
- **noon.com indexes ESR part numbers and prints them in its own spec block.** A plain
  `q=<part number>` search returns the correct listing. This alone resolved **19 ambiguous
  ESR legs directly to `high`**.
- **extra.com's `modelNumber` carries the exact ESR part number** (same field as D7's `mpn`
  rule). It confirmed 4 and **killed 6** legs that had been aimed at a group-mate's listing.
- **amazon.sa is decidable only after the line is named**: get the line from noon/extra, then
  read the dp variant twister. 18 legs upgraded, 4 were genuine gaps (line absent from the
  twister).
- **Order of operations for any colliding-name family: part number on noon → `modelNumber`
  on extra → amazon twister. Never gap on a name collision alone.**

### Resume a validator with cross-site evidence instead of accepting `unresolved`
The amazon validator's first pass returned **23 of 47 unresolved**, every one citing the
duplicate-name file. The extra validator, running concurrently, discovered `modelNumber`.
`SendMessage`-ing that finding back to the amazon validator — with the pid→SKU table and the
instruction to identify the part-number line — took it to **2 unresolved**, with no
re-fetching of pages it had already read. **When one validator's site fact dissolves
another's ambiguity, resume the second one; do not push its `unresolved` list to review.**
This is now the highest-leverage move in the validation step.

### Validation is not only subtractive
Batch 19's validators netted **+37 matched legs** (253 → 290) — 44 upgrades against 14
downgrades and 1 reject. Batches 11 and 17 were net-negative. Budget for validation as
recovery work, not just as a filter.

### Dead-URL check: four batches at zero
Batches 16–19 all returned **0 dead URLs** across every claimed page. The check is still the
first thing a validator does, but it has stopped finding anything — consider sampling it and
spending the fetch budget on ambiguity resolution instead.

## D10. Batch 20 — D9 generalises beyond ESR; validate rare-site legs on purpose (2026-08-09)

Final batch of the phase. The name-collision failure D9 found in ESR **reappeared immediately
in the Dell block**, which is the proof it is not an ESR quirk:

- 64730 and 64728 are both named `ديل Latitude 3440 / 8GB DDR4 / 512GB NVMe — جديد`,
  byte-identical. The only difference lives inside the SKU: `…-I71345U-8-512` vs
  `…-I71355U-8-512-MX` — a different CPU and an added MX550. One matched three sites; the
  other is a real gap.
- **Rule: when two rows share a name, diff their SKUs before you diff anything else.** The
  display name is the least reliable field in the catalogue.
- Distinguish that from a *true* duplicate: 64672 / 64722 are both ThinkPad E14 G7
  `21SX001VAD` and correctly resolve to the same pages on four sites. Record both, flag
  `catalog_issue`, let the push key collapse them.

### Sample EVERY rare-site match, not a blind 10%
Batch 20 produced **afaqalhasoob.com's first match in ~1,450 products**. The validator was
told to try to refute it specifically; it re-fetched cold, found a real Zid product page with
schema.org markup and an agreeing spec block, and kept it at `high`. **afaqalhasoob is thin,
not dead — keep grepping it.** Add every leg on a site that rarely matches (afaq, rowadalahbar,
alshamel, amwajest, rawand, ahbarhd) to the validation set on top of the high sample; those
are the legs where a false positive is both most likely and most damaging.

### noon spec-block under-reading is now a three-batch pattern
Three of batch 20's seven upgrades were matchers asserting noon printed no part number when
the spec block plainly carried the Lenovo MTM (21SX001VAD, 22AY007AAD). Batches 18 and 19 hit
the same thing. **Instruct matchers to grep the rendered noon page for `Model Number` /
`رقم الموديل` before concluding it is absent** — and never let "noon shows no part number"
stand as evidence without that grep.

### Refurbished house-SKU rows are structurally unmatchable
The `RFB-LEN-…` / "وحدة ثانية" Lenovo cluster carries no MTM and no KSA retailer stocks
refurbished Lenovo — verified live across noon/amazon/jarir/extra. Close them all-13 with a
`catalog_issue`; do not burn a per-site budget on them. (Distinct from D3's مجدد rows, which
DO have manufacturer part numbers.)

### Harness reuse is nearly free
Batch 20's harness took ~10 minutes: **symlink** the previous batch's `sitemaps/` and
`brands/` (hours old), copy `leads.py` / `mkunits.py` / `ck.sh` / `apply_verdicts*.py`,
sed the batch number, reassemble BRIEF from the generic head plus a new family block. Do not
re-download sitemaps within the same day.

### `ck.sh`'s shape check paid for itself again
Five rec files came back with every leg wrapped in a single-element list
(`matches: [[{…}]]`). The checkpoint sweep rejected all five; content was intact so they were
unwrapped mechanically instead of re-run, costing zero fetches. **Never loosen that
validation** — without it, five malformed records would have entered state silently.

## G. Known catalog issues (report, don't silently fix)
33941 CF331A→CF332A · OKI 3K PN = 45807119 · 33585/33587 TN-2305 duplicates · 113R00695 =
MAGENTA not yellow (33390) · rowadalahbar template-bug pages unreliable for specs ·
**batch 19: 8 duplicate MacBook Pro 16" catalogue rows** (64943/66767, 64945/66775,
64823/64942, 64936/64951) — same machine twice, one row with an `APL-…` SKU and one without;
both claim the same competitor URL, so the push key collapses them · **ESR rows carry no case
line in the product name** — the SKU series is the only thing distinguishing otherwise
identical rows (see D9).
