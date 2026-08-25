---
name: match-competitors
description: Match our store catalog against competitor catalogs. ACTIVE MODE (2026-08-10) = ZERO-MATCH VERIFICATION SWEEP - re-verify the 1,552 products that got 0 matches in the original 20-batch run, group by group in value order, Mint excluded; laptops group is CLOSED & PUSHED (15 recovered, prod on the NEW Railway project at products=3494/matches=4547); storage-memory is IN PROGRESS (16/155 done, all gaps so far); invoking with no args continues the sweep at the next unfinished group. Original 20-batch matching is complete. STRICT SAME-BRAND rule; links only, no prices; matcher output field MUST be competitor_url (never url - the payload builder silently drops url). Store now has variable products - check matching-phase2/variant_fix_candidates.json (282 zero rows gain instant coverage via matched siblings). Args: none|sweep [group] | status | batch N | retry | review.
---

# match-competitors — batched competitor URL matching orchestrator

You are the **Session Orchestrator** for the matching run described in
`/srv/crawmatic/crawmatic/PLAN_COMPETITOR_MATCHING_EXECUTION.md` (see its
Amendment v3 2026-07-21 for scope and Amendment v4 2026-07-22 for the
strict same-brand rule). You delegate all web work to sub-agents and never
open competitor pages yourself. Continuity comes only from state files — never from
chat history.

## ACTIVE MODE — ZERO-MATCH VERIFICATION SWEEP (2026-08-10). READ THIS FIRST

The original 20-batch run is complete and pushed. The merchant's standing instruction is now:
**verify every product that got 0 matches, group by group, highest total list value first,
and close each one — Mint (منت) products are EXCLUDED** (598 expected gaps under the
same-brand rule; do not touch them without an explicit ask).

**State lives in `/srv/crawmatic/matching-phase2/zero_match_groups.json`** — the `groups`
key holds the 13 work groups (product lists sorted by price desc), the `sweep_status` key
records which groups are closed. Human-readable mirror: `ZERO_MATCH_GROUPS.md`.

**Status right now:**
- **laptops (638k SAR, 161): CLOSED & PUSHED.** 141 true gaps, 15 recovered (18 validated
  legs, live in prod), 7 ambiguous → review files, 147 retry legs in
  `$DATA/zero_sweep_retry_legs.json` (123 = extra.com, see trap below). Records:
  `$DATA/zero_sweep_records/`, session log `logs/session-zero-sweep-laptops.md`.
- **storage-memory (607k, 155): IN PROGRESS — 16 done (all true gaps: enterprise
  servers/SAS SSDs are quote-only).** Continue from the top of its todo, skipping done recs.
- Then: components (201k, 46) → ink-toner non-Mint (86k, 86) → peripherals (64k, 101) →
  other → cables-adapters (234) → office-supplies (79) → displays (37) →
  phones-accessories (21) → printers (7) → networking (3).

**PROD moved to a NEW Railway project on 2026-08-10** (same workspace_id `c23797f7-…`,
link lives in /srv/crawmatic/crawmatic). Verified after re-push: products=3494,
**matches=4547**. Match table is `competitor_product_matches`.

### Sweep procedure (what actually worked for laptops — follow it)
1. Rebuild the per-group harness in the session scratchpad:
   sitemaps via playbook STEP 0 (all TEN catalogues), `scripts/leads.py` (repoint BATCH to
   the group todo), a `todo.json` of the group's products sorted by price desc, and a
   `BRIEF.md` from **`references/zero-sweep-BRIEF-template.md`** — update its SCRATCHPAD
   path and the category rules section for the group.
2. Spawn matcher agents **SYNCHRONOUSLY in waves of 4** (`run_in_background: false`).
   Background waves get killed by session interruptions with zero records written — this
   happened twice. One agent per product, sonnet, ~10-line prompt: product_id, the
   model-specific traps, sibling rows, "read BRIEF.md", output path `rec-<pid>.json`.
3. After each wave: validate 13 entries per rec, queue `error_transient` legs into
   `retry_legs.json`, launch the next 4. Track matches found for the validator pass.
4. When the group's todo is empty: one **opus validator** re-opens EVERY matched leg
   (adversarial: render check, title+slug+description agreement, condition, Amazon
   import-signal check on live `ifd_price_row`) and writes keep/downgrade/reject verdicts.
5. Apply verdicts, then write back: append full corrected records to the right phase's
   `matches.master.jsonl` (append-only, last-wins), update the affected
   `results/batch-NNN.results.json` in place, append ambiguous legs to
   `needs_human_review.json`, update `sweep_status` in zero_match_groups.json, write
   `logs/session-zero-sweep-<group>.md`.
6. Build + verify the push, hand the `push_batch.sh` commands to the user (human-gated);
   when they paste them back, run them and check `final counts` moves by exactly the
   number of new legs.

### Sweep traps (each of these cost real time — do not relearn them)
- **Matcher output field is `competitor_url`, NEVER `url`** — `build_batch_payload.py`
  silently drops rows named `url`; totals-not-moving after a push is the tell.
- **jarir slugs end in numeric catalogue SKUs** — token greps MISS live listings. ALWAYS
  also run `https://www.jarir.com/sa-en/catalogsearch/result?search=<model>` for anything
  jarir plausibly carries. This alone recovered 3 laptop matches.
- **Do not rely on WebSearch** (200/session budget dies mid-group). Direct site-search
  URLs via WebFetch + `scripts/fetch.py` for bot-walls: noon `/search/?q=`, amazon `/s?k=`.
- **extra.com search is an Algolia JS SPA** — curl-class fetches can NEVER render it. One
  try, then `error_transient`. Its retry backlog needs a one-time structural fix (Algolia
  API endpoint or rendering fetch), not per-product retries.
- **Refurbished rows (RFB-/REF-/renewed/مجدد) are ~always true gaps** — competitors sell
  new only; check amazon Renewed for exact config, then close. 108 of 141 laptop gaps.
- **Amazon Global Store**: only a LIVE `ifd_price_row`/import-fee text caps to medium —
  the HTML-comment boilerplate does not.
- **VARIANT FAMILIES (store now has variable products):** check
  `$DATA/variant_fix_candidates.json` — 473 zero rows sit in multi-config families and
  **282 gain instant coverage from an already-matched sibling**. Agents must emit a
  `variant_family` field and note sibling-config matches (sibling URL ≠ this row's match).
  After any Woo consolidation, audit match external_ids — variation re-keying silently
  kills price sync (see push-variant-backfill trap).
- Missing-laptops "why" report (artifact) source: scratchpad `laptop_gaps_classified.json`;
  gap-reason classes: refurbished 74%, config-variant, sparse-row, old-model, CTO.

## Background — original 20-batch run (COMPLETE)

**Where the run actually is (updated 2026-08-09, after batch 20):**
- **Phase 2 is FINISHED — 8/8 batches, all 1,398 products.**
  `/srv/crawmatic/matching-phase2`, run id `matching-2026-08-phase2`, source
  `matching/new_products_2026-08-08.json` (the products the 12-batch phase-1 run never
  covered). Batch ids ran 13–20. `state.py claim` has nothing left to hand out — **do not
  generate new batches**; if more products need matching, that is a new run and a merchant
  decision.
- Phase 1 (`/srv/crawmatic/matching`) is also complete — 12/12, 2,096 products. Its own
  retry queue (160) and review file (54) are still real work; only touch them if asked.
- **Phase-2 retry queue: 27** (batch 19's bot-walled jarir/extra legs, one item per
  product+competitor). `needs_human_review.json`: **111**.

**PUSH STATUS — ALL batches 13–20 are in prod (17/18/19/20 pushed 2026-08-09).**
Verified final counts after the batch-20 push: **products=3494, matches=4239.**
- 17: +200 products / +80 matches. 19: +200 / +284 (payload 290 — 6 rows collapsed on
  duplicate catalogue URLs). 20: +108 / +89 (payload 93 — 4 collapsed, the ThinkPad E14 G7
  `21SX001VAD` pair on four sites). All `STATUS 200`, all variant backfills `UPDATE <n>`.
- **18 was a no-op re-push** — 100 products and 63 matches upserted but the totals did not
  move, meaning an earlier session had already pushed it and the session-18 log's
  "NOT pushed" note was wrong. This is the idempotency check doing its job: trust
  `final counts`, not a log's claim about what was pushed.
- Batch 16's scrubber-garbled titles are still in prod; re-pushing 16 would refresh them.

**REMAINING BACKLOG, in value order:**

**DEFERRED WORK — the batch-20 gate has now passed, so these are unblocked:**
1. **`$DATA/AMAZON_GLOBALSTORE_RECHECK.json`** — 35 "strong" + 11 "weak" amazon legs across
   batches 2–16 whose confidence was capped at `medium` citing "Global Store", which turns
   out to be boilerplate inside an HTML comment on nearly every dp page (playbook D7). The
   file carries the product ids, the method, and which ones also cite a *real* import signal.
   Re-open each dp, grep for `ifd_price_row` / `رسوم الاستيراد`: absent + specs still agree
   ⇒ upgrade to high; present ⇒ keep medium. Apply with an `apply_verdicts.py`-style script,
   **never `state.py checkpoint`** (it no-ops on already-checkpointed products — playbook E2).
   Re-push the touched batches afterwards.
2. **PN-first noon re-sweep of batch 13's ~90 Mint noon gaps** — those rows were searched
   before we learned noon's Mint slugs omit the brand (playbook D4). Highest-value recovery.
3. **The 27-leg phase-2 retry sweep** (`state.py retry-pop`) — batch 19's jarir/extra legs
   lost to one bot-walled window; cheap coverage recovery.

**Before doing anything: read `$DATA/logs/session-<latest>.md`.** Each batch's log
carries the per-site facts and catalogue defects that batch discovered. Skipping it is
how the same mistake gets made twice.

Match each product against the **13 competitor sites in `$DATA/competitors.json`**
(noon, amazon.sa, jarir, extra, ahbarhd, stech.ink, pcpalace, fqtoners, rowadalahbar,
rawand, amwajest, afaqalhasoob, alshamel): find the direct, canonical product-page URL
on EACH competitor. **Links only — NO price capture.** Batches of **200 products**, run
steadily one batch per session ("not that rush") — no parallel batch racing.

**Build the cheap-close harness before spawning any agent — playbook section D6.**
`sitemaps/` (ten complete local catalogues) + `leads.py` + `brandmap.py`. On batch 16
only 59 of 200 rows had any local hit at all, so the other ~1,940 local legs closed on
catalogue evidence with zero fetches. The batch-15/16 scripts are reusable verbatim —
find them under a previous session's scratchpad and just repoint `BATCH`.

Per-product matcher agents return **one `matches[]` entry per competitor in
competitors.json order** (13 entries). Keep the per-site budget tight: ~2 searches +
1-2 page fetches per competitor; `site:<domain> <identifier>` web searches are the
primary strategy for the smaller stores; mark a competitor `no_match`/`gap` quickly
when nothing surfaces, and skip obviously-irrelevant site/product combos (toner
stores don't carry laptops; extra.com doesn't carry toner refills). The playbook has
per-site sections for all 13.

**STRICT SAME-BRAND RULE (2026-07-22 — supersedes the old "OEM original = medium"
rule).** A match means the competitor sells the **exact same product: same brand,
same part number, same color/size/yield, single unit**. A Mint (منت) product matches
ONLY a Mint listing. An HP/Canon/Epson OEM original is **NOT a match** for a Mint
compatible — recording OEM pages against Mint products is exactly the brand-mixing /
price-gap failure that forced batch 1 to be redone. Verification is three-way: the
brand and part number must be confirmed in the page **title**, the URL **slug**, and
the product **description/spec fields** (when the slug is opaque — e.g. amazon
`/dp/ASIN` — title + description carry the burden). Any disagreement between the
three (title says one brand/color, description says another) ⇒ not a match; push to
review only if the page genuinely appears to be the same brand and product.

Archived results from earlier runs (`$DATA/woo-run-archive/`,
`$DATA/archive-2026-07-22-oem-rule/`, `batch1-archived-results.json`) were verified
under the OLD rule and are **contaminated with cross-brand OEM matches — do NOT
reuse them as verified**. They may serve only as search hints; every URL must be
re-verified under the strict same-brand rule before being recorded.

---

Paths:
- `SKILL_DIR` = `/srv/crawmatic/crawmatic/.claude/skills/match-competitors`
- `DATA` = `/srv/crawmatic/matching-phase2`  ← **the ACTIVE run (batches 13–20)**
- **`export MATCH_DATA_DIR=/srv/crawmatic/matching-phase2` before EVERY `state.py`,
  `validate_urls.py` and `push_batch.sh` call.** All three default to
  `/srv/crawmatic/matching`, which is the COMPLETED phase-1 run. Forgetting this is the
  easiest way to waste a session: `state.py status` cheerfully reports "12/12 done" for a
  run that finished 2026-08-08 and hands you phase 1's stale retry queue. (Cost ~30 min
  on 2026-08-09 before phase 2 was noticed.)
- `PHASE1` = `/srv/crawmatic/matching` — finished (12/12, 2,096 products). Its retry
  queue still holds **160** items and `needs_human_review.json` **54**; both are real
  work, but only touch them if the user asks for phase 1 explicitly.
- Push scripts live in `PHASE1/push/` regardless of run — pass `MATCH_DATA_DIR`, e.g.
  `MATCH_DATA_DIR=$DATA bash /srv/crawmatic/matching/push/push_batch.sh 17`.
- State CLI: `MATCH_DATA_DIR=$DATA python3 $SKILL_DIR/scripts/state.py <cmd>`

## Arguments

- *(none)* — **continue the ZERO-MATCH SWEEP**: read `zero_match_groups.json` sweep_status,
  pick the first unfinished group (storage-memory as of 2026-08-10), rebuild the harness,
  and run waves per the Sweep procedure above. Do NOT claim original-run batches.
- `sweep [group]` — same, but force a specific group (e.g. `sweep peripherals`).
- `status` — print sweep_status per group + retry/review counts + prod totals. Stop.
- `batch N` — original-run mode: force batch N (legacy; batches are all complete).
- `retry` — drain the ORIGINAL run's retry queue only (the sweep's extra.com legs need the
  structural Algolia fix instead — see traps).
- `review` — walk `$DATA/needs_human_review.json` with the user interactively (now
  includes the sweep's ambiguous legs, incl. the 3 duplicate Latitude 7430 rows that must
  be catalogue-deduped/varianted before any re-push).

## Procedure

### 0. Load state
Run `state.py status`. If it errors (no state), tell the user to run
`python3 $SKILL_DIR/scripts/prepare_batches.py` and stop — never auto-generate batches
mid-run. If a current batch exists (stale or not), you will resume it: `claim` returns
only the products not yet checkpointed.

### 1. Drain retries (≤15)
`state.py retry-pop --max 15`. For each item, spawn a matcher agent (model per tier
rules below; failed Tier-B items escalate to Opus). Checkpointing of retry results:
if the product's batch is already completed, append the corrected record manually to
`$DATA/matches.master.jsonl` and note it in the session log; otherwise checkpoint
normally. Items that fail again: `state.py retry-push` (auto-routes to human review
after 3 attempts).

### 2. Claim the batch, then build the batch harness (do this BEFORE any agent)
`state.py claim` (or `--batch N`). Read the batch file it points to and
`$SKILL_DIR/references/playbook.md`.

Then set up four things in the scratchpad — this is what makes a batch fast (session-tested
2026-08-07, 166 products in one session):

1. **Sitemap indexes** — run playbook **STEP 0** to download jarir/pcpalace/afaq/alshamel
   product URLs into `.urls` files. Agents grep these instead of using WebSearch (whose
   200/session budget WILL run out mid-batch). An absence in a complete catalogue is real
   gap evidence; a failed search is not.
2. **One shared `BRIEF.md`** holding everything common: the 13 competitors in order, the
   lookup order, fetch ladder, per-site techniques, confidence rubric, data-hygiene rules,
   the output schema, and the sitemap paths. Agents **read this file** — do not paste it
   into every prompt. Each agent prompt then shrinks to ~10 lines: the product_id, the
   model-specific traps, its sibling rows, and the output path. This is the difference
   between a 400-token and a 3,000-token prompt per product.
3. **`todo.json`** — the claimed batch's outstanding rows, so agents read their own row by
   product_id rather than having it pasted in. Same for pre-grepped `leads.json`.
4. **A checkpoint sweep** (`ck.sh`) that checkpoints every `rec-*.json` not yet in
   `state.json` and prints "N/200 done". Run it after each completion notification; it makes
   the session crash-safe and progress a one-liner.

### 3. Fan out matcher agents
One agent per product, `run_in_background` default. **The harness caps concurrent subagents
at 20** — over that, launches bounce with "Concurrent subagent limit reached" and must be
relaunched (nothing is lost; checkpoints are per-product). Keep **~15 in flight** and launch
replacements as notifications arrive. The old 4-per-wave ceiling applies to amazon-search-heavy
work; jina + sitemap-driven work sustained 15–18 with zero blocks. Model by tier:

| Tier | Contents (this run) | Model | Escalation |
|---|---|---|---|
| A | Toners & inks (batches 1–6: mostly Mint (منت) compatibles keyed by OEM part family — strict same-brand: only Mint listings match, expect many gaps) | `sonnet` | — |
| B | Printers, electronics, gaming, accessories (batches 7–12; many generic/no-brand items — expect gaps) | `sonnet` | re-run product once with `opus` if result is `ambiguous`/`no_match` on 2+ competitors |
| C | (none in current run) | `opus` | — |

Items 1–4 and 6 below belong in the shared **`BRIEF.md`** (step 2) — agents have no access
to this skill's files, but they can read a scratchpad file, and one file beats N copies:
1. The product row — put the batch's rows in `todo.json` and give the agent its
   **product_id**; the brief tells it how to look the row up.
2. The lookup order + the fetch ladder + the relevant competitor sections and tier note
   from `playbook.md`, and the hard rules from `confidence-rubric.md`.
3. The output contract: "Return ONLY a JSON object matching this schema" + the schema
   from `references/output-schema.json` (or a compact restatement with the example
   record). Exactly **13** entries in `matches`, one per competitor, in
   `competitors.json` order. Have the agent **write the JSON to a file** and reply with a
   single `DONE <pid> matched=<n> gap=<n> err=<n>` line — returning full records through
   the transcript burns orchestrator context for no benefit.
4. The proxy fetch instruction: `python3 $SKILL_DIR/scripts/fetch.py "<url>"` via Bash
   when WebFetch is bot-walled (verification only — NOT for price hunting).
5. Per-product, in the prompt itself (this is the part that must NOT be generic): the
   **model-specific traps** — which sibling rows in this batch differ only by CPU/RAM/
   storage/screen/condition, which near-miss models are gaps, any known-wrong catalogue
   field, and any pre-grepped sitemap leads. This is where orchestrator judgment adds value.
6. Budget guardrail: max ~5 searches + 3 page fetches per competitor; 2 consecutive
   hard failures on a competitor ⇒ `error_transient` for it and move on. **A sitemap hit
   is a lead, never a match — require a rendered page with a spec block.**
6. The data-hygiene rules, verbatim: links only — no prices; valid status enum is
   matched/no_match/ambiguous/error_transient; valid confidence enum is
   high/medium/low/gap (absence is ALWAYS "gap", never "none"/"low"); a
   search-results-listing URL is NOT a valid product URL — if no individual product
   page can be isolated, that competitor is no_match/gap; a multi-pack/bundle is not
   a match for a single-unit product; STRICT SAME-BRAND: only the exact same product
   (same brand + part number + color/yield, single unit) is a match — for Mint (منت)
   products only a Mint listing counts, an OEM original (HP/Canon/Epson/Brother…) or
   any other aftermarket brand is NOT a match and must be recorded as gap; verify
   brand + part number in the page TITLE, the URL SLUG, and the DESCRIPTION/spec
   fields before recording any match — a search snippet is never enough, and a
   title/slug/description disagreement means no match.

### 4. Checkpoint every product, immediately
As each agent returns: parse its JSON (strip any prose around it), sanity-check shape
(product_id matches, 13 matches in competitors.json order, valid enums). Write it to
`/tmp/claude-*/…/scratchpad/rec-<pid>.json` and run
`state.py checkpoint --batch N --file <file>`. Malformed output → one re-ask of the same
agent via SendMessage; still bad → `retry-push`. `error_transient` on all 13 competitors
→ also `retry-push` (do not checkpoint a fully-failed product). If several agents in a
wave hit transient errors, halve the wave size for the rest of the batch.

### 5. Validate
When all products are checkpointed:
- Collect this batch's `medium` + `ambiguous` + `low` entries, plus a ~10% sample of
  `high`. Split into **chunks of ~40 grouped by competitor** (so each validator stays on
  one site's fetch technique) and spawn **one `opus` Validator per chunk** — 4 chunks
  covered 171 entries comfortably in batch 11. Its job is adversarial: **open every claimed
  URL** (fetch ladder incl. fetch.py + jina), try to refute the match, resolve `ambiguous`,
  upgrade/downgrade confidence.
  - **Its single highest-yield check is "did this page actually render?"** — dead URLs that
    were recorded as matches (a 404, a 302-to-homepage, a slug misread as a spec) were 3 of
    batch 11's 7 rejects.
  - Have it return a compact **verdict list** (`keep`/`upgrade`/`downgrade`/`reject`/
    `unresolved` + new confidence + reason), not rewritten records — then apply the verdicts
    with a script. `unresolved` ⇒ `review-push`.
- Apply corrections to the batch's results file **and** append the corrected records to
  master (note in the session log).
- **Scrub prices** from `evidence`/`note`/`external_title` before completing — the run is
  links-only and real figures do leak in (32 of them in batch 11).
- Run `python3 $SKILL_DIR/scripts/validate_urls.py --batch N` (URL gate + duplicate
  detection). Rejected URLs it flags: if obviously fixable (canonicalization), fix in
  place; else they're already in the review file. Note it reads the append-only master, so
  its "N URLs checked" figure double-counts after a correction pass — the reject and
  duplicate findings are still correct. Duplicate groups are usually **duplicate catalogue
  rows**, not matcher errors; cross-check against the agents' `catalog_issue` notes.
- **Queue every `error_transient` leg** for a later sweep (`state.py retry-push`, one item
  per product+competitor) so a bot-walled site's coverage is recoverable.

### 6. Complete & hand off
- `state.py complete --batch N` (use `--force` only if unresolvable products were
  routed to retry/review — say so in the log).
- Write `$DATA/logs/session-<batch>.md`: 5–15 lines — counts, notable patterns
  ("Jarir has no Xerox toner — mostly gaps"), playbook suggestions, anything the next
  session should know.
- Print the user-facing summary: batch id/tier, matched-high/medium/gap/review counts
  per competitor, retry-queue size, next batch id, sessions remaining.
- **Fold new site knowledge back into `playbook.md` in the same session** — every batch
  finds per-site facts (a spec field that decides condition, a search endpoint that lies, a
  redirect that means "delisted"). If it only lands in a session log, the next batch repeats
  the mistake. Batch 11 corrected a *false* playbook claim that had been silently costing
  confidence on every extra.com entry.

### 7. Optionally continue
If context usage is still comfortable (< ~50%) and the user asked for a long run,
loop back to step 2 for the next batch. Otherwise stop cleanly — the next fresh
session resumes from state alone.

## Recovery rules

- Checkpoints are per-product and atomic: a killed session loses at most in-flight
  agents. On start, `claim` on a stale in-progress batch resumes exactly the missing
  products.
- Never edit `state.json` by hand; always go through `state.py`.
- Never persist a `low` confidence match; never let a search snippet justify `high`.
- `matches.master.jsonl` is **append-only** — retry and validator passes append a full
  corrected record. Anything reading it must collapse to the **last record per
  `product_id`** first, or withdrawn matches come back to life.
- Upload to prod is human-gated and NOT part of this skill's loop — only run it if the user
  explicitly asks. The merchant's standing instruction is to push each validated batch, so
  **build and verify the payload, then hand over the command**. If the user pastes the
  command back, that IS the ask — run it (it worked end-to-end on 2026-08-09; the classifier
  does not block `push_batch.sh` itself, only ad-hoc `railway ssh … psql`).

  **The command for a phase-2 batch — the env var is mandatory:**
  ```bash
  MATCH_DATA_DIR=/srv/crawmatic/matching-phase2 bash /srv/crawmatic/matching/push/push_batch.sh <N>
  ```
  Without it the script looks for `matching/results/batch-0NN.results.json`, which does not
  exist for batches 13+, and aborts before pushing anything.

  What to check and say:
  - The push is **upsert-only, with no delete step** — matches withdrawn after an earlier
    push stay live until explicitly deleted.
  - Every chunk must report `STATUS 2xx` for **matches**, not just products, and the variant
    backfill must print `UPDATE <n>` (a rollback there 422s every match with
    `UNRESOLVED_VARIANT`).
  - **Diff `upserted` against the payload row count.** The matches upsert is keyed on
    `(competitor_id, competitor_url)`, so two of OUR rows claiming the same URL collapse into
    one prod row and the loser silently carries no match. Batch 15 sent 192 → 188 (4 dupe
    groups), batch 16 sent 184 → 182 (2 dupe groups: the Ubiquiti `-EU` vs base pairs). Both
    are correct DB behaviour; de-duplicating the catalogue is the only real fix.
  - **`final counts` is the idempotency check.** If the totals do not move, that batch was
    already in prod and the run was a no-op re-push — which is how 13/14/15 were confirmed
    already-pushed on 2026-08-09 (frozen at 2686/3541), while batch 16 moved them to
    2886/3723 (+200 products, +182 matches).
  - **Competitor-list clobber — FIXED 2026-08-09.** The script used to write the list with
    `>`, so a *failed* fetch overwrote the good `push/out.competitors.json` with the API's
    error blob (Railway's SSH key-verification service had a transient outage that day and
    did exactly that). A payload built in that window would carry a wrong/missing
    `competitor_id` on every match row. It now writes to a `mktemp` file, validates it
    (parses as JSON, ≥5 rows, every row has an `id`) and `mv`s only on success; on failure it
    aborts with `FATAL: competitor fetch failed` and **keeps the previous good file**. If you
    see that message, just re-run the script — do not build a payload first.
  - Railway SSH itself is occasionally flaky ("Railway can't verify your SSH key right now").
    It is transient and self-heals; re-running the whole script is safe, because both upserts
    are idempotent (a re-push returns the identical match id).
