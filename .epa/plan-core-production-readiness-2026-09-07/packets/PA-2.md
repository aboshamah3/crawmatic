# Packet PA-2 — Phase A: Stage A — Contain risk, trustworthy baseline
Model: opus   Parallel-safe: yes (with PA-6)   Depends on: PA-1
Run dir: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07   Worktree: main tree
Contract: read /root/.claude/skills/epa/references/worker-contract.md first
Wave: A-w2   Plan: /srv/crawmatic/PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md

## Tasks

### A2 — Regex execution with a hard timeout (F02)
Acceptance criteria:
- `regex>=2024.11` is added to `libs/scrape-core/pyproject.toml` (and the lockfile per repo convention).
- `scrape_core.extraction.regex.search_with_deadline(pattern, subject, *, timeout) -> regex.Match | None` raises `RegexDeadlineExceeded` via `regex.compile(pattern).search(subject, timeout=timeout)` catching `TimeoutError`. Both plan tests pass.
- `_first_regex_match` uses it per node with a cumulative per-page budget of `4 x timeout`; on deadline `extract_regex` returns `None` and the caller records `error_code=REGEX_TIMEOUT`.
- `app_shared.profiles.validation.compile_regex_or_reject(pattern, field=...)` enforces `EXTRACTION_REGEX_MAX_PATTERN_CHARS`, keeps the W3.2 heuristic preflight, and runs a 100 ms probe against `'a'*64+'!'` and `'9'*64`, raising `ProfileValidationError`.
- New settings: `EXTRACTION_REGEX_TIMEOUT_SECONDS: float = 0.25`, `EXTRACTION_REGEX_MAX_PATTERN_CHARS: int = 512`, `EXTRACTION_REGEX_QUARANTINE_AFTER: int = 3`; the existing `REGEX_BOUNDS_*` module constants are promoted to `Settings` (the TODO at `regex.py:93`).
- `ScrapeErrorCode.REGEX_TIMEOUT` added to `libs/shared/app_shared/enums.py`.
- One Alembic revision chained from the current head adds `strategy_profiles.regex_quarantined_at TIMESTAMPTZ NULL` and `regex_timeout_count INT NOT NULL DEFAULT 0`; `scripts/check_single_head.sh` still reports exactly one head.
- Quarantine: on `REGEX_TIMEOUT` the workspace-scoped UPDATE increments the counter and sets `regex_quarantined_at` at the threshold; the profile resolver skips the regex strategy for quarantined profiles; `POST /admin/profiles/{id}/regex-unquarantine` resets both.
- `scripts/preflight_regex_profiles.py --report out.json` runs stored `price_regex`/`stock_regex` against 6 adversarial subjects and exits 2 if any offender is found. Do NOT run it against production.

Plan excerpt (authoritative; do not re-read the plan file):
> ### Task A2: Regex execution with a hard timeout (F02)
>
> **Files:**
> - Modify: `libs/scrape-core/pyproject.toml` (add `"regex>=2024.11"`), lockfile per repo convention
> - Modify: `libs/scrape-core/scrape_core/extraction/regex.py:85-200`
> - Modify: `libs/shared/app_shared/profiles/validation.py:80-120`
> - Modify: `libs/shared/app_shared/enums.py` (`ScrapeErrorCode.REGEX_TIMEOUT`)
> - Create: `alembic/versions/<rev>_strategy_profile_regex_quarantine.py` (adds `strategy_profiles.regex_quarantined_at TIMESTAMPTZ NULL`, `regex_timeout_count INT NOT NULL DEFAULT 0`)
> - Create: `scripts/preflight_regex_profiles.py`
> - Test: `tests/unit/test_extraction_regex_timeout.py`, `tests/unit/test_profile_regex_validation.py`
>
> **Interfaces:**
> - Produces settings: `EXTRACTION_REGEX_TIMEOUT_SECONDS: float = 0.25`, `EXTRACTION_REGEX_MAX_PATTERN_CHARS: int = 512`, `EXTRACTION_REGEX_QUARANTINE_AFTER: int = 3` (timeouts per 24 h). Promotes the existing `REGEX_BOUNDS_*` module constants to `Settings` (the TODO at `regex.py:93`).
> - Produces: `search_with_deadline(pattern: str, subject: str, *, timeout: float) -> regex.Match | None` raising `RegexDeadlineExceeded`.
>
> - [ ] **Step 1: Failing tests**:
>
> ```python
> import pytest, time
> from scrape_core.extraction.regex import search_with_deadline, RegexDeadlineExceeded
>
> def test_catastrophic_pattern_raises_within_budget():
>     t0 = time.monotonic()
>     with pytest.raises(RegexDeadlineExceeded):
>         search_with_deadline(r"(a|aa)+$", "a" * 40 + "!", timeout=0.2)
>     assert time.monotonic() - t0 < 1.0
>
> def test_profile_validation_refuses_oversized_pattern():
>     from app_shared.profiles.validation import compile_regex_or_reject, ProfileValidationError
>     with pytest.raises(ProfileValidationError):
>         compile_regex_or_reject("a" * 600, field="price_regex")
> ```
>
> - [ ] **Step 2: Run** → FAIL. **Step 3:** implement with `regex.compile(pattern).search(subject, timeout=timeout)` catching `TimeoutError` → `RegexDeadlineExceeded`; `_first_regex_match` uses it per node with a cumulative per-page budget of `4 × timeout`; on deadline `extract_regex` returns `None` and the caller records `error_code=REGEX_TIMEOUT`; `compile_regex_or_reject` enforces pattern length, keeps the W3.2 heuristic preflight, and runs a 100 ms probe against `"a"*64 + "!"` and `"9"*64`.
> - [ ] **Step 4: Quarantine** — on `REGEX_TIMEOUT` the pipeline increments `strategy_profiles.regex_timeout_count` (workspace-scoped UPDATE) and sets `regex_quarantined_at = now()` at the threshold; the profile resolver skips the regex strategy for quarantined profiles; admin route `POST /admin/profiles/{id}/regex-unquarantine` (existing admin router) resets both.
> - [ ] **Step 5: Preflight script** — `scripts/preflight_regex_profiles.py --report out.json` runs every stored `price_regex`/`stock_regex` through `search_with_deadline` against 6 adversarial subjects; prints offenders; exit 2 if any. Added to the release checklist (A10).
> - [ ] **Step 6:** tests green; single Alembic head; commit `feat(extraction): regex engine with hard deadline, pattern caps, profile quarantine (F02)`.

Report file: /srv/crawmatic/crawmatic/.epa/plan-core-production-readiness-2026-09-07/reports/A2.md

## Scope
Files/areas in scope:
- **A2** — Modify `libs/scrape-core/pyproject.toml`, `libs/scrape-core/scrape_core/extraction/regex.py`, `libs/shared/app_shared/profiles/validation.py`, `libs/shared/app_shared/enums.py`, `libs/shared/app_shared/config.py`, the existing admin router `apps/api/app/routers/admin.py`. Create `alembic/versions/<rev>_strategy_profile_regex_quarantine.py`, `scripts/preflight_regex_profiles.py`, `tests/unit/test_extraction_regex_timeout.py`, `tests/unit/test_profile_regex_validation.py`.

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
- **A2** — `extraction/regex.py` is 312 lines, `profiles/validation.py` 356; `ScrapeErrorCode` is at `libs/shared/app_shared/enums.py:343` with room to add members. The plan says 'existing admin router' - the real routers are `apps/api/app/routers/admin.py` and `jobs_admin.py`; there is no `admin_ops.py` yet (B9 creates that one). Put the unquarantine route in `admin.py`.

Task-specific notes:
- **A2** — `enums.py` and `config.py` are the highest-collision files in the plan (A1, A5, A8 also touch config.py in this stage). No sibling packet touching them is dispatched concurrently with this one - do not widen your scope into other tasks' edits.
- **A2** — This is the first Alembic revision of Stage A. Chain from the CURRENT head (`c8d2e3f4a5b6` at plan time - re-read `alembic heads` before generating).

Verification command (all commands run from `/srv/crawmatic/crawmatic`):
1. **Unit gate — ALWAYS required** (plan Global Constraints): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
2. **Single Alembic head** (this packet adds a revision): `cd /srv/crawmatic/crawmatic && sudo -u mahmoud bash scripts/check_single_head.sh`

Report the result as the worker contract's `Verify:` line — `<command> → exit <n> (<one-line result>)` — for the unit gate at minimum, plus one line per additional command you ran.

## Assumptions that apply
- **Owner gates are deferred (ASSUMPTIONS.md answer 2).** For any step labelled OWNER GATE, prepare every artifact and the exact command, execute nothing, and mark the gate deferred in your report. Steps needing a deployed release or the staging environment are deferred outright.
- **No spend (answer 3).** C2 step 2, C3 step 2, C4 step 2 and D3 step 1 are NOT run; total run spend is $0. Every money-capable script still ships with a required `--max-usd` and refuses without it.
- **Docker is allowed on this host (answer 4)**, but `/` is at ~97% (2.5 GB free). An ENOSPC or resource failure is a BLOCKERS.md entry and the affected integration/image verification is marked *deferred* — not a task failure. The unit suite is always required.
- Alembic revision ids are generated by Alembic and chained from the head that exists when you run (`c8d2e3f4a5b6` at plan time). One head at all times.
- Compose Postgres is `postgres:17.5-bookworm`; the plan's tech-stack line says PostgreSQL 18. Where the production major matters, determine it from the newest dump header and log the finding.

## Authorized destructive actions
- none. Never delete backups, drop production tables, deploy, change Railway variables, run production scrapes, force-push, or spend money. If a task appears to require one, report `ESCALATE` with the exact action.

