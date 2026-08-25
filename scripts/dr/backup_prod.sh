#!/usr/bin/env bash
# Encrypted logical backup of BOTH production databases (engine + SaaS).
#
# EPA task W5.4 / READY-010 step 1. Runs on the ops host from
# /etc/cron.d/crawmatic-dr (installed by scripts/dr/install_schedule.sh).
#
# Produces one "set" per run:
#
#   $DR_ROOT/sets/set-<UTC timestamp>/
#     engine.dump.gpg     pg_dump -Fc, AES-256, mode 0600
#     saas.dump.gpg       pg_dump -Fc, AES-256, mode 0600
#     manifest.json       snapshot id, server version, per-table row counts
#                         read from information_schema INSIDE the dump's own
#                         snapshot, plus full-table content checksums for a
#                         sample of tables, plus sha256 of each ciphertext
#     SHA256SUMS          sha256sum-compatible, for `sha256sum -c`
#
# Production access is READ-ONLY: `pg_dump` plus `SELECT`s in a REPEATABLE
# READ transaction. No DDL, no writes, no configuration change.
#
# See RUNBOOK.md for the RPO gap statement, the key-custody caveat and the
# alerting caveat. None of those are hidden in this script: they are real
# limits of a host-local backup and the runbook names the owner gate for each.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dr_lib.sh
source "$HERE/dr_lib.sh"

RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"
SET_NAME="set-$RUN_TS"
WORK=""
STAGE=""

cleanup() {
  local rc=$?
  dr_snap_close 2>/dev/null || true
  dr_clear_pgenv
  [[ -n "$WORK"  && -d "$WORK"  ]] && rm -rf "$WORK"
  # A half-written set must never be left where the verifier could pick it up
  # as "the latest backup".
  [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf "$STAGE"
  exit $rc
}
trap cleanup EXIT

# ── Retention ──────────────────────────────────────────────────────────────
# Keeps every set from the last $DR_KEEP_HOURS hours, plus the newest set of
# each of the last $DR_KEEP_DAYS days, never fewer than $DR_MIN_KEEP sets, and
# never more than $DR_MAX_BYTES in total. Called before AND after a dump: once
# to make room, once to hold the budget.
dr_prune() {
  [[ -d "$DR_SETS_DIR" ]] || return 0
  local now cutoff_h keep_list=() all=() s base epoch day seen_days=() pruned=0
  now=$(date -u +%s)
  cutoff_h=$(( now - DR_KEEP_HOURS * 3600 ))

  mapfile -t all < <(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | sort -r)
  (( ${#all[@]} )) || return 0

  local idx=0
  for s in "${all[@]}"; do
    base=$(basename "$s"); base="${base#set-}"
    epoch=$(date -u -d "${base:0:4}-${base:4:2}-${base:6:2} ${base:9:2}:${base:11:2}:${base:13:2}" +%s 2>/dev/null || echo 0)
    day="${base:0:8}"
    if (( idx < DR_MIN_KEEP )); then
      keep_list+=("$s")
    elif (( epoch >= cutoff_h )); then
      keep_list+=("$s")
    elif [[ ! " ${seen_days[*]-} " == *" $day "* ]] \
         && (( epoch >= now - DR_KEEP_DAYS * 86400 )); then
      keep_list+=("$s")   # newest surviving set of that calendar day
    fi
    seen_days+=("$day")
    idx=$((idx + 1))
  done

  for s in "${all[@]}"; do
    if [[ ! " ${keep_list[*]} " == *" $s "* ]]; then
      rm -rf "$s"; pruned=$((pruned + 1))
      dr_log INFO "pruned $(basename "$s") (outside retention window)"
    fi
  done

  # Hard byte budget. Oldest first, but never below $DR_MIN_KEEP sets: a full
  # disk must not be able to delete the last backup that exists.
  local total
  total=$(dr_dir_bytes "$DR_ROOT")
  while (( total > DR_MAX_BYTES )); do
    mapfile -t all < <(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | sort)
    (( ${#all[@]} > DR_MIN_KEEP )) || {
      dr_log WARN "size budget $(dr_human "$DR_MAX_BYTES") exceeded (now $(dr_human "$total")) but only ${#all[@]} sets remain — refusing to prune below DR_MIN_KEEP=$DR_MIN_KEEP"
      break
    }
    rm -rf "${all[0]}"; pruned=$((pruned + 1))
    dr_log INFO "pruned $(basename "${all[0]}") (size budget)"
    total=$(dr_dir_bytes "$DR_ROOT")
  done
  dr_log INFO "retention: $pruned set(s) pruned, $(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | wc -l) kept, store $(dr_human "$(dr_dir_bytes "$DR_ROOT")")"
}

# ── One target ─────────────────────────────────────────────────────────────
# Emits $STAGE/<name>.dump.gpg and $WORK/<name>.meta.json
dr_backup_target() {  # $1 = name, $2 = project id, $3 = service
  local name="$1" project="$2" service="$3"
  local out="$STAGE/$name.dump.gpg"
  local wd="$WORK/$name"; mkdir -p "$wd"

  dr_log INFO "[$name] loading credentials via Railway bridge (names only)"
  dr_load_pgenv "$project" "$service"

  dr_log INFO "[$name] opening REPEATABLE READ snapshot session"
  dr_snap_open "$wd"

  dr_snap_query "$wd/snap.txt"   "SELECT pg_export_snapshot();"
  local snapshot; snapshot=$(head -n1 "$wd/snap.txt")
  [[ -n "$snapshot" ]] || dr_die "[$name] pg_export_snapshot() returned nothing"

  dr_snap_query "$wd/version.txt" "SELECT current_setting('server_version');"
  local server_version; server_version=$(head -n1 "$wd/version.txt")

  dr_snap_query "$wd/tables.txt" "$DR_SQL_TABLE_LIST"
  local ntables; ntables=$(grep -c . "$wd/tables.txt" || true)
  (( ntables > 0 )) || dr_die "[$name] information_schema reported zero base tables — refusing to record an empty manifest"
  dr_log INFO "[$name] $ntables base tables in snapshot $snapshot (server $server_version)"

  dr_build_count_sql < "$wd/tables.txt" > "$wd/counts.sql"
  dr_snap_query "$wd/counts.txt" "$(cat "$wd/counts.sql")"

  # Checksum sample: the largest tables that are still cheap to hash.
  local cks=""
  mapfile -t sample < <(
    awk -F'|' -v max="$DR_CKSUM_MAX_ROWS" '$2 > 0 && $2 <= max {print $2"|"$1}' "$wd/counts.txt" \
      | sort -t'|' -k1,1nr | head -n "$DR_CKSUM_TABLES" | cut -d'|' -f2-
  )
  local t i=0
  : > "$wd/cksums.txt"
  for t in "${sample[@]:-}"; do
    [[ -n "$t" ]] || continue
    dr_snap_query "$wd/ck.$i.txt" "$(dr_build_cksum_sql "$t")"
    cat "$wd/ck.$i.txt" >> "$wd/cksums.txt"
    i=$((i + 1))
  done
  dr_log INFO "[$name] content checksums computed for $i table(s)"

  # ── The dump itself. Same snapshot as every number above; straight into
  #    gpg, so no plaintext copy of production data is ever written to disk.
  dr_log INFO "[$name] pg_dump -Fc --snapshot=<shared> | gpg --symmetric AES256"
  local t0 t1
  t0=$(date -u +%s)
  set +e
  "$PG_BIN/pg_dump" -Fc --snapshot="$snapshot" 2> "$wd/pg_dump.err" \
    | dr_gpg_encrypt_stdin "$out" 2> "$wd/gpg.err"
  local st=("${PIPESTATUS[@]}")
  set -e
  t1=$(date -u +%s)
  if [[ "${st[0]}" != 0 ]]; then
    dr_die "[$name] pg_dump exited ${st[0]}: $(tail -c 500 "$wd/pg_dump.err" | dr_scrub | tr '\n' ' ')"
  fi
  if [[ "${st[1]}" != 0 ]]; then
    dr_die "[$name] gpg exited ${st[1]}: $(tail -c 500 "$wd/gpg.err" | dr_scrub | tr '\n' ' ')"
  fi
  chmod 600 "$out"

  dr_snap_close

  # ── A dump that lists zero TABLE DATA sections is a failed backup, not a
  #    backup (inherited from saas/deploy/railway/backup-prod-db.sh). Verified
  #    through the encryption, so this also proves the ciphertext decrypts.
  local sections
  # (pg_restore reads the archive from stdin when given no filename; an
  #  explicit `-` is treated as a literal filename and fails.)
  #  `pg_restore -l` stops after the archive's table of contents, so gpg is
  #  left writing into a closed pipe and reports "Broken pipe" on stderr. That
  #  is the expected shape of this check, not a fault, so gpg's stderr is
  #  dropped here — and only here.
  sections=$(dr_gpg_decrypt_stdout "$out" 2>/dev/null | "$PG_BIN/pg_restore" -l 2>/dev/null | grep -c 'TABLE DATA' || true)
  (( sections >= 1 )) || dr_die "[$name] $out decrypts but contains no TABLE DATA sections"

  local bytes sha
  bytes=$(stat -c '%s' "$out")
  sha=$(sha256sum "$out" | cut -d' ' -f1)
  dr_log INFO "[$name] ok $(dr_human "$bytes") in $((t1 - t0))s, $sections table-data sections, sha256 ${sha:0:16}…"

  jq -n \
    --arg name "$name" --arg file "$name.dump.gpg" --arg sha "$sha" \
    --arg snapshot "$snapshot" --arg sv "$server_version" \
    --argjson bytes "$bytes" --argjson secs "$((t1 - t0))" \
    --argjson sections "$sections" \
    --rawfile counts "$wd/counts.txt" --rawfile cksums "$wd/cksums.txt" '
      def kv: split("\n") | map(select(length > 0) | split("|"))
              | map({table: .[0], value: .[1]});
      {
        name: $name, file: $file, sha256: $sha, bytes: $bytes,
        dump_seconds: $secs, table_data_sections: $sections,
        snapshot: $snapshot, server_version: $sv,
        tables: ($counts | kv | map({table: .table, rows: (.value | tonumber)})),
        checksums: ($cksums | kv | map({table: .table, md5: .value}))
      }' > "$WORK/$name.meta.json"

  dr_clear_pgenv
}

# ── main ───────────────────────────────────────────────────────────────────
main() {
  dr_require_root
  [[ -x "$PG_BIN/pg_dump" ]] || dr_die "$PG_BIN/pg_dump not found (PostgreSQL 18 client is required; /usr/bin/pg_dump is 16.x and cannot dump these servers)"
  command -v railway >/dev/null || dr_die "railway CLI not on PATH"
  command -v jq      >/dev/null || dr_die "jq not on PATH"
  dr_ensure_key

  install -d -m 700 "$DR_ROOT" "$DR_SETS_DIR" "$DR_REPORTS_DIR"

  dr_log INFO "=== backup run $SET_NAME start ==="
  dr_prune

  local free; free=$(dr_free_bytes "$DR_ROOT")
  if (( free < DR_MIN_FREE_BYTES )); then
    dr_die "only $(dr_human "$free") free on $(df -P "$DR_ROOT" | awk 'NR==2{print $6}') — below DR_MIN_FREE_BYTES $(dr_human "$DR_MIN_FREE_BYTES"); refusing to dump (a backup that fills the ops host is an outage, not a backup)"
  fi
  dr_log INFO "disk: $(dr_human "$free") free, store $(dr_human "$(dr_dir_bytes "$DR_ROOT")")"

  WORK=$(mktemp -d /tmp/dr-backup.XXXXXX); chmod 700 "$WORK"
  STAGE="$DR_SETS_DIR/.staging-$RUN_TS"; install -d -m 700 "$STAGE"

  local entry name project service
  for entry in "${DR_TARGETS[@]}"; do
    IFS='|' read -r name project service <<<"$entry"
    dr_backup_target "$name" "$project" "$service"
  done

  jq -n --arg id "$SET_NAME" --arg created "$(dr_ts)" \
        --arg host "$(hostname)" --arg tool "scripts/dr/backup_prod.sh" \
        --slurpfile metas <(cat "$WORK"/*.meta.json) '
     {backup_set: $id, created_utc: $created, host: $host, tool: $tool,
      encryption: "gpg --symmetric --cipher-algo AES256 (key: host-local file, mode 0600)",
      targets: ($metas | map({key: .name, value: .}) | from_entries)}' \
     > "$STAGE/manifest.json"
  chmod 600 "$STAGE/manifest.json"

  ( cd "$STAGE" && sha256sum ./*.dump.gpg ./manifest.json > SHA256SUMS )
  chmod 600 "$STAGE/SHA256SUMS"

  mv "$STAGE" "$DR_SETS_DIR/$SET_NAME"
  STAGE=""
  chmod 700 "$DR_SETS_DIR/$SET_NAME"

  dr_prune
  dr_log INFO "=== backup run $SET_NAME OK ($(dr_human "$(dr_dir_bytes "$DR_SETS_DIR/$SET_NAME")")) ==="
}

main "$@"
