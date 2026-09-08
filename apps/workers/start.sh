#!/usr/bin/env bash
# apps/workers/start.sh — two Celery consumer pools, one container (EPA
# B4, F09).
#
# `critical@%h` consumes `scrape_dispatch,maintenance` — dispatch plus
# the reapers/reconcilers/breaker/outbox sweeps that keep the fleet's
# state machines from getting stuck. `bulk@%h` consumes
# `price_analysis,strategy_discovery,webhook_events` — traffic that can
# tolerate more queueing latency. One process each, in the same
# container/service (`apps/workers/Dockerfile`'s single `CMD`), so an
# overloaded bulk backlog can never starve the maintenance pool — the
# reason this file exists rather than one `celery worker` with all five
# queues on one `-c`.
#
# `CELERY_CRITICAL_CONCURRENCY` / `CELERY_BULK_CONCURRENCY` default to
# the same value as `libs/shared/app_shared/config.py`'s `Settings`
# defaults (2) — keep both in sync if you change one. See
# `docs/ops/CAPACITY.md` for the RAM budget this implies.
set -euo pipefail

CELERY_CRITICAL_CONCURRENCY="${CELERY_CRITICAL_CONCURRENCY:-2}"
CELERY_BULK_CONCURRENCY="${CELERY_BULK_CONCURRENCY:-2}"

celery -A app.workers.celery_app worker --loglevel=info -Q scrape_dispatch,maintenance -c $CELERY_CRITICAL_CONCURRENCY -n critical@%h &
critical_pid=$!

celery -A app.workers.celery_app worker --loglevel=info -Q price_analysis,strategy_discovery,webhook_events -c $CELERY_BULK_CONCURRENCY -n bulk@%h &
bulk_pid=$!

# Real semantic check, not just "the container is up": if EITHER pool's
# process exits for any reason (crash, OOM, SIGTERM, a clean-looking
# exit 0), the other is torn down and this script exits non-zero, so the
# container's healthcheck/restart policy notices instead of quietly
# running with half its queues unconsumed. `wait -n` returns as soon as
# either background job exits.
#
# The `if`/`else` (rather than a bare `wait -n ...; exit_code=$?`) is
# load-bearing under `set -e`: a nonzero exit here IS the expected,
# common case (one pool crashed), and a bare statement's nonzero status
# would trip `errexit` immediately -- aborting the script right here,
# before the "kill the other pool" cleanup below ever runs, and leaving
# the surviving pool as an orphan process. A command that is the
# condition of an `if` is exempt from `errexit` by definition.
if wait -n "$critical_pid" "$bulk_pid"; then
    exit_code=0
else
    exit_code=$?
fi

kill "$critical_pid" "$bulk_pid" >/dev/null 2>&1 || true
wait "$critical_pid" "$bulk_pid" >/dev/null 2>&1 || true

# Both pools are meant to run forever; either one exiting — even with
# status 0 — is unexpected, so this always reports failure.
if [[ "$exit_code" -eq 0 ]]; then
    exit_code=1
fi

exit "$exit_code"
