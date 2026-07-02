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

exec "$@"
