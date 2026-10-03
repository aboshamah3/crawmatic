#!/bin/sh
# Renders the baked scrapyd.conf template (placeholder tokens
# __SCRAPYD_USERNAME__ / __SCRAPYD_PASSWORD__) with the real HTTP
# basic-auth credentials from the environment, then execs the given
# command (scrapyd). scrapyd.conf is a plain ini file with no native
# env-var interpolation, so substitution happens here at container
# start rather than at build time (SCRAPYD_USERNAME/SCRAPYD_PASSWORD
# are only known at runtime, via `.env` / compose `environment:`).
set -eu

: "${SCRAPYD_USERNAME:?SCRAPYD_USERNAME is required}"
: "${SCRAPYD_PASSWORD:?SCRAPYD_PASSWORD is required}"

# Scrapyd process concurrency for this node (2026-09-29, E3.4). Each running
# spider is a whole Chromium, so this is a memory decision: size it from the
# instance's measured memory (see docs/ops/CAPACITY.md, "Sizing max_proc on
# the browser node"). Default 1 = the value baked here before it was a knob.
SCRAPYD_MAX_PROC="${SCRAPYD_MAX_PROC:-1}"
case "$SCRAPYD_MAX_PROC" in
  ''|*[!0-9]*|0) echo "SCRAPYD_MAX_PROC must be a positive integer, got '$SCRAPYD_MAX_PROC'" >&2; exit 64 ;;
esac

sed \
  -e "s/__SCRAPYD_USERNAME__/${SCRAPYD_USERNAME}/g" \
  -e "s/__SCRAPYD_PASSWORD__/${SCRAPYD_PASSWORD}/g" \
  -e "s/__SCRAPYD_MAX_PROC__/${SCRAPYD_MAX_PROC}/g" \
  scrapyd.conf > scrapyd.conf.rendered
mv scrapyd.conf.rendered scrapyd.conf

# Review R10 (2026-09-09): the durable result spool must be writable BEFORE
# scrapyd starts accepting schedules. `ResultSpool` refuses to build a
# pipeline over an unwritable directory, but that check is per-spider and
# after the fact -- the daemon would come up healthy and then kill every job
# in `BatchedPersistencePipeline.__init__`, one `[Errno 13]` per per-job log.
# A node that cannot spool has nowhere to put results it has already paid to
# fetch, so it refuses to start instead, once, on stderr.
python -m scrape_core.result_spool

exec "$@"
