# Contract: per-request robots middleware (`scrape_core.robots`)

`RobotsPolicyMiddleware` — a custom Scrapy **downloader** middleware resolving `robots_policy` **per request** from the competitor config the spider loaded, NOT Scrapy's process-global `ROBOTSTXT_OBEY` (FR-006, §8, research D7).

## Settings

- `ROBOTSTXT_OBEY = False` in `price_monitor/settings.py` (the global toggle is disabled).
- `RobotsPolicyMiddleware` registered in `DOWNLOADER_MIDDLEWARES`.

## Policy (existing `app_shared.enums.RobotsPolicy`)

| `robots_policy` | Behavior |
|-----------------|----------|
| `RESPECT` | fetch/parse the domain's robots rules; a disallowed path is **skipped and recorded** (error code `BLOCKED`), no observation with a price |
| `IGNORE_AFTER_APPROVAL` | fetch regardless (competitor approved to ignore robots) |
| `REVIEW_REQUIRED` | treat as not-yet-approved → skip/record (conservative) |

The policy is read from the per-competitor config attached to the request meta, so different competitors in the same spider run get different policies (the whole point vs. the global toggle).

## Testability

The robots fetcher is injectable so fixtures supply a robots body without a network call (FR-021). Unit tests: `RESPECT` skips a disallowed path; `IGNORE_AFTER_APPROVAL` fetches; policy is read per-request (two competitors, two policies, one run).
