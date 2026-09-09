# apps/scrapers/Dockerfile — Scrapyd HTTP node (`price_monitor` project).
#
# Build context is the repo root (docker-compose.yml: build.context: .),
# because `uv sync --package scrapers` needs the whole uv workspace to
# resolve. The Scrapy project + scrapyd.conf are baked into the image at
# build time (COPY . . below covers apps/scrapers/**); there is no
# runtime `scrapyd-deploy`/addversion.json upload step for this
# skeleton (no spiders exist yet — later specs bake eggs the same way).
FROM python:3.13.5-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY . .

RUN uv sync --locked --package scrapers --no-dev

# Software inventory baked into the image (F04): pip freeze, dpkg -l — no
# browser on this image, so `image_inventory.py`'s browser section records
# `available: false` here, which is correct, not a gap.
RUN python scripts/security/image_inventory.py --out /app/packages.json

WORKDIR /app/apps/scrapers
RUN mkdir -p eggs logs dbs items \
    && chmod +x docker-entrypoint.sh

# Bake the spider egg into Scrapyd's eggs_dir at build time. Before this,
# every redeploy shipped an empty eggs/ and the project had to be manually
# re-registered via addversion.json after each `railway up`
# (SCRAPER_FIXES_REPORT_2026-07-27 operational gotcha) — until then the
# node answered every schedule.json with "project not found".
RUN mkdir -p eggs/price_monitor \
    && uvx --from scrapyd-client==2.0.3 scrapyd-deploy --build-egg eggs/price_monitor/1_0.egg

# Review R10 (2026-09-09): the durable result spool
# (`Settings.SCRAPE_RESULT_SPOOL_PATH`, default
# `/var/lib/crawmatic/spool/scrape_results.sqlite3`) is opened
# UNCONDITIONALLY by `BatchedPersistencePipeline.__init__`, i.e. while
# Scrapy is building the pipeline, before a spider has fetched anything.
# Nothing here created that directory and nothing mounted a volume at it,
# so every new spider on this image died at construction with a bare
# `[Errno 13]` on a path that appeared in neither this file nor
# docker-compose.yml. It is created (and owned by the runtime user)
# alongside the netledger buffer's directory, so the two durable queues
# live on one volume and one mount makes the host crash-safe -- the
# reason `config.py` placed them side by side in the first place.
RUN mkdir -p /var/lib/crawmatic/spool /var/lib/crawmatic/netledger

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app \
    && chown -R app:app /var/lib/crawmatic
USER app

EXPOSE 6800

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["scrapyd"]
