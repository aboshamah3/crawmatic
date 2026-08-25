-- Amazon/S-Tech full run 1f8cdb7b-928f-4979-a0f7-055492eabf5c
-- UPDATED 2026-08-09 ~20:40 after proxy top-up: job is RUNNING and healthy
-- (61/61 attempts succeeding). STEP 1 (cancel) is OBSOLETE — do not run it.
--
-- Remaining: requeue the 559 outage-era failures (UNKNOWN_ERROR = curl 407,
-- PROXY_FAILED = tunnel refused) + 4 timeouts. Leaves PRICE_NOT_FOUND (84,
-- of which 78 correctly recorded OUT_OF_STOCK) and HTTP_404 (3) alone.
-- recover_stalled_batches re-dispatches PENDING targets on RUNNING jobs
-- automatically (allow up to ~stall-timeout + amazon rate gate pacing).
BEGIN;
UPDATE scrape_job_targets
SET status = 'PENDING', locked_at = NULL, started_at = NULL,
    completed_at = NULL, error_code = NULL
WHERE scrape_job_id = '1f8cdb7b-928f-4979-a0f7-055492eabf5c'
  AND status = 'FAILED'
  AND error_code IN ('UNKNOWN_ERROR', 'PROXY_FAILED', 'TIMEOUT');
COMMIT;
