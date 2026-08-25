#!/usr/bin/env bash
# run_full_scrape_test.sh — trigger a one-shot full-catalog scrape run in
# production and hand monitoring credentials to the session.
#
#   bash scripts/run_full_scrape_test.sh
#
# Needs ADMIN_EMAIL / ADMIN_PASSWORD in .env (the bootstrap SUPER_ADMIN
# login). What it does:
#   1. logs in to the production API
#   2. prints catalog counts (products / variants / active matches)
#   3. creates a WORKSPACE-scope refresh rule (interval 1 min) — the
#      scheduler fires it within ~90s, creating a SCHEDULED job that
#      covers every ACTIVE match
#   4. captures the job id from the DB, then DELETES the rule so the
#      full run does not repeat every minute
#   5. mints a jobs:read API key and appends it + the job id to .env so
#      the session can poll job progress without admin credentials
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then set -a; source .env; set +a; fi
: "${ADMIN_EMAIL:?ADMIN_EMAIL is required in .env}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required in .env}"
API_URL="${API_URL:-https://api-production-d643.up.railway.app}"

jsonget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(d$1)"; }

echo "==> logging in"
TOKEN=$(curl -sf -X POST "$API_URL/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | jsonget "['access_token']")

echo "==> catalog counts (via railway connect postgres)"
railway connect postgres <<'SQL'
\t on
\a
SELECT 'products='  || count(*) FROM products;
SELECT 'variants='  || count(*) FROM product_variants;
SELECT 'matches_active=' || count(*) FROM competitor_product_matches WHERE status = 'ACTIVE';
SELECT 'proxy_providers_global=' || count(*) FROM proxy_providers WHERE workspace_id IS NULL;
SELECT 'global_default_policy=' || count(*) FROM access_policies WHERE workspace_id IS NULL AND name = 'global_default';
SQL

RULE_NAME="one-shot-full-run-$(date +%s)"
echo "==> creating WORKSPACE refresh rule '$RULE_NAME'"
RULE_ID=$(curl -sf -X POST "$API_URL/v1/refresh-rules" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$RULE_NAME\",\"scope\":\"WORKSPACE\",\"interval_minutes\":1}" \
  | jsonget "['id']")
echo "    rule_id=$RULE_ID"

echo "==> waiting for the scheduler to fire (polling for the job, up to ~4 min)"
JOB_LINE=""
for i in $(seq 1 16); do
  sleep 15
  JOB_LINE=$(railway connect postgres <<'SQL' 2>/dev/null | grep -E '^[0-9a-f]{8}-' | head -1 || true
\t on
\a
SELECT id || '|' || status || '|' || total_targets
FROM scrape_jobs
WHERE source = 'SCHEDULER' AND created_at > now() - interval '10 minutes'
ORDER BY created_at DESC LIMIT 1;
SQL
)
  if [[ -n "$JOB_LINE" ]]; then break; fi
  echo "    ...not yet (attempt $i)"
done

echo "==> deleting refresh rule so the run stays one-shot"
curl -sf -X DELETE "$API_URL/v1/refresh-rules/$RULE_ID" \
  -H "Authorization: Bearer $TOKEN" >/dev/null && echo "    rule deleted"

if [[ -z "$JOB_LINE" ]]; then
  echo "!! no SCHEDULER job appeared — likely zero ACTIVE matches (empty scope creates no job, FR-015)."
  exit 1
fi
JOB_ID="${JOB_LINE%%|*}"
echo "==> job created: $JOB_LINE  (id|status|total_targets)"

echo "==> minting a jobs:read monitor API key"
MONITOR_KEY=$(curl -sf -X POST "$API_URL/v1/api-keys" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"claude-scrape-monitor","scopes":["jobs:read"]}' \
  | jsonget "['api_key']")

# .env is git-ignored; the key is scope-limited to reading job status.
{
  echo "SCRAPE_TEST_JOB_ID=$JOB_ID"
  echo "MONITOR_API_KEY=$MONITOR_KEY"
} >> .env
echo "==> job id + monitor key appended to .env"

echo "==> initial job status:"
curl -sf "$API_URL/v1/jobs/$JOB_ID" -H "Authorization: Bearer $MONITOR_KEY"
echo
echo "==> done — the session can now poll progress with the monitor key"
