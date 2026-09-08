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

from sqlalchemy import (
    BigInteger,
    Select,
    and_,
    cast,
    distinct,
    func,
    literal,
    or_,
    select,
    tuple_,
)

from app_shared.costauth.service import FLEET_PROVIDER_DIRECT
from app_shared.enums import AccessMethod, RequestOrigin
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob
from app_shared.models.network_operations import (
    NetworkOperation,
    NetworkOperationAllocation,
    NetworkTransport,
)
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

#: `network_operations.transport` values that *can* be paid (Task B3).
#:
#: Task C6 (F17) demoted this tuple from a discriminator to a **shape**:
#: it still separates the HTTP-shaped proxied count from the browser-
#: shaped one, but it no longer decides whether an operation was paid at
#: all. `BROWSER` was the bug — a browser navigation made straight from
#: the fleet's own egress IP costs no proxy money, and counting it as
#: `proxied_browser_attempted` billed a customer for a free fetch.
PROXIED_TRANSPORTS = (
    NetworkTransport.PROXY.value,
    NetworkTransport.BROWSER.value,
)

#: `network_operations.provider` for fleet egress. Anything else is a
#: provider identity — `"proxy"`/`"browser"` for a fleet-class scope, or
#: the concrete `proxy_providers.id` the spider resolved.
DIRECT_PROVIDER = FLEET_PROVIDER_DIRECT


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
    followed by three additive Task B3 columns (`proxied_http_attempted`,
    `proxied_browser_attempted`, `proxy_bytes`) and one additive Task C6
    column (`allocated_cost_micro_units`).

    **Task C6 (F17) — what changed and why.**

    B3 answered "was this paid?" with `transport IN ('PROXY','BROWSER')`
    and summed the answer once per *logical attempt*. Three separate
    over-counts came out of that:

    1. **A direct browser navigation was billed as proxied.** Transport
       says how the fetch was *shaped*, not who was *paid*. The fleet
       runs real browsers from its own egress IP; those cost browser
       CPU, not proxy bytes. The paid predicate is now the conjunction
       the ledger actually records — `network_operations.provider <>
       'direct'` **and** `request_attempts.proxy_provider_id IS NOT
       NULL`. Both halves are needed: `provider` alone still reads
       `'browser'` for a direct navigation, and `proxy_provider_id`
       alone is a logical-row hint with no physical operation behind it.
    2. **A physical operation shared by N logical attempts was counted N
       times.** One fetch of one competitor URL can satisfy three
       matches of the same product; B3's per-match fold then re-summed
       the same `network_request_id` three times in the outer aggregate.
       Counting is now `COUNT(DISTINCT network_request_id)` over a CTE
       that already holds one row per physical operation per (workspace,
       product, cycle), so a shared operation contributes exactly once.
    3. **Children were invisible.** A browser navigation's subresource
       fetches are themselves `network_operations` rows carrying
       `parent_operation_id` and no `request_attempts` row of their own,
       so the equi-join on `network_operation_id` missed every one of
       them — and they are where a browser page's bytes actually live.
       The join now also follows `parent_operation_id`.

    Money is reported through `network_operation_allocations`
    (`cost_allocations`, C1) rather than by summing a physical cost per
    logical attempt: the allocation table is the ONLY place that says
    what share of one physical operation a given workspace owes, and its
    per-operation rows sum to exactly the operation's
    `estimated_cost_micro_units` (a deferred constraint trigger enforces
    it). Summing the physical cost per attempt instead would bill each
    of three co-tenants the whole fetch. The invariant the integration
    fixtures assert is the one that falls out of this: **allocated cost
    ≤ physical cost**, always, with equality only for a single-tenant
    operation.

    All of that rides ONE scan of the partitioned `request_attempts`
    (`attempt_scan`), which `per_link` (link counters, folded per match)
    and `per_op` (physical-operation facts, folded per operation) both
    read — so the risk-P2 partition-pruning guarantee in this module's
    docstring survives unchanged.

    `SUM(bigint)` renders as Postgres `numeric`, which a driver decodes
    as `Decimal` — every SUM here is `cast(..., BigInteger)`-wrapped, at
    both aggregation levels, so the JSON-facing value is a plain Python
    `int`, never a `Decimal`, and `COALESCE(..., 0)` keeps it `0` rather
    than `NULL` when a cycle's links carried no physical operation at all.
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

    # --- the ONE scan of `request_attempts` (risk P2). Both downstream
    # CTEs read this, so adding the physical-operation dimension does not
    # add a second pass over the partitioned table.
    attempt_scan = (
        select(  # noqa: workspace-scope
            RequestAttempt.workspace_id.label("workspace_id"),
            CompetitorProductMatch.product_id.label("product_id"),
            attempt_cycle_ts.label("cycle_ts"),
            RequestAttempt.match_id.label("match_id"),
            RequestAttempt.success.label("attempt_ok"),
            is_protected.label("protected"),
            RequestAttempt.network_operation_id.label("network_operation_id"),
            RequestAttempt.proxy_provider_id.label("proxy_provider_id"),
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
        .where(
            RequestAttempt.created_at >= since,
            RequestAttempt.created_at < until,
            # Task 2.3 fix round 1: discovery probes (`tasks_strategy
            # ._probe_sample`) write `RequestAttempt` rows tagged
            # `origin='discovery'`, with real `match_id`s resolved from
            # `competitor_product_matches` -- internal COGS traffic, not
            # customer activity. This scan is the sole source of every
            # link/protected-link counter this export bills from, so
            # excluding non-scrape origins here (same WHERE, same
            # partition-pruned scan) keeps discovery probing from
            # silently inflating a customer's `links_total`/
            # `protected_links_attempted`.
            RequestAttempt.origin == RequestOrigin.SCRAPE,
        )
        .cte("attempt_scan")
    )

    # --- inner: fold retries, one row per (cycle, workspace, product, match)
    per_link = (
        select(
            attempt_scan.c.workspace_id.label("workspace_id"),
            attempt_scan.c.product_id.label("product_id"),
            attempt_scan.c.cycle_ts.label("cycle_ts"),
            attempt_scan.c.match_id.label("match_id"),
            func.bool_or(attempt_scan.c.attempt_ok).label("link_ok"),
            func.bool_or(attempt_scan.c.protected).label("protected"),
            func.bool_or(
                and_(attempt_scan.c.protected, attempt_scan.c.attempt_ok)
            ).label("protected_ok"),
        )
        .group_by(
            attempt_scan.c.workspace_id,
            attempt_scan.c.product_id,
            attempt_scan.c.cycle_ts,
            attempt_scan.c.match_id,
        )
        .cte("per_link")
    )

    # --- Task C6: one row per PHYSICAL operation per (workspace, product,
    # cycle). Grouping by `network_request_id` here is what collapses an
    # operation shared by N logical attempts to a single row *before*
    # anything is summed; `transport`/`bytes_compressed` join the GROUP BY
    # because `network_request_id` is unique-but-not-primary, so Postgres
    # will not infer the functional dependency for us.
    #
    # The join follows the operation identity BOTH ways: an attempt's own
    # operation (`network_request_id = network_operation_id`) and every
    # child of it (`parent_operation_id = network_operation_id`) — a
    # browser page's subresources have no `request_attempts` row of their
    # own and are where its bytes live.
    #
    # `provider <> 'direct' AND proxy_provider_id IS NOT NULL` is the paid
    # predicate (F17). Transport no longer decides it.
    is_proxied = and_(
        NetworkOperation.provider != DIRECT_PROVIDER,
        attempt_scan.c.proxy_provider_id.is_not(None),
    )
    per_op = (
        select(  # noqa: workspace-scope
            attempt_scan.c.workspace_id.label("workspace_id"),
            attempt_scan.c.product_id.label("product_id"),
            attempt_scan.c.cycle_ts.label("cycle_ts"),
            NetworkOperation.network_request_id.label("network_request_id"),
            NetworkOperation.transport.label("transport"),
            NetworkOperation.bytes_compressed.label("bytes_compressed"),
            func.bool_or(is_proxied).label("proxied"),
            # The workspace's OWN share of this physical operation. At
            # most one allocation row exists per (operation, workspace)
            # -- `uq_noa_operation_id_workspace_id` -- so MAX is an
            # identity here, not a choice between values.
            func.max(
                NetworkOperationAllocation.allocated_cost_micro_units
            ).label("allocated_cost_micro_units"),
        )
        .select_from(attempt_scan)
        .join(
            NetworkOperation,
            or_(
                NetworkOperation.network_request_id
                == attempt_scan.c.network_operation_id,
                NetworkOperation.parent_operation_id
                == attempt_scan.c.network_operation_id,
            ),
        )
        .outerjoin(
            NetworkOperationAllocation,
            and_(
                NetworkOperationAllocation.operation_id
                == NetworkOperation.network_request_id,
                NetworkOperationAllocation.workspace_id
                == attempt_scan.c.workspace_id,
            ),
        )
        .group_by(
            attempt_scan.c.workspace_id,
            attempt_scan.c.product_id,
            attempt_scan.c.cycle_ts,
            NetworkOperation.network_request_id,
            NetworkOperation.transport,
            NetworkOperation.bytes_compressed,
        )
        .cte("per_op")
    )

    # --- Task C6: fold the distinct physical operations up to the export's
    # grain. `COUNT(DISTINCT network_request_id)` is belt and braces on top
    # of `per_op`'s grouping: it stays correct even if a future join makes
    # `per_op` emit a physical operation twice for one (workspace, product,
    # cycle). Bytes and allocated cost are plain SUMs *because* `per_op`
    # already holds one row per operation — that is the whole point.
    op_totals = (
        select(
            per_op.c.workspace_id.label("workspace_id"),
            per_op.c.product_id.label("product_id"),
            per_op.c.cycle_ts.label("cycle_ts"),
            func.count(distinct(per_op.c.network_request_id))
            .filter(
                per_op.c.proxied,
                per_op.c.transport == PROXIED_TRANSPORTS[0],
            )
            .label("proxied_http_attempted"),
            func.count(distinct(per_op.c.network_request_id))
            .filter(
                per_op.c.proxied,
                per_op.c.transport == PROXIED_TRANSPORTS[1],
            )
            .label("proxied_browser_attempted"),
            func.coalesce(
                cast(
                    func.sum(per_op.c.bytes_compressed).filter(per_op.c.proxied),
                    BigInteger,
                ),
                literal(0),
            ).label("proxy_bytes"),
            func.coalesce(
                cast(
                    func.sum(per_op.c.allocated_cost_micro_units), BigInteger
                ),
                literal(0),
            ).label("allocated_cost_micro_units"),
        )
        .group_by(per_op.c.workspace_id, per_op.c.product_id, per_op.c.cycle_ts)
        .cte("op_totals")
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
            # `op_totals` holds AT MOST ONE row per (workspace, product,
            # cycle), so `MAX` over the join is an identity, not a choice
            # -- the same shape `bool_or(per_check.observed)` above uses.
            # It is emphatically NOT a re-sum across matches: that is the
            # over-count Task C6 exists to remove.
            func.coalesce(
                cast(func.max(op_totals.c.proxied_http_attempted), BigInteger),
                literal(0),
            ).label("proxied_http_attempted"),
            func.coalesce(
                cast(func.max(op_totals.c.proxied_browser_attempted), BigInteger),
                literal(0),
            ).label("proxied_browser_attempted"),
            func.coalesce(
                cast(func.max(op_totals.c.proxy_bytes), BigInteger), literal(0)
            ).label("proxy_bytes"),
            func.coalesce(
                cast(func.max(op_totals.c.allocated_cost_micro_units), BigInteger),
                literal(0),
            ).label("allocated_cost_micro_units"),
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
        .outerjoin(
            op_totals,
            and_(
                op_totals.c.workspace_id == per_link.c.workspace_id,
                op_totals.c.product_id == per_link.c.product_id,
                op_totals.c.cycle_ts == per_link.c.cycle_ts,
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
