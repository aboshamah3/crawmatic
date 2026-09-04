"""Usage-export aggregation (PLAN §7.2) — all of it in SQL (risk P2).

Shape of the answer, per PLAN §5.3: one row per
`(workspace_id, product_id, cycle_ts)` describing one **product check
cycle**, with the link counters the SaaS prices from and the
success flag that decides whether a credit is consumed at all.

Three facts drive the query, all verified against the live schema:

1. `request_attempts` has `match_id`, not `product_id` — product
   attribution comes from joining `competitor_product_matches`.
2. A *link* is a match, not an attempt. Retries write several
   `request_attempts` rows for one `match_id`, so the inner CTE folds
   attempts per match with `bool_or` and the outer aggregate counts
   folded rows. Without this a retried link would be billed twice.
3. `check_successful` is read from `price_observations`, not from
   attempt success: the Fairness law (PLAN §5.1.4) consumes a credit
   only for a successful **price observation**.
4. `request_attempts.origin` (Task 2.3) must be `'scrape'`. The domain-
   strategy discovery probe ladder also writes `request_attempts` rows
   now (`origin='discovery'`, with real `match_id`s), and that traffic
   is internal COGS, not customer activity — it must never inflate a
   customer's `links_total`/`protected_links_attempted`.

`cycle_ts` is `date_trunc('hour', COALESCE(scrape_jobs.created_at,
request_attempts.created_at))`. Hour truncation makes the export
idempotent (re-exporting a window yields byte-identical rows) and is
collision-free at every cadence the SaaS sells — the fastest is every
6 hours (PLAN §5.2 `FREQUENCIES`), so two genuine cycles can never
share a bucket, while retries of one cycle correctly collapse into it.

Partition-awareness (risk P2): the only predicate on the partitioned
`request_attempts`/`price_observations` is a bounded range on their
partition keys (`created_at` / `scraped_at`), so Postgres prunes to the
one or two monthly partitions the window touches. The window is capped
at 31 days by `validate_window`.

Deviation from the brief's reference implementation: neither of the two
fallbacks the brief anticipated (`func.count().filter(...)` rejected;
`.having()` on a tuple comparison compiling badly) actually triggers on
this repo's SQLAlchemy 2.0.51 — both compile cleanly, so the query shape
is unchanged from the brief.

This is the most deliberately cross-workspace query in the repo: the
export aggregates over every workspace in one statement, so the
`per_link`/`per_check` CTEs' leading `select(...)` calls carry an
explicit `# noqa: workspace-scope` marker even though the CI guard
(`scripts/check_workspace_scoping.py`) would not flag them anyway — it
only matches `select(Model)`, not `select(Model.column, ...)` — so the
markers exist purely to make the intent explicit to a human reader, not
to satisfy the guard's AST pattern.

Every value that reaches this query — the window bounds, the cursor
position, and the protected-method set — is passed as a **bound
parameter**, never interpolated into SQL text. `.in_()` on the fixed
`PROTECTED_ACCESS_METHODS` tuple compiles to an expanding bind param
(`IN (__[POSTCOMPILE_access_method_1])`), which is correct and is what
we want; the tests that need to see the two method names assert against
a `literal_binds=True` compilation rather than asking this module to
inline them. Production SQL is not shaped to make a string assertion
convenient.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import BigInteger, Select, and_, cast, func, literal, select, tuple_

from app_shared.enums import AccessMethod, RequestOrigin
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob
from app_shared.models.network_operations import NetworkOperation, NetworkTransport
from app_shared.models.observations import PriceObservation, RequestAttempt

MAX_WINDOW_DAYS = 31
DEFAULT_USAGE_LIMIT = 500
MAX_USAGE_LIMIT = 1000

#: Access methods that ride the paid residential proxy. Everything else
#: (DIRECT_HTTP, DIRECT_HTTP_RETRY) is compute-only and near-free.
PROTECTED_ACCESS_METHODS = (
    AccessMethod.PROXY_HTTP.value,
    AccessMethod.PLAYWRIGHT_PROXY.value,
)

#: `network_operations.transport` values billed against the fleet's proxy
#: spend (B2) — the same two facts `proxied_http_attempted` /
#: `proxied_browser_attempted` / `proxy_bytes` (Task B3) report per cycle.
#: `DIRECT` is fleet egress with no proxy cost and is excluded.
PROXIED_TRANSPORTS = (
    NetworkTransport.PROXY.value,
    NetworkTransport.BROWSER.value,
)


class InvalidUsageCursor(ValueError):
    """The `cursor` query parameter was not a token we issued."""


class UsageWindowTooLarge(ValueError):
    """`until - since` exceeded `MAX_WINDOW_DAYS`."""


class InvalidUsageWindow(ValueError):
    """`since`/`until` are inverted or equal (`until <= since`).

    Distinct from `UsageWindowTooLarge` (review finding I6b): an
    inverted window is malformed, not "too large" -- conflating the two
    made the router return the misleading `422 WINDOW_TOO_LARGE` for a
    caller that simply swapped its query params.
    """


class UsageCursor(NamedTuple):
    """Keyset position in the aggregate's natural sort order."""

    cycle_ts: datetime
    workspace_id: uuid.UUID
    product_id: uuid.UUID


def clamp_usage_limit(requested: int | None) -> int:
    if requested is None:
        return DEFAULT_USAGE_LIMIT
    return max(1, min(int(requested), MAX_USAGE_LIMIT))


def validate_window(since: datetime, until: datetime) -> None:
    """Reject an inverted or over-long window (PLAN §7.2, risk P2).

    Raises `InvalidUsageWindow` for `until <= since` (malformed) and
    `UsageWindowTooLarge` for `until - since > MAX_WINDOW_DAYS` (the
    real over-long case) -- two distinct exceptions so the router can
    map them to two distinct, honest error codes (review finding I6b).
    """
    if until <= since:
        raise InvalidUsageWindow("`until` must be after `since`.")
    if until - since > timedelta(days=MAX_WINDOW_DAYS):
        raise UsageWindowTooLarge(
            f"The usage window may not exceed {MAX_WINDOW_DAYS} days."
        )


def normalize_window(since: datetime, until: datetime) -> tuple[datetime, datetime]:
    """Treat a naive `since`/`until` as UTC (review finding I6c).

    A naive `datetime` reaches Postgres as a bare `timestamp` and is
    interpreted in the session TimeZone, silently shifting the window.
    Every other timestamp boundary in this API is UTC; an absent
    `tzinfo` here is treated the same way rather than trusting the
    session's ambient TimeZone. Datetimes that already carry a tzinfo
    are returned unchanged (same object, not a copy).
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return since, until


def encode_usage_cursor(cursor: UsageCursor) -> str:
    payload = json.dumps(
        {
            "c": cursor.cycle_ts.isoformat(),
            "w": str(cursor.workspace_id),
            "p": str(cursor.product_id),
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_usage_cursor(token: str) -> UsageCursor:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return UsageCursor(
            cycle_ts=datetime.fromisoformat(payload["c"]),
            workspace_id=uuid.UUID(payload["w"]),
            product_id=uuid.UUID(payload["p"]),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidUsageCursor("Malformed cursor.") from exc


def _cycle_ts_expr(attempt_created_at, job_created_at):
    return func.date_trunc("hour", func.coalesce(job_created_at, attempt_created_at))


def build_usage_query(
    *,
    since: datetime,
    until: datetime,
    after: UsageCursor | None,
    limit: int,
) -> Select:
    """The whole export as one statement. Never aggregates in Python.

    Returns a `Select` whose columns are, in order:
    `workspace_id, product_id, cycle_ts, links_total, links_succeeded,
    protected_links_attempted, protected_links_succeeded,
    check_successful` — the frozen §7.2 contract, positionally stable —
    followed by three additive Task B3 columns: `proxied_http_attempted`,
    `proxied_browser_attempted`, `proxy_bytes`. The three are per (cycle,
    workspace, product) facts about the underlying `network_operations`
    (B2) rather than about `request_attempts`: unlike `links_total` etc,
    which fold retries per match (a link is billed once no matter how
    many times it was retried), these three count every physical PROXY/
    BROWSER operation the cycle actually made, retries included — that is
    what the fleet was actually charged for. They are computed inside the
    same `per_link` scan (one `LEFT OUTER JOIN` from `request_attempts` to
    `network_operations` via `network_operation_id`, added BEFORE the
    match-folding `GROUP BY`) and then re-summed across matches in the
    outer aggregate, so this stays a single pass over `request_attempts`
    (see `test_query_still_bounds_origin_as_a_partition_prunable_predicate`).
    `SUM(bigint)` renders as Postgres `numeric`, which a driver decodes as
    `Decimal` — every SUM here is `cast(..., BigInteger)`-wrapped, at both
    the inner and outer aggregation level, so the JSON-facing value is a
    plain Python `int`, never a `Decimal`, and `COALESCE(..., 0)` keeps it
    `0` rather than `NULL` when a cycle's links carried no proxy/browser
    operation at all.
    """
    is_protected = RequestAttempt.access_method.in_(PROTECTED_ACCESS_METHODS)

    # Build each `cycle_ts` expression ONCE and reuse the same object in
    # both the SELECT list and the GROUP BY. Calling `_cycle_ts_expr`
    # twice yields two equal-looking but distinct expression trees, each
    # with its own bind parameter for the `'hour'` literal — so Postgres
    # renders `date_trunc($1, ...)` in the SELECT and `date_trunc($5, ...)`
    # in the GROUP BY, fails to match them, and rejects the query with
    # "column scrape_jobs.created_at must appear in the GROUP BY clause".
    # Compile-only tests cannot see this; the live gate did.
    attempt_cycle_ts = _cycle_ts_expr(RequestAttempt.created_at, ScrapeJob.created_at)
    observation_cycle_ts = _cycle_ts_expr(
        PriceObservation.scraped_at, ScrapeJob.created_at
    )

    # --- inner: fold retries, one row per (cycle, workspace, product, match)
    per_link = (
        select(  # noqa: workspace-scope
            RequestAttempt.workspace_id.label("workspace_id"),
            CompetitorProductMatch.product_id.label("product_id"),
            attempt_cycle_ts.label("cycle_ts"),
            RequestAttempt.match_id.label("match_id"),
            func.bool_or(RequestAttempt.success).label("link_ok"),
            func.bool_or(is_protected).label("protected"),
            func.bool_or(and_(is_protected, RequestAttempt.success)).label(
                "protected_ok"
            ),
            # Task B3: per-match physical-operation facts, folded the same
            # way as the boolean flags above (one row per match_id) and
            # re-summed across matches in the outer aggregate below.
            func.count()
            .filter(NetworkOperation.transport == PROXIED_TRANSPORTS[0])
            .label("proxy_http_count"),
            func.count()
            .filter(NetworkOperation.transport == PROXIED_TRANSPORTS[1])
            .label("proxy_browser_count"),
            func.coalesce(
                cast(
                    func.sum(NetworkOperation.bytes_compressed).filter(
                        NetworkOperation.transport.in_(PROXIED_TRANSPORTS)
                    ),
                    BigInteger,
                ),
                literal(0),
            ).label("proxy_bytes"),
        )
        .join(
            CompetitorProductMatch,
            and_(
                CompetitorProductMatch.id == RequestAttempt.match_id,
                CompetitorProductMatch.workspace_id == RequestAttempt.workspace_id,
            ),
        )
        .outerjoin(
            ScrapeJob,
            and_(
                ScrapeJob.id == RequestAttempt.scrape_job_id,
                ScrapeJob.workspace_id == RequestAttempt.workspace_id,
            ),
        )
        .outerjoin(
            NetworkOperation,
            NetworkOperation.network_request_id == RequestAttempt.network_operation_id,
        )
        .where(
            RequestAttempt.created_at >= since,
            RequestAttempt.created_at < until,
            # Task 2.3 fix round 1: discovery probes (`tasks_strategy
            # ._probe_sample`) write `RequestAttempt` rows tagged
            # `origin='discovery'`, with real `match_id`s resolved from
            # `competitor_product_matches` -- internal COGS traffic, not
            # customer activity. `per_link` is the sole source of every
            # link/protected-link counter this export bills from, so
            # excluding non-scrape origins here (same WHERE, same
            # partition-pruned scan -- no second pass over
            # `request_attempts`) keeps discovery probing from silently
            # inflating a customer's `links_total`/
            # `protected_links_attempted`.
            RequestAttempt.origin == RequestOrigin.SCRAPE,
        )
        .group_by(
            RequestAttempt.workspace_id,
            CompetitorProductMatch.product_id,
            attempt_cycle_ts,
            RequestAttempt.match_id,
        )
        .cte("per_link")
    )

    # --- observations: did this product actually yield a price this cycle?
    per_check = (
        select(  # noqa: workspace-scope
            PriceObservation.workspace_id.label("workspace_id"),
            PriceObservation.product_id.label("product_id"),
            observation_cycle_ts.label("cycle_ts"),
            func.bool_or(PriceObservation.success).label("observed"),
        )
        .outerjoin(
            ScrapeJob,
            and_(
                ScrapeJob.id == PriceObservation.scrape_job_id,
                ScrapeJob.workspace_id == PriceObservation.workspace_id,
            ),
        )
        .where(
            PriceObservation.scraped_at >= since,
            PriceObservation.scraped_at < until,
        )
        .group_by(
            PriceObservation.workspace_id,
            PriceObservation.product_id,
            observation_cycle_ts,
        )
        .cte("per_check")
    )

    stmt = (
        select(
            per_link.c.workspace_id.label("workspace_id"),
            per_link.c.product_id.label("product_id"),
            per_link.c.cycle_ts.label("cycle_ts"),
            func.count().label("links_total"),
            func.count().filter(per_link.c.link_ok).label("links_succeeded"),
            func.count().filter(per_link.c.protected).label(
                "protected_links_attempted"
            ),
            func.count().filter(per_link.c.protected_ok).label(
                "protected_links_succeeded"
            ),
            func.coalesce(
                func.bool_or(per_check.c.observed), literal(False)
            ).label("check_successful"),
            # Task B3: re-sum the per-match physical-operation facts across
            # every match in the cycle. `SUM(bigint)` renders as Postgres
            # `numeric`; cast back to `BigInteger` so the driver hands back
            # a plain `int`, and `COALESCE(..., 0)` so an all-zero cycle
            # reports `0`, never `NULL`.
            func.coalesce(
                cast(func.sum(per_link.c.proxy_http_count), BigInteger), literal(0)
            ).label("proxied_http_attempted"),
            func.coalesce(
                cast(func.sum(per_link.c.proxy_browser_count), BigInteger), literal(0)
            ).label("proxied_browser_attempted"),
            func.coalesce(
                cast(func.sum(per_link.c.proxy_bytes), BigInteger), literal(0)
            ).label("proxy_bytes"),
        )
        .select_from(per_link)
        .outerjoin(
            per_check,
            and_(
                per_check.c.workspace_id == per_link.c.workspace_id,
                per_check.c.product_id == per_link.c.product_id,
                per_check.c.cycle_ts == per_link.c.cycle_ts,
            ),
        )
        .group_by(per_link.c.workspace_id, per_link.c.product_id, per_link.c.cycle_ts)
    )

    if after is not None:
        stmt = stmt.having(
            tuple_(
                per_link.c.cycle_ts, per_link.c.workspace_id, per_link.c.product_id
            )
            > tuple_(
                literal(after.cycle_ts),
                literal(after.workspace_id),
                literal(after.product_id),
            )
        )

    return stmt.order_by(
        per_link.c.cycle_ts, per_link.c.workspace_id, per_link.c.product_id
    ).limit(limit + 1)
