"""Frozen ORACLE: the usage-export query exactly as it was before E6 (2026-09-29).

`app.services.admin_usage.build_usage_query` was rewritten for speed (the
OR-join on `network_operations` became a UNION ALL of two equi-joins bounded
on the partition key, and the keyset cursor moved from HAVING into the scan).
The SaaS importer derives exactly-once identity from this export's rows and
cursor, so the rewrite must return the SAME rows in the SAME order for the
SAME cursors. `test_admin_usage_equivalence.py` runs both against one seeded
database and compares them. Do not "fix" or modernise this file: its whole
value is that it is the old behaviour, verbatim.
"""

from __future__ import annotations

from datetime import datetime

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

from app.services.admin_usage import (
    DIRECT_PROVIDER,
    PROTECTED_ACCESS_METHODS,
    PROXIED_TRANSPORTS,
    UsageCursor,
)
from app_shared.enums import RequestOrigin
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob
from app_shared.models.network_operations import NetworkOperation, NetworkOperationAllocation
from app_shared.models.observations import PriceObservation, RequestAttempt


def _cycle_ts_expr(attempt_created_at, job_created_at):
    return func.date_trunc("hour", func.coalesce(job_created_at, attempt_created_at))


def build_usage_query_oracle(
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
