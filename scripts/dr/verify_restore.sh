#!/usr/bin/env bash
# Restore-verification job: proves a backup RESTORES, not that pg_dump exited 0.
#
# EPA task W5.4 / READY-010 step 2. Scheduled daily from /etc/cron.d/crawmatic-dr.
#
# What it asserts, for every target in the chosen backup set:
#
#   1. SHA256SUMS of the set still match (the ciphertext has not rotted).
#   2. The ciphertext decrypts and restores into a THROWAWAY PostgreSQL 18
#      container with exit status 0 and an empty stderr.
#   3. The SET of base tables read from the restored database's
#      information_schema is IDENTICAL to the set recorded in the manifest —
#      so a table added to production after this script was written cannot be
#      silently skipped, and a table lost in transit cannot pass unnoticed.
#   4. Every per-table row count matches the manifest exactly. The manifest's
#      counts were taken inside the dump's own snapshot (see dr_lib.sh), so
#      "exactly" is the correct bar: a mismatch is corruption, never drift.
#   5. Full-table content checksums for the sampled tables match — row counts
#      alone would pass a restore that preserved every row's existence and
#      corrupted every row's contents.
#
# Failure => non-zero exit AND a `DR-ALERT` line on stderr. On this host that
# IS the alert: there is no pager, no on-call rotation and no alert router.
# Wiring these exits to a real notification channel is an OWNER GATE, named in
# RUNBOOK.md §Alerting. Cron mails the output to root if a local MTA exists,
# which is a mailbox, not an alert.
#
# The scratch container holds a full plaintext copy of production data, so it
# is destroyed unconditionally on exit — success, failure, or interrupt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dr_lib.sh
source "$HERE/dr_lib.sh"

DR_PG_IMAGE="${DR_PG_IMAGE:-postgres:18-alpine}"
CONTAINER=""
WORK=""
SET_DIR=""
FAILURES=0

cleanup() {
  local rc=$?
  if [[ -n "$CONTAINER" ]]; then
    # `-v` is not optional: the postgres image declares an anonymous volume for
    # PGDATA, so `docker rm` without it leaves the restored production data on
    # disk as a dangling volume — invisible, unencrypted, and unbounded across
    # daily runs.
    docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
    dr_log INFO "scratch container $CONTAINER and its data volume removed (held plaintext production data)"
  fi
  [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"
  exit $rc
}
trap cleanup EXIT

usage() { echo "usage: $0 [--set set-YYYYmmddTHHMMSSZ] [--report PATH]"; }

SET_ARG=""; REPORT=""
while (( $# )); do
  case "$1" in
    --set)    SET_ARG="$2"; shift 2 ;;
    --report) REPORT="$2";  shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

fail() { FAILURES=$((FAILURES + 1)); dr_alert "$*"; RESULTS+=("FAIL|$*"); }
pass() { dr_log INFO "PASS $*"; RESULTS+=("PASS|$*"); }
RESULTS=()

free_port() {
  local p
  for p in $(seq 55520 55599); do
    ss -ltn "sport = :$p" 2>/dev/null | grep -q ":$p" || { echo "$p"; return 0; }
  done
  dr_die "no free loopback port in 55520-55599"
}

main() {
  dr_require_root
  dr_ensure_key
  command -v docker >/dev/null || dr_die "docker not on PATH"
  [[ -x "$PG_BIN/pg_restore" ]] || dr_die "$PG_BIN/pg_restore not found"

  if [[ -n "$SET_ARG" ]]; then
    SET_DIR="$DR_SETS_DIR/$SET_ARG"
  else
    SET_DIR=$(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' 2>/dev/null | sort | tail -n1)
  fi
  [[ -n "$SET_DIR" && -d "$SET_DIR" ]] || dr_die "no backup set found under $DR_SETS_DIR"
  local set_name; set_name=$(basename "$SET_DIR")
  REPORT="${REPORT:-$DR_REPORTS_DIR/verify-$set_name.md}"
  install -d -m 700 "$DR_REPORTS_DIR"

  dr_log INFO "=== restore verification of $set_name start ==="
  local run_t0; run_t0=$(date -u +%s)

  # 1. integrity of the stored ciphertext
  if ( cd "$SET_DIR" && sha256sum -c --status SHA256SUMS ); then
    pass "SHA256SUMS of $set_name verify"
  else
    fail "SHA256SUMS of $set_name DO NOT verify — stored backup is damaged"
  fi

  WORK=$(mktemp -d /tmp/dr-verify.XXXXXX); chmod 700 "$WORK"

  # 2. isolated restore target
  local port pw
  port=$(free_port)
  pw=$(openssl rand -hex 24)
  CONTAINER="dr-verify-$$"
  docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD="$pw" \
    -p "127.0.0.1:$port:5432" "$DR_PG_IMAGE" >/dev/null
  dr_log INFO "scratch $DR_PG_IMAGE started as $CONTAINER on 127.0.0.1:$port (loopback only)"

  local ready=0 i
  for i in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -q -U postgres >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
  done
  (( ready )) || dr_die "scratch container never became ready"

  dr_clear_pgenv
  export PGHOST=127.0.0.1 PGPORT="$port" PGUSER=postgres PGPASSWORD="$pw" PGDATABASE=postgres

  local total_restore=0
  local target
  for target in $(jq -r '.targets | keys[]' "$SET_DIR/manifest.json"); do
    verify_target "$target" || true
  done

  local run_t1; run_t1=$(date -u +%s)
  local elapsed=$(( run_t1 - run_t0 ))

  write_report "$set_name" "$elapsed" "$total_restore"

  if (( FAILURES )); then
    dr_alert "restore verification of $set_name FAILED with $FAILURES assertion failure(s) — report: $REPORT"
    return 1
  fi
  dr_log INFO "=== restore verification of $set_name PASSED in ${elapsed}s — report: $REPORT ==="
}

verify_target() {  # $1 = target name
  local name="$1"
  local file sha db
  file=$(jq -r --arg n "$name" '.targets[$n].file' "$SET_DIR/manifest.json")
  sha=$(jq  -r --arg n "$name" '.targets[$n].sha256' "$SET_DIR/manifest.json")
  db="restore_check_$name"

  local actual_sha; actual_sha=$(sha256sum "$SET_DIR/$file" | cut -d' ' -f1)
  if [[ "$actual_sha" == "$sha" ]]; then
    pass "[$name] ciphertext sha256 matches manifest (${sha:0:16}…)"
  else
    fail "[$name] ciphertext sha256 ${actual_sha:0:16}… != manifest ${sha:0:16}…"
    return 1
  fi

  dr_psql -d postgres -c "CREATE DATABASE \"$db\";" >/dev/null

  local t0 t1
  t0=$(date -u +%s)
  set +e
  dr_gpg_decrypt_stdout "$SET_DIR/$file" 2> "$WORK/$name.gpg.err" \
    | "$PG_BIN/pg_restore" --no-owner --no-privileges -d "$db" 2> "$WORK/$name.restore.err"
  local st=("${PIPESTATUS[@]}")
  set -e
  t1=$(date -u +%s)
  local secs=$(( t1 - t0 ))
  total_restore=$(( total_restore + secs ))

  if [[ "${st[0]}" == 0 ]]; then
    pass "[$name] gpg decrypt exit 0"
  else
    fail "[$name] gpg decrypt exit ${st[0]}: $(tail -c 300 "$WORK/$name.gpg.err" | tr '\n' ' ')"
    return 1
  fi
  if [[ "${st[1]}" == 0 && ! -s "$WORK/$name.restore.err" ]]; then
    pass "[$name] pg_restore exit 0, stderr empty, ${secs}s"
  elif [[ "${st[1]}" == 0 ]]; then
    fail "[$name] pg_restore exit 0 but wrote $(wc -l < "$WORK/$name.restore.err") stderr line(s): $(head -c 300 "$WORK/$name.restore.err" | tr '\n' ' ')"
  else
    fail "[$name] pg_restore exit ${st[1]}: $(tail -c 300 "$WORK/$name.restore.err" | tr '\n' ' ')"
    return 1
  fi

  # ── table SET equality (information_schema on BOTH sides) ────────────────
  jq -r --arg n "$name" '.targets[$n].tables[].table' "$SET_DIR/manifest.json" | sort > "$WORK/$name.expected_tables"
  dr_psql -d "$db" -c "$DR_CANONICAL_SETTINGS $DR_SQL_TABLE_LIST" | sort > "$WORK/$name.actual_tables"

  local missing extra
  missing=$(comm -23 "$WORK/$name.expected_tables" "$WORK/$name.actual_tables" | tr '\n' ' ')
  extra=$(comm   -13 "$WORK/$name.expected_tables" "$WORK/$name.actual_tables" | tr '\n' ' ')
  if [[ -z "$missing" && -z "$extra" ]]; then
    pass "[$name] table set identical ($(wc -l < "$WORK/$name.expected_tables") base tables)"
  else
    [[ -n "$missing" ]] && fail "[$name] tables in manifest but MISSING after restore: $missing"
    [[ -n "$extra"   ]] && fail "[$name] tables present after restore but absent from manifest: $extra"
  fi

  # ── per-table row counts ─────────────────────────────────────────────────
  jq -r --arg n "$name" '.targets[$n].tables[] | "\(.table)|\(.rows)"' "$SET_DIR/manifest.json" | sort > "$WORK/$name.expected_counts"
  dr_build_count_sql < "$WORK/$name.actual_tables" > "$WORK/$name.counts.sql"
  dr_psql -d "$db" -f "$WORK/$name.counts.sql" | sort > "$WORK/$name.actual_counts"

  # Count DISTINCT table names that differ, not lines: a table present on one
  # side only produces a single line, and reporting "0 tables differ" next to
  # a failure is the kind of detail that makes an operator distrust the whole
  # report.
  local diffnames diffcount
  diffnames=$(comm -3 "$WORK/$name.expected_counts" "$WORK/$name.actual_counts" | sed 's/^[[:space:]]*//' | cut -d'|' -f1 | sort -u | grep -c . || true)
  diffcount="$diffnames"
  if (( diffcount == 0 )); then
    pass "[$name] all $(wc -l < "$WORK/$name.expected_counts") per-table row counts match the dump-time snapshot exactly"
  else
    fail "[$name] $diffcount table(s) differ in row count — $(comm -3 "$WORK/$name.expected_counts" "$WORK/$name.actual_counts" | head -20 | tr '\n' ' ')"
  fi

  # ── content checksums ────────────────────────────────────────────────────
  local nck=0 ckfail=0 tbl want got
  while IFS='|' read -r tbl want; do
    [[ -n "$tbl" ]] || continue
    nck=$((nck + 1))
    got=$(dr_psql -d "$db" -c "$DR_CANONICAL_SETTINGS $(dr_build_cksum_sql "$tbl")" | cut -d'|' -f2-)
    if [[ "$got" != "$want" ]]; then
      ckfail=$((ckfail + 1))
      fail "[$name] content checksum mismatch on $tbl (manifest ${want:0:12}… restored ${got:0:12}…)"
    fi
  done < <(jq -r --arg n "$name" '.targets[$n].checksums[] | "\(.table)|\(.md5)"' "$SET_DIR/manifest.json")
  (( ckfail == 0 )) && pass "[$name] $nck full-table content checksum(s) match"

  dr_log INFO "[$name] restored $(wc -l < "$WORK/$name.actual_tables") tables in ${secs}s"
}

write_report() {  # $1 = set name, $2 = wall seconds, $3 = restore seconds
  local set_name="$1" elapsed="$2" restore_secs="$3"
  local verdict="PASS"; (( FAILURES )) && verdict="FAIL"
  {
    echo "# Restore verification — $set_name"
    echo
    echo "- Verdict: **$verdict** ($FAILURES failed assertion(s))"
    echo "- Run (UTC): $(dr_ts)"
    echo "- Wall clock: ${elapsed}s   |   pg_restore time only: ${restore_secs}s"
    echo "- Backup set: \`$SET_DIR\`"
    echo "- Scratch target: throwaway \`$DR_PG_IMAGE\` container, loopback-only, destroyed at exit"
    echo "- Production access during this job: **none** (the job reads stored ciphertext only)"
    echo
    echo "## Assertions"
    echo
    printf '| Result | Assertion |\n|---|---|\n'
    local r
    for r in "${RESULTS[@]}"; do
      printf '| %s | %s |\n' "${r%%|*}" "$(printf '%s' "${r#*|}" | sed 's/|/\\|/g')"
    done
    echo
    echo "## Manifest summary (names and counts only — no credential material)"
    echo
    echo '```json'
    jq '{backup_set, created_utc, encryption,
         targets: (.targets | map_values({file, bytes, sha256, server_version,
                                          table_count: (.tables | length),
                                          total_rows: ([.tables[].rows] | add),
                                          checksum_tables: [.checksums[].table]}))}' \
       "$SET_DIR/manifest.json"
    echo '```'
    echo
    if (( FAILURES )); then
      echo "> **DR-ALERT** — this backup did not verify. Do not treat it as a recovery point."
      echo "> With no pager configured on this host, this report plus the job's non-zero exit"
      echo "> is the alert. Real alert routing is an open owner gate (RUNBOOK.md §Alerting)."
    fi
  } > "$REPORT"
  chmod 600 "$REPORT"
}

main "$@"
