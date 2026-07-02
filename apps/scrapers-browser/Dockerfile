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
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY . .

RUN uv sync --locked --package scrapers-browser --no-dev

# Playwright's Chromium + required OS-level libraries, baked at build.
RUN playwright install --with-deps chromium

WORKDIR /app/apps/scrapers-browser
RUN mkdir -p eggs logs dbs items \
    && chmod +x docker-entrypoint.sh

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 6800

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["scrapyd"]
