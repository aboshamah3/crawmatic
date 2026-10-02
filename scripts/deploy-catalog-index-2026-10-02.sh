#!/usr/bin/env bash
# =============================================================================
# OWNER-RUN. Engine production deploy of feat/catalog-index-2026-10-02
# (plan outreach/docs/superpowers/plans/2026-10-02-catalog-index-core-engine.md,
# Task 14). NOT run by Claude: the permission classifier denies every Railway
# mutation. Run it yourself:
#
#     ! bash /srv/crawmatic/crawmatic/scripts/deploy-catalog-index-2026-10-02.sh
#
# Order: preflight -> INDEX_SERVICE_TOKEN on api (--skip-deploys) -> migrate
#        (a7c41e9d2b56: three new tables, additive) -> api -> dr-backup
#        (dumps skip the index rows) -> verification.
# Only `api` serves the new routes and only `api` checks the migration head
# (/ready), so worker/scheduler/scrapers keep running the previous release.
# Idempotent: SKIP_MIGRATE=1 skips the migrate step; the token is generated
# once and kept in $TOKEN_FILE (0600) for the outreach service (Task 16).
# Rollback: redeploy fix/daily-failures-2026-09-29 @ b68dab2 to api with the
# 09-29 baked identity; the three tables can stay (nothing old reads them).
# =============================================================================
set -uo pipefail

PROJECT=69dc4bda-0d97-4290-a82f-822ed97d3fb8
ENVNAME=production
REPO=/srv/crawmatic/crawmatic
BRANCH=feat/catalog-index-2026-10-02
NEW_HEAD=a7c41e9d2b56
API=${API:-https://api-production-7193.up.railway.app}
NVM_BIN=${NVM_BIN:-/home/mahmoud/.nvm/versions/node/v24.18.0/bin}
EVIDENCE=/srv/crawmatic/evidence/deploy-catalog-index-2026-10-02
LOG=$EVIDENCE/DEPLOY-LOG.txt
BAKED_SRC=${BAKED_SRC:-$EVIDENCE/_baked_release.py}
BAKED_DST=$REPO/libs/shared/app_shared/_baked_release.py
TOKEN_FILE=/root/.crawmatic/index-service-token

mkdir -p "$EVIDENCE"
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%FT%TZ; }
step() { echo; echo "----- $(ts)  $*"; }
echo "=== engine deploy $BRANCH start $(ts) (log: $LOG)"

# shellcheck disable=SC1091
source /root/.railway/accounts.sh
[[ -n "${RAILWAY_TOKEN_RAILWAY2:-}" ]] || { echo "!! RAILWAY_TOKEN_RAILWAY2 not in /root/.railway/accounts.sh"; exit 1; }

rw() {
  sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH="$NVM_BIN:$PATH" \
    bash -c 'cd "$1" && shift && railway "$@"' _ "$REPO" "$@"
}
jv() {
  curl -s -m 15 "$API/version" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1') or '')
except Exception: print('')"
}
wait_for() {
  local label=$1 max=$2; shift 2; local t=0
  until "$@"; do
    t=$((t+15)); if (( t >= max )); then echo "!! TIMEOUT after ${max}s waiting for: $label"; return 1; fi
    sleep 15
  done
  echo "OK: $label ($(ts))"
}
bake() { cp "$BAKED_SRC" "$BAKED_DST" && chown mahmoud:mahmoud "$BAKED_DST" && chmod 644 "$BAKED_DST"; }
unbake() { rm -f "$BAKED_DST"; }
trap unbake EXIT
deploy() {
  local svc=$1 dlog; dlog=$(mktemp "/tmp/deploy-$svc.XXXXXX.log")
  step "railway up $svc"
  bake
  rw up -s "$svc" -e "$ENVNAME" -p "$PROJECT" --ci 2>&1 | tee "$dlog" \
    | grep -E 'Deployment:|Deploy complete|rror|failed' | head -20
  unbake
  if grep -q 'Deploy complete' "$dlog"; then echo "OK: $svc deploy complete"; return 0; fi
  echo "!! $svc deploy did not report 'Deploy complete' - see $dlog"; return 1
}

# ---- 0. preflight ------------------------------------------------------------
step "0. preflight"
CUR_BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
[[ "$CUR_BRANCH" == "$BRANCH" ]] || { echo "!! $REPO is on '$CUR_BRANCH', expected $BRANCH"; exit 1; }
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || { echo "!! $REPO tree is dirty"; git -C "$REPO" status --short | head; exit 1; }
git -C "$REPO" merge-base --is-ancestor b68dab2 HEAD || { echo "!! HEAD does not contain b68dab2"; exit 1; }
SHA=$(git -C "$REPO" rev-parse HEAD)
CODE_HEAD=$(cd "$REPO" && sudo -n -u mahmoud .venv/bin/python -m alembic heads 2>/dev/null | awk '{print $1}' | head -1)
[[ "$CODE_HEAD" == "$NEW_HEAD" ]] || { echo "!! alembic head in tree is '$CODE_HEAD', expected $NEW_HEAD"; exit 1; }
[[ -f "$BAKED_SRC" ]] || { echo "!! $BAKED_SRC missing: build it first (plan Task 14 Step 2)"; exit 1; }
grep -q "\"expected_db_migration\": \"$NEW_HEAD\"" "$BAKED_SRC" \
  || { echo "!! $BAKED_SRC does not expect $NEW_HEAD; a stale identity makes /ready fail closed"; exit 1; }
echo "HEAD $SHA; live /version: git_sha=$(jv git_sha) db_head=$(jv db_migration_head)"

# ---- 1. token ------------------------------------------------------------------
step "1. INDEX_SERVICE_TOKEN on api (no redeploy)"
if [[ ! -s "$TOKEN_FILE" ]]; then
  install -d -m 700 "$(dirname "$TOKEN_FILE")"
  ( umask 077; openssl rand -hex 32 > "$TOKEN_FILE" )
  echo "generated a new token in $TOKEN_FILE"
fi
# The value goes in on stdin, never on a command line (`ps` would show it).
# Railway's own output is shown on failure; it never contains the value.
set_out=$(tr -d '\n' < "$TOKEN_FILE" \
  | rw variable set INDEX_SERVICE_TOKEN --stdin -s api -e "$ENVNAME" -p "$PROJECT" --skip-deploys 2>&1)
set_rc=$?
if (( set_rc != 0 )); then
  echo "!! could not set INDEX_SERVICE_TOKEN on api (railway exit $set_rc):"
  sed 's/^/   railway: /' <<<"$set_out" | head -20
  exit 1
fi
rw variable list -s api -e "$ENVNAME" -p "$PROJECT" --json 2>/dev/null \
  | python3 -c 'import sys,json; sys.exit(0 if "INDEX_SERVICE_TOKEN" in json.load(sys.stdin) else 1)' \
  || { echo "!! railway reported success but api has no INDEX_SERVICE_TOKEN"; exit 1; }
echo "OK: INDEX_SERVICE_TOKEN set (value not shown)"

# ---- 2. migrate --------------------------------------------------------------
if [[ "${SKIP_MIGRATE:-0}" == "1" || "$(jv db_migration_head)" == "$NEW_HEAD" ]]; then
  step "2. migrate skipped (db head already $NEW_HEAD, or SKIP_MIGRATE=1)"
else
  step "2. migrate: a7c41e9d2b56 (three new tables, additive)"
  deploy migrate || exit 1
  wait_for "live migration head == $NEW_HEAD" 900 \
    bash -c "[[ \"\$(curl -s -m 15 $API/version | python3 -c 'import sys,json;print(json.load(sys.stdin).get(\"db_migration_head\"))')\" == \"$NEW_HEAD\" ]]" \
    || { echo "!! migrate did not reach $NEW_HEAD. Nothing else was deployed."; exit 1; }
fi

# ---- 3. api ------------------------------------------------------------------
rw variable set "GIT_SHA=$SHA" -s api -e "$ENVNAME" -p "$PROJECT" --skip-deploys >/dev/null 2>&1 || echo "note: GIT_SHA not set"
deploy api || exit 1
wait_for "api /ready" 900 bash -c "curl -s -o /dev/null -w '%{http_code}' -m 15 $API/ready | grep -q 200" || true

# ---- 3b. dr-backup -------------------------------------------------------------
# The cron dr-backup service runs scripts/dr/dr_lib.sh from its own image. It must
# carry Task 8's exclusion BEFORE the prod load, or the next 4-hourly backup tries
# to dump ~1.6 GB into a 1 GiB budget. SKIP_DR_BACKUP=1 skips this step.
DR_OK=1
if [[ "${SKIP_DR_BACKUP:-0}" != "1" ]]; then
  deploy dr-backup || { DR_OK=0; echo "!! dr-backup deploy failed: do NOT run the prod load until it is redeployed from this branch"; }
fi

# ---- 4. verification ---------------------------------------------------------
step "4. verification"
FAILS=0
check() { if [[ "$2" == "0" ]]; then echo "PASS  $1"; else echo "FAIL  $1"; FAILS=$((FAILS+1)); fi; }
code() { curl -s -o /dev/null -w '%{http_code}' -m 20 "$@"; }
TOKEN=$(cat "$TOKEN_FILE")
[[ "$(code "$API/ready")" == "200" ]]; check "/ready 200" $?
[[ "$(jv db_migration_head)" == "$NEW_HEAD" && "$(jv expected_db_migration)" == "$NEW_HEAD" ]]; check "db head and baked expected head are $NEW_HEAD" $?
[[ "$(code -H "Authorization: Bearer $TOKEN" "$API/v1/admin/index/status")" == "200" ]]; check "index token reads /v1/admin/index/status" $?
[[ "$(code -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d '{"products":[{"ref":"smoke","title":"CeraVe Hydrating Cleanser 473 ml","gtin":"3337875597296"}]}' \
      "$API/v1/admin/index/lookup")" == "200" ]]; check "index token can POST a lookup" $?
[[ "$(code -H "Authorization: Bearer $TOKEN" "$API/v1/admin/index/workspaces/00000000-0000-0000-0000-000000000000/candidates")" == "401" ]]
check "index token is refused on the workspace routes (401)" $?
[[ "$(code "$API/v1/admin/index/status")" == "401" ]]; check "no token: 401" $?
n=$(curl -s -m 20 "$API/openapi-public.json" | grep -c '/v1/admin/index'); [[ "$n" == "0" ]]
check "index routes absent from /openapi-public.json" $?
[[ "$DR_OK" == "1" || "${SKIP_DR_BACKUP:-0}" == "1" ]]; check "dr-backup redeployed with the index exclusion (or skipped on purpose)" $?
unset TOKEN
echo
if (( FAILS == 0 )); then echo "=== DEPLOY VERIFIED $(ts)"; else echo "=== $FAILS CHECK(S) FAILED $(ts)"; fi
exit $FAILS
