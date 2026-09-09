# dr-backup — encrypted logical backups taken from INSIDE Railway (C10 / F21).
#
# ── Why postgres:18 and not 17, and how that was DETERMINED ────────────────
#
# Not assumed, not copied from the plan text, and not copied from
# `docker-compose.yml` (which pins `postgres:17.5-bookworm`). Read out of the
# newest stored dump itself on 2026-09-08:
#
#   $ gpg --decrypt .../sets/set-20260907T200001Z/engine.dump.gpg | head -c 512 | strings
#   PGDMP
#   railway
#   18.4 (Debian 18.4-1.pgdg13+1)        <- server the dump was taken FROM
#   18.4 (Ubuntu 18.4-1.pgdg24.04+1)     <- pg_dump that took it
#
#   $ jq '.targets|map_values(.server_version)' .../manifest.json
#   { "engine": "18.4 (Debian 18.4-1.pgdg13+1)",
#     "saas":   "18.6 (Debian 18.6-1.pgdg13+2)" }
#
# So production is PostgreSQL **18** on both databases; the plan's tech-stack
# line is right and the compose pin is STALE (recorded in
# `scripts/dr/RUNBOOK.md` §0, and already flagged by
# `scripts/dr/rehearse_upgrade.sh`, which reads the major from the dump on
# every run rather than trusting either file).
#
# The MAJOR is pinned here because pg_dump refuses a server newer than itself;
# the PATCH is deliberately left floating (`18-alpine`, not `18.4-alpine`) so a
# Railway-side patch upgrade — 18.4 to 18.6 already happened between the two
# databases — cannot break the backup overnight. A pinned patch would turn a
# provider maintenance window into a silent backup outage.
#
# The official image is used purely as a source of a matching `pg_dump`,
# `pg_restore` and `psql`; the server it ships is never started (ENTRYPOINT is
# reset, and the cron service's start command is `/app/backup.sh`).
FROM postgres:18-alpine

# gnupg  — symmetric AES-256 encryption of every dump before it lands
# python3 — sidecar capture (consistent sqlite snapshots, evidence listing)
#           and the token-protected file server (`serve.py`, stdlib only)
# jq, bash, curl, tar, coreutils — the shared dr_lib.sh contract
#           (coreutils: alpine's busybox `stat`/`wc`/`numfmt` differ enough
#           from GNU's that dr_lib.sh's byte accounting would silently skew)
RUN apk add --no-cache \
      bash \
      coreutils \
      curl \
      findutils \
      gnupg \
      jq \
      python3 \
      tar \
      tzdata

ENV DR_ROOT=/backups \
    PG_BIN=/usr/local/bin \
    DR_KEEP_HOURS=48 \
    DR_KEEP_DAYS=14 \
    DR_KEEP_WEEKS=8 \
    TZ=UTC

WORKDIR /app

# The shared library is COPIED IN rather than reimplemented: the ops host and
# this service must produce byte-identical set layouts and manifests, and the
# only way to guarantee that over time is one file. Build context is the repo
# root (see railway.json).
COPY scripts/dr/dr_lib.sh /app/dr_lib.sh
COPY apps/dr-backup/backup.sh /app/backup.sh
COPY apps/dr-backup/serve.py /app/serve.py
RUN chmod 0755 /app/backup.sh /app/serve.py && chmod 0644 /app/dr_lib.sh

VOLUME ["/backups"]

# Reset the base image's postgres entrypoint: this container is a client, not
# a server. Nothing starts a postmaster here.
ENTRYPOINT []
CMD ["/app/backup.sh"]
