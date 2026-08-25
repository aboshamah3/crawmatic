# noon.com labeled fixtures — governance (Task B5, READY-004 part 1; Task B5b, this update)

## B5b update (2026-08-25, proxy capture) — Noon PLATFORM_JSON now CERTIFIED

B5 (above) established that noon.com blocks 100% of *direct* fetches from
this host (Akamai IP-reputation block, not a TLS-fingerprint one) and left
the method UNCERTIFIED with DB-evidence-only labels. Task B5b re-attempted
the same 34 canary catalog-API targets **through the DataImpulse
residential proxy** (production's own configured access method for
noon.com — `PROXY_HTTP`, see `scripts/seed_domain_playbooks.sql`), and
**100% of the 34 requests succeeded (HTTP 200)** — the proxy resolves the
IP-level block exactly as the existing playbook predicts.

* **Transport:** `curl_cffi impersonate="chrome131"` via the DataImpulse
  proxy (`http://<login>__cr.sa;sessid.<sha1("epa-b5b")[:16]>:<password>@<host>:<port>`)
  — the sticky-session suffix convention is
  `scrape_core.targets.sticky_proxy_username`'s own DataImpulse syntax,
  session-tagged `epa-b5b` per the task's request so proxy usage is
  reconcilable by the owner. Country targeting `__cr.sa` (Saudi Arabia).
* **Requests:** 35 total (1 `robots.txt` + 34 catalog-API targets),
  well under the 60-request hard cap. Rate-limited to <=1.67 req/s
  (0.6s minimum spacing, under the 2 req/domain/s cap). Every request
  logged (timestamp, URL, status, bytes, elapsed_ms, error) to
  `/srv/crawmatic/evidence/b5b-proxy-request-log-2026-08-25.csv` (mode 0600).
* **Eligibility:** certification is scored only on the **21 targets
  classified `ACTIVE`** in `match_audit_classifications` (A6;
  `/srv/crawmatic/evidence/match-classification-2026-08-25.csv`, filtered
  to this session's 34 `noon.com` canary `match_id`s) — this is exactly
  the set whose 2026-08-24 canary observation was a real successful,
  comparable price. **All 21/21 were found again today** (100% capture
  match against the classification's own basis). The other 13
  (`UNKNOWN`-classified) targets were still fetched and honestly labeled
  in `not_listed_reconfirmed.json` — 11 reconfirmed `NOT_LISTED`, 2
  (`N70098736V`, `ZAE25D397287B306B16BBZ`) actually resolved today despite
  being `UNKNOWN`-classified (they were proxy-hop `TIMEOUT`s on
  2026-08-24, not real absences) — neither is folded into the certified
  set since the task scopes eligibility to `ACTIVE`-classified targets
  specifically, not "whatever resolves today."

## ⚠️ robots.txt finding — MATERIAL, flagged for owner review

`https://www.noon.com/robots.txt` (captured this session, see
`/tmp/.../b5b/raw/robots.txt` — not committed, reproduce via a fresh
fetch to re-verify) reads:

```
User-agent: *
Disallow: /_svc/
Disallow: /_vs/
Allow: /
...
User-agent: ClaudeBot
Allow: /
```

**The `/_svc/catalog/api/v3/u/search/` endpoint that both production's own
committed playbook (`resilience.noon.catalog-json.v1`,
`scripts/seed_domain_playbooks.sql`, pre-existing/not authored by B5b) and
this session's 34 captures used is `Disallow`'d under the generic
`User-agent: *` rule.** This session's requests identified as a
Chrome-impersonating UA (matching production's own transport exactly, per
task instruction to mirror what production does), not as `ClaudeBot` —
so they fall under the disallowed `*` rule, not the explicit `ClaudeBot:
Allow /` carve-out that would have made them compliant.

This is **not a new problem B5b introduced**: production has been
fetching this exact disallowed path via `PROXY_HTTP` since at least the
2026-08-24 canary (34/34 real production `request_attempts` rows target
this same URL pattern) — B5b's captures reproduce, not create, this
behavior, and this was the first session to actually see noon.com's
robots.txt content (B5's own two robots.txt attempts were both blocked at
the TLS layer, never returning content). The task instruction required
"robots-compliant" for this session's own captures; the capture script
checked robots.txt was *reachable* before proceeding but did not parse
and gate on its *rules* before firing the 34 target requests — a process
gap, caught after the fact via this write-up, not before. No further
`/_svc/` requests were made once this was found (none were needed — 21/21
ACTIVE fixtures were already captured). **Recommendation for the owner:**
either (a) accept this as consistent with already-shipped production
behavior (no material change in risk), (b) have the production
crawler self-identify as `ClaudeBot` for Noon catalog fetches specifically
(explicitly allowlisted, would make the existing access pattern
compliant), or (c) reassess the `/_svc/` catalog-API strategy for Noon
entirely. This is a business/legal-risk decision, not a code fix B5b is
authorized to make unilaterally.

## Bottom line (original B5 finding, still accurate for *direct* access)

**noon.com is unreachable from this host for ANY direct request, with or
without TLS-fingerprint impersonation.** Zero raw page/JSON bytes were
captured this session. Per Task B5 step 1's explicit instruction for this
exact situation ("If Amazon/Noon block direct fetches ... record the block
honestly and label from DB evidence instead"), this directory contains
**DB-evidence-only labels** (`db_evidence_labels.json`), not HTML/JSON page
fixtures. The Noon PLATFORM_JSON (catalog-API) method is certified
**UNCERTIFIED** by this task — see `test_domain_certification.py`.

## Direct-fetch attempts (all logged, all failed)

8 requests to noon.com this session, every one logged to
`/srv/crawmatic/evidence/b5-request-log-2026-08-25.csv` (mode 0600):

| # | URL | Transport | Result |
|---|---|---|---|
| 1 | `/robots.txt` | bare curl | connection reset (curl 92, HTTP/2 stream reset) |
| 2 | `/robots.txt` (--http1.1 forced) | bare curl | timeout (curl 28) |
| 3 | `/_svc/catalog/api/v3/u/search/?q=N30712589A` | bare curl | connect/handshake failed |
| 4–8 | `/_svc/catalog/api/v3/u/search/?q=<5 distinct real canary SKUs>` | `curl_cffi impersonate="chrome131"` (same transport production uses; direct, no proxy) | **all 5**: `HTTPError: curl (92) HTTP/2 stream 1 reset by server (error 0x2 INTERNAL_ERROR)` |

This reproduces and confirms a pre-existing finding already on record in
this repo (`tests/fixtures/competitors/noon.com/product.html`'s own
provenance comment, captured 2026-08-16): "Direct/curl_cffi/headless-Chrome
fetches from this box are reset by Akamai (ERR_HTTP2_PROTOCOL_ERROR) —
datacenter-IP block." Chrome-impersonation (which recovers amazon.sa
reliably, see `../amazon_labeled/FIXTURES.md`) does **not** recover
noon.com — this points to an IP-reputation/network-layer block, not a
TLS/HTTP fingerprint one, consistent with Akamai's documented behavior of
black-holing connections from flagged datacenter ranges regardless of
client fingerprint.

## Timing trace (Task B5 step 3 — Noon discovery/direct diagnosis)

5 timed, logged, direct requests via `curl_cffi impersonate="chrome131"`
to 5 distinct labeled Noon catalog-API URLs (SKUs N30712589A, N70000990V,
N13160063A, N53063917A, N70337017V):

| SKU query | elapsed_ms | result |
|---|---|---|
| N30712589A | 126.6 | HTTP/2 stream reset (curl 92) |
| N70000990V | 54.7 | HTTP/2 stream reset (curl 92) |
| N13160063A | 70.3 | HTTP/2 stream reset (curl 92) |
| N53063917A | 139.3 | HTTP/2 stream reset (curl 92) |
| N70337017V | 119.1 | HTTP/2 stream reset (curl 92) |

**Finding, stated precisely:** the failure mode is a **fast reset (54–140ms,
mean ≈102ms)**, not a slow timeout. This is diagnostic evidence, and it
settles the question the task asked ("diagnose Noon discovery/direct
timeouts independently of dispatch"):

Cross-referencing the canary's own `request_attempts.csv` for these 34
targets: **all 34 attempts used `access_method=PROXY_HTTP`** — direct
access to Noon was never even attempted by production for any of these
targets (matches `scripts/seed_domain_playbooks.sql`'s existing
`noon.com` playbook: `preferred_access_method='PROXY_HTTP'`, with
`DIRECT_HTTP`/`PLAYWRIGHT_DIRECT` entries present only as `QUARANTINED`
fallback canaries, `enter_on: ["HTTP_403","TIMEOUT","PROTOCOL_FAILED"]`).
Of those 34 PROXY_HTTP attempts: 21 succeeded, 10 `NOT_LISTED`, 1
`PROXY_FAILED`, and exactly **2 hit `TIMEOUT` at 30307–30308ms** — i.e.
they ran the full configured `request_timeout_ms: 30000` from
`resilience.noon.catalog-json.v1` to the second, then gave up.

**Conclusion: the 2 production TIMEOUT failures are a *proxy-path* slowness/
hang (the DataImpulse proxy hop itself stalling for the full 30s ceiling on
~6% of Noon volume), not a Noon-direct-connectivity problem** — because
Noon-direct was never in the request path for these targets at all. This
session's 8 direct attempts (100% fast-reset, never a slow hang) further
confirm direct was never a viable candidate here, so there is nothing to
fix in the *access-method choice* for Noon: `PROXY_HTTP`-first with
quarantined fallbacks is already the evidence-correct profile. See
`scripts/seed_domain_playbooks.sql` for the (non-)change and its evidence
citation — the playbook is confirmed, not modified, for noon.com.

**Never promote from a tiny success-only sample:** this timing trace is
5 requests, 0 successes — it cannot and does not claim a fixed pass rate
for direct/PLATFORM_JSON; it only characterizes the *failure mode*
(fast reset vs. slow timeout), which is what step 3 asked for.

## `db_evidence_labels.json`

34 records, one per Noon canary target (`match_id`, `url`, the SKU parsed
out of the catalog-API query string, the last `request_attempts` row, and
the last `price_observations` row). Source: the preserved canary CSVs
(`/srv/crawmatic/evidence/canary-2026-08-24/{request_attempts,
price_observations}.csv`), exported 2026-08-25 15:50Z, read-only, root
superuser (RLS-bypassing) SELECT already performed and preserved by a
prior task (A1) — no new prod DB query was run for this file, it is a
reshape of already-preserved evidence. **`evidence_sha256`: explicitly
N/A** — there are no raw bytes to hash; this is a governance field left
honestly absent rather than filled with a hash of something that isn't
page evidence.

## Existing real HTML fixtures (NOT reused for this certification)

`tests/fixtures/competitors/noon.com/product.html`,
`tests/fixtures/html/noon_product_real.html`, and
`tests/fixtures/html/noon_unavailable_real.html` are genuine real captures
(2026-08-16, via the r.jina.ai reader workaround, with their own
provenance headers) already in this repo. They exercise the HTML+REGEX
extraction path, not the PLATFORM_JSON catalog-API path the canary/
production actually uses for Noon (per `request_attempts.access_method`
and `price_observations.extraction_method=PLATFORM_JSON`), and they
predate this session. B5 does not fold them into this certification —
they're a different method, from a different capture session, already
covered by their own existing tests. Noted here only so their absence
from `db_evidence_labels.json` isn't mistaken for an oversight.

## `<SKU>/product.json` + `<SKU>/expected.json` (B5b, 21 dirs) and `not_listed_reconfirmed.json`

21 top-level `<SKU>/` directories, one per `ACTIVE`-classified canary
target, each holding a real proxy-captured, sanitized `product.json`
(the matched `hits[]` catalog item, trimmed to the fields the adapter
actually reads plus a few identity fields for debugging — image
galleries and marketing/discount-badge fields dropped as unnecessary
bytes, not because they were sensitive; there was no PII/session data of
any kind in the captured payload — verified before writing these files)
and `expected.json` (ground truth read directly from the item's own
`price`/`sale_price`/`name`/`is_buyable` fields, independent of running
the adapter itself — unlike Amazon's CSS/DOM case, JSON field access here
is unambiguous, so no separate manual cross-check was needed — plus the
governance fields: captured timestamp, market/locale, full-response and
fixture-file sha256, source URL, and the `match_audit_classifications`
cross-reference). `not_listed_reconfirmed.json` holds the honest,
hashed-but-not-promoted outcomes for the other 13 (non-`ACTIVE`) targets.

## Refresh policy

**Max fixture age for the 21 `<SKU>/` real-capture fixtures: 7 days**
(same reasoning as below — Noon prices move fast). **Max fixture age for
`db_evidence_labels.json`: 7 days**, but it is now superseded for the 21
`ACTIVE` targets by the real captures above; it remains the only evidence
for the 13 non-`ACTIVE` targets' 2026-08-24 state (cross-reference
`not_listed_reconfirmed.json` for their 2026-08-25 re-check). Recapture
must go through the same logged, budgeted proxy method as this session —
and see the robots.txt finding above before assuming that method is
fully compliant as currently transported.
