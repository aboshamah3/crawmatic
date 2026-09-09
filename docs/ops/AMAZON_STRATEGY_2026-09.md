# Amazon HTTP-Leg Strategy Decision Record (2026-09)

Status: **DRAFT -- result rows BLANK, pending the owner's Step 2 run.**

Owner: this document is filled in by whoever runs Step 2 of EPA task C2
(plan `PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md`, Phase C, deep dive
Sec12 item 3, audit F08). This EPA run wrote the tool, the tests, and this
decision record only -- **no proxied request, no direct request to Amazon,
and no money was spent producing it** (ASSUMPTIONS.md answer 3: every
spend step in this plan is deferred to the owner).

## Background

The 2026-08 evidence (`evidence/cost-performance-2026-09-07/`) found a
Scrapy-native HTTP fetch to `amazon.sa` answered with a CAPTCHA wall at
HTTP 200, root-caused to the Scrapy/Twisted HTTP client itself rather than
headers (see `apps/scrapers/price_monitor/settings.py`'s
`DEFAULT_REQUEST_HEADERS` docstring for the full isolation-matrix history).
The fix already shipped is a Chrome-impersonating transport
(`scrape_core.impersonate.ImpersonatingDownloadHandler`, `curl_cffi`,
`Settings.SCRAPE_IMPERSONATE_PROFILE = "chrome131"`) selected per domain via
`Settings.SCRAPE_IMPERSONATE_DOMAINS` (`amazon.sa,noon.com` today).

**What has never been measured**: with that exact transport and the
resolved production `AccessPolicy` (the same profile the real spider would
resolve for the same target today, via `scrape_core.targets`'s
`resolve_effective_policy`/`next_attempt`/`assign_proxy`), what fraction of
Amazon HTTP-leg fetches -- direct and proxied -- actually yield a price?
That number is what decides whether Amazon stays HTTP-first in C4's
`domain_playbooks`, or moves to browser-first with an HTTP recovery probe.

## Instrument

`scripts/probe_amazon_http_leg.py` (this EPA run). Per physical attempt it
records exactly:

1. `status` -- the HTTP status reached (`None` for a transport failure that
   never got one);
2. `title_present` -- whether the page carried a product-identity signal
   (title/product name/SKU/structured data);
3. `price` -- the price extracted **via the resolved profile's own
   extraction method**, not a probe-specific parser;
4. `classification` -- the C1 `ScrapeErrorCode` this attempt classifies as
   (`scripts/probe_amazon_http_leg.classify_probe_attempt`, a thin
   dispatcher over the existing `scrape_core.errors.classify_http_status`/
   `classify_exception` and the C1 `classify_extraction_outcome` -- never a
   second classifier that could disagree with what production stamps);
5. `wire_bytes` -- A7's `WireBytesMiddleware`/`compute_wire_bytes()`
   wire-size measurement for the attempt.

Report math (per-leg and combined price-success rate, classification
counts, wire-bytes average) is verified against fixtures in
`tests/unit/test_probe_amazon_report.py` -- 29 tests, all green, no network
or database touched.

## Decision table (plan-authoritative)

Computed from the **combined** (direct + proxied) HTTP price-success
percentage across all runs:

| HTTP price success | Decision |
| --- | --- |
| >= 80% | HTTP-first stays; browser fallback capped at **1 per refresh**. |
| 30% - 80% | HTTP-first only for the classified "HTTP-works" subset (C4's response-signature classification), with sampled probes. |
| < 30% | Browser-first for Amazon, with a **5% HTTP recovery probe**. |

`scripts/probe_amazon_http_leg.classify_decision` implements this exact
table (`HTTP_PRICE_SUCCESS_HIGH = 80.0`, `HTTP_PRICE_SUCCESS_LOW = 30.0`,
both boundaries inclusive on the higher band, matching the plan's
`>= 80%` / `30-80%` / `< 30%` wording literally).

## Step 2 -- the deferred spend step (owner-run only)

**Not run by this EPA task.** Spend cap: **<= $1.00** total. The exact
command (also printed by the script's own `--dry-run` mode as
`deferred_live_command` in its JSON output):

```bash
sudo -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/python \
    scripts/probe_amazon_http_leg.py \
    --targets 50 --runs 3 --max-usd 1.00 \
    --domain amazon.sa --workspace <workspace-uuid> \
    --report evidence/amazon-http-leg-2026-09/report.json
```

Preconditions before running it for real:

- Prefer a staging Scrapyd node if D1's environment exists; if it does not,
  run from this host with the recorded caveat that **Railway IP reputation
  is not reproduced locally** (deep dive Sec2) -- a local-host success rate
  is not directly comparable to production's.
- `--workspace` must be a real workspace UUID with ACTIVE `amazon.sa`
  matches (`scripts/probe_amazon_http_leg.draw_target_set` reuses Gate D's
  workspace-scoped candidate pool, restricted to the given domain).
- The script refuses to start without `--max-usd` (parser-level required
  argument) and its own `SpendGuard` refuses any charge that would push
  cumulative spend past that cap.

After the run, record **which case applied** and the exact
`domain_playbooks` values C4 should set, in the Result section below.

## Result (BLANK -- fill in after Step 2)

- Run date: _pending_
- Workspace: _pending_
- Targets / runs: _pending_
- Direct-leg price success: _pending_
- Proxied-leg price success: _pending_
- Combined HTTP price success: _pending_
- Decision case (`decision_case` from the report): _pending_
- `domain_playbooks` values to set for `amazon.sa` (C4): _pending_
- Actual spend: _pending_ (cap was $1.00)
- Report file: _pending_ (e.g. `evidence/amazon-http-leg-2026-09/report.json`)

## Notes

- Runs against a staging or fixture environment must never spend against
  production proxy budget; if only production access is available, treat
  this as a canary run subject to the same discipline as C3's browser
  canary (dedicated logging, no refresh-rule interference).
- This document does not authorize the run. The run is authorized by
  whoever owns the deferred spend gate per this plan's ASSUMPTIONS.md.
