#!/usr/bin/env bash
# trigger_full_run.sh — seed the DataImpulse proxy (if missing) and fire a
# one-shot full-catalog scrape in production, all via the postgres
# container's unix socket (the public `railway connect` path has a broken
# password; see docs note at bottom).
#
#   bash scripts/trigger_full_run.sh
#
# Steps: encrypt proxy password with prod ENCRYPTION_KEYS -> upsert
# proxy_providers + upgrade global_default policy -> insert a WORKSPACE
# refresh rule due immediately -> wait for the scheduler to create the
# job -> delete the rule (one-shot) -> print the job id + status.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${PROXY_LOGIN:?}"; : "${PROXY_PASSWORD:?}"
PROXY_HOST="${PROXY_HOST:-gw.dataimpulse.com}"; PROXY_PORT="${PROXY_PORT:-823}"

PSQL=(railway ssh --service postgres -- env -u PGHOST -u PGPORT psql -U postgres -d railway -tA)

echo "==> encrypting proxy password with production ENCRYPTION_KEYS"
CIPHER=$(PROXY_PASSWORD="$PROXY_PASSWORD" railway run --service api -- \
  uv run python scripts/encrypt_proxy_password.py 2>/dev/null | tr -d '[:space:]')
VER="${CIPHER%%|*}"; TOK="${CIPHER#*|}"
[[ "$CIPHER" == *"|"* ]] || { echo "encryption failed: '$CIPHER'" >&2; exit 1; }
echo "    key version $VER"

echo "==> seeding proxy provider + policy, and firing the run"
"${PSQL[@]}" <<SQL
\set ON_ERROR_STOP on
BEGIN;
INSERT INTO proxy_providers (id, workspace_id, name, type, base_url, username, password_encrypted, password_key_version, country_code, status, monthly_budget_limit, created_at, updated_at)
SELECT gen_random_uuid(), NULL, 'dataimpulse-residential', 'RESIDENTIAL', 'http://${PROXY_HOST}:${PROXY_PORT}', '${PROXY_LOGIN}__cr.sa', '${TOK}', ${VER}, 'SA', 'ACTIVE', 60000, now(), now()
WHERE NOT EXISTS (SELECT 1 FROM proxy_providers WHERE workspace_id IS NULL AND name='dataimpulse-residential');

UPDATE proxy_providers SET base_url='http://${PROXY_HOST}:${PROXY_PORT}', username='${PROXY_LOGIN}__cr.sa', password_encrypted='${TOK}', password_key_version=${VER}, status='ACTIVE', updated_at=now()
WHERE workspace_id IS NULL AND name='dataimpulse-residential';

UPDATE access_policies SET
  provider_id=(SELECT id FROM proxy_providers WHERE workspace_id IS NULL AND name='dataimpulse-residential'),
  country_code='SA', use_proxy_on_retry=true, allow_browser_fallback=true, rotate_per_request=true, updated_at=now()
WHERE workspace_id IS NULL AND name='global_default';

INSERT INTO refresh_rules (id, workspace_id, name, scope, interval_minutes, priority, enabled, next_run_at, created_at, updated_at)
SELECT gen_random_uuid(), w.id, 'one-shot-full-run', 'WORKSPACE', 1, 0, true, now(), now(), now()
FROM workspaces w
WHERE NOT EXISTS (SELECT 1 FROM refresh_rules WHERE name='one-shot-full-run');
COMMIT;
SELECT 'provider_ok='||count(*) FROM proxy_providers WHERE workspace_id IS NULL AND status='ACTIVE';
SELECT 'policy_ok='||count(*) FROM access_policies WHERE workspace_id IS NULL AND provider_id IS NOT NULL AND use_proxy_on_retry;
SQL

echo "==> waiting for the scheduler to fire (30s poll cadence)"
JOB=""
for i in $(seq 1 10); do
  sleep 20
  JOB=$(echo "SELECT id||'|'||status||'|'||total_targets FROM scrape_jobs WHERE source='SCHEDULER' AND created_at > now() - interval '10 minutes' ORDER BY created_at DESC LIMIT 1;" \
    | "${PSQL[@]}" 2>/dev/null | grep -E '^[0-9a-f]{8}-' | head -1 || true)
  [[ -n "$JOB" ]] && break
  echo "    ...waiting (attempt $i)"
done

echo "==> disabling the one-shot rule"
echo "DELETE FROM refresh_rules WHERE name='one-shot-full-run';" | "${PSQL[@]}" >/dev/null
if [[ -z "$JOB" ]]; then
  echo "!! no job appeared — check scheduler logs"; exit 1
fi
echo "==> JOB FIRED: id|status|total_targets = $JOB"
echo "SCRAPE_TEST_JOB_ID=${JOB%%|*}" >> .env
echo "==> job id saved to .env — the session takes over monitoring from here"
