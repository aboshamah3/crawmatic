# Runbook: onboarding a new competitor

> Closes Task 3.4 (2026-08-16 saas-core-optimization brief): turn "onboard a
> competitor" from 429-forensics archaeology into a 30-minute procedure.
>
> Companion tooling: [`scripts/onboard_competitor.py`](../scripts/onboard_competitor.py)
> (probe + report + `--apply` seed) and
> [`tests/fixtures/competitors/`](../tests/fixtures/competitors/) (the golden-fixture
> regression net, `tests/unit/test_competitor_fixtures.py`).

**`--apply` writes to the database in one transaction. Dry-run (the default, no
`--apply`) never does.** Always read the report before applying it.

---

## 0. Before you start

You need:

- 5+ real product-page URLs on the new domain (a sitemap's `sitemap_products*.xml`
  / `/products/` collection is usually the fastest way to find them — see §1.1).
- The target `workspace_id` (only needed for `--apply`).
- `UV_PROJECT_ENVIRONMENT=/srv/crawmatic/.venv-core` set, running as `mahmoud`
  (never root) via `sudo -u mahmoud -H bash -lc '...'`, same as every other
  script in this repo.

---

## 1. Run the script (dry run)

```bash
sudo -u mahmoud -H bash -lc '
export UV_PROJECT_ENVIRONMENT=/srv/crawmatic/.venv-core
cd /srv/crawmatic/wt-proxy-cost
uv run python scripts/onboard_competitor.py --domain newcompetitor.sa --urls urls.txt
'
```

`urls.txt` is one URL per line (blank lines / `#` comments ignored); the first 5
are probed. No `--apply` flag yet — this only fetches direct HTTP (realistic
browser headers, never the paid proxy) and prints a report. Nothing is written
to the database.

### 1.1 Finding 5 product URLs fast

Most storefronts expose a sitemap index at `/sitemap.xml`; follow it to a
`sitemap_products*.xml` / `sitemap-<n>.xml` child and grab `<loc>` entries
matching `/products/`, `/p/`, or a numeric product-id slug. If the storefront's
listing pages render client-side (no `<loc>` products, category pages come back
mostly static navigation), fetch one category page directly and grep for a
`/p/<id>` link in the raw HTML — plain `curl` with a realistic `User-Agent`
usually already surfaces the product-tile URLs even when full search is
JS-rendered.

If the domain is Cloudflare/Akamai/PerimeterX-walled and direct fetches from
this box reset (`ERR_HTTP2_PROTOCOL_ERROR`, a "Just a moment..." interstitial,
or a custom apology page — see §2 below), try the same URLs through
`r.jina.ai`'s reader with `x-return-format: html`:

```bash
curl -L -A "$UA" -H "x-return-format: html" "https://r.jina.ai/https://example.com/product-page"
```

That trick is the fallback this task used for `noon.com`'s fixture (see
`tests/fixtures/html/noon_product_real.html`'s provenance header). It does not
always work (it did not for `amazon.sa` — both direct HTTP and the Jina
fallback were challenged 2026-08-17) — if it doesn't, capturing a fixture for
that domain needs a paid-proxy fetch, out of a dry-run-only workflow; escalate
rather than force it.

---

## 2. Review the report

The report has three sections, in order:

1. **Per-URL probe results** — for each of the (up to 5) URLs: HTTP status,
   whether it was blocked/challenged (and by what signal), and — if not
   blocked — which extraction strategy fired (`JSON_LD`, or an
   `EMBEDDED_JSON_CANDIDATE`/`CSS_CANDIDATE` hint) plus the price/currency it
   found.
2. **Access-ladder recommendation** — `DIRECT_ONLY` / `DIRECT_THEN_PROXY` /
   `PROXY_FIRST`, from how many of the 5 probes were blocked, plus the
   (always conservative) rate rule: **10 rpm / concurrency 1 / 2 s cooldown**.
3. **Extraction strategy** — the strategy `--apply` will seed into the new
   `scrape_profiles` row: `JSON_LD` needs no profile fields at all
   (`jsonld_enabled` defaults `True`); an `EMBEDDED_JSON`/`CSS` candidate
   copies the detected JSON pointer / CSS selector in, flagged **CONFIRM by
   hand** — it is a heuristic hint, not a guarantee the real strategy will
   resolve it the same way.

**What to check by hand before trusting the report:**

- If the tier is `DIRECT_ONLY` but the report also says `CAUTION: none of
  JSON-LD/embedded-JSON/CSS fired on ANY probe`, open one probed URL yourself.
  `_CHALLENGE_MARKERS` in the script is a known-marker list (Cloudflare/
  Akamai/Incapsula/PerimeterX/Amazon's own apology page), not exhaustive — a
  domain with its own unrecognized custom block page will read as a clean
  `200` with nothing extracted, which looks identical to "this domain just
  doesn't have JSON-LD/common CSS pricing".
- If the extraction strategy is `EMBEDDED_JSON_CANDIDATE` or `CSS_CANDIDATE`,
  open the probed URL's view-source and confirm the pointer/selector actually
  points at the *current* price, not a struck-through original price, a
  related-product carousel, or a per-variant price that changes with
  selection.

---

## 3. Apply

Once the report looks right:

```bash
sudo -u mahmoud -H bash -lc '
export UV_PROJECT_ENVIRONMENT=/srv/crawmatic/.venv-core
cd /srv/crawmatic/wt-proxy-cost
uv run python scripts/onboard_competitor.py --domain newcompetitor.sa --urls urls.txt \
  --apply --workspace-id <uuid>
'
```

This re-probes (cheap, idempotent) and then, **in one transaction**, get-or-creates:

- `access_policies` — named `<domain>-access-policy`, strategy = the
  recommended tier.
- `domain_access_rules` — the domain-wide rate rule (10 rpm / conc 1 / 2 s
  cooldown by default), pointed at the access policy above.
- `scrape_profiles` — named `<domain>-profile`, carrying whatever extraction
  config §2 derived (nothing, for a clean `JSON_LD` domain).
- `competitors` — created if `--competitor-id` wasn't passed and no row for
  this `(workspace, domain)` exists yet; its `default_access_policy_id` /
  `default_scrape_profile_id` are wired to the rows above either way.

Safe to re-run: an already-onboarded domain reuses its existing rows (matched
by `(workspace_id, domain)` / `(workspace_id, name)`) instead of duplicating
them. Either everything above commits, or nothing does — a probe/DB error
partway through never leaves a partial write.

Pass `--competitor-id <uuid>` if the competitor row already exists (e.g. it
was created through the product UI) and you only want the access
policy/rate-rule/profile seeded for it.

---

## 4. Add the fixture

Freeze a regression fixture for the new competitor so a future extraction
change can't silently break it without a test failing:

```bash
mkdir -p tests/fixtures/competitors/newcompetitor.sa
# save one probed product page's raw HTML:
curl -L -A "$UA" -H "Accept-Language: en" "<one of the probed URLs>" \
  -o tests/fixtures/competitors/newcompetitor.sa/product.html
```

Then freeze `expected.json` by running the real chain once and copying its
output — never hand-type the numbers:

```bash
sudo -u mahmoud -H bash -lc '
export UV_PROJECT_ENVIRONMENT=/srv/crawmatic/.venv-core
cd /srv/crawmatic/wt-proxy-cost
uv run python -c "
from scrape_core.extraction.pipeline import extract
html = open(\"tests/fixtures/competitors/newcompetitor.sa/product.html\", encoding=\"utf-8\").read()
c = extract(html)
print(c)
"
'
```

`expected.json` shape (see any existing `tests/fixtures/competitors/*/expected.json`
for a worked example):

```json
{
  "price": "<candidate.raw_price_text, exact>",
  "currency": "<candidate.currency>",
  "availability": "<candidate.stock.value, or null>",
  "extraction_method": "<candidate.method.value>",
  "provenance": {
    "method": "live-captured (direct HTTP, <date>)",
    "source": "<the exact URL captured>"
  }
}
```

Then add the domain to `_COMPETITOR_DOMAINS` in
`tests/unit/test_competitor_fixtures.py` if it isn't there yet (the 12-domain
cohort from `matching/REPORT_COST_PER_COMPETITOR_2026-08-12.md` already is —
a genuinely new competitor is an addition to that tuple).

---

## 5. Run the harness

```bash
sudo -u mahmoud -H bash -lc '
export UV_PROJECT_ENVIRONMENT=/srv/crawmatic/.venv-core
cd /srv/crawmatic/wt-proxy-cost
uv run pytest tests/unit/test_competitor_fixtures.py -v
'
```

The new domain's case must **pass**, not skip. A skip with a `TODO:` reason
means step 4 didn't produce a fixture yet — go back and finish it. Every other
domain's case passing too is the point: this is the same suite the *next*
extraction-chain change runs against, so a regression on `stech.ink` while
you were only touching `newcompetitor.sa` fails loudly right here, not three
weeks later as an unexplained price drop in production.

---

## 6. Run a 10-link canary

Before trusting the new access policy/rate rule/profile at full catalog
volume, dispatch a small real batch (10 links is enough to catch a wrong
extraction method or an unexpectedly tight rate limit without burning a full
day's proxy budget if the tier guess was wrong). Use the same
`competitor_product_matches` -> scrape-job path any other domain uses; there
is no dedicated "canary" endpoint — this is a small, deliberately-sized normal
job. Watch:

- `request_attempts` for the batch: expect `success=true`,
  `access_method` matching the applied tier, no `RATE_LIMITED` rejections
  (10 rpm / conc 1 should not be tripped by 10 links dispatched normally).
- `price_observations`: expect `success=true`, `extraction_method` matching
  what the report/fixture predicted, and a plausible `price`/`currency`.

If the canary disagrees with the report (wrong method fires, prices look
wrong, or requests get blocked despite `DIRECT_ONLY`), do not scale up —
re-probe with `--urls` pointing at a larger/different sample and revisit
§2/§3 before dispatching the full catalog.

---

## 7. Check the per-domain dashboard the next day

`docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md` covers the general alert surface;
for a freshly onboarded domain specifically, confirm the next day that:

- the domain's `strategy_attempt_stats` success rate is where the report
  predicted (a `DIRECT_ONLY` domain that is actually getting blocked half the
  time under real volume needs its access policy revisited — go back to §3
  with a `PROXY_FIRST`/`DIRECT_THEN_PROXY` tier);
- no `cost.spend_per_domain_per_day` / `cost.wasted_paid_rate` alert fired
  for it (`docs/ops/RUNBOOK_STOP_DISPATCH_AND_SPEND.md` §1 if one did);
- `domain_strategy_profiles` for the domain shows `status=ACTIVE` or
  `LEARNING`, not stuck `DISCOVERY_REQUIRED`/`DEGRADED`.

---

## Design notes (why the script is shaped this way)

- **Dry-run default, `--apply` opt-in, one transaction.** The script never
  writes to the database unless you pass `--apply`, and `--apply` either
  commits everything (competitor + access policy + rate rule + profile) or
  rolls back everything — never a partial onboarding that leaves, say, a rate
  rule with no matching access policy.
- **The probe never uses the paid proxy**, dry-run or `--apply` — it is
  answering "what does this domain look like to a plain, unauthenticated
  fetch", which is exactly the question that determines whether a proxy is
  needed at all. Proxying the probe would beg its own question.
- **The rate rule is always the same conservative default** (10 rpm / conc 1 /
  2 s cooldown) regardless of tier — only the *access tier* (whether to proxy,
  and from which attempt) varies with what the probes saw. A brand-new domain
  earns a faster rate only after it has demonstrated it can take one, not on
  day one's guess.
- **Extraction-strategy detection reuses the real production parser**
  (`scrape_core.extraction.jsonld.extract_jsonld` verbatim for JSON-LD; the
  same script-tag scanning order as `scrape_core.extraction.embedded_json`
  for the embedded-JSON candidate scan) rather than a separate hand-rolled
  parser — a "candidate" the probe reports is guaranteed resolvable the same
  way the real chain would resolve it once configured.
