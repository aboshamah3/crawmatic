#!/usr/bin/env bash
# EPA W5.5-GA item B (report §10) — load-test suite orchestrator.
#
# Starts a NAMED, throwaway Postgres container, provisions the three
# production roles + runs alembic to head (same machinery
# .github/workflows/ci.yml's `tenant-isolation` job uses), runs every
# scenario script, writes the evidence report, and tears the container
# down BY NAME — always, on success or failure (`trap ... EXIT`).
#
# No bulk docker prune of any kind. `b3_matrix` (dispatch-integrity's own
# scratch-DB usage) and any other worker's containers are never touched —
# this script only ever refers to CONTAINER_NAME below.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

CONTAINER_NAME="epa_w55ga_b_scratch_pg"
RESULTS_DIR="$REPO_ROOT/tests/load/.results"
EVIDENCE_DIR="/srv/crawmatic/evidence/w55ga-load-2026-08-26"
REPORT_PATH="$EVIDENCE_DIR/REPORT.md"

PG_USER="crawmatic_owner"
PG_PASSWORD="ownerpw"
PG_DB="crawmatic"
APP_PW="loadtest-app-pw-$$"
AUTH_PW="loadtest-auth-pw-$$"
MIGRATE_PW="loadtest-migrate-pw-$$"

cleanup() {
  local exit_code=$?
  echo "--- cleanup: removing ${CONTAINER_NAME} (by name) ---" >&2
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  exit "$exit_code"
}
trap cleanup EXIT

echo "--- starting scratch Postgres (${CONTAINER_NAME}) ---" >&2
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker run -d --name "${CONTAINER_NAME}" \
  -e POSTGRES_USER="${PG_USER}" \
  -e POSTGRES_PASSWORD="${PG_PASSWORD}" \
  -e POSTGRES_DB="${PG_DB}" \
  -p 127.0.0.1::5432 \
  postgres:18-alpine >/dev/null

for _ in $(seq 1 30); do
  if docker exec "${CONTAINER_NAME}" pg_isready -U "${PG_USER}" -d "${PG_DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "${CONTAINER_NAME}" pg_isready -U "${PG_USER}" -d "${PG_DB}"

HOST_PORT="$(docker port "${CONTAINER_NAME}" 5432/tcp | head -1 | cut -d: -f2)"
OWNER_URL="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@127.0.0.1:${HOST_PORT}/${PG_DB}"

echo "--- provisioning roles ---" >&2
PROVISION_DB_ROLES_URL="${OWNER_URL}" \
MIGRATION_DATABASE_URL="${OWNER_URL}" \
CRAWMATIC_APP_DB_PASSWORD="${APP_PW}" \
CRAWMATIC_AUTH_DB_PASSWORD="${AUTH_PW}" \
CRAWMATIC_MIGRATE_DB_PASSWORD="${MIGRATE_PW}" \
  uv run python scripts/provision_db_roles.py --provision

echo "--- migrating to head ---" >&2
MIGRATION_DATABASE_URL="${OWNER_URL}" uv run alembic upgrade head

export LOAD_TEST_APP_DATABASE_URL="postgresql+psycopg://crawmatic_app:${APP_PW}@127.0.0.1:${HOST_PORT}/${PG_DB}"
export LOAD_TEST_SYSTEM_DATABASE_URL="postgresql+psycopg://crawmatic_auth:${AUTH_PW}@127.0.0.1:${HOST_PORT}/${PG_DB}"
export LOAD_TEST_RESULTS_DIR="${RESULTS_DIR}"

rm -rf "${RESULTS_DIR}"
mkdir -p "${RESULTS_DIR}"

SCENARIOS=(
  scenario_noisy_tenant.py
  scenario_hot_domain.py
  scenario_large_catalog.py
  scenario_scheduler_backlog.py
  scenario_pool_exhaustion.py
  scenario_n1_detection.py
  scenario_browser_saturation.py
)

declare -A SCENARIO_STATUS
OVERALL_START="$(date -u +%s)"

for scenario in "${SCENARIOS[@]}"; do
  echo "--- running ${scenario} ---" >&2
  if uv run python "tests/load/${scenario}" >"${RESULTS_DIR}/${scenario}.log" 2>&1; then
    SCENARIO_STATUS["${scenario}"]="PASS"
  else
    SCENARIO_STATUS["${scenario}"]="FAIL (exit $?)"
  fi
  tail -5 "${RESULTS_DIR}/${scenario}.log" >&2 || true
done

OVERALL_END="$(date -u +%s)"
TOTAL_SECONDS=$((OVERALL_END - OVERALL_START))

echo "--- writing report to ${REPORT_PATH} ---" >&2
mkdir -p "${EVIDENCE_DIR}"
uv run python tests/load/render_report.py \
  --results-dir "${RESULTS_DIR}" \
  --out "${REPORT_PATH}" \
  --total-seconds "${TOTAL_SECONDS}" \
  --scenario-status "$(for s in "${SCENARIOS[@]}"; do echo "${s}=${SCENARIO_STATUS[${s}]}"; done)"

echo "--- done: ${REPORT_PATH} ---" >&2
