# Task Report: A1-fix1 — Proxied browser legs really take the upstream (phase-A review, Finding 1)
Status: DONE
Attempt: 1

Files changed:
- `libs/scrape-core/scrape_core/browser/egress_guard.py` — leg selection moved from a browser-supplied `Proxy-Authorization` header to a **dedicated loopback listener per upstream**; `register_upstream()` now returns a *port* (plus `register_upstream_async()` and `upstream_port()`); `stop_async()` closes the per-upstream listeners; a stale `leg=proxy` credential now fails closed; module docstring rewritten to record why the credential design cannot work.
- `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py` — proxied context kwargs are now `{"server": f"http://127.0.0.1:{leg_port}"}` with **no** username/password; `PROXY_LEG_USERNAME` import dropped; comment records why a port and not a credential.
- `tests/unit/test_browser_egress_guard.py` — +5 tests driving a real loopback fake upstream proxy (`FakeUpstreamProxy`).
- `tests/integration/test_browser_egress_guard_image.py` — case 5: a real Chromium with a per-context proxy on the registered leg port, against a threaded fake upstream.
- `apps/scrapers-browser/Dockerfile` — build-gate comment now names the proxied case (comment only; A4's `uvx`/pin lines untouched).

`apps/scrapers-browser/price_monitor_browser/settings.py` needed **no** change: the launch-level
`--proxy-server=http://127.0.0.1:{guard.port}` / `--proxy-bypass-list=<-loopback>` still name the
guard's *main* listener, which is now explicitly the direct leg.

Intended commit message (NOT committed — orchestrator commits per phase):
`fix(browser): select the proxied egress leg by listener port, not browser proxy auth (F01)`

## The fix

`EgressGuard.register_upstream(UpstreamProxy(...)) -> int` binds a new loopback listener on
`127.0.0.1:0` whose accept callback is bound to that upstream. Everything accepted there is
forwarded to that upstream after the identical `_validate_destination` (URL safety → resolve →
reject if ANY address is non-public → dial the validated address). Everything accepted on the main
listener is a direct leg. The leg is therefore the accepting socket and cannot fail to be
presented, unlike a `Proxy-Authorization` header Chromium only sends after a `407` the guard
cannot issue on a port shared with unproxied contexts.

Fail-closed paths (all tested):
- a request arriving with the old `username="leg=proxy"` credentials on a listener with no
  upstream → `502` / `REJECTED_UPSTREAM_REFUSED`, never a direct dial;
- `register_upstream()` on a guard with no running loop → `RuntimeError`, never the direct port;
- destination validation runs *before* the upstream is dialed, so a privately-resolving name on a
  proxied leg is refused and the upstream is never contacted at all.

Ledger truth is unchanged and now honest: the spider still sets
`meta["playwright_context"] = f"proxy:{provider_id}"` only on the path that registers a real
upstream leg, so `netledger_middleware.is_proxied(meta)` books PROXY exactly when the bytes really
cross DataImpulse. The provider credential (sticky username from `sticky_proxy_username` +
decrypted password) stays inside the process; Chromium receives a bare `http://127.0.0.1:<port>`.

## Verification

1. **Unit gate (always required):**
`sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"` → exit 0
```
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
4207 passed, 19 skipped, 6 deselected, 63 warnings in 544.62s (0:09:04)
```
(4202 at the phase-A review + the 5 new proxied-leg unit tests.)

2. **Guard unit file (the new fake-upstream tests):**
`sudo -u mahmoud .venv/bin/pytest tests/unit/test_browser_egress_guard.py -q -p no:cacheprovider` → exit 0
```
..............                                                           [100%]
14 passed in 0.75s
```

3. **The exact command the Dockerfile build stage runs**, executed natively against real Chromium:
`sudo -u mahmoud .venv/bin/pytest tests/integration/test_browser_egress_guard_image.py -m browser -q -p no:cacheprovider` → exit 0
```
......                                                                   [100%]
6 passed in 20.35s
```
Case 5 asserts, with a real browser: the fake upstream received
`CONNECT fixture.test:<port> HTTP/1.1` **and** the provider's `Proxy-Authorization`;
`ALLOWED_UPSTREAM >= 1` with `ALLOWED_DIRECT == 0`; the 302 hop to `http://127.0.0.1:<port>/secret`
was refused on the proxied listener too (`REJECTED_PRIVATE_IP_LITERAL >= 1`); `/secret` was never
served.

4. **Mutation check that case 5 is not vacuous** — the proxied context temporarily repointed at the
guard's *direct* listener (i.e. the old, credential-selected behaviour):
`sudo -u mahmoud .venv/bin/pytest tests/integration/test_browser_egress_guard_image.py -m browser -q -p no:cacheprovider -k proxied` → exit 1
```
E       AssertionError: the proxied leg was not forwarded upstream: []
WARNING  scrape_core.browser.egress_guard: refusing 127.0.0.1:38839 (PRIVATE_OR_INTERNAL_IP)
1 failed, 5 deselected in 11.29s
```
The file was restored from a scratchpad copy immediately afterwards and re-run green (evidence 3
above is the post-restore run).

5. Image build itself: **NOT run — still deferred** (see Blockers).

## Deviations

1. **`PROXY_LEG_USERNAME` is kept** (exported, and still checked) although nothing sets it any
   more. It exists solely so a caller still wired the credential way fails closed rather than
   silently dialing direct — deleting it would make the stale wiring degrade to exactly the
   regression this fix removes. Documented at the constant and in `_wants_upstream_leg`.
2. **`EgressGuard.upstream` (the constructor argument) changed meaning**: it is now the leg for the
   guard's *main* listener rather than a fallback for an unknown token. `ensure_process_guard()`
   passes nothing, so the process guard's main listener is a direct leg exactly as before; no
   caller in the repo passes `upstream=`.
3. **Two registration entry points.** `register_upstream_async()` is the in-loop form (tests, and
   any caller already on the guard's loop); `register_upstream()` is the thread-safe wrapper the
   spider uses from Scrapy's reactor thread. A single sync method would deadlock when called from
   inside the guard's own loop.
4. **`apps/scrapers-browser/Dockerfile` touched** for one comment line naming the fifth scenario.
   No RUN/layer change, so A4's `scrapyd-client` pin work in that file is unaffected.

Deviation impact: MINOR

## Blockers

- **Docker image build still not run** (unchanged from A1, BLOCKERS.md 2026-09-07 15:50): `/` has
  ~2 GB free and the browser image needs ~2.5–3 GB. The gate command itself was run natively
  against real Chromium and passes 6/6, so what remains unverified is only that the stage runs
  *inside the image*. Command once disk allows:
  `cd /srv/crawmatic/crawmatic && docker build -f apps/scrapers-browser/Dockerfile -t crawmatic-scrapers-browser:f01 .`

## Notes for reviewer

- **`_serve_via_upstream` is no longer an untested path.** It is covered three ways: the wire-level
  unit test (forwarded `CONNECT` line + `Proxy-Authorization` + a byte echoed back through the
  tunnel), the validation-before-forwarding unit test (upstream never dialed), and the real-browser
  case 5.
- **One listener per upstream, not per request.** `register_upstream` is keyed on the frozen
  `UpstreamProxy` dataclass, so a crawl with N sticky sessions on one provider binds N listeners —
  a sticky key changes the *username*, hence the dataclass, hence the port. That is bounded by the
  number of live sticky sessions, but it is a real growth dimension: listeners are only released
  when the guard stops (process exit). If sticky rotation is ever made per-request, this needs an
  eviction policy. Worth a Stage B follow-up note rather than speculative code now.
- **`ALLOWED_DIRECT == 0` in case 5 holds because the fixture resolver knows only two names**;
  Chromium's own background requests (if any) hit the main listener and die at
  `REJECTED_UNRESOLVABLE`. If that assertion ever flakes, that is the reason.
- **The unit-suite side effect flagged in the review's follow-ups is unchanged** (importing
  `price_monitor_browser/settings.py` still starts one daemon thread + one loopback listener). Out
  of scope for this fix; the review logged it as non-blocking.
- **`docs/ops/SECRETS_BY_COMPONENT.md` needs no change** — it names the guard module as the place
  egress-guard configuration lives, which is still true, and never described the token scheme.
