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
