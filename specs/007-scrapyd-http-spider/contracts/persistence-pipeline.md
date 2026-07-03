# Contract: batched persistence pipeline (`scrape_core.pipelines`)

`BatchedPersistencePipeline` — a Scrapy item pipeline that persists `ScrapeResult` items in **small batches**, never one commit per item (FR-016, §8, US5/SC-006, research D6).

## Flush policy

Flush when **either** threshold trips:
- **Size**: buffer reaches `SCRAPE_FLUSH_MAX_ITEMS` (default **50**, from `Settings`).
- **Time**: `SCRAPE_FLUSH_INTERVAL_SECONDS` (default **2.0**) elapsed since the last flush — driven by a Twisted `LoopingCall`.

Plus a **final flush** in `close_spider` so a partial last batch is never lost (US5 scenario 3). Thresholds are configuration (env/DB-tunable), not hardcoded constants.

## Flush transaction (reactor-safe)

Each flush is **one** `deferToThread` call (the D1 seam, `scrape_core.db.run_in_thread` + `workspace_txn`) executing a **single** transaction that:
1. Bulk-inserts the batch's `price_observations` rows (partition-routed by `scraped_at`).
2. Bulk-inserts the batch's `request_attempts` rows (partition-routed by `created_at`).
3. Upserts `match_current_prices` for each **successful** observation via `insert(...).on_conflict_do_update` on `unique(workspace_id, match_id)` — a failure/rejected item does **not** overwrite the current price (FR-014).

No DB call runs on the reactor thread (US5 scenario 2). Workspace context is set inside the transaction (RLS active).

## Guarantees

- Over N items, DB commit count ≪ N (SC-006): buffered flushes, not per-item.
- All N observations persist by spider close (final flush).
- Batches may mix matches of the same workspace for a run; the pipeline groups the upserts but keeps a single transaction per flush.

## Tests (unit)

- Flush triggers at N items and at T seconds; final flush at `close_spider` drains a partial buffer.
- N items → ≪ N flush transactions.
- The flush routes through the `deferToThread` seam (mocked) — never a direct reactor-thread commit.
