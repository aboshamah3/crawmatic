# Amazon/S-Tech Full Price Run — Final Report

**Date:** 2026-08-09 (job created 13:45, finished ~23:15 UTC)
**Job:** `1f8cdb7b-928f-4979-a0f7-055492eabf5c` — 1,628 targets, final status `PARTIAL_FAILED`
**Plan:** `matching/PLAN_AMAZON_PRICE_FIX_2026-08-09.md` · SQL: `run_amazon_stech_full_2026-08-09.sql`, `pause_resume_amazon_job_2026-08-09.sql`

## Outcome

| Metric | Value |
|---|---|
| Targets completed with price | **1,439 / 1,628 (88.4%)** |
| Successful price rows written since deploy | 1,448 |
| Listings recorded OUT_OF_STOCK | **392** (plugin shows "غير متوفر" + struck-through last price) |
| Failed | 183 — 174 PRICE_NOT_FOUND (mostly genuine OOS), 4 dead links (404), 3 timeout, 1 unknown, 1 blocked |
| Skipped | 6 |

Genuine scrape-error rate after the fixes: **5 of 1,628 (0.3%)**.

## What shipped today

1. **twistd.pid crash-loop — root-caused & permanently fixed.** Scrapyd's crash leaves `twistd.pid` in the container's writable layer; Railway crash-restarts reuse that layer, so every restart hit "Another twistd server is running, PID 1". `apps/scrapers/docker-entrypoint.sh` now removes the pidfile before start. *(Deployed but uncommitted — commit it.)*
2. **OOS feature deployed end-to-end** (`railway up` scrapers + api after 1,920 unit tests passed): spider OOS sniffing, pipeline upsert preserving last-known price, API `stock_status`/`success` fields, plugin v0.3.1 installed. Verified live: 392 OOS rows recorded.
3. **Scoped selector + `"priceAmount"` regex** (SQL applied 13:45): the carousel mispricing bug is **confirmed dead**. The three RTX 5080 listings now read 7,990.54 / 8,058.33 / 6,450.47 SAR (previously 281.95); the 6,450.47 was hand-verified against the live page — a real price drop, not an artifact.

## Incident during the run

DataImpulse proxy traffic exhausted mid-run (`407 TRAFFIC_EXHAUSTED`, 5 GiB plan fully used) — every attempt failed for ~5 hours. User topped up (~53 GB added); the 563 outage-era failures were requeued via `pause_resume_amazon_job_2026-08-09.sql` and all drained successfully (61/61 attempt success immediately after top-up).

## Follow-ups

1. **250 ACTIVE amazon matches have no price row** — pushed to prod by matching phase-2 batches *after* this job was created, so they were never targeted. Include them in the next scrape job / refresh sweep.
2. **16 extraction-gap pages** — failed PRICE_NOT_FOUND but recorded *neither* price *nor* OUT_OF_STOCK; possibly a third page layout:
   B00061RWRC, B0009RMKBG, B00GE4F1AU, B07S8DXNBS, B07YFW5HBN, B0856S249V, B085HHDWSJ, B085XJ3NN9, B0966SRSH9, B09YYWSS62, B0B3DRLHTC, B0C1K8H13G, B0D6RWH4H7, B0DDTVND83, B0DKDJTWYD, B004O4P7UG
3. **2 dead links** (HTTP 404, candidates for match archive): B00SXGUSDM, B01FDHMHTQ.
4. **Product decision pending:** OOS competitors with a last-known price still count in cheapest/average aggregates.
5. **Commit the working tree** — entrypoint fix + OOS code are live in prod but uncommitted in the repo.
