# apps/scrapers-browser/Dockerfile — Scrapyd browser node
# (`price_monitor_browser` project, scrapy-playwright + Chromium).
#
# Build context is the repo root (docker-compose.yml: build.context: .),
# because `uv sync --package scrapers-browser` needs the whole uv
# workspace to resolve. The Scrapy project + scrapyd.conf are baked into
# the image at build time (COPY . . below covers
# apps/scrapers-browser/**); there is no runtime
# `scrapyd-deploy`/addversion.json upload step for this skeleton (no
# spiders exist yet — later specs bake eggs the same way). Playwright's
# Chromium browser + OS deps are installed at build time so no
# first-request download/install happens at runtime.
FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    # Shared browser location, NOT the installing user's ~/.cache: the
    # build installs as root but the container runs as `app` (home
    # /app), so a default install lands in /root/.cache/ms-playwright
    # while runtime looks in /app/.cache/ms-playwright -- every launch
    # then failed "Executable doesn't exist" with the browsers present
    # but unreachable in the image. Pinning the path (and chowning it
    # below) keeps build and runtime pointed at the same directory.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY . .

RUN uv sync --locked --package scrapers-browser --no-dev

# Playwright's Chromium + required OS-level libraries, baked at build.
RUN playwright install --with-deps chromium

# READY F01 build gate: prove IN THIS IMAGE that Chromium cannot egress
# past `scrape_core.browser.egress_guard` — redirect-to-loopback, a
# privately-resolving sub-resource, service workers, popups, and a proxied
# context whose leg must really be forwarded upstream. The guard's
# whole value depends on Chromium honouring the `--proxy-server` /
# `--proxy-bypass-list=<-loopback>` launch arguments this image's settings
# module sets, which only a real browser can demonstrate; so if the guard
# is bypassable, THE IMAGE DOES NOT BUILD.
#
# Needs no database, Redis or Scrapyd (a build stage has none) — the test
# supplies its own fixture HTTP server and its own settings environment.
# `-p no:cacheprovider` keeps the build layer free of a .pytest_cache.
#
# The sync above is `--no-dev`, so pytest is absent: pull the dev group in
# for the gate and strip it again in the SAME layer, so the shipped image
# still carries no test dependencies.
RUN uv sync --locked --package scrapers-browser \
    && .venv/bin/pytest tests/integration/test_browser_egress_guard_image.py \
        -m browser -q -p no:cacheprovider \
    && uv sync --locked --package scrapers-browser --no-dev

# Software inventory baked into the image (F04): pip freeze, dpkg -l, and
# the ACTUAL installed Playwright/Chromium versions — run after the
# egress-guard gate above so it reflects the final --no-dev environment,
# not the temporary pytest-carrying one.
RUN python scripts/security/image_inventory.py --out /app/packages.json

WORKDIR /app/apps/scrapers-browser
RUN mkdir -p eggs logs dbs items \
    && chmod +x docker-entrypoint.sh

# Bake the spider egg into Scrapyd's eggs_dir at build time. Before this,
# every redeploy shipped an empty eggs/ and the project had to be manually
# re-registered via addversion.json after each `railway up`
# (SCRAPER_FIXES_REPORT_2026-07-27 operational gotcha) — until then the
# node answered every schedule.json with "project not found".
RUN mkdir -p eggs/price_monitor_browser \
    && uvx --from scrapyd-client==2.0.3 scrapyd-deploy --build-egg eggs/price_monitor_browser/1_0.egg

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app \
    && chown -R app:app /ms-playwright
USER app

EXPOSE 6800

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["scrapyd"]
