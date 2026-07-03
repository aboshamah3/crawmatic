# Contract: enqueue-by-name producer seam (`app_shared.messaging`)

Lets the API (and later the scheduler) enqueue Celery work by **task name** without importing `apps/workers` — the dependency boundary that keeps the worker (and its future scrapy-adjacent) import closure out of the API (Constitution I, `task_names` module's stated purpose).

## `enqueue(name, *, queue, kwargs=None) -> None`

- Lazily construct a module-level `celery.Celery(broker=Settings.REDIS_URL)` producer (no result backend needed) and `send_task(name, kwargs=kwargs, queue=queue)`.
- `name` comes from `app_shared.task_names` (`SCRAPE_DISPATCH_JOB`, etc.); `queue` is the target Celery queue (`"scrape_dispatch"`, `"maintenance"`).
- Import boundary: this module may import `celery` (the ban is scrapy/twisted/playwright/fastapi); `task_names.py` itself stays celery-free.

## Tests (`test_jobs_messaging.py`)

- `enqueue` routes to the right queue with the right task name + kwargs via a fake/patched producer.
- Import-boundary test: `app_shared.messaging` + `app_shared.jobs.*` import no scrapy/twisted/playwright/fastapi; the API router imports `app_shared.jobs.service`/`messaging`, never `apps.workers`.
