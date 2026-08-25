# PLAN — Competitor URL Matching Execution (Phase: Discovery/Matching)

**Date:** 2026-07-20
**Status:** Implemented; scope amended 2026-07-21 (v3) and 2026-07-22 (v4) — see Amendments below

---

## 📍 Status snapshot, 2026-07-27 (matching + the pricing side that consumes it)

**Matching run `matching-2026-07`:** batches **1–4 complete and pushed to production**
(755 products; batch 4 pushed 2026-07-26 via `matching/push/push_batch.sh 4`).
**Batch 5 in progress — 70/200** at the time of writing. Retry queue 99.
Nothing below changes the matching rules; it records the state of the *pricing* side so a
future session doesn't re-derive it.

**Pricing status per site** — full detail in `matching/COMPETITOR_SCRAPE_PROFILES.md`
(human) and the `scrape` blocks in `matching/competitors.json` (machine):

| Sites | Pricing status |
|---|---|
| The 10 Salla/Zid/Shopify/extra sites | ✅ working, direct HTTP + JSON-LD, no proxy |
| jarir.com | ✅ working. The "32 s throttle" from 26 July was **transient** — re-measured 2026-07-27 at ≤0.1 s/page from the scrapers container. **Do not proxy it** (proxy is slower). |
| amazon.sa | ✅ **now working** — real prices via Chromium + residential proxy (first capture 11,592.95 SAR). Per merchant direction 2026-07-27 ("publicly available data"), **amazon.sa alone** is set to `robots_policy = IGNORE_AFTER_APPROVAL`; the other 12 competitors remain `RESPECT`. |
| noon.com | ❌ **price gap** — noon blocks automated clients broadly (direct, proxy on SA/AE/EG/US/no-country, Chromium with and without HTTP/2 — all fail). **Only the jina reader gets through**, returning the correct title *and* price. Pricing noon therefore needs a jina fetch path in the scraper (URL rewrite + regex over markdown) — a real feature, not yet built. Noon **match links are unaffected** and keep being collected normally. |

**Scraper bugs fixed 2026-07-27** (commits `450139c`, `d631f1d`, `7ea5a88`, `42ecfc8`,
plus the two browser-image fixes): policy timeout now reaches Scrapy; the match lock no
longer blocks a retry's DIRECT→PROXY escalation (the proxy had been used 0 times in 173
attempts); sticky proxy sessions are sent; `finalize_jobs`/`recover_stalled_batches` are
scheduled; Chromium is installed where the runtime user can reach it; and the browser
carries a realistic User-Agent. Narrative: `SCRAPER_FIXES_REPORT_2026-07-27.md`.

**Nothing in the matching skill/playbook needed changing** from that work — matching is
links-only and already routes noon and amazon through jina, which remains correct.

---

## ⚠️ Amendment v4, 2026-07-22 (STRICT SAME-BRAND — supersedes the batch-1 brand rule; batch 1 redone)

The first batch-1 run (report: `BATCH_01_REPORT_13SITES.md`) applied a rule that let
a Mint (منت) product be "matched" to the **OEM original** (HP/Canon/Epson genuine)
at `medium` confidence. The merchant rejected this: it mixes brands (Mint recorded
against Canon pages etc.) and the price gap between a Mint compatible and an OEM
original makes those links useless as competitor references.

**New rule (in force for the batch-1 redo and all remaining batches):**

1. **Exact same product only.** A match requires the competitor listing to be the
   same brand, same part number, same color/size/yield, single unit. Mint products
   match ONLY Mint listings. OEM originals and every other aftermarket brand
   (FQ, A.W.D, S-TECH, HD, FMQ, Generic, …) are `no_match`/`gap` — never `medium`.
2. **Three-way verification.** Brand + part number must be confirmed in the page
   **title**, the URL **slug**, and the **description/spec fields** (opaque slugs
   like amazon `/dp/ASIN` shift the burden to title + description). Any
   disagreement between the three ⇒ no match. Search snippets never count.
3. **Search the brand explicitly** (`Mint <part number>`, `منت <model>`) — marketplace
   Brand fields often mislabel Mint as "Generic"; the title is the authority.
4. **Contaminated artifacts quarantined.** The old batch-1 output (878 mostly-OEM
   "medium" links) moved to `matching/archive-2026-07-22-oem-rule/`;
   `matches.master.jsonl` restarted empty; state reset to 0/12 batches. Archived
   URLs (incl. `woo-run-archive/`) are search hints only — never reused as verified.
5. Expect many more gaps (in the old run only 5/200 products had a true Mint listing,
   all on amazon/noon). A gap is the correct answer, not a failure.

SKILL.md, `references/confidence-rubric.md` and `references/playbook.md` were
updated to match. **Batch 1 restarts from zero under this rule.**

---

## ⚠️ Amendment v3, 2026-07-21 (supersedes v2 and conflicting text below; brand rule superseded by v4)

The v2 pivot was based on a mistakenly-uploaded file (a price/quantity list; the
"499 items" came from its numbered rows). The merchant's actual direction:

1. **Products:** our full store catalog (~2,141 published WooCommerce products).
2. **Competitors:** the merchant's links Excel (`مواقع اون لاين (1).xlsx`, 17 rows →
   4 share.google duplicates collapsed, 1 resolved to extra.com = **13 unique
   sites**) → stored as `matching/competitors.json`: noon.com, amazon.sa, jarir.com,
   extra.com, ahbarhd.com, stech.ink, pcpalace.com.sa, fqtoners.com,
   rowadalahbar.com, rawand.com.sa, amwajest.com, afaqalhasoob.com, alshamel.sa.
   All scripts (prepare_batches, validate_urls) read the competitor list from there —
   nothing is hard-coded to amazon/noon/jarir anymore.
3. **Task:** for EACH product, search on ALL competitors and record the direct,
   canonical product-page URL per competitor. **Links only, no prices.**
4. **Pacing:** batches of **200 products**, run steadily batch-by-batch (**12
   batches** generated for the 2,141 products), one per session.
5. Matcher-agent budget per product: ~2 searches + 1-2 fetches per competitor;
   `site:<domain> <identifier>` searches are the primary strategy for smaller sites.
   Skip obviously-irrelevant site/product combos fast (toner stores don't carry
   laptops; extra.com doesn't carry toner refills).
6. Batch-01 of the original 3-competitor run (149 verified amazon/noon/jarir URLs)
   stays archived at `matching/woo-run-archive/` and its URLs are reused when those
   products/sites come up.

Status: competitors.json created, 12 batches of 200 generated, state initialized —
**ready to run batch 1**. The v2 sheet-matching work is set aside under
`matching/_ignore-price-sheet/`.

---

## Amendment v2, 2026-07-21 — OBSOLETE (based on wrong file, kept for history)

Per the merchant's direction after the batch-01 pilot:

1. **The match target is now the competitor's Excel catalog, not web search.**
   The merchant supplied a competitor inventory sheet —
   `matching/قائمه الاسعار والكميات 2 (2).xlsx` (~499 numbered items: toner/ink
   part-number families like `CE505A/280A/CRG719`, Epson dye inks, Brother/Canon/
   Samsung/Toshiba/Kyocera/copier toners with EAN barcodes on the Canon rows, Kobra
   shredders, HP/Brother printers, gaming chairs/accessories). The task: for each of
   **our ~2,141 store products**, find its matching item **inside this sheet**.
   The per-product amazon.sa/noon/jarir web-search workflow is **parked**; its
   batch-01 output (149 verified URLs) is archived intact at
   `matching/woo-run-archive/` and the machinery can be revived for future
   web-competitor runs.
2. **No price capture.** The sheet's qty/price columns are ignored; the deliverable
   is the product↔sheet-item mapping only.
3. **Method changed from agent-web-search to deterministic catalog join**
   (`scripts/match_sheet.py`): normalized part-number/token join between our products'
   MPN candidates (from the archived batch files) and the sheet's code/name tokens,
   with layered disambiguation — longest-token ranking, BK/C/Y/M color tie-break
   (Arabic color words ↔ sheet suffixes), Mint-vs-OEM section preference,
   duplicate-row collapse, multi-color kit (طقم) fan-out to whole color families,
   exact-full-code joins (`CH-106 BK` → `CH106BK`), vendor-prefix stripping
   (`TA-T-FC50P-C`), and weak-token (≤3 char) demotion. Runs locally in seconds,
   zero web/LLM cost.
4. **Results (first full run):** 347 matched (328 high / 19 medium), 0 ambiguous,
   1,794 of ours unmatched (the sheet covers only toner/printer/gaming categories),
   217/499 sheet items covered. Outputs: `matching/sheet-matches.csv` (+ `.jsonl`),
   `matching/sheet-unmatched-ours.csv`, `matching/sheet-uncovered.csv`.
5. **Optional LLM cleanup stage (pending merchant go-ahead):** ~833 unmatched
   products are ink/toner and *could* contain fuzzy matches the token join missed —
   a small agent pass (whole sheet in context + chunks of unmatched products) can
   sweep those. The remaining ~960 unmatched (laptops/phones/servers/monitors…) have
   no counterpart in this sheet by construction.

The sections below describe the original (now parked) web-search architecture and
remain valid for future web-competitor runs.

---
**Scope:** Find the correct competitor product URL on amazon.sa, noon.com, jarir.com for every published Mushtryati product (~2,141 of 2,424 in `mushtryati_products.json`), using batched fresh Claude Code sessions driven by a dedicated Skill.

This is the "Automatic product matching later" phase that PROJECT_SPEC §1/§38 deliberately deferred. It runs **outside** the crawmatic runtime (Claude sessions + scripts), and feeds its output into the existing runtime via the already-built bulk-upsert path. Nothing in `apps/` or `libs/` changes in this phase except optional additive helpers.

---

## 1. Ground truth (verified against the repo)

| Fact | Source |
|---|---|
| 2,424 products, 2,141 `publish` / 283 `draft`; 97% have SKU | `mushtryati_products.json` |
| SKUs are mostly internal-but-MPN-derived (`3JA30AE-1`, `samsung-galaxy-tab-a11plus-5g-128`) | same |
| Catalog skew: ~1,000+ ink/toner cartridges; laptops 157, printers 154, gaming 148, accessories | same |
| Names are Arabic; **slugs are English and carry brand+model** — best single search key | same |
| Output schema + confidence vocabulary (`high`/`medium`/`gap`) already exist | `mushtryati_product_matches.json` `_meta`, `specs/005-competitors-matches/data-model.md` |
| Write path exists: `POST /v1/matches/bulk-upsert` → `libs/shared/app_shared/matches/upsert.py` | 005 contract |
| Every URL must pass `libs/shared/app_shared/url_safety.py::validate_competitor_url` | PROJECT_SPEC §23 |
| No match-confidence column — fixture convention stores it in `competitor_variant_options.confidence` | MUSHTRYATI_SCRAPE_TEST_REPORT.md |
| Amazon URLs canonicalize to `https://www.amazon.sa/dp/{ASIN}`; Noon key is the `N…V` code; Jarir slug ends in a numeric/`jpmNNNN` product id | fixture `_meta` note |
| No search APIs configured in repo; Claude sessions have WebSearch/WebFetch + Apify MCP | API-SETUP.md, session tools |

**Decisions baked into this plan:** match published products only (drafts get a later mini-run); matching happens at product level with variant options recorded (`competitor_variant_options`) exactly as the fixture does; upload to prod is a separate, human-gated step at the end — during execution everything lives in local JSON.

---

## 2. Batching strategy

**Recommendation: 50 products per session-batch (not 200), tier-adjusted: 75 for easy tiers, 30 for hard tiers.**

Why 200 is wrong: the main session never sees sub-agent transcripts, but it does hold, per product: the input row (~80 tokens), the returned compact match record for 3 competitors (~250–400 tokens), and orchestration/checkpoint overhead. At 200 products that is 80–100k tokens of *irreducible* results context before any reasoning, retries, or validation escalations — one ambiguous product cluster would push the session into compaction mid-batch, which is exactly what checkpoint-based session handoff is designed to avoid. At 50 (easy: 75), a batch closes comfortably in ~30–40% of a context window with headroom for retries and validation.

- Batch = a slice file generated **once** up front by a prep script (deterministic, reproducible): `matching/batches/batch-NNN.json`.
- Each batch runs in a **fresh session**; continuity comes only from `matching/state.json` + accumulated results, never chat history.
- ~2,141 products → roughly **34 batches** (see tier table below). One batch ≈ one session ≈ 50 sub-agent fan-outs at concurrency 6–8.

## 3. Product grouping

**Hybrid: complexity tier first, then category/family, then brand-sorted inside each batch.**

| Tier | Contents (by category) | ~Count | Batch size | Why |
|---|---|---|---|---|
| **A — templated** | Ink/toner cartridges (احبار*), paper, basic accessories | ~1,300 | 75 | Match key is a hard identifier (HP 963XL / 3JA28AE, CF217A…). Search is formulaic, variants rare. → ~18 batches |
| **B — model-line** | Printers, monitors, networking, gaming peripherals | ~450 | 50 | Model numbers exist but bundles/regional variants need checking. → ~9 batches |
| **C — variant-hell** | Laptops, phones, tablets, consoles | ~390 | 30 | RAM/storage/color/connectivity reconciliation (the fixture's MacBook/Tab A11+ cases). → ~13 batches |

Brand-sorting inside a batch means a matcher agent working product #14 (HP 963XL Cyan) benefits from the coordinator's playbook notes right after #13 (HP 963XL Magenta), and duplicate detection catches near-identical listings early. Category purity keeps the per-batch playbook (search recipes) short and loaded once.

Tier assignment is deterministic in the prep script from `categories[].name` (with a small keyword map for the ~5% that need it), and recorded per product in the batch file so nothing is recomputed at run time.

## 4. Agent workflow

Flatter than the proposed 6-layer chain — "Progress Agent" and "Persistence Agent" must NOT be LLM agents; checkpointing is deterministic script work, and putting it behind a model adds failure modes. Final architecture:

```
Session Orchestrator  (main session, runs the match-competitors Skill)
 ├─ state.py CLI ................ load/claim batch, checkpoint, complete   [script, not agent]
 ├─ Matcher agents (fan-out, 6–8 concurrent, one PER PRODUCT, all 3 competitors)
 ├─ Validator agent (one per batch tail; adversarial re-check of medium/low/ambiguous)
 └─ validate_urls.py ............ SSRF/url_safety gate + normalized-URL dedup  [script]
```

**Session Orchestrator (the Skill):** loads `state.json`, claims next batch (or resumes a partial one), reads the batch file and tier playbook, fans out matcher agents in waves, appends each returned record to the results file **as it arrives** (checkpoint = every product), triggers validation, runs the dedup/URL gate, marks the batch complete, prints the handoff summary. It never opens competitor pages itself.

**Matcher agent (per product):** receives ONE product (id, sku, slug, English-normalized title, Arabic name, brand, category, price, tier playbook excerpt) and returns ONE compact JSON record for all three competitors. One agent per product — not per product-per-competitor — because product understanding (what model is this? which specs pin the variant?) is the expensive part and is shared across competitors; it also lets the agent cross-check competitors against each other (price sanity, title agreement). Tool ladder inside the agent: WebSearch → competitor on-site search via WebFetch → **proxy fetch** (`scripts/fetch.py`, DataImpulse SA residential proxy from `crawmatic/.env` — Amazon/Noon regularly bot-wall plain fetches and geo-gate prices) → Apify `rag-web-browser` as last resort. Hard output contract, no prose.

**Validator agent (per batch):** receives only the batch's `medium` + `ambiguous` + `low-confidence` records (typically 5–15) and adversarially verifies: fetch the claimed URL, confirm brand + identifier/spec string, resolve multi-candidate cases, upgrade/downgrade confidence, or mark `needs_human_review`. `high` records with an exact-identifier evidence string are spot-checked at 10%, not re-verified wholesale.

**Scripts (deterministic):** `state.py` (atomic tmp+rename JSON writes, batch claim/heartbeat/complete), `validate_urls.py` (calls `validate_competitor_url` + `derive_match_url_fields` from `libs/shared` via `uv run`, and cross-product duplicate detection on `normalized_competitor_url`), `prepare_batches.py` (one-time), `report.py` (stats + bulk-upsert payload export).

## 5. Sub-agent model rotation

| Role | Model | Rationale |
|---|---|---|
| Session Orchestrator | Whatever the session runs (Opus default) | Cheap per-batch; mostly delegation + bookkeeping |
| Matcher — Tier A | **Sonnet** | Identifier-driven search; recipe is mechanical; ~60% of all products — this is where the cost saving lives |
| Matcher — Tier B | **Sonnet, escalate to Opus on failure** | First pass Sonnet; if it returns `ambiguous`/`no_match`, the orchestrator re-runs that one product with an Opus matcher before queuing a retry |
| Matcher — Tier C | **Opus** | Variant reconciliation and cross-competitor spec matching is where Sonnet produces plausible-but-wrong matches |
| Validator | **Opus, always** | Adversarial verification is the accuracy backstop; never economize here |

Mechanically: the Agent tool's `model` parameter (`sonnet`/`opus`), chosen from the product's tier recorded in the batch file. The Tier-B escalation doubles as an automatic quality ratchet without paying Opus prices for the easy majority.

## 6. Matching strategy (lookup order per product)

Executed by the matcher agent, stopping at the first strategy that yields a verified candidate:

1. **Extracted MPN / hard identifier** — regex the SKU, name, and slug for manufacturer part numbers (`3JA28AE`, `CF217A`, `MX2U3`…). Search each competitor for the bare identifier. This resolves most of Tier A instantly.
2. **SKU as-is** — only when it looks like a real MPN (internal suffixes like `-1` stripped).
3. **Brand + model line** (`HP 963XL magenta`, `Samsung Galaxy Tab A11+ 5G 128GB`) — English, from slug.
4. **English title** — use the product `name` directly when it is already English (~860 of 2,141 are); only fall back to the de-hyphenated slug when the name is Arabic. Precomputed per product as `english_title` in the batch file — never normalize first.
5. **Arabic name search** — amazon.sa and noon both index Arabic; useful for local-market items where English recall fails.
6. **Fallback: web search with site restriction** (`site:jarir.com 3JA28AE`, or `site:noon.com <english_title>`) — same title rule as #4: name-if-English else slug. Often beats on-site search, especially Jarir's.
7. **Candidate verification** (mandatory, not a fallback): open/inspect the top candidate, confirm brand AND (identifier match OR full spec-string match incl. variant attributes: capacity/color/RAM/storage/connectivity/yield). Canonicalize the URL (`/dp/{ASIN}`, Noon `N…V` code, Jarir product id). Capture the competitor's displayed price as `observed_price` (SAR) while on the page — some competitors only reveal price on the product page, and it doubles as a sanity check against our price.

**Confidence scoring** — keep the repo vocabulary and add a numeric score for tooling:
- `high` (≥0.9): exact identifier (MPN/ASIN/SKU) confirmed on the competitor page.
- `medium` (0.6–0.89): same model line confirmed, one variant attribute unconfirmed or bundled behind a variant selector (record what differs in a `note`, and `competitor_variant_options`).
- `low` (<0.6): title similarity only — never persisted as a match; goes to the validator, then retry/human review.
- `gap`: no listing after all strategies — recorded explicitly (it is a deliverable, not a failure).

**Duplicate detection** — two layers: (a) inside the record, an agent may not return the same URL for two different variant options; (b) batch- and corpus-wide, `validate_urls.py` groups all accumulated matches by `normalized_competitor_url` — the same competitor URL claimed by 2+ different source products is auto-flagged `needs_human_review` unless both sides are marked as the same variant group. This mirrors the runtime's `uq_cpm_ws_variant_competitor_norm_url` arbiter, so nothing we produce can collide at upsert time.

## 7. Progress tracking & session handoff

New directory **`/srv/crawmatic/matching/`** (outside the git repo's runtime code; the skill and scripts live in the repo):

```
matching/
  state.json                  # single source of truth for handoff
  batches/batch-001.json …    # immutable input slices (prep script, run once)
  results/batch-001.results.json …   # per-batch output, appended per product
  matches.master.jsonl        # append-only accumulation (1 line = 1 product record)
  retry-queue.json            # {product_id, competitor?, reason, attempts, last_error}
  needs_human_review.json
  logs/session-*.md           # one handoff note per session (freeform, short)
```

`state.json` shape:

```json
{
  "run_id": "matching-2026-07",
  "batches": {"total": 34, "completed": [1,2,3], "in_progress": 4, "remaining": [5,"…"]},
  "current_batch": {"id": 4, "claimed_at": "…", "session": "…", "done_product_ids": ["…"],
                     "last_checkpoint": "…"},
  "stats": {"products_done": 163, "high": 118, "medium": 27, "gap": 12, "review": 6,
             "per_competitor": {"amazon.sa": {"matched": 130, "gap": 8}, "noon.com": {}, "jarir.com": {}}},
  "retry_queue_size": 9
}
```

Rules: every write is atomic (tmp+rename) via `state.py`; checkpoint after **every product** (result appended + `done_product_ids` updated); batch completion moves the id to `completed`, folds stats, and clears `current_batch`. A fresh session needs to read exactly one file to know everything: which batch is next (or half-done), what's in the retry queue, and cumulative stats. If `current_batch.claimed_at` is stale (> 2h), the batch is treated as abandoned and resumed from `done_product_ids`.

## 8. Retry strategy

Per product-competitor outcome → routing:

| Outcome | Meaning | Action |
|---|---|---|
| `matched` (high/medium) | verified URL | persist |
| `error_transient` | search/fetch failure, rate limit, bot wall | retry queue, `attempts+1`; backoff by rotating modality (WebSearch → WebFetch → Apify). Max 3 attempts across sessions, then → `permanent_gap` with reason |
| `no_match` | all strategies exhausted, page genuinely absent | record `gap` after strategies 1–6 tried (agent must list which it tried); one Opus re-attempt for Tier B/C before finalizing |
| `ambiguous` | 2+ plausible candidates | → Validator (Opus) same session; unresolved → `needs_human_review` with both URLs |
| `low_confidence` | title-similarity only | → Validator; unresolved → retry queue once with Opus, then human review |
| URL fails safety gate | `validate_competitor_url` rejects | never persisted; flagged, agent re-derives canonical URL once |

Retry execution: each new session drains up to 15 retry-queue items **before** claiming a fresh batch (they're cheap — one agent each). Rate-limit storms (many transient errors in one wave) → orchestrator halves concurrency for the rest of the batch instead of burning attempts.

## 9. The Claude Code Skill

**Location:** `crawmatic/.claude/skills/match-competitors/` (alongside the speckit skills). Invoked as `/match-competitors` in any fresh session from `/srv/crawmatic/crawmatic`.

```
match-competitors/
  SKILL.md                    # orchestration procedure (the prompt)
  scripts/
    prepare_batches.py        # one-time: products JSON → tiers → batches/ + state.json
    state.py                  # claim | checkpoint | complete | status | retry-pop  (CLI)
    validate_urls.py          # url_safety gate + normalized-URL corpus dedup
    fetch.py                  # competitor page fetch through the DataImpulse SA proxy (verification + price)
    report.py                 # stats; --export-upsert → bulk-upsert payloads
  references/
    playbook.md               # per-competitor search recipes + per-tier notes + canonical URL forms
    confidence-rubric.md      # high/medium/low/gap definitions + worked examples from the gold fixture
    output-schema.json        # matcher agent output contract (JSON Schema)
```

**Inputs:** none required — fully state-driven. Optional args: `/match-competitors status` (report only), `batch N` (force a batch), `retry` (drain retry queue only), `review` (present `needs_human_review` items interactively).

**SKILL.md procedure (summary):**
1. `state.py status` → if no state, tell the user to run `prepare_batches.py` first (never auto-generate batches mid-run).
2. Drain ≤15 retry items (matcher agents, model per tier rules).
3. Claim next/partial batch; load only its batch file + the tier's playbook section.
4. Fan out matcher agents in waves of 6–8 (Agent tool, `model` from tier; strict output contract from `output-schema.json`). On each return: validate shape, `state.py checkpoint`.
5. Tier-B failures → one Opus re-run. Collect medium/ambiguous/low → Validator agent (Opus) + 10% spot-check of highs.
6. `validate_urls.py` over the batch results (safety gate + corpus dedup) → route rejects.
7. `state.py complete` → fold stats; write `logs/session-N.md` handoff note; print summary (matched/gap/review counts, next batch id, retry-queue size).
8. If context usage is still low and the user asked for a long run, claim the next batch and repeat; otherwise stop cleanly — the next session picks up from `state.json` alone.

**Recovery:** because checkpointing is per-product and atomic, a killed session loses at most one in-flight product. The skill's step 1 detects a stale `in_progress` batch and resumes it from `done_product_ids` without any conversation history.

**Output contract (matcher agent → orchestrator), fixture-compatible:**

```json
{ "product_id": 12041, "sku": "3JA28AE-1", "tier": "A",
  "matches": [
    {"competitor": "amazon.sa", "status": "matched", "competitor_url": "https://www.amazon.sa/dp/B07XLGD9GX",
     "competitor_variant_identifier": "B07XLGD9GX", "external_title": "HP 963XL Magenta Original Ink",
     "competitor_variant_options": {}, "confidence": "high", "confidence_score": 0.95,
     "observed_price": 152.0, "evidence": "MPN 3JA28AE shown on page", "strategies_tried": [1]},
    {"competitor": "jarir.com", "status": "no_match", "confidence": "gap", "strategies_tried": [1,3,4,6]}
  ]}
```

## 10. Hand-off to crawmatic (after the run, separate step)

`report.py --export-upsert` converts `matches.master.jsonl` (high + accepted-medium only) into `POST /v1/matches/bulk-upsert` payloads keyed by `variant_sku`/`variant_external_id`, confidence stashed in `competitor_variant_options.confidence` per existing convention. Chunked ≤200 per request. This step is human-triggered and reviewed — never automatic. (Optional future: a real `match_confidence` column via a small 005 amendment; not this phase.)

## 11. Implementation order (next step, not now)

1. `specs/017-competitor-url-matching/` via `/speckit-specify` referencing this doc (keeps Spec Kit discipline).
2. `prepare_batches.py` + `state.py` + schema files → run prep, eyeball 3 batch files.
3. SKILL.md + playbook/rubric (seed playbook from the gold fixture's 7 products).
4. **Pilot: batch-001 (Tier A, 20 products only)** → measure accuracy vs. manual check, token cost, wall time → tune batch size/concurrency.
5. Full run (~34 sessions), review queue triage as it accumulates, then `--export-upsert`.
