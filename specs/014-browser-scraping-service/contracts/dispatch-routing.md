# Contract — Dispatch routing fix (US2, FR-015/016)

`apps/workers/app/workers/tasks_jobs.py`

## Defect

Both `dispatch_job` and `recover_stalled_batches` already pick the node pool by mode
(`SCRAPYD_BROWSER_URLS` if `batch.mode == BROWSER` else `SCRAPYD_HTTP_URLS`) but pass the **hardcoded**
HTTP constants `_SCRAPYD_PROJECT = "price_monitor"` / `_GENERIC_PRICE_SPIDER =
"generic_price_spider"` to `client.schedule(...)` for **every** batch. A browser batch is thus sent to
a browser node but told to run the HTTP project/spider (which the browser image does not deploy).

## Fix (surgical)

Add module constants:

```python
_SCRAPYD_BROWSER_PROJECT = "price_monitor_browser"
_GENERIC_BROWSER_SPIDER = "generic_browser_price_spider"
```

In both tasks, inside the `for batch in batches:` loop, choose project+spider by mode alongside the
existing node choice:

```python
if batch.mode == ScrapeProfileMode.BROWSER:
    project, spider, nodes = _SCRAPYD_BROWSER_PROJECT, _GENERIC_BROWSER_SPIDER, settings.SCRAPYD_BROWSER_URLS
else:
    project, spider, nodes = _SCRAPYD_PROJECT, _GENERIC_PRICE_SPIDER, settings.SCRAPYD_HTTP_URLS
node_url = select_node(batch.domain, nodes)
client.schedule(project, spider, workspace_id=..., scrape_job_id=..., match_ids=batch.match_ids,
                mode=batch.mode, batch_index=batch.batch_index, node_url=node_url)
```

`recover_stalled_batches` uses the same selection with its `batch_index=f"{batch.batch_index}:r{window}"`.

## Unchanged (must stay identical)

- `plan_batches` — batches are already mode-pure (grouped by `(domain, mode)`); no change.
- `select_node` deterministic node selection.
- `ScrapydDispatchClient.schedule` claim/commit/release idempotency on
  `dispatched:{scrape_job_id}:{batch_index}` — a retried/re-dispatched browser batch returns the
  persisted jobid, never double-runs (FR-016, SC-008).
- Basic-auth on every `schedule.json` (client already authenticates both pools identically).

## Acceptance mapping

- US2 AS1 — mixed job: browser batches → `(price_monitor_browser, generic_browser_price_spider)` on
  `SCRAPYD_BROWSER_URLS`; HTTP batches → `(price_monitor, generic_price_spider)` on `SCRAPYD_HTTP_URLS`;
  no batch mixes modes.
- US2 AS2 — browser batch authenticates + reuses the idempotency guard + match locks; retry no double-run.
- US2 AS3 — all-HTTP domain: nothing sent to the browser pool.
- SC-002 — 100% of browser targets to the browser service, 0% to HTTP, and conversely.
