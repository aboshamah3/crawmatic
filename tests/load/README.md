# `tests/load/` — EPA W5.5-GA item B (report §10)

Small, honest load/soak harness for GA sign-off. **Every number this
directory produces is DEV-SERVER-BOUND** — measured on this one dev box,
under docker, with no other tenant traffic — and must never be quoted as a
production budget without re-measurement on production-shaped hardware.
This follows the precedent already set in this codebase's own history
(`saas-wt-w51`'s `specs/003-core-screens-rebuild/` load-test honesty, cited
in that repo's `CLAUDE.md`): "never assert prod budgets on a dev server."

## What is here

| Script | What it drives | Needs a DB? |
|---|---|---|
| `scenario_noisy_tenant.py` | `app_shared.scheduling.fair_queue.run_pass` — one noisy tenant (thousands of due items) vs. several quiet tenants, weighted fair share | No (pure library) |
| `scenario_hot_domain.py` | `fair_queue.plan_pass` — fifty tenants all pointed at one hot domain, per-domain fleet cap | No (pure library) |
| `scenario_large_catalog.py` | `app_shared.jobs.batching.plan_batches` at 10k/25k/50k targets, timed | No (pure library) |
| `scenario_scheduler_backlog.py` | Multi-pass `fair_queue.run_pass` draining a large backlog, passes-to-drain measured | No (pure library) |
| `scenario_pool_exhaustion.py` | A bounded SQLAlchemy `QueuePool` (`pool_size`/`max_overflow` small) against the scratch DB, driven past its limit by concurrent threads | **Yes** |
| `scenario_n1_detection.py` | `app_shared.maintenance.rollups.run_daily_rollup` (READ-ONLY, `dry_run=True`) against the scratch DB, query count measured via a SQLAlchemy statement-execute event listener at increasing row counts | **Yes** |
| `scenario_browser_saturation.py` | **Not runnable here** — no browser fleet. Documents the scenario, its parameters, and how to run it against a real fleet; always reports `SKIPPED-here`. | N/A |
| `harness.py` | Shared utilities: `QueryCounter` (the N+1 detector), the report writer, scratch-DB env resolution. | — |
| `run_load_suite.sh` | Orchestrates: start a NAMED scratch Postgres container, provision roles + migrate, run every scenario, write the report, tear the container down BY NAME. | — |

## Running

```sh
cd /srv/crawmatic/crawmatic
bash tests/load/run_load_suite.sh
```

Writes `/srv/crawmatic/evidence/w55ga-load-2026-08-26/REPORT.md`. The
DB-free scenarios (`noisy_tenant`, `hot_domain`, `large_catalog`,
`scheduler_backlog`) can also be run standalone with no docker/DB at all:

```sh
uv run python tests/load/scenario_noisy_tenant.py
```

## Scratch database discipline

The scratch Postgres container is created **by name**
(`epa_w55ga_b_scratch_pg`) and removed **by name** at the end of
`run_load_suite.sh` (`docker rm -f epa_w55ga_b_scratch_pg`), whether the
suite passes or fails (`trap ... EXIT`). No bulk container/volume prune is
ever run by this harness. `b3_matrix` (the engine CI `dispatch-integrity`
job's own scratch-DB usage) is untouched — this harness uses its own,
differently-named container and never touches another job's or another
EPA worker's containers.

## Why these six, and not more

The task names noisy-tenant fairness, hot-domain cap, large-catalog
planning, scheduler-backlog drain, DB pool exhaustion, and N+1 detection as
in-scope-here, and browser saturation as explicitly not-runnable-here. Four
of the six are pure-function scenarios over
`app_shared.scheduling.fair_queue` / `app_shared.jobs.batching` — both
libraries are stdlib-only (see `fair_queue.py`'s own docstring on this),
so they need no database, no Docker, and no cleanup at all, and can run in
well under a second even at tens of thousands of candidates. The other two
need a real Postgres and are the only ones `run_load_suite.sh` needs
docker for.
