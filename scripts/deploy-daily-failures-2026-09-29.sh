#!/usr/bin/env bash
# =============================================================================
# OWNER-RUN. Engine production deploy of branch fix/daily-failures-2026-09-29
# (plan /srv/crawmatic/PLAN_DAILY_FAILURES_FIX_2026-09-29.md, section 3 + 4).
#
# NOT run by Claude: the permission classifier denies every Railway mutation.
# Run it yourself (the SaaS S1-S3 deploy goes FIRST, see the plan section 4):
#
#     ! bash /srv/crawmatic/crawmatic/scripts/deploy-daily-failures-2026-09-29.sh
#
# or in the background:
#     nohup bash /srv/crawmatic/crawmatic/scripts/deploy-daily-failures-2026-09-29.sh \
#       > /root/deploy-daily-failures.out 2>&1 &
#
# Order:  preflight -> migrate (e6a1c0d4f2b9 usage-export indexes, CONCURRENTLY;
#         f1c7e2a9b3d4 catalog-only ADD COLUMN) -> worker -> scheduler -> scrapers
#         (+ egg re-register) -> scrapers-browser -> api -> verification block.
# Idempotent: every step is safe to re-run; ONLY_SERVICES="scrapers-browser api"
# limits the service loop to resume after one failed service. SKIP_MIGRATE=1
# skips the migrate step once the head is already live.
#
# Traps handled (from earlier deploys):
#   * every `scrapers` deploy wipes the Scrapyd egg -> re-register it in-container
#     (`register_egg.py`); scrapers-browser has its egg baked in.
#   * setting a Railway variable triggers a redeploy -> every `variable set` here
#     uses --skip-deploys, and none is REQUIRED by this release.
#   * never run `railway domain` without a subcommand (it creates a domain).
# Rollback: redeploy the previous tree (fix/wire-bytes-untied-response-2026-09-23
# @ 3a6ec1f, or main) with the same order; the two migrations are additive
# (three indexes, one defaulted column) and need no downgrade to roll the code back.
# =============================================================================
set -uo pipefail

PROJECT=69dc4bda-0d97-4290-a82f-822ed97d3fb8
ENVNAME=production
REPO=/srv/crawmatic/crawmatic
BRANCH=fix/daily-failures-2026-09-29
NEW_HEAD=f1c7e2a9b3d4
API=${API:-https://api-production-7193.up.railway.app}
NVM_BIN=${NVM_BIN:-/home/mahmoud/.nvm/versions/node/v24.18.0/bin}
# The release-identity file earlier deploys baked into the upload tree (never
# committed). Optional: without it /version reports the env identity.
BAKED_SRC=${BAKED_SRC:-/tmp/claude-0/-srv-crawmatic/e1e509e9-d56c-4168-ac60-a609851c5b7d/scratchpad/ops/_baked_release.py.deploy-1f97664}
BAKED_DST=$REPO/libs/shared/app_shared/_baked_release.py
EVIDENCE=/srv/crawmatic/evidence/deploy-daily-failures-2026-09-29
LOG=$EVIDENCE/DEPLOY-LOG.txt
# Usage window the SaaS importer has been stuck on since 2026-09-24 20:12Z.
USAGE_SINCE="2026-09-24T20:12:03.856Z"
USAGE_UNTIL="2026-09-24T21:12:08.206Z"

mkdir -p "$EVIDENCE"
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%FT%TZ; }
step() { echo; echo "----- $(ts)  $*"; }
echo "=== engine deploy $BRANCH start $(ts) (log: $LOG)"

# shellcheck disable=SC1091
source /root/.railway/accounts.sh
[[ -n "${RAILWAY_TOKEN_RAILWAY2:-}" ]] || { echo "!! RAILWAY_TOKEN_RAILWAY2 not in /root/.railway/accounts.sh"; exit 1; }

rw() {  # railway CLI as mahmoud (CLI on the nvm PATH), from the repo root, on the railway2 account
  sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH="$NVM_BIN:$PATH" \
    bash -c 'cd "$1" && shift && railway "$@"' _ "$REPO" "$@"
}
jv() {  # jv <field> -> that field from GET /version ('' on failure)
  curl -s -m 15 "$API/version" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1') or '')
except Exception: print('')"
}
wait_for() {  # wait_for "<label>" <max_seconds> <command...>
  local label=$1 max=$2; shift 2; local t=0
  until "$@"; do
    t=$((t+15)); if (( t >= max )); then echo "!! TIMEOUT after ${max}s waiting for: $label"; return 1; fi
    sleep 15
  done
  echo "OK: $label ($(ts))"
}
bake() {
  if [[ -f "$BAKED_SRC" ]]; then
    cp "$BAKED_SRC" "$BAKED_DST" && chown mahmoud:mahmoud "$BAKED_DST" && chmod 644 "$BAKED_DST"
  else
    echo "note: $BAKED_SRC absent - uploading without a baked release identity"
  fi
}
unbake() { rm -f "$BAKED_DST"; }
trap unbake EXIT
deploy() {  # deploy <service>  -> 0 on "Deploy complete"
  local svc=$1 dlog; dlog=$(mktemp "/tmp/deploy-$svc.XXXXXX.log")
  step "railway up $svc"
  bake
  rw up -s "$svc" -e "$ENVNAME" -p "$PROJECT" --ci 2>&1 | tee "$dlog" \
    | grep -E 'Deployment:|Deploy complete|Writing egg|rror|failed' | head -20
  unbake
  if grep -q 'Deploy complete' "$dlog"; then echo "OK: $svc deploy complete"; return 0; fi
  echo "!! $svc deploy did not report 'Deploy complete' - see $dlog"; return 1
}

# ---- 0. preflight ------------------------------------------------------------
step "0. preflight"
CUR_BRANCH=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
[[ "$CUR_BRANCH" == "$BRANCH" ]] || { echo "!! $REPO is on '$CUR_BRANCH', expected $BRANCH"; exit 1; }
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || { echo "!! $REPO tree is dirty - refuse to upload an untested tree"; git -C "$REPO" status --short | head; exit 1; }
for c in dde3b13 7446d49 3a6ec1f; do
  git -C "$REPO" merge-base --is-ancestor "$c" HEAD || { echo "!! HEAD does not contain prerequisite $c"; exit 1; }
done
SHA=$(git -C "$REPO" rev-parse HEAD)
echo "HEAD $SHA on $BRANCH; commits since the 09-23 fixes:"; git -C "$REPO" log --oneline 3a6ec1f..HEAD
CODE_HEAD=$(cd "$REPO" && sudo -n -u mahmoud .venv/bin/python -m alembic heads 2>/dev/null | awk '{print $1}' | head -1)
[[ "$CODE_HEAD" == "$NEW_HEAD" ]] || { echo "!! alembic head in tree is '$CODE_HEAD', expected $NEW_HEAD"; exit 1; }
echo "live /version: git_sha=$(jv git_sha) db_head=$(jv db_migration_head)"
df -h / | tail -1

# ---- 1. migrate ----------------------------------------------------------------
if [[ "${SKIP_MIGRATE:-0}" == "1" || "$(jv db_migration_head)" == "$NEW_HEAD" ]]; then
  step "1. migrate skipped (db head already $NEW_HEAD, or SKIP_MIGRATE=1)"
else
  step "1. migrate: e6a1c0d4f2b9 (3 indexes: ON ONLY + CONCURRENTLY per partition + ATTACH) and f1c7e2a9b3d4 (ADD COLUMN ... DEFAULT 0)"
  deploy migrate || exit 1
  wait_for "live migration head == $NEW_HEAD" 1500 \
    bash -c "[[ \"\$(curl -s -m 15 $API/version | python3 -c 'import sys,json;print(json.load(sys.stdin).get(\"db_migration_head\"))')\" == \"$NEW_HEAD\" ]]" \
    || { echo "!! migrate did not reach $NEW_HEAD. Nothing else was deployed. Check: railway logs -s migrate -e $ENVNAME -p $PROJECT"; \
         echo "   An interrupted CONCURRENTLY build leaves an INVALID index; drop it by name (pg_index.indisvalid=false) and re-run."; exit 1; }
fi

# ---- 2. services -------------------------------------------------------------
for svc in ${ONLY_SERVICES:-worker scheduler scrapers scrapers-browser api}; do
  if [[ "$svc" == "api" ]]; then
    rw variable set "GIT_SHA=$SHA" -s api -e "$ENVNAME" -p "$PROJECT" --skip-deploys >/dev/null 2>&1 \
      || echo "note: could not set GIT_SHA on api (continuing)"
  fi
  deploy "$svc" || { echo "!! stopping at $svc. Resume with: ONLY_SERVICES=\"<remaining services>\" SKIP_MIGRATE=1 bash $0"; exit 1; }
  if [[ "$svc" == "scrapers" ]]; then
    step "re-register the price_monitor egg (every scrapers deploy wipes it)"
    sleep 30
    rw ssh -s scrapers -e "$ENVNAME" -p "$PROJECT" -- python /app/apps/scrapers/register_egg.py 2>&1 | tail -5 \
      || echo "!! egg re-register failed - scraping on the HTTP node stalls until it succeeds; re-run the line above"
  fi
done
wait_for "api /ready" 900 bash -c "curl -s -o /dev/null -w '%{http_code}' -m 15 $API/ready | grep -q 200" || true

# ---- 3. verification -----------------------------------------------------------
step "3. verification"
FAILS=0
check() {  # check "<label>" <0|1>
  if [[ "$2" == "0" ]]; then echo "PASS  $1"; else echo "FAIL  $1"; FAILS=$((FAILS+1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' -m 20 "$1"; }

[[ "$(code "$API/health")" == "200" ]]; check "/health 200" $?
[[ "$(code "$API/ready")" == "200" ]];  check "/ready 200" $?
DBH=$(jv db_migration_head); CH=$(jv code_migration_head)
[[ "$DBH" == "$NEW_HEAD" && "$CH" == "$NEW_HEAD" ]]; check "alembic head: db=$DBH code=$CH expected=$NEW_HEAD" $?

# The service token never leaves this shell: read from the api service's own
# variables (read-only), used as a header, unset afterwards.
TOKEN=$(rw variables -s api -e "$ENVNAME" -p "$PROJECT" --kv 2>/dev/null | grep -E '^SAAS_SERVICE_TOKEN=' | cut -d= -f2-)
if [[ -z "$TOKEN" ]]; then
  check "read SAAS_SERVICE_TOKEN from the api service (needed for the next checks)" 1
else
  out=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -m 60 -H "Authorization: Bearer $TOKEN" \
        --get --data-urlencode "since=$USAGE_SINCE" --data-urlencode "until=$USAGE_UNTIL" "$API/v1/admin/usage")
  status=${out%% *}; secs=${out##* }
  [[ "$status" == "200" ]] && python3 -c "import sys; sys.exit(0 if float('$secs') < 5.0 else 1)"
  check "/v1/admin/usage stuck window answers HTTP $status in ${secs}s (< 5 s; was 28.9 s)" $?

  metrics=$(curl -s -m 60 -H "Authorization: Bearer $TOKEN" "$API/ops/metrics")
  echo "$metrics" | grep -q '"rule_id": *"security.rls_inert"'; rc=$?
  [[ $rc -ne 0 && -n "$metrics" ]]; check "/ops/metrics no longer reports rule_id security.rls_inert" $?
  echo "      worst_severity=$(echo "$metrics" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("worst_severity"))
except Exception: print("unparseable")')  firing: $(echo "$metrics" | python3 -c 'import sys,json
try: print(",".join(sorted({a["rule_id"] for a in json.load(sys.stdin).get("alerts",[])})))
except Exception: print("?")')"

  pending=$(echo "$metrics" | python3 -c 'import sys,json
try:
    nodes=json.load(sys.stdin)["snapshot"]["scrapyd"]["nodes"]
    b=[n for n in nodes if "browser" in (n.get("node_url") or "")]
    print(b[0]["pending"] if b and b[0].get("available") else "unavailable")
except Exception: print("unavailable")')
  [[ "$pending" =~ ^[0-9]+$ ]] && (( pending <= 4 ))
  check "Scrapyd browser node pending=$pending (<= SCRAPYD_MAX_PENDING_PER_NODE 4; was 2725)" $?
  unset TOKEN metrics
fi

echo
if (( FAILS == 0 )); then echo "=== DEPLOY VERIFIED $(ts)"; else echo "=== $FAILS CHECK(S) FAILED $(ts) - see above"; fi
cat <<'EOF'

Owner follow-ups (none is required for this deploy to be correct):
  * E7  DataImpulse usage evidence: set the plan login/password on the WORKER only, without a redeploy:
          railway variable set DATAIMPULSE_USAGE_API_LOGIN=... DATAIMPULSE_USAGE_API_PASSWORD=... \
            -s worker -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8 --skip-deploys
        then redeploy the worker. Until then cost_rollup.provider_evidence_missing stays CRITICAL (truthfully).
  * E3.4 browser max_proc: stays 1. Size SCRAPYD_MAX_PROC from measured memory (docs/ops/CAPACITY.md).
  * E1  BROWSER_MAX_CONTEXTS default is now 3; if the scrapers-browser service sets it explicitly, set it
        to >= active proxy providers + 1 (or remove it).
  * E4  Canary: certify its domain or remove its refresh rule; it now fails in < 2 min with its denial code.
  * E5  First reaper tick closes the 16 historical DEFERRED targets of terminal jobs (JOB_ALREADY_TERMINAL);
        check the worker log line 'terminal_job_targets_closed=16'.
EOF
exit $FAILS
