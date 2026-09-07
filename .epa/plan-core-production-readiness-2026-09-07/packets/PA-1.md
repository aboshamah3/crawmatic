# Packet PA-1 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: opus   Parallel-safe: yes (with PA-7)   Depends on: P0-2
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w1   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A1 — Browser egress enforced at connection time (F01, P0)
Acceptance criteria:
- `libs/scrape-core/scrape_core/browser/egress_guard.py` provides `EgressGuard(bind_host, upstream, resolver)` with `.start()/.stop()` (daemon-thread wrappers), `.start_async()/.stop_async()`, `.decisions: Counter` and a `GuardDecision` enum including `REJECTED_PRIVATE_RESOLUTION`.
- The three plan unit tests in `tests/unit/test_browser_egress_guard.py` pass verbatim: private-resolving public name refused, IP-literal loopback refused, public resolution dialed on the validated IP.
- The guard resolves the host itself, rejects when ANY resolved address fails `app_shared.url_safety._reject_ip`, and dials the exact validated address (no re-resolution, closing DNS rebinding). Query strings are never logged.
- Wiring: `PLAYWRIGHT_LAUNCH_OPTIONS['args']` gains `--proxy-server=http://127.0.0.1:<port>` and `--proxy-bypass-list=<-loopback>` when `BROWSER_EGRESS_GUARD_ENABLED`; spider context kwargs set `service_workers=BROWSER_SERVICE_WORKERS`; proxied contexts route through the guard, which forwards CONNECT to DataImpulse after the same validation.
- New settings in `libs/shared/app_shared/config.py`: `BROWSER_EGRESS_GUARD_ENABLED: bool = True`, `BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS: float = 10.0`, `BROWSER_SERVICE_WORKERS: Literal['block','allow'] = 'block'`.
- `ssrf.py`'s module docstring is rewritten to state that the Playwright route hook sees only the first request of a redirect chain and that `egress_guard` is the enforcement point; the subresource branch (~`:205-245`) uses the DNS-aware check with a 60 s per-host cache cleared per context.
- The `browser` pytest marker is REGISTERED in `/srv/crawmatic/crawmatic/pyproject.toml` `[tool.pytest.ini_options] markers` (only `integration` is registered today).
- `tests/integration/test_browser_egress_guard_image.py` (markers `integration`, `browser`) covers redirect-to-loopback, private-DNS subresource, service worker and popup; a Dockerfile stage runs it so the image cannot build if the guard is bypassed.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A1: Browser egress enforced at connection time (F01, P0)
>
> **Files:**
> - Create: `libs/scrape-core/scrape_core/browser/egress_guard.py`
> - Modify: `libs/scrape-core/scrape_core/browser/ssrf.py:1-60` (docstring), `:205-245` (subresource branch)
> - Modify: `apps/scrapers-browser/price_monitor_browser/settings.py:100-150` (launch options, context options)
> - Modify: `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py:560-620` (context kwargs: `service_workers="block"`, proxy routing through the guard)
> - Modify: `apps/scrapers-browser/Dockerfile` (image test stage)
> - Modify: `libs/shared/app_shared/config.py` (new settings)
> - Test: `tests/unit/test_browser_egress_guard.py`, `tests/integration/test_browser_egress_guard_image.py`
>
> **Interfaces:**
> - Produces: `EgressGuard(bind_host="127.0.0.1", upstream: UpstreamProxy | None, resolver=SafeResolver) -> EgressGuard` with `.start() -> int` (port), `.stop()`, `.decisions: Counter`. `UpstreamProxy(server: str, username: str | None, password: str | None)`.
> - Produces settings: `BROWSER_EGRESS_GUARD_ENABLED: bool = True`, `BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS: float = 10.0`, `BROWSER_SERVICE_WORKERS: Literal["block","allow"] = "block"`.
> - Consumes: `app_shared.url_safety._reject_ip`, `scrape_core.safety.resolver.SafeResolver`.
>
> **Design (why this closes the gap):** Chromium is launched with `--proxy-server=http://127.0.0.1:<port>` and `--proxy-bypass-list=<-loopback>` so **every** connection the browser makes, including redirect hops, subresources, workers, popups and WebSockets, is a `CONNECT`/plain-HTTP request to the guard. The guard resolves the hostname itself, rejects if *any* resolved address fails `_reject_ip` (loopback, private, link-local, CGNAT, multicast, IPv6 ULA/link-local, v4-mapped), and then dials the **exact validated address**, never re-resolving (closes rebinding). For proxied legs the guard forwards `CONNECT host:port` to DataImpulse only after the same hostname validation; the residential exit resolves for itself. IP-literal destinations are rejected unless public. The Playwright route hook stays as defense in depth.
>
> - [ ] **Step 1: Failing unit tests** — `tests/unit/test_browser_egress_guard.py`:
>
> ```python
> import asyncio, pytest
> from scrape_core.browser.egress_guard import EgressGuard, GuardDecision
>
> class FakeResolver:
>     def __init__(self, table): self.table = table
>     async def resolve(self, host, port): return self.table[host]
>
> @pytest.mark.asyncio
> async def test_connect_to_privately_resolving_public_name_is_refused():
>     guard = EgressGuard(resolver=FakeResolver({"evil.example": [("127.0.0.1", 0)]}))
>     port = await guard.start_async()
>     r, w = await asyncio.open_connection("127.0.0.1", port)
>     w.write(b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n"); await w.drain()
>     assert (await r.readline()).startswith(b"HTTP/1.1 403")
>     assert guard.decisions[GuardDecision.REJECTED_PRIVATE_RESOLUTION] == 1
>     w.close(); await guard.stop_async()
>
> @pytest.mark.asyncio
> async def test_ip_literal_loopback_is_refused_even_with_bypass():
>     guard = EgressGuard(resolver=FakeResolver({}))
>     port = await guard.start_async()
>     r, w = await asyncio.open_connection("127.0.0.1", port)
>     w.write(b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n"); await w.drain()
>     assert (await r.readline()).startswith(b"HTTP/1.1 403")
>     w.close(); await guard.stop_async()
>
> @pytest.mark.asyncio
> async def test_public_resolution_is_dialed_on_the_validated_ip(unused_tcp_port):
>     async def ok(r, w):
>         w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"); await w.drain(); w.close()
>     srv = await asyncio.start_server(ok, "127.0.0.1", unused_tcp_port)
>     guard = EgressGuard(resolver=FakeResolver({"shop.example": [("127.0.0.1", unused_tcp_port)]}), _allow_loopback_for_tests=True)
>     port = await guard.start_async()
>     r, w = await asyncio.open_connection("127.0.0.1", port)
>     w.write(b"GET http://shop.example/ HTTP/1.1\r\nHost: shop.example\r\n\r\n"); await w.drain()
>     assert (await r.readline()).startswith(b"HTTP/1.1 200")
>     w.close(); srv.close(); await guard.stop_async()
> ```
>
> - [ ] **Step 2: Run** → FAIL (module missing).
> - [ ] **Step 3: Implement `egress_guard.py`** — asyncio `start_server` on `127.0.0.1:0`; parse the request line; for `CONNECT host:port` and absolute-URI `GET/POST`: `host = _normalize_host(...)`; if IP literal → `_reject_ip`; else `addrs = await resolver.resolve(host, port)`; reject if empty or any `_reject_ip(addr)`; dial `asyncio.open_connection(validated_ip, port)` with `BROWSER_EGRESS_GUARD_CONNECT_TIMEOUT_SECONDS`; for CONNECT reply `HTTP/1.1 200 Connection Established` then bidirectional relay (`asyncio.gather` of two pump coroutines); for plain HTTP rewrite the request line to origin-form and relay. When `upstream` is set and the context asked for a proxied leg (username prefix `leg=proxy;` on the guard's own proxy auth, set per context by the spider), forward `CONNECT` to `upstream.server` with `Proxy-Authorization` after validation. Count every decision in `self.decisions`. Never log the URL query string. Run the loop in a daemon thread (`start()`/`stop()` wrappers) so Scrapy's reactor is untouched.
> - [ ] **Step 4: Wire it** — `settings.py`: at import build `PLAYWRIGHT_LAUNCH_OPTIONS["args"] += [f"--proxy-server=http://127.0.0.1:{port}", "--proxy-bypass-list=<-loopback>"]` when `BROWSER_EGRESS_GUARD_ENABLED`; spider context kwargs add `service_workers=BROWSER_SERVICE_WORKERS`; proxied contexts set `proxy={"server": f"http://127.0.0.1:{port}", "username": "leg=proxy", "password": "<context-token>"}` so the guard, not Chromium, talks to DataImpulse (the sticky-session username the spider already builds via `sticky_proxy_username` moves into the guard's upstream call). Keep `PLAYWRIGHT_ABORT_REQUEST` and extend `ssrf.py:205` so subresources use the DNS-aware check with a 60 s per-host cache (cleared per context).
> - [ ] **Step 5: Rewrite the `ssrf.py` docstring**: state that the hook sees only the first request of a redirect chain and that `egress_guard` is the enforcement point.
> - [ ] **Step 6: Integration test in the exact image** — `tests/integration/test_browser_egress_guard_image.py` (markers `integration`, `browser`): start a fixture HTTP server with `/redirect` → `Location: http://127.0.0.1:<port>/secret`, `/sub` returning HTML whose `<script src="http://private.test/x.js">` resolves privately via `--host-resolver-rules`, `/worker` registering a service worker, `/popup` calling `window.open`; launch Chromium with the real settings module; assert every private hop is refused (guard decisions ≥ 1 per case) and `/secret` never received a request. Add a Dockerfile stage `RUN .venv/bin/pytest tests/integration/test_browser_egress_guard_image.py -m browser` so the image cannot build if the guard is bypassed.
> - [ ] **Step 7:** Unit + integration green; full unit suite green. Commit `feat(browser): connection-time egress guard; block service workers; subresource DNS validation (F01)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A1.md

## Scope
Files/areas in scope:
- **A1** — Create `libs/scrape-core/scrape_core/browser/egress_guard.py`, `tests/unit/test_browser_egress_guard.py`, `tests/integration/test_browser_egress_guard_image.py`. Modify `libs/scrape-core/scrape_core/browser/ssrf.py`, `apps/scrapers-browser/price_monitor_browser/settings.py`, `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py`, `apps/scrapers-browser/Dockerfile`, `libs/shared/app_shared/config.py`, `pyproject.toml` (markers only).

Conventions:
- Repo root `/srv/crawmatic/crawmatic`, branch `epa/plan-core-production-readiness-2026-09-07`. Run every engine command as `mahmoud`: `sudo -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/pytest ...`.
- The interpreter in `.venv` is **Python 3.13.14** even though the plan text says 3.12. Do not "fix" that.
- Definition of done per the plan: failing test first, implementation, tests green.
- **Do NOT run `git commit`.** The plan's "Commit `...`" steps are for the orchestrator, which commits per phase after review. Leave your changes uncommitted in the main tree (worker contract rule 8) and put the intended commit message in your report. Never `git add -A`.
- **Core only.** Nothing under `/srv/crawmatic/saas` may be modified.
- One Alembic head at all times; every new revision chains from the current head. Partitioned tables never get a bulk `DELETE`; drops are whole partitions.
- Money is micro-USD (1 USD = 1,000,000). The proxy billing unit is a named setting, never an implicit constant.
- Every read/write is workspace-scoped unless it is a registered system sweep (`_system_session()`); new fleet tables without a `workspace_id` are never exposed to tenant roles.
- Secrets: variable NAMES and file locations only, never values. Never run `env`/`printenv`/`set`.

Context (from CONTEXT.md ## Areas):
- **A1** — All cited files exist and the line ranges fit: `ssrf.py` 272 lines, browser `settings.py` 176, the spider 859, the Dockerfile 61, `config.py` 880. NOTE: the plan cites the spider at `:560-620` in the Files list and `:589-620` in the design text - use the real context-kwargs construction site, not the literal line number.

Task-specific notes:
- **A1** — `apps/scrapers-browser/Dockerfile` is also touched by A4 (uvx pin) in a LATER wave - do not pre-empt that change.
- **A1** — The image build / `-m browser` integration run is docker work: serialized; skip if a parallel sibling is running, note it in the report. Disk on `/` is ~97% full - an ENOSPC is a BLOCKER entry and a deferred verification, not a task failure (ASSUMPTIONS.md answer 4). The unit suite is always required.

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. Integration/docker checks: `docker compose up -d postgres redis` then `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/integration -q -m integration` (scoped to this packet's test files). **Serialized; skip if a parallel sibling is running, and note the skip in the report.** ENOSPC or an unavailable stack → BLOCKER + deferred, never a task failure.

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

