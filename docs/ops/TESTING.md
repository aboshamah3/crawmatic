# Testing: the two commands, and the marker policy

> Closes plan task 0.4 (production-readiness audit §2: *"the unit suite must not
> be able to reach a real dependency"*).
>
> Companion: [`tests/conftest.py`](../../tests/conftest.py), which enforces
> everything on this page, and `pyproject.toml` `[tool.pytest.ini_options]`,
> where the markers are registered.

---

## 1. The two commands

Run everything as the repo owner from the engine repo root
(`/srv/crawmatic/crawmatic`):

```bash
# UNIT — the gate. No network, no database, no Redis, no Scrapyd.
uv run pytest tests/unit -q -m "not integration"

# INTEGRATION — needs the docker-compose stack up (db, pgbouncer, redis,
# scrapyd) and a real environment pointing at it.
docker compose up -d db pgbouncer redis
uv run pytest tests/integration -q
```

CI runs exactly the first command as its gate
(`.github/workflows/ci.yml`), and runs individual integration files in
dedicated jobs that bring up the services they need.

`-p no:cacheprovider` is a useful addition when running as a different user
than the tree's owner (it stops pytest trying to write `.pytest_cache`):

```bash
sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"
```

## 2. The unit suite runs against a deliberately invalid environment

`tests/conftest.py` installs a session-scoped, autouse fixture
(`isolated_unit_environment`) that rewrites every dependency endpoint before
the first test runs:

| Variable | Value during a unit session |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid` |
| `REDIS_URL` | `redis://127.0.0.1:1/0` |
| `SCRAPYD_HTTP_URLS` | `http://127.0.0.1:1` |
| `SCRAPYD_BROWSER_URLS` | `http://127.0.0.1:1` |

It then calls `app_shared.config.get_settings.cache_clear()` so nothing that
read settings during collection keeps the pre-patch environment alive.

Two properties matter:

* **Invalid, not missing.** Every value is a well-formed URL on the reserved
  TCP port 1, so code that merely *reads* configuration behaves normally. Only
  code that actually *connects* fails — instantly, with
  `ConnectionRefusedError`, instead of hanging on a timeout.
* **Ambient environment cannot rescue a test.** Before this fixture existed a
  unit test that reached a real dependency passed on a developer machine with
  a live stack and failed everywhere else. A green suite now says something
  about the code rather than about the machine.

`tests/unit/test_unit_environment_isolation.py` asserts the guard is in force,
so it cannot be disabled silently.

**If a test genuinely needs a live dependency, mark it `integration`.** Never
weaken the fixture.

### Opting a unit test into a real server

`tests/unit/test_rate_limiter.py` and `tests/unit/test_stats_buffer.py` run
against a hand-rolled in-memory Redis double by default and can be pointed at a
real server with a dedicated variable:

```bash
CRAWMATIC_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
  uv run pytest tests/unit/test_rate_limiter.py tests/unit/test_stats_buffer.py -q
```

They used to key off `REDIS_URL` itself, which made *which client they exercise*
a property of the ambient environment — unset meant the fake, set meant whatever
that variable named. That is the audit §2 problem in miniature, and it is why a
deliberate opt-in gets its own variable: a test's dependencies must never change
shape because of a variable someone else set for a different reason.

## 3. Marker policy

Markers are registered in `pyproject.toml` under
`[tool.pytest.ini_options] markers`. An unregistered marker is a typo waiting
to happen — `-m "not integraton"` silently selects everything — so a new
marker is added there in the same change that first uses it.

| Marker | Meaning | Deselect with |
|---|---|---|
| `integration` | Needs the docker-compose DB/pgbouncer/Redis/Scrapyd stack. Exempt from the invalid-environment fixture. | `-m "not integration"` |
| `browser` | Needs a real Chromium/Playwright browser. *(registered by plan task A1)* | `-m "not browser"` |
| `load` | Load/soak scenario under `tests/load/`; minutes, not seconds. *(registered by plan task B7)* | `-m "not load"` |
| `benchmark` | Offer-extraction benchmark gate under `tests/benchmarks/`; asserts a score threshold, not a behaviour. *(registered by plan task C7)* | `-m "not benchmark"` |

Rules:

1. **None of them belongs in the unit gate.** A test that needs a browser, a
   live stack, minutes of runtime, or a fixture corpus is not a unit test: the
   gate that blocks a release stays fast and hermetic. Today that is enforced
   by path (`tests/unit` only) plus `-m "not integration"`; a marked test that
   does land under `tests/unit/` is deselected by adding its marker to the
   gate's `-m` expression, e.g. `-m "not integration and not browser"`.
2. **`tests/integration/` is marked automatically.** The repo has always
   separated these suites by directory while only a couple of files carried the
   decorator; `pytest_collection_modifyitems` in `tests/conftest.py` applies the
   `integration` marker to everything collected from that directory. Marking a
   file explicitly is still fine (and required for an integration test that
   lives elsewhere) — the two agree.
3. **The marker, not the directory, drives the environment exemption**, and it
   is evaluated *per test*: a session that mixes a couple of integration tests
   in with the unit suite keeps the isolation for the unit tests instead of
   standing down wholesale.

## 4. Other suites

| Path | What it is | How to run |
|---|---|---|
| `tests/unit/` | The gate. ~4k tests, hermetic, seconds-to-minutes. | command above |
| `tests/integration/` | Live-stack behaviour: RLS, dispatch, migrations, isolation. | command above |
| `tests/benchmarks/` | Offer-extraction accuracy gate against a fixture corpus. | `uv run pytest tests/benchmarks -q` |
| `tests/load/` | Scenario harness (pool exhaustion, hot domain, scheduler backlog). Not a pytest suite. | `tests/load/run_load_suite.sh` |
| `tests/fixtures/` | Shared fixture data (HTML corpora, payloads). No tests. | — |

## 5. Secret discipline in test output

A pydantic `ValidationError` prints the whole input dictionary it was handed,
which in a settings context can include credential values. Do not paste raw
`Settings()` construction failures into reports, issues, or CI logs that leave
the box; quote the missing field name and redact the rest. The isolation
fixture keeps the four endpoints above out of that blast radius, but it is not
a substitute for reading what you paste.
