# ZERO-MATCH VERIFICATION SWEEP — storage-memory group (BRIEF for matcher agents)

## Mission
Your product previously got **0 matches on all 13 competitors** in the July/August matching
run. VERIFY that gap adversarially: either confirm the product truly does not exist on each
competitor, or find the match the first pass missed. Never invent a match.

## Your product
Read your row from `SCRATCHPAD/todo.json` (`products[]`, find your `product_id`).
Pre-grepped local-catalogue leads: `SCRATCHPAD/leads.json` under your product_id (string
key): `tokens`, `hits` per site (URL + token), `clean_sites` = complete local catalogues
with ZERO hits (that absence is real gap evidence).

`SCRATCHPAD` = /tmp/claude-1000/-srv-crawmatic/97b7b3b3-8e9e-4a69-ba5a-0eeddf1b0a1b/scratchpad

## The 13 competitors — EXACTLY this order in your output
1. noon.com  2. amazon.sa  3. jarir.com  4. extra.com  5. ahbarhd.com  6. stech.ink
7. pcpalace.com.sa  8. fqtoners.com  9. rowadalahbar.com  10. rawand.com.sa
11. amwajest.com  12. afaqalhasoob.com  13. alshamel.sa

Category rules for THIS group (servers / RAM / SSD / HDD / flash):
- Sites 5,8,9,10,11 are ink/toner/office stores — they carry NONE of this. If in your
  `clean_sites`, write `no_match`/`gap` with note "toner/office store + 0 sitemap hits",
  zero fetches. stech (6) likewise toner — its hits are numeric-token noise, slug-eyeball only.
- **Enterprise servers (PowerEdge/ProLiant/T560 etc.)**: jarir/extra/noon do not sell rack
  servers — quick gap with note. Real candidates: pcpalace, afaqalhasoob, alshamel, amazon.sa
  (rare). A server "match" must be the same base model AND the same configuration (CPU SKU,
  RAM, drive count) — server configs are quotes, a different config is NOT a match.
- Consumer RAM/SSD/HDD/flash: candidates are amazon, noon, jarir, pcpalace, afaq, alshamel.
  Part number is king: verify the exact manufacturer part (e.g. KD5AGSA80…, MZ-V9P…) in
  title+slug+description. Capacity/speed/CL/form-factor (SODIMM vs UDIMM, NVMe vs SATA,
  single vs kit-of-2) must all match. A 2x8GB kit is NOT a 1x16GB match.

## Method, in cost order
1. **Local catalogue evidence (free):** `SCRATCHPAD/sitemaps/*.urls` are COMPLETE for the
   ten grep-able sites. Re-grep with extra tokens yourself (delimiter-bounded thinking).
   A sitemap hit is a LEAD, never a match — fetch and verify a rendered spec block.
2. **JARIR WARNING:** jarir slugs end in a NUMERIC catalogue SKU — token greps MISS live
   listings. For anything jarir plausibly carries (consumer SSD/RAM/flash), ALWAYS run
   `https://www.jarir.com/sa-en/catalogsearch/result?search=<query>` even when the grep is clean.
3. **Do NOT use WebSearch** (budget unreliable). Direct site-search URLs via WebFetch, and
   `python3 /home/mahmoud/.claude/skills/match-competitors/scripts/fetch.py "<url>"` via Bash
   when bot-walled:
   - noon:  `https://www.noon.com/saudi-en/search/?q=<query>`
   - amazon: `https://www.amazon.sa/s?k=<query>` (fetch.py works well)
   - extra.com: its search is an Algolia JS app curl can NEVER render — ONE WebFetch try,
     then `error_transient` and move on. Do not burn fetches on it.
4. Budget: ~2 searches + 1-2 page fetches per plausible competitor; 2 consecutive hard
   failures ⇒ `error_transient` for that site and move on.

## STRICT SAME-BRAND / EXACT-PART RULE (non-negotiable)
Match = competitor sells the EXACT same product: same brand, same part/model number, same
capacity/speed/form factor/kit count, single unit, same condition (new vs refurbished/مجدد).
Verify in page TITLE + URL SLUG + DESCRIPTION/specs; any disagreement ⇒ not a match.
Search-results/category URLs are never match URLs. Bundles/kits ≠ single units.
If genuinely the same product but one spec can't be confirmed ⇒ `ambiguous` (goes to review).
Amazon: a REAL Global Store/import signal (live `ifd_price_row`, import-fee text — not the
HTML-comment boilerplate) caps confidence at `medium`.

## Output — LINKS ONLY, NO PRICES anywhere (not even notes)
Write JSON to `SCRATCHPAD/rec-<product_id>.json`:
```json
{
  "product_id": <int>, "sku": "<from todo.json>", "tier": "B",
  "verified_zero": true|false,
  "matches": [
    {"competitor": "noon.com", "status": "matched|no_match|ambiguous|error_transient",
     "confidence": "high|medium|low|gap",
     "competitor_url": "<ONLY when matched — field name MUST be competitor_url, never url>",
     "note": "<1-2 lines evidence, NO prices>", "strategies_tried": [..]},
    ... exactly 13 entries, in the order above ...
  ]
}
```
**VARIANT-FAMILY CHECK (mandatory since 2026-08-10):** the store now supports variable
products. If your product is one config/color/capacity of a family with sibling rows in
todo.json or the catalogue (check `matching-phase2/variant_fix_candidates.json` for your
product_id), add a top-level `"variant_family": "<family key>"` field to your output and
note in the relevant leg when a SIBLING config is what the competitor sells — that sibling
URL is NOT your match, but the note lets consolidation attach it at family level.

`confidence` for absence is ALWAYS "gap". Reply with ONLY one line:
`DONE <product_id> matched=<n> ambiguous=<n> gap=<n> err=<n>`
