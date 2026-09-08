#!/usr/bin/env bash
# Shared library for the Crawmatic disaster-recovery scripts.
#
# EPA task W5.4 (READY-010, P0.8). Sourced by backup_prod.sh and
# verify_restore.sh; never executed directly.
#
# ── Design rules this file exists to enforce ────────────────────────────────
#
# 1. NO CREDENTIAL EVER REACHES argv, A LOG, OR A REPORT.
#    Connections are made through PG* environment variables only. `pg_dump`,
#    `pg_restore` and `psql` are always invoked WITHOUT a DSN argument, so a
#    password can never appear in `ps`, in a shell history, or in a crash
#    trace. Everything written to a log passes through `dr_scrub`, which
#    replaces the live PGPASSWORD/PGUSER/PGHOST values and any `postgres://`
#    URL with `[REDACTED:<NAME>]`.
#
# 2. NO PLAINTEXT DUMP EVER TOUCHES THE DISK.
#    `pg_dump` writes to stdout and is piped straight into `gpg --symmetric`.
#    The only artifact on disk is the ciphertext.
#
# 3. THE ROW COUNTS AND THE DUMP COME FROM THE SAME SNAPSHOT.
#    A count taken in a separate transaction from a live production database
#    drifts, and a drifting baseline turns the restore assertion into noise
#    that operators learn to ignore. So a REPEATABLE READ session is opened,
#    `pg_export_snapshot()` is called, and `pg_dump --snapshot=` reuses it.
#    The table list, the row counts and the content checksums are all read
#    inside that same transaction. A mismatch at restore time is therefore a
#    REAL failure, never concurrent-write drift.
#
# 4. THE TABLE LIST IS READ FROM information_schema AT DUMP TIME.
#    A table added to production after this script was written cannot be
#    silently skipped by the verifier, because the verifier compares SETS of
#    table names, not just the counts of a hardcoded list.

set -euo pipefail

# ── Configuration (env-overridable so the scripts can be exercised in a
#    scratch directory without touching the real backup store) ──────────────
DR_ROOT="${DR_ROOT:-/srv/crawmatic/backups/dr}"
DR_SETS_DIR="$DR_ROOT/sets"
DR_REPORTS_DIR="$DR_ROOT/reports"
DR_KEY_FILE="${DR_KEY_FILE:-/root/.crawmatic-dr/backup.key}"

# PostgreSQL 18 client binaries. The distro default /usr/bin/pg_dump is 16.x
# and CANNOT dump either production server (both are 18.x) — it exits with a
# server-version error. Never fall back to PATH.
PG_BIN="${PG_BIN:-/usr/lib/postgresql/18/bin}"

# Railway bridge. Both production projects are reachable with the Railway-2
# token; `accounts.sh` is 0600 root-owned and exports token values only.
DR_RAILWAY_ACCOUNTS="${DR_RAILWAY_ACCOUNTS:-/root/.railway/accounts.sh}"
DR_RAILWAY_ENV="${DR_RAILWAY_ENV:-production}"

# Retention (see RUNBOOK.md §Retention). Sizing is deliberately conservative:
# the ops host runs at ~90% disk, so an unbounded backup directory is itself
# an outage.
DR_KEEP_HOURS="${DR_KEEP_HOURS:-24}"    # keep every set from the last N hours
DR_KEEP_DAYS="${DR_KEEP_DAYS:-7}"       # plus the newest set of each of the last N days
DR_MIN_KEEP="${DR_MIN_KEEP:-3}"         # never prune below this many sets, ever
DR_MAX_BYTES="${DR_MAX_BYTES:-1073741824}"   # 1 GiB hard budget for $DR_ROOT
DR_MIN_FREE_BYTES="${DR_MIN_FREE_BYTES:-2147483648}"  # refuse to dump under 2 GiB free

# Checksum sampling: full-table content checksums are computed for up to N
# tables, skipping tables above DR_CKSUM_MAX_ROWS (a checksum over millions of
# rows would make an hourly job expensive for no extra signal).
DR_CKSUM_TABLES="${DR_CKSUM_TABLES:-3}"
DR_CKSUM_MAX_ROWS="${DR_CKSUM_MAX_ROWS:-200000}"

# ── Targets ────────────────────────────────────────────────────────────────
# name|railway project id|railway service name
DR_TARGETS=(
  "engine|69dc4bda-0d97-4290-a82f-822ed97d3fb8|postgres"
  "saas|91debd0f-1dc4-4b7f-aa74-b9874ac27071|Postgres"
)

# Session settings applied to EVERY session that produces or verifies a
# checksum. `md5(row::text)` renders values with the session's formatting
# rules, so a timezone or float-digit difference between the dump host and the
# restore host would look exactly like data corruption. Pin them.
DR_CANONICAL_SETTINGS="SET TIME ZONE 'UTC'; SET DateStyle = 'ISO, YMD'; SET IntervalStyle = 'iso_8601'; SET extra_float_digits = 3; SET bytea_output = 'hex'; SET client_encoding = 'UTF8';"

# ── Logging ────────────────────────────────────────────────────────────────
dr_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Replace every live credential value with a named placeholder. Applied to all
# subprocess output before it reaches a log file or a report.
dr_scrub() {
  local -a args=()
  [[ -n "${PGPASSWORD:-}" ]] && args+=(-e "s|${PGPASSWORD//|/\\|}|[REDACTED:PGPASSWORD]|g")
  [[ -n "${PGUSER:-}"     ]] && args+=(-e "s|${PGUSER//|/\\|}|[REDACTED:PGUSER]|g")
  [[ -n "${PGHOST:-}"     ]] && args+=(-e "s|${PGHOST//|/\\|}|[REDACTED:PGHOST]|g")
  args+=(-e 's|postgres\(ql\)\?://[^[:space:]"]*|[REDACTED:DSN]|g')
  sed "${args[@]}"
}

dr_log() {
  local level="$1"; shift
  printf '%s %-5s %s\n' "$(dr_ts)" "$level" "$*" | dr_scrub
}

# The loud line. With no pager and no on-call rotation on this host, a
# non-zero exit plus this prefix in the log IS the alert (see RUNBOOK.md
# §Alerting — real alert routing is an owner gate).
dr_alert() {
  printf '%s ALERT DR-ALERT %s\n' "$(dr_ts)" "$*" | dr_scrub >&2
}

dr_die() { dr_alert "$*"; exit 1; }

dr_require_root() {
  [[ "$(id -u)" -eq 0 ]] || dr_die "must run as root (needs $DR_KEY_FILE and $DR_ROOT)"
}

# ── Encryption key ─────────────────────────────────────────────────────────
# Symmetric AES-256 via gpg. Honest caveat, repeated in the runbook: the key
# file lives on the SAME HOST as the ciphertext, so it protects the dumps
# against off-host copying (a leaked rsync target, a stolen disk image, a
# mis-scoped file share) but NOT against an attacker who already has root
# here. Off-host key escrow is an owner gate.
dr_ensure_key() {
  if [[ ! -s "$DR_KEY_FILE" ]]; then
    dr_die "encryption key $DR_KEY_FILE is missing or empty — run scripts/dr/install_schedule.sh --init-key"
  fi
  local mode; mode=$(stat -c '%a' "$DR_KEY_FILE")
  [[ "$mode" == "600" ]] || dr_die "encryption key $DR_KEY_FILE has mode $mode, expected 600"
  local owner; owner=$(stat -c '%u' "$DR_KEY_FILE")
  [[ "$owner" == "0" ]] || dr_die "encryption key $DR_KEY_FILE is not owned by root"
}

dr_gpg_encrypt_stdin() {  # $1 = output path
  gpg --batch --yes --quiet --no-tty \
      --pinentry-mode loopback --passphrase-file "$DR_KEY_FILE" \
      --symmetric --cipher-algo AES256 --compress-algo none \
      -o "$1"
}

dr_gpg_decrypt_stdout() { # $1 = input path
  gpg --batch --yes --quiet --no-tty \
      --pinentry-mode loopback --passphrase-file "$DR_KEY_FILE" \
      --decrypt "$1"
}

# ── Railway credential bridge ──────────────────────────────────────────────
# Reads the service's variables and exports PG* ONLY. `railway variables --kv`
# prints values, so its output is never echoed: it is captured into a shell
# variable, parsed, and the variable is unset.
dr_load_pgenv() {  # $1 = project id, $2 = service
  local project="$1" service="$2" vars
  # shellcheck disable=SC1090
  source "$DR_RAILWAY_ACCOUNTS" >/dev/null 2>&1 \
    || dr_die "cannot source $DR_RAILWAY_ACCOUNTS"
  [[ -n "${RAILWAY_TOKEN_RAILWAY2:-}" ]] \
    || dr_die "RAILWAY_TOKEN_RAILWAY2 not exported by $DR_RAILWAY_ACCOUNTS"

  vars=$(RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" \
         railway variables --service "$service" --project "$project" \
                 --environment "$DR_RAILWAY_ENV" --kv 2>/dev/null) \
    || dr_die "railway variables failed for project=$project service=$service"

  get() { grep -m1 -E "^$1=" <<<"$vars" | cut -d= -f2-; }
  PGUSER=$(get PGUSER);       export PGUSER
  PGPASSWORD=$(get PGPASSWORD); export PGPASSWORD
  PGDATABASE=$(get PGDATABASE); export PGDATABASE
  PGHOST=$(get RAILWAY_TCP_PROXY_DOMAIN); export PGHOST
  PGPORT=$(get RAILWAY_TCP_PROXY_PORT);   export PGPORT
  export PGCONNECT_TIMEOUT=20
  vars=""; unset vars

  [[ -n "$PGUSER" && -n "$PGPASSWORD" && -n "$PGDATABASE" && -n "$PGHOST" && -n "$PGPORT" ]] \
    || dr_die "incomplete PG* set for project=$project service=$service (names only: PGUSER/PGPASSWORD/PGDATABASE/RAILWAY_TCP_PROXY_DOMAIN/RAILWAY_TCP_PROXY_PORT)"
}

dr_clear_pgenv() {
  unset PGUSER PGPASSWORD PGDATABASE PGHOST PGPORT PGCONNECT_TIMEOUT || true
}

# ── psql helpers (never take a DSN; always inherit PG*) ─────────────────────
dr_psql() { "$PG_BIN/psql" -X -q -A -t -v ON_ERROR_STOP=1 "$@"; }

# ── Long-lived REPEATABLE READ session, used to share one snapshot between
#    the metadata queries and pg_dump. Output is routed through `\o <file>`
#    because psql closes (and therefore flushes) the previous output stream on
#    every `\o`, which gives a reliable "this result is ready" signal without
#    depending on stdio buffering behaviour.
dr_snap_open() {  # $1 = workdir
  DR_SNAP_WD="$1"
  mkfifo "$DR_SNAP_WD/in"
  dr_psql < "$DR_SNAP_WD/in" > "$DR_SNAP_WD/out" 2> "$DR_SNAP_WD/err" &
  DR_SNAP_PID=$!
  exec {DR_SNAP_FD}> "$DR_SNAP_WD/in"
  dr_snap_send "$DR_CANONICAL_SETTINGS"
  dr_snap_send "BEGIN ISOLATION LEVEL REPEATABLE READ;"
}

dr_snap_send() { printf '%s\n' "$*" >&"$DR_SNAP_FD"; }

# Run one query and block until its result file is COMPLETE.
#
# Completion is detected by an explicit end marker, not by the file simply
# existing: `\o <file>` creates the file the instant it is executed, long
# before the query it precedes has produced a single row. The marker is
# emitted by a trailing `SELECT` inside the same `\o` block, so it can only
# appear after every preceding statement has finished.
DR_SNAP_EOF='__DR_QUERY_COMPLETE__'

dr_snap_query() {  # $1 = output file, $2.. = SQL
  local out="$1"; shift
  rm -f "$out"
  dr_snap_send "\\o $out"
  dr_snap_send "$*"
  dr_snap_send "SELECT '$DR_SNAP_EOF';"
  dr_snap_send "\\o"
  local waited=0
  while true; do
    if [[ -s "$out" ]] && [[ "$(tail -n1 "$out")" == "$DR_SNAP_EOF" ]]; then
      sed -i "/^${DR_SNAP_EOF}\$/d" "$out"
      return 0
    fi
    sleep 0.2; waited=$((waited + 1))
    if (( waited > 1500 )); then   # 5 minutes
      dr_die "snapshot session query timed out: $(head -c 200 "$DR_SNAP_WD/err" 2>/dev/null | dr_scrub)"
    fi
    kill -0 "$DR_SNAP_PID" 2>/dev/null \
      || dr_die "snapshot session died: $(tail -c 400 "$DR_SNAP_WD/err" 2>/dev/null | dr_scrub)"
  done
}

dr_snap_close() {
  [[ -n "${DR_SNAP_FD:-}" ]] || return 0
  dr_snap_send "COMMIT;"
  exec {DR_SNAP_FD}>&-
  unset DR_SNAP_FD
  wait "$DR_SNAP_PID" 2>/dev/null || true
  unset DR_SNAP_PID
}

# ── SQL builders (shared by dump-side and restore-side so the two sides can
#    never drift apart) ─────────────────────────────────────────────────────
DR_SQL_TABLE_LIST="SELECT table_schema || '.' || table_name
  FROM information_schema.tables
 WHERE table_type = 'BASE TABLE'
   AND table_schema NOT IN ('pg_catalog', 'information_schema')
 ORDER BY 1;"

# Build a single UNION ALL count query from a newline-separated table list.
dr_build_count_sql() {  # stdin = table list, stdout = SQL
  local first=1 t schema name
  while IFS= read -r t; do
    [[ -n "$t" ]] || continue
    schema="${t%%.*}"; name="${t#*.}"
    if (( first )); then first=0; else printf ' UNION ALL '; fi
    printf "SELECT %s::text, count(*)::text FROM %s.%s" \
      "$(dr_sql_lit "$t")" "$(dr_sql_ident "$schema")" "$(dr_sql_ident "$name")"
  done
  (( first )) && printf "SELECT NULL::text, NULL::text WHERE false"
  printf ' ORDER BY 1;\n'
}

# Full-table content checksum: md5 of every row's text rendering, aggregated
# in a stable order. Independent of physical row order, so a faithful restore
# reproduces it exactly.
dr_build_cksum_sql() {  # $1 = qualified table name
  local schema="${1%%.*}" name="${1#*.}"
  printf "SELECT %s::text, coalesce(md5(string_agg(h, '' ORDER BY h)), 'EMPTY') FROM (SELECT md5(t.*::text) AS h FROM %s.%s t) s;\n" \
    "$(dr_sql_lit "$1")" "$(dr_sql_ident "$schema")" "$(dr_sql_ident "$name")"
}

dr_sql_ident() { printf '"%s"' "${1//\"/\"\"}"; }
dr_sql_lit()   { printf "'%s'" "${1//\'/\'\'}"; }

# ── Disk ───────────────────────────────────────────────────────────────────
dr_free_bytes() { df -PB1 "$1" | awk 'NR==2 {print $4}'; }
dr_dir_bytes()  { du -sb "$1" 2>/dev/null | awk '{print $1}'; }
dr_human()      { numfmt --to=iec --suffix=B "$1" 2>/dev/null || printf '%s' "$1"; }

# ═══════════════════════════════════════════════════════════════════════════
# C10 (F21) — measurement, retention and reporting for backups that run
# INSIDE Railway on the private network.
#
# Everything below is shared by BOTH sides of the new topology so the two can
# never drift:
#
#   apps/dr-backup/backup.sh   runs in Railway, dumps over `*.railway.internal`
#                              (no public egress), encrypts, keeps the volume
#                              retention, and posts a measured report.
#   scripts/dr/backup_prod.sh  runs on this ops host, PULLS the newest set from
#                              that service, keeps 2 days locally, and posts the
#                              pull leg of the same report.
#
# WHY MEASURE AT ALL: the deep dive measured 0.853 GB/day of idle egress from
# the production Postgres, almost all of it this backup pulling full logical
# dumps across the public TCP proxy every 4 hours. `bytes_exported` (plaintext
# out of pg_dump, before encryption) and `bytes_on_wire` (what the interface
# actually moved) are what make "we moved the backup onto the private network"
# a MEASURED claim instead of an assertion.
# ═══════════════════════════════════════════════════════════════════════════

# Retention on the backup volume (RUNBOOK §Retention). 4-hourly sets for 2
# days, one set per day for 14 days, one set per ISO week for 8 weeks.
DR_KEEP_WEEKS="${DR_KEEP_WEEKS:-0}"

# Report sink. NAMES only, never values: the bearer token is read from
# $DR_REPORT_TOKEN (exported by the caller's environment or by
# $DR_REPORT_TOKEN_FILE, 0600). With neither set the post is skipped with a
# WARN — a backup must never fail because a reporting endpoint is down.
DR_REPORT_URL="${DR_REPORT_URL:-}"                 # e.g. http://api.railway.internal:8000
DR_REPORT_PATH="${DR_REPORT_PATH:-/admin/ops/backup-report}"
DR_REPORT_TOKEN_FILE="${DR_REPORT_TOKEN_FILE:-}"
DR_REPORT_TIMEOUT="${DR_REPORT_TIMEOUT:-10}"

DR_REPORT_SCHEMA="crawmatic.backup-report.v1"

# ── Byte measurement ───────────────────────────────────────────────────────

# Pass stdin through to stdout, writing the exact byte count to $1 AFTER the
# stream has ended.
#
# Why a fifo and an explicit `wait` rather than `tee >(wc -c > file)`: a
# process substitution is not waited on by the shell, so the count file is
# read by the caller in a race with the counter still writing it — which shows
# up as an intermittently EMPTY bytes_exported, i.e. exactly the kind of
# silently-wrong measurement this function exists to prevent.
dr_pipe_count() {  # $1 = path to receive the byte count
  local out="$1" fifo rc=0
  fifo="$(mktemp -u "${TMPDIR:-/tmp}/dr-count.XXXXXX")"
  mkfifo -m 600 "$fifo"
  ( wc -c < "$fifo" | tr -d ' \n' > "$out" ) &
  local counter=$!
  tee "$fifo" || rc=$?
  wait "$counter" || true
  rm -f "$fifo"
  return "$rc"
}

# Bytes moved by every non-loopback interface since boot (rx + tx).
#
# This is the honest measurement available inside a container: /proc/net/dev
# is the kernel's own counter for this network namespace, so the delta across
# a dump is what the private connection actually carried — TLS framing,
# retransmits and all. It is deliberately NOT "bytes written by pg_dump":
# that number is bytes_exported, and the whole point of recording both is to
# see the difference.
dr_net_bytes() {
  awk 'NR>2 {
         split($0, f, ":");
         iface = f[1]; gsub(/^[ \t]+|[ \t]+$/, "", iface);
         if (iface == "lo") next;
         split(f[2], v, " ");
         total += v[1] + v[9];
       }
       END { printf "%d", total + 0 }' /proc/net/dev 2>/dev/null || printf '0'
}

# ── Retention (shared by the volume side and the host pull side) ───────────
#
# Keeps, in this order:
#   * every set newer than $DR_KEEP_HOURS hours,
#   * the newest set of each of the last $DR_KEEP_DAYS calendar days,
#   * the newest set of each of the last $DR_KEEP_WEEKS ISO weeks,
#   * never fewer than $DR_MIN_KEEP sets under any circumstances,
#   * never more than $DR_MAX_BYTES in the store (oldest pruned first, still
#     never below $DR_MIN_KEEP — a full disk must not be able to delete the
#     last backup that exists).
dr_prune_sets() {  # $1 = sets dir (default $DR_SETS_DIR), $2 = store root for the byte budget
  local sets_dir="${1:-$DR_SETS_DIR}" root="${2:-$DR_ROOT}"
  [[ -d "$sets_dir" ]] || return 0
  local now cutoff_h keep=() all=() s base epoch day week pruned=0
  local seen_days=() seen_weeks=()
  now=$(date -u +%s)
  cutoff_h=$(( now - DR_KEEP_HOURS * 3600 ))

  mapfile -t all < <(find "$sets_dir" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | sort -r)
  (( ${#all[@]} )) || return 0

  local idx=0
  for s in "${all[@]}"; do
    base=$(basename "$s"); base="${base#set-}"
    epoch=$(date -u -d "${base:0:4}-${base:4:2}-${base:6:2} ${base:9:2}:${base:11:2}:${base:13:2}" +%s 2>/dev/null || echo 0)
    day="${base:0:8}"
    week=$(date -u -d "@$epoch" +%G-W%V 2>/dev/null || echo "unknown")
    if (( idx < DR_MIN_KEEP )); then
      keep+=("$s")
    elif (( epoch >= cutoff_h )); then
      keep+=("$s")
    elif [[ ! " ${seen_days[*]-} " == *" $day "* ]] \
         && (( DR_KEEP_DAYS > 0 && epoch >= now - DR_KEEP_DAYS * 86400 )); then
      keep+=("$s")   # newest surviving set of that calendar day
    elif [[ ! " ${seen_weeks[*]-} " == *" $week "* ]] \
         && (( DR_KEEP_WEEKS > 0 && epoch >= now - DR_KEEP_WEEKS * 604800 )); then
      keep+=("$s")   # newest surviving set of that ISO week
    fi
    seen_days+=("$day"); seen_weeks+=("$week")
    idx=$((idx + 1))
  done

  for s in "${all[@]}"; do
    if [[ ! " ${keep[*]} " == *" $s "* ]]; then
      rm -rf "$s"; pruned=$((pruned + 1))
      dr_log INFO "pruned $(basename "$s") (outside retention window)"
    fi
  done

  local total; total=$(dr_dir_bytes "$root")
  while (( total > DR_MAX_BYTES )); do
    mapfile -t all < <(find "$sets_dir" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | sort)
    (( ${#all[@]} > DR_MIN_KEEP )) || {
      dr_log WARN "size budget $(dr_human "$DR_MAX_BYTES") exceeded (now $(dr_human "$total")) but only ${#all[@]} sets remain — refusing to prune below DR_MIN_KEEP=$DR_MIN_KEEP"
      break
    }
    rm -rf "${all[0]}"; pruned=$((pruned + 1))
    dr_log INFO "pruned $(basename "${all[0]}") (size budget)"
    total=$(dr_dir_bytes "$root")
  done

  dr_log INFO "retention: $pruned set(s) pruned, $(find "$sets_dir" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | wc -l) kept (hours=$DR_KEEP_HOURS days=$DR_KEEP_DAYS weeks=$DR_KEEP_WEEKS), store $(dr_human "$(dr_dir_bytes "$root")")"
}

# ── The measured backup report ─────────────────────────────────────────────
#
# Payload contract (`$DR_REPORT_SCHEMA`). Every field is a count, a duration or
# a name; NOTHING here is or contains a credential, a DSN or customer data —
# `tests/unit/test_backup_report_fields.py` is the enforcement of that claim.
#
#   schema          constant, so the receiver can reject a shape it predates
#   backup_set      set-<UTC>
#   stage           "backup" (produced inside Railway) | "pull" (fetched here)
#   source          which host/service produced this leg
#   private_network true when this leg never touched a public hostname
#   targets[]       name, bytes_exported, bytes_encrypted, bytes_on_wire,
#                   dump_seconds, server_version, alembic_head
#   totals          the same three byte counts summed, plus seconds
#   retention       the window this store keeps, and what it holds now
dr_backup_report_payload() {  # $1 stage, $2 source, $3 set name, $4 targets json file, $5 retention json
  local stage="$1" source="$2" set_name="$3" targets_file="$4" retention="${5:-{\}}"
  jq -n \
    --arg schema "$DR_REPORT_SCHEMA" \
    --arg set "$set_name" \
    --arg stage "$stage" \
    --arg source "$source" \
    --arg created "$(dr_ts)" \
    --argjson private "${DR_PRIVATE_NETWORK:-true}" \
    --slurpfile targets "$targets_file" \
    --argjson retention "$retention" '
      ($targets | if length == 1 and (.[0] | type) == "array" then .[0] else . end) as $t
      | {
          schema: $schema,
          backup_set: $set,
          stage: $stage,
          source: $source,
          created_utc: $created,
          private_network: $private,
          targets: $t,
          totals: {
            bytes_exported:  ([$t[].bytes_exported  // 0] | add // 0),
            bytes_encrypted: ([$t[].bytes_encrypted // 0] | add // 0),
            bytes_on_wire:   ([$t[].bytes_on_wire   // 0] | add // 0),
            seconds:         ([$t[].dump_seconds    // 0] | add // 0)
          },
          retention: $retention
        }'
}

# POST the payload. FAIL-SOFT BY DESIGN: a backup that succeeded is not made
# worse by a reporting endpoint that is down, so a failed post is a WARN and a
# non-zero return the caller is free to ignore — never a dr_die.
dr_post_backup_report() {  # $1 = payload file
  local payload="$1" token="" url
  [[ -s "$payload" ]] || { dr_log WARN "backup report: empty payload, not posted"; return 1; }
  if [[ -z "$DR_REPORT_URL" ]]; then
    dr_log WARN "backup report: DR_REPORT_URL unset — report kept locally only ($payload)"
    return 1
  fi
  if [[ -n "${DR_REPORT_TOKEN:-}" ]]; then
    token="$DR_REPORT_TOKEN"
  elif [[ -n "$DR_REPORT_TOKEN_FILE" && -s "$DR_REPORT_TOKEN_FILE" ]]; then
    token="$(<"$DR_REPORT_TOKEN_FILE")"; token="${token%%$'\n'*}"
  fi
  if [[ -z "$token" ]]; then
    dr_log WARN "backup report: no token (DR_REPORT_TOKEN / DR_REPORT_TOKEN_FILE unset) — not posted"
    return 1
  fi
  url="${DR_REPORT_URL%/}$DR_REPORT_PATH"
  local code
  # --data-binary @file, never the payload on argv; the token goes in a header
  # supplied on stdin-configured `-H @-`-free form below, and `-sS` keeps the
  # body out of the log while still surfacing transport errors.
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
              --max-time "$DR_REPORT_TIMEOUT" \
              -X POST "$url" \
              -H 'Content-Type: application/json' \
              -H "Authorization: Bearer $token" \
              --data-binary "@$payload" 2>&1 | tail -n1) || true
  token=""; unset token
  case "$code" in
    2*) dr_log INFO "backup report posted to \$DR_REPORT_URL$DR_REPORT_PATH (HTTP $code)"; return 0 ;;
    *)  dr_log WARN "backup report POST to \$DR_REPORT_URL$DR_REPORT_PATH returned '$code' — kept locally at $payload"; return 1 ;;
  esac
}

# ── Migration head ─────────────────────────────────────────────────────────
# Read inside the dump's own snapshot on the backup side, and out of the
# restored database on the verify side, so "the restore is at the revision the
# dump was taken at" is an assertion rather than a hope.
DR_SQL_ALEMBIC_HEAD="SELECT version_num FROM alembic_version ORDER BY version_num;"

# ── One dumped target, measured ────────────────────────────────────────────
#
# Moved here from backup_prod.sh by C10 so that the ops host (legacy fallback
# mode) and the in-Railway `dr-backup` service produce BYTE-IDENTICAL set
# layouts and manifests from ONE implementation. A restore drill that passes
# against a host-produced set and fails against a service-produced one because
# the two writers drifted is the exact failure this consolidation removes.
#
# Preconditions: PG* already exported for the target (the two callers load
# credentials differently — Railway CLI on the host, service variables inside
# Railway — and that difference is the ONLY thing they do differently).
# Emits: $2/<name>.dump.gpg and $3/<name>.meta.json
dr_dump_target() {  # $1 = name, $2 = stage dir, $3 = work dir
  local name="$1" stage="$2" work="$3"
  local out="$stage/$name.dump.gpg"
  local wd="$work/$name"; mkdir -p "$wd"

  local wire0; wire0=$(dr_net_bytes)

  dr_log INFO "[$name] opening REPEATABLE READ snapshot session"
  dr_snap_open "$wd"

  dr_snap_query "$wd/snap.txt"   "SELECT pg_export_snapshot();"
  local snapshot; snapshot=$(head -n1 "$wd/snap.txt")
  [[ -n "$snapshot" ]] || dr_die "[$name] pg_export_snapshot() returned nothing"

  dr_snap_query "$wd/version.txt" "SELECT current_setting('server_version');"
  local server_version; server_version=$(head -n1 "$wd/version.txt")

  # Migration head, read INSIDE the dump's own snapshot: this is what
  # verify_restore.sh asserts the restored database is at.
  #
  # Asked in TWO steps on purpose. The snapshot session runs with
  # ON_ERROR_STOP=1, and PostgreSQL resolves every relation in a statement at
  # PARSE time — so a single `CASE WHEN to_regclass(...) IS NULL THEN ... ELSE
  # (SELECT ... FROM alembic_version)` still errors on a database that has no
  # such table (the SaaS database does not: it is migrated by Prisma), which
  # would abort the whole snapshot and kill the backup. Probe first, read only
  # if it exists.
  local alembic_head="none"
  dr_snap_query "$wd/head_probe.txt" "SELECT coalesce(to_regclass('public.alembic_version')::text, '');"
  if [[ -s "$wd/head_probe.txt" ]] && [[ -n "$(head -n1 "$wd/head_probe.txt")" ]]; then
    dr_snap_query "$wd/head.txt" \
      "SELECT coalesce(string_agg(version_num, ',' ORDER BY version_num), 'none') FROM alembic_version;"
    alembic_head=$(head -n1 "$wd/head.txt" 2>/dev/null || true)
    [[ -n "$alembic_head" ]] || alembic_head="none"
  else
    dr_log INFO "[$name] no public.alembic_version relation — alembic_head recorded as 'none' (not an error: not every target is Alembic-migrated)"
  fi

  dr_snap_query "$wd/tables.txt" "$DR_SQL_TABLE_LIST"
  local ntables; ntables=$(grep -c . "$wd/tables.txt" || true)
  (( ntables > 0 )) || dr_die "[$name] information_schema reported zero base tables — refusing to record an empty manifest"
  dr_log INFO "[$name] $ntables base tables in snapshot $snapshot (server $server_version, alembic head $alembic_head)"

  dr_build_count_sql < "$wd/tables.txt" > "$wd/counts.sql"
  dr_snap_query "$wd/counts.txt" "$(cat "$wd/counts.sql")"

  local cks_sample
  mapfile -t cks_sample < <(
    awk -F'|' -v max="$DR_CKSUM_MAX_ROWS" '$2 > 0 && $2 <= max {print $2"|"$1}' "$wd/counts.txt" \
      | sort -t'|' -k1,1nr | head -n "$DR_CKSUM_TABLES" | cut -d'|' -f2-
  )
  local t i=0
  : > "$wd/cksums.txt"
  for t in "${cks_sample[@]:-}"; do
    [[ -n "$t" ]] || continue
    dr_snap_query "$wd/ck.$i.txt" "$(dr_build_cksum_sql "$t")"
    cat "$wd/ck.$i.txt" >> "$wd/cksums.txt"
    i=$((i + 1))
  done
  dr_log INFO "[$name] content checksums computed for $i table(s)"

  # ── The dump itself. Same snapshot as every number above; straight into
  #    gpg through the byte counter, so no plaintext copy is ever written to
  #    disk and `bytes_exported` is measured on the real stream.
  dr_log INFO "[$name] pg_dump -Fc --snapshot=<shared> | count | gpg --symmetric AES256"
  local t0 t1
  t0=$(date -u +%s)
  set +e
  "$PG_BIN/pg_dump" -Fc --snapshot="$snapshot" 2> "$wd/pg_dump.err" \
    | dr_pipe_count "$wd/bytes_exported" \
    | dr_gpg_encrypt_stdin "$out" 2> "$wd/gpg.err"
  local st=("${PIPESTATUS[@]}")
  set -e
  t1=$(date -u +%s)
  if [[ "${st[0]}" != 0 ]]; then
    dr_die "[$name] pg_dump exited ${st[0]}: $(tail -c 500 "$wd/pg_dump.err" | dr_scrub | tr '\n' ' ')"
  fi
  if [[ "${st[2]}" != 0 ]]; then
    dr_die "[$name] gpg exited ${st[2]}: $(tail -c 500 "$wd/gpg.err" | dr_scrub | tr '\n' ' ')"
  fi
  chmod 600 "$out"

  dr_snap_close

  local wire1; wire1=$(dr_net_bytes)
  local bytes_on_wire=$(( wire1 - wire0 ))
  (( bytes_on_wire >= 0 )) || bytes_on_wire=0

  # A dump that lists zero TABLE DATA sections is a failed backup, not a
  # backup. Verified THROUGH the encryption, so this also proves the
  # ciphertext decrypts. (`pg_restore -l` stops after the TOC, leaving gpg
  # writing into a closed pipe: its "Broken pipe" is expected here and only
  # here, so its stderr is dropped.)
  local sections
  sections=$(dr_gpg_decrypt_stdout "$out" 2>/dev/null | "$PG_BIN/pg_restore" -l 2>/dev/null | grep -c 'TABLE DATA' || true)
  (( sections >= 1 )) || dr_die "[$name] $out decrypts but contains no TABLE DATA sections"

  local bytes sha exported
  bytes=$(stat -c '%s' "$out")
  sha=$(sha256sum "$out" | cut -d' ' -f1)
  exported=$(cat "$wd/bytes_exported" 2>/dev/null || echo 0)
  [[ "$exported" =~ ^[0-9]+$ ]] || exported=0
  dr_log INFO "[$name] ok $(dr_human "$bytes") ciphertext from $(dr_human "$exported") plaintext, $(dr_human "$bytes_on_wire") on the wire, in $((t1 - t0))s, $sections table-data sections, sha256 ${sha:0:16}…"

  jq -n \
    --arg name "$name" --arg file "$name.dump.gpg" --arg sha "$sha" \
    --arg snapshot "$snapshot" --arg sv "$server_version" --arg head "$alembic_head" \
    --argjson bytes "$bytes" --argjson secs "$((t1 - t0))" \
    --argjson sections "$sections" \
    --argjson exported "$exported" --argjson wire "$bytes_on_wire" \
    --rawfile counts "$wd/counts.txt" --rawfile cksums "$wd/cksums.txt" '
      def kv: split("\n") | map(select(length > 0) | split("|"))
              | map({table: .[0], value: .[1]});
      {
        name: $name, file: $file, sha256: $sha, bytes: $bytes,
        bytes_exported: $exported, bytes_encrypted: $bytes, bytes_on_wire: $wire,
        dump_seconds: $secs, table_data_sections: $sections,
        snapshot: $snapshot, server_version: $sv, alembic_head: $head,
        tables: ($counts | kv | map({table: .table, rows: (.value | tonumber)})),
        checksums: ($cksums | kv | map({table: .table, md5: .value}))
      }' > "$work/$name.meta.json"
}

# ── Sidecar inspection (used by verify_restore.sh) ─────────────────────────
#
# These live here rather than inline in the verifier because they are the only
# two questions worth asking of a restored durable queue, and because
# `sqlite3` is NOT installed on the ops host — CPython's stdlib module is, on
# both the host and in the dr-backup image, so it is the portable answer.

dr_sqlite_integrity() {  # $1 = sqlite file -> "ok" or a ONE-LINE reason
  python3 - "$1" <<'PY' 2>&1 || true
import sqlite3, sys
# One line, always: this string ends up inside a DR-ALERT and a report table
# row, and a traceback pasted into a markdown table helps nobody.
try:
    con = sqlite3.connect(sys.argv[1])
    try:
        print(con.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        con.close()
except Exception as exc:
    print(f"NOT A READABLE SQLITE DATABASE: {type(exc).__name__}: {exc}")
PY
}

dr_sqlite_counts() {  # $1 = sqlite file -> "table=N, table=N" (rows still queued)
  python3 - "$1" <<'PY' 2>/dev/null || true
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
try:
    tables = sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    parts = []
    for t in tables:
        n = con.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
        parts.append(f"{t}={n}")
    print(", ".join(parts) or "no tables")
finally:
    con.close()
PY
}

# ── RLS backfill audit (B2-fix1 follow-up, 2026-09-08) ─────────────────────
#
# Production's fail-closed RLS silently filters DML issued by
# `crawmatic_migrate`: a migration that backfills with a bare UPDATE/INSERT
# reports "0 rows" and moves on. Harmless in production (those migrations ran
# before the policies existed) — but a DR restore that replays the whole chain
# onto an EMPTY database would silently skip every one of them, and the
# restore would look clean.
#
# Prints one `file|OP|table` line per occurrence. Parsed with `ast` so only
# real SQL string arguments count: `b6e5d1c94a72`'s docstring explains at
# length why a bare `UPDATE dispatch_intents` would be wrong, and a grep-based
# audit would report that prose as the very bug it warns about.
dr_rls_backfill_scan() {  # $1 = alembic/versions dir
  python3 - "$1" <<'PY'
import ast
import glob
import os
import re
import sys

versions = sys.argv[1]

# The RLS-protected tables are exactly those a migration enables policies on
# through app_shared.models.rls's emitters.
policy = re.compile(r"""emit_\w*rls\w*_policy\(\s*["']([a-z_][a-z0-9_]*)["']""", re.I)
rls = set()
for path in glob.glob(os.path.join(versions, "*.py")):
    with open(path, encoding="utf-8") as fh:
        rls.update(m.group(1) for m in policy.finditer(fh.read()))

dml = [
    ("UPDATE", re.compile(r"""\bUPDATE\s+(?:ONLY\s+)?["']?(?:public\.)?([a-z_][a-z0-9_]*)""", re.I)),
    ("INSERT", re.compile(r"""\bINSERT\s+INTO\s+["']?(?:public\.)?([a-z_][a-z0-9_]*)""", re.I)),
    ("DELETE", re.compile(r"""\bDELETE\s+FROM\s+["']?(?:public\.)?([a-z_][a-z0-9_]*)""", re.I)),
]

hits = set()
for path in sorted(glob.glob(os.path.join(versions, "*.py"))):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    for op, pat in dml:
                        for m in pat.finditer(sub.value):
                            if m.group(1) in rls:
                                hits.add((os.path.basename(path), op, m.group(1)))

for hit in sorted(hits):
    print("|".join(hit))
PY
}
