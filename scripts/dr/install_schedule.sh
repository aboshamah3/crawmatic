#!/usr/bin/env bash
# Install / remove the DR schedule on the ops host, and initialise the backup
# encryption key.
#
# EPA task W5.4. Deliberately a script rather than a wiki page: a schedule
# that only exists in prose is a schedule nobody can prove is installed, and
# `--status` / `--uninstall` make it as easy to remove as to add.
#
#   ./install_schedule.sh --init-key    create /root/.crawmatic-dr/backup.key (0600)
#   ./install_schedule.sh --install     write /etc/cron.d/crawmatic-dr
#   ./install_schedule.sh --status      show the installed schedule + key state
#   ./install_schedule.sh --uninstall   remove /etc/cron.d/crawmatic-dr (keeps backups & key)
#
# The cron file coexists with the two schedules that were already on this host
# (/etc/cron.d/crawmatic-saas-backup, /etc/cron.d/crawmatic-ops-alerts) and
# touches neither.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dr_lib.sh
source "$HERE/dr_lib.sh"

CRON_FILE="/etc/cron.d/crawmatic-dr"
BACKUP_LOG="/var/log/crawmatic-dr-backup.log"
VERIFY_LOG="/var/log/crawmatic-dr-verify.log"

# cron's default PATH cannot find the PostgreSQL 18 client binaries, the
# Railway CLI (installed under nvm), or gpg's helpers. The nightly SaaS backup
# on this same host failed silently on exactly that mistake on 2026-08-17.
CRON_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin:/usr/lib/postgresql/18/bin:/root/.nvm/versions/node/v22.16.0/bin"

init_key() {
  install -d -m 700 "$(dirname "$DR_KEY_FILE")"
  if [[ -s "$DR_KEY_FILE" ]]; then
    echo "key already exists at $DR_KEY_FILE — refusing to overwrite"
    echo "(overwriting it would make every existing encrypted dump unreadable)"
    return 1
  fi
  ( umask 077; openssl rand -base64 48 > "$DR_KEY_FILE" )
  chmod 600 "$DR_KEY_FILE"
  echo "created $DR_KEY_FILE ($(stat -c '%a %U:%G' "$DR_KEY_FILE"))"
  cat <<'EOF'

KEY CUSTODY CAVEAT — read this, it is not boilerplate:

  This passphrase file lives on the SAME HOST as the ciphertext it protects.
  That means the encryption defends the dumps against being copied OFF this
  host (a mis-scoped rsync target, a stolen disk image, a shared filesystem)
  and defends nothing at all against an attacker who already has root here.

  It also means that losing this host loses BOTH the backups and the key.
  Until the key is escrowed somewhere this host cannot reach, the encryption
  is a confidentiality control, NOT a durability control.

  Escrowing this key off-host (and moving to a managed secret manager) is an
  OWNER GATE. See scripts/dr/RUNBOOK.md.
EOF
}

install_cron() {
  [[ -s "$DR_KEY_FILE" ]] || { echo "run --init-key first" >&2; exit 1; }
  cat > "$CRON_FILE" <<EOF
# Crawmatic disaster recovery — EPA task W5.4 (READY-010, P0.8).
#
# The scripts live in the engine repo (scripts/dr/) so they are reviewed and
# versioned like any other code; only the encryption key lives on the host, at
# $DR_KEY_FILE (0600, root-owned).
#
# CADENCE AND THE RPO GAP — stated here so nobody reads this file and believes
# the approved RPO is met:
#   Approved target ......... RPO <= 15 minutes
#   Delivered by this cron .. RPO <= 4 hours (worst case: a failure 3h59m
#                             after the last successful set loses that window)
#   Why ..................... These are FULL LOGICAL DUMPS of two production
#                             databases pulled over the Railway TCP proxy onto
#                             a host with ~7 GB free. A 15-minute RPO requires
#                             continuous WAL archiving or provider PITR, which
#                             is a provider-side capability, not something a
#                             cron job on this box can synthesise.
#   Closing it .............. OWNER GATE: enable Railway/provider PITR (or WAL
#                             archiving to off-site immutable storage).
#                             See scripts/dr/RUNBOOK.md.
SHELL=/bin/bash
PATH=$CRON_PATH
MAILTO=root

# Backup: every 4 hours, on the hour.
0 */4 * * * root flock -n /run/crawmatic-dr-backup.lock $HERE/backup_prod.sh >> $BACKUP_LOG 2>&1

# Restore verification: daily at 03:20 UTC, ~20 minutes after the 03:00 set so
# it verifies a fresh backup and never races the dump that produced it.
20 3 * * * root flock -n /run/crawmatic-dr-verify.lock $HERE/verify_restore.sh >> $VERIFY_LOG 2>&1

# Host-side credential file-permission sweep: daily at 04:10 UTC.
10 4 * * * root $HERE/../security/check_file_permissions.sh --mode host --root /srv/crawmatic >> $VERIFY_LOG 2>&1
EOF
  chmod 644 "$CRON_FILE"
  touch "$BACKUP_LOG" "$VERIFY_LOG"; chmod 640 "$BACKUP_LOG" "$VERIFY_LOG"
  echo "installed $CRON_FILE"
  echo "--- $CRON_FILE"
  cat "$CRON_FILE"
}

uninstall_cron() {
  rm -f "$CRON_FILE"
  echo "removed $CRON_FILE (backups under $DR_ROOT and the key at $DR_KEY_FILE are untouched)"
}

status() {
  echo "== cron =="
  if [[ -f "$CRON_FILE" ]]; then grep -vE '^\s*#' "$CRON_FILE" | grep -E '\S'; else echo "(not installed)"; fi
  echo
  echo "== key =="
  if [[ -s "$DR_KEY_FILE" ]]; then
    echo "$DR_KEY_FILE  mode=$(stat -c '%a' "$DR_KEY_FILE")  owner=$(stat -c '%U' "$DR_KEY_FILE")  bytes=$(stat -c '%s' "$DR_KEY_FILE")"
  else
    echo "(missing — run --init-key)"
  fi
  echo
  echo "== backup store =="
  if [[ -d "$DR_SETS_DIR" ]]; then
    echo "$DR_ROOT  $(dr_human "$(dr_dir_bytes "$DR_ROOT")")  $(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | wc -l) set(s)"
    find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | sort | tail -5 | while read -r d; do
      echo "  $(basename "$d")  $(dr_human "$(dr_dir_bytes "$d")")  mode=$(stat -c '%a' "$d")"
    done
  else
    echo "(no sets yet)"
  fi
}

case "${1:---status}" in
  --init-key)  init_key ;;
  --install)   install_cron ;;
  --uninstall) uninstall_cron ;;
  --status)    status ;;
  *) echo "usage: $0 [--init-key|--install|--uninstall|--status]" >&2; exit 2 ;;
esac
