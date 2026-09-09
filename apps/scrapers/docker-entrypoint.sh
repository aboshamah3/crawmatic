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

sed \
  -e "s/__SCRAPYD_USERNAME__/${SCRAPYD_USERNAME}/g" \
  -e "s/__SCRAPYD_PASSWORD__/${SCRAPYD_PASSWORD}/g" \
  scrapyd.conf > scrapyd.conf.rendered
mv scrapyd.conf.rendered scrapyd.conf

# A scrapyd crash leaves twistd.pid behind, and Railway restarts reuse the
# container's writable layer, so the stale pidfile blocks every subsequent
# start ("Another twistd server is running, PID 1"). Safe to remove here:
# this runs before scrapyd starts, and nothing else runs in this container.
rm -f twistd.pid

# Review R10 (2026-09-09): the durable result spool must be writable BEFORE
# scrapyd starts accepting schedules. `ResultSpool` refuses to build a
# pipeline over an unwritable directory, but that check is per-spider and
# after the fact -- the daemon would come up healthy and then kill every job
# in `BatchedPersistencePipeline.__init__`, one `[Errno 13]` per per-job log.
# A node that cannot spool has nowhere to put results it has already paid to
# fetch, so it refuses to start instead, once, on stderr.
python -m scrape_core.result_spool

exec "$@"
