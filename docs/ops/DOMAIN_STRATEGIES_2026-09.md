# Domain strategies and labeled canaries — 2026-09

Owner runbook for EPA task **C4** (F08, plan §11 items 2 and 5).

> **Status: the canary gate is DEFERRED. Nothing in this document has been
> run.** C4 step 2 is a SPEND step and this EPA run's pre-flight deferred
> all spend (ASSUMPTIONS answer 3, total run spend $0). Every command
> below is written out exactly as it must be typed, every result cell is
> **empty on purpose**, and the sign-off row is unsigned. An empty cell is
> an honest "not measured"; a filled one would be a fabrication.

---

## 1. What a versioned domain strategy is

`domain_playbooks` gained five columns (migration `a7c31d0f9e42`):

| column | meaning | `NULL` means |
|---|---|---|
| `strategy_version INT NOT NULL DEFAULT 1` | version of this domain's escalation *shape*, stamped onto every ladder decision | — (never null) |
| `cheap_path TEXT` | `AccessMethod` value to try FIRST when nothing else pins the start | no hint; ordinary priority order |
| `fallback_path TEXT` | the EXPENSIVE `AccessMethod` whose use is rationed | nothing is treated as the rationed rung |
| `fallback_cap_per_refresh INT` | how many times ONE target may take `fallback_path` in ONE refresh | uncapped (pre-C4 behaviour). `0` = never |
| `recovery_probe_fraction NUMERIC` | per-domain override of `SCRAPE_RECOVERY_PROBE_FRACTION` | use the fleet setting |

Two separate counters now live on that row and they are **not**
interchangeable:

* `profile_version` (W4.1) moves on every
  `app_shared.domains.lifecycle.transition` — an approval/evidence event
  about whether the domain may be scraped at all.
* `strategy_version` (C4) moves only when the strategy shape changes.

An attempt is stamped with `strategy_version`, so "which strategy
produced this price" stays answerable even when an unrelated approval
bumps the profile. Bump `strategy_version` **by hand, in the same
statement that changes any of the four columns below it** — a shape
change that keeps its old version number makes every attempt stamped
with it a lie.

Where it is read:

* `app_shared.strategy.methods.PlaybookStrategy.from_row` — the value
  object; validates `cheap_path`/`fallback_path` against `AccessMethod`
  and degrades an unrecognised value to "no hint" with a warning.
* `app_shared.strategy.methods.resolve_next_physical_attempt` — the one
  ladder chokepoint. Stamps the version on every decision (selection,
  refusal and dead end), moves `cheap_path` to the front when nothing
  else pins the start, and rations `fallback_path`.
* `apps/workers/.../tasks_jobs._resolve_domains_and_modes` and
  `scrape_core.targets.load_targets` — one bounded playbook read per
  dispatch pass / per spider load, never per target.
* `scrape_core.targets.next_strategy_attempt` — the reactor-side
  escalation ladder, via `SpiderTarget.playbook_strategy`. This is where
  `fallback_cap_per_refresh` actually bites, because this is where a
  target escalates to the browser.

---

## 2. Seeding a strategy

`scripts/seed_domain_playbooks.sql` carries the **shape** of the rows,
with the C4 values left `NULL` for the owner to fill (see the block
marked `EPA C4` near the end of that file). The values are a decision
about money, so this run deliberately does not invent them.

The shape to fill, per domain:

```sql
UPDATE domain_playbooks SET
    strategy_version        = <bump this whenever any value below changes>,
    cheap_path              = '<AccessMethod, e.g. DIRECT_HTTP>',
    fallback_path           = '<AccessMethod, e.g. PLAYWRIGHT_PROXY>',
    fallback_cap_per_refresh = <N, or NULL for uncapped, 0 for never>,
    recovery_probe_fraction  = <0.00-1.00, or NULL to use the setting>,
    updated_at = now()
WHERE domain = '<domain>';
```

Suggested starting points, from the evidence already in this repo —
**suggestions, not applied**:

| domain | evidence | `cheap_path` | `fallback_path` |
|---|---|---|---|
| `amazon.sa` | proxy-first playbook; browser rendering is the expensive recovery (`resilience.amazon.rendered.v1`) | `PROXY_HTTP` | `PLAYWRIGHT_PROXY` |
| `noon.com` | direct 100 % blocked at TLS/HTTP2 (B5, 8/8); proxy catalog JSON certified 21/21 (B5b) | `PROXY_HTTP` | `PLAYWRIGHT_PROXY` |
| `stech.ink` | direct Shopify product JSON is the proven primary | `DIRECT_HTTP` | `PROXY_HTTP` |

`fallback_cap_per_refresh` and `recovery_probe_fraction` have **no**
evidence-backed suggestion: nothing has ever measured how often the
expensive rung actually rescues a target on these domains. Pick them
after the canary below has run, not before.

---

## 3. Labeled fixtures

| file | rows | priced | availability signal |
|---|---|---|---|
| `tests/fixtures/labeled_offers/amazon.jsonl` | 30 | 19 | weak — 29/30 `UNKNOWN` (see the fixtures README) |
| `tests/fixtures/labeled_offers/noon.jsonl` | 34 | 21 | 21 `IN_STOCK`, 13 `UNKNOWN` |
| `tests/fixtures/labeled_offers/stech.jsonl` | 30 | 30 | 26 `IN_STOCK`, 4 `OUT_OF_STOCK` |

Provenance, per-row sources and the known weaknesses are in
`tests/fixtures/labeled_offers/README.md`. Read it before treating a
disagreement as a regression.

---

## 4. The acceptance bar

**>= 95 % label agreement on `price` + `currency` + `availability`**,
computed **per field** over the labels that got a result, plus:

* **every label must get a result.** A canary that answered 20 of 30
  labeled targets has not shown the strategy works; `--results` scoring
  fails on any `missing_results`.
* `seller` and `variant` are recorded and reported, never scored (the
  evidence bundle has no seller for the two marketplaces).

`scripts/run_domain_canary.py` implements exactly this
(`ACCEPTANCE_AGREEMENT = 0.95`, `SCORED_FIELDS = (price, currency,
availability)`), exits `0` on pass, `1` below the bar, `2` on a missing
required flag and `3` on a refused owner gate.

---

## 5. The exact commands

`--max-usd` is **required on every invocation**, including the offline
one. There is no default and no "unlimited" value.

### 5.1 Offline scoring (free, safe, already exercised by CI)

```bash
cd /srv/crawmatic/crawmatic
uv run python scripts/run_domain_canary.py \
    --domain noon.com --max-usd 0 \
    --labels tests/fixtures/labeled_offers/noon.jsonl \
    --results /srv/crawmatic/evidence/domain-canary-2026-09/noon-results.jsonl \
    --json-out /srv/crawmatic/evidence/domain-canary-2026-09/noon-score.json
```

### 5.2 The live canary — OWNER GATE, DEFERRED, SPENDS MONEY

Per domain, ceiling **<= $1.00 each, <= $2.00 total** (plan C4 step 2):

```bash
# noon.com
uv run python scripts/run_domain_canary.py \
    --domain noon.com --max-usd 1.00 \
    --labels tests/fixtures/labeled_offers/noon.jsonl \
    --owner-go OWNER-GO:canary:noon.com

# stech.ink
uv run python scripts/run_domain_canary.py \
    --domain stech.ink --max-usd 1.00 \
    --labels tests/fixtures/labeled_offers/stech.jsonl \
    --owner-go OWNER-GO:canary:stech.ink
```

Those invocations currently **refuse with exit 3 and a NOT IMPLEMENTED
notice**: the live fetch loop is not written, because writing an
unexercised fetcher during a no-spend run would ship the one part of the
script nobody has ever watched work. Until it is, run the canary through
the ordinary dispatch path and score the export:

1. Create a scrape job over exactly the labeled `match_id`s for the
   domain (`tests/fixtures/labeled_offers/<domain>.jsonl`, field
   `match_id`) and dispatch it the normal way.
2. Export one JSONL row per target with the same field names as the
   labels (`match_id`, `price`, `currency`, `availability`) from
   `price_observations` for that `scrape_job_id`.
3. Score it with §5.1. Attach the `--json-out` file to the sign-off row
   below.

### 5.3 Kill switch

If a canary misbehaves, `docs/ops/RUNBOOK_STOP_DISPATCH_AND_SPEND.md` is
the stop procedure. The per-target attempt budget
(`SCRAPE_TARGET_MAX_PHYSICAL_ATTEMPTS`, `SCRAPE_TARGET_DEADLINE_SECONDS`)
now bounds each target independently of it — C4 wired the gate C1 built,
so a runaway target is capped even if nobody is watching.

---

## 6. Results — EMPTY, gate deferred

Fill one row per domain per run. Leave a cell blank rather than guessing.

| run date | domain | strategy_version | targets | results | price agr. | currency agr. | availability agr. | verdict | spend USD | evidence |
|---|---|---|---|---|---|---|---|---|---|---|
|  | noon.com |  |  |  |  |  |  |  |  |  |
|  | stech.ink |  |  |  |  |  |  |  |  |  |
|  | amazon.sa |  |  |  |  |  |  |  |  |  |

**Gate sign-off**

| item | value |
|---|---|
| Gate | C4 step 2 — labeled domain canary, >= 95 % on price + currency + availability |
| Status | **DEFERRED — not run** (EPA pre-flight: no spend) |
| Authorized ceiling | $1.00 per domain, $2.00 total |
| Actual spend | $0.00 |
| Approver | _(unsigned)_ |
| Date | _(unrun)_ |

---

## 7. Known limitations to read before signing

* **The live fetch path has never executed.** Only the refusal, the
  argument contract and the scorer are exercised by tests.
* **amazon.sa availability is nearly all `UNKNOWN`** in the labels, so
  that column's agreement number is close to meaningless for that
  domain until a capture with real stock text replaces it. Judge amazon
  on `price` and `currency`.
* **amazon.sa currency is locale-dependent** outside the `/-/en/` URL
  form (2026-08-25 B5 finding, still unfixed). A currency disagreement
  there is likely a URL-form problem, not a strategy regression.
* **`fallback_cap_per_refresh` is enforced through Redis.** The counter
  lives at `budget:{job}:{match}:fallback` and fails **open** — a Redis
  outage reports zero fallbacks used, so the cap goes inert rather than
  refusing the expensive rung fleet-wide on a cache blip. The
  physical-attempt budget and the wall-clock deadline still bound the
  spend in that state (the deadline is pure arithmetic and needs no
  Redis at all).
* **Nothing here has been run against a live database.** The migration
  is offline-verified only (single head), consistent with the rest of
  this EPA run.
