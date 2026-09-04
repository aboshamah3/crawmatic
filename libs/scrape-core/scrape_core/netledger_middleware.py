"""The network boundary itself: a downloader middleware that owns the ledger.

EPA C4 (READY-005). :mod:`app_shared.netledger.recorder` knows HOW to
write C1's physical ledger; this module is WHERE. A Scrapy downloader
middleware is the exact seam the plan asks for, and it is the same seam
for both scrapers: ``process_request`` is the last thing that runs before
the download handler opens a socket, and ``process_response`` /
``process_exception`` are the first things that run after it is done.

Middleware ORDER matters, and 130 is not arbitrary
--------------------------------------------------
Registered at priority **130** in both projects' ``DOWNLOADER_MIDDLEWARES``:

* Scrapy runs ``process_request`` in ASCENDING priority, so 130 puts the
  ledger open strictly AFTER ``SsrfGuardMiddleware`` (100) and
  ``RobotsPolicyMiddleware`` (110). A request those two reject never
  reaches a socket, so it must never leave an operation row behind — a
  ledger full of fetches that did not happen is as wrong as one missing
  fetches that did.
* Scrapy runs ``process_response`` in DESCENDING priority, so 130 puts
  the ledger close AFTER the built-in ``HttpCompressionMiddleware`` (590)
  has already decoded the body. That is what makes
  ``bytes_decompressed`` measurable at all: at any priority above 590 the
  body is still compressed.
* The COMPRESSED count does not depend on middleware position at all. It
  comes from Scrapy's ``bytes_received`` signal, which reports the raw
  bytes as they arrive off the transport — the truest available reading
  of "what the wire carried", and the same fact B6 records for the
  browser path.

Redirect chains are ONE operation, on purpose and with a caveat
---------------------------------------------------------------
``RedirectMiddleware`` (600) returns a *Request* rather than a Response,
which short-circuits the response chain before this middleware at 130 —
so a redirect hop never closes an operation here. The redirected request
carries a COPY of the original ``meta`` (Scrapy's ``Request.replace``
copies rather than shares), which means the operation identity and the
running ``bytes_received`` total both travel forward and the whole chain
closes once, on the final response.

Nothing is lost by that: every hop's transport bytes are counted, and the
operation lines up exactly 1:N with the ``request_attempts`` rows the
fetch produces (the spiders already treat a redirect chain as ONE logical
attempt with a ``final_url``). What it does under-count is the **request
count** of a chain, which matters only for a provider that bills per
request rather than per byte. Splitting a chain into a row per hop needs
a link C1's schema does not have — ``retry_parent_id`` means "re-attempts
this" and ``parent_operation_id`` means "sub-resource of this", and a
redirect hop is neither — so that is a schema decision, recorded as an
open question rather than guessed at here.

What this middleware deliberately does NOT do
---------------------------------------------
It does not touch B6's resource-blocking policy. That policy lives on the
Playwright REQUEST side (``PLAYWRIGHT_ABORT_REQUEST`` ->
``scrape_core.browser.ssrf.abort_unsafe_request``, which runs url-safety
strictly first, then the cost/category block). A blocked sub-resource is
aborted before Chromium ever produces a ``response`` event, so it never
reaches this module's accounting — accounting runs strictly third, on
exactly the traffic the first two allowed through.

One C3 grant, MANY operations
-----------------------------
A crawl is dispatched under ONE ``authorization_id`` covering the whole
batch (up to ``SCRAPE_DISPATCH_HTTP_BATCH_MAX`` targets), and this
middleware opens one operation per target under it. So each close ACCRUES
its own observed cost against the grant (``settle_partial``) and never
terminates it — a boundary that sees one fetch at a time cannot know it
is holding the batch's last one. The heartbeat loop renews the grant's
lease while any of them is in flight, and C3's sweeper performs the
terminal close once its ledger check confirms none is open any more. See
``app_shared.costauth.service``'s "One grant, MANY operations".

Failure posture
---------------
An open failure raises ``IgnoreRequest``: the fetch is abandoned, the
spider's own ``errback`` records the attempt, and no socket opens. That
is the plan's "ledger-open failure FAILS THE PAID OPERATION CLOSED",
enforced here rather than trusted to each call site. A close failure
never raises — the recorder queues it durably and the sweeper replays it.

Enable/disable is an AUDITABLE switch, not a silent hole: setting
``NETLEDGER_ENABLED = False`` logs a loud warning at spider start. There
is no path where accounting is skipped quietly.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from scrapy import signals
from scrapy.exceptions import IgnoreRequest, NotConfigured

from app_shared.costauth import (
    FLEET_PROVIDER_BROWSER,
    FLEET_PROVIDER_DIRECT,
    FLEET_PROVIDER_PROXY,
)
from app_shared.models.network_operations import NetworkTransport
from app_shared.netledger.recorder import (
    LedgerOpenError,
    NetLedgerRecorder,
    OperationIntent,
    OperationOutcome,
)
from scrape_core.db import await_in_thread, run_in_thread

logger = logging.getLogger(__name__)

__all__ = ["NetLedgerMiddleware", "operation_intent_for"]

#: Meta key carrying the operation identity, stamped by this middleware
#: at open. Both spiders read it back onto their `ScrapeResult` so the
#: logical `request_attempts` row references the physical operation that
#: carried it (``request_attempts.network_operation_id``, C1).
META_OPERATION_ID = "network_request_id"

_META_RAW_BYTES = "_netledger_raw_bytes"
_META_START = "_netledger_start_monotonic"

#: The physical transports that cost the fleet money per request. A
#: DIRECT fetch leaves the fleet's own egress with no provider charge, so
#: its operation is recorded with a NULL cost (and therefore no
#: allocations) rather than a fabricated zero — pricing fleet egress is
#: C5's reconciliation problem, not a number to invent here.
_PAID_TRANSPORTS = (NetworkTransport.PROXY, NetworkTransport.BROWSER)


def _is_proxied(meta: dict[str, Any]) -> bool:
    """Did this leg's bytes actually cross a PAID proxy?

    Separate from :func:`_transport_for` because a BROWSER navigation can
    go either way: ``PLAYWRIGHT_PROXY`` pays DataImpulse per byte,
    ``PLAYWRIGHT_DIRECT`` pulls the same page over the fleet's own egress
    for nothing. Since H4/B2 prices proxied BYTES, the two must be told
    apart — charging a direct browser page for 2.6 MB of proxy traffic
    it never bought is the same class of fiction as the one-cent floor.
    """
    return bool(meta.get("proxy")) or str(
        meta.get("playwright_context", "")
    ).startswith("proxy:")


def _transport_for(meta: dict[str, Any]) -> NetworkTransport:
    if meta.get("playwright"):
        return NetworkTransport.BROWSER
    if _is_proxied(meta):
        return NetworkTransport.PROXY
    return NetworkTransport.DIRECT


def _provider_for(meta: dict[str, Any], transport: NetworkTransport) -> str:
    provider_id = meta.get("proxy_provider_id")
    if provider_id:
        return str(provider_id)
    if transport is NetworkTransport.BROWSER:
        return FLEET_PROVIDER_BROWSER
    if transport is NetworkTransport.PROXY:
        return FLEET_PROVIDER_PROXY
    return FLEET_PROVIDER_DIRECT


def operation_intent_for(
    request: Any,
    spider: Any,
    *,
    retry_parent_id: uuid.UUID | None = None,
) -> OperationIntent:
    """Build the pre-dispatch intent from a Scrapy request + its spider.

    Kept a free function so a test (and any future non-Scrapy call site)
    can build the exact same intent the middleware builds, instead of
    re-deriving the transport/provider mapping and drifting from it.

    The three DECISION columns — ``entitlement_version``,
    ``budget_decision_version``, ``breaker_decision`` — travel the same
    route as ``authorization_id`` does: the dispatcher reads them off the
    C3 grant, the Scrapyd POST carries them as spider arguments, and they
    arrive here on the spider (a per-request ``meta`` override wins, for
    the same reason it does for the grant id). Without them C1's ledger
    recorded WHICH grant an operation ran under but never WHAT that grant
    decided, so the three columns were NULL in production and an audit
    could not reconstruct a spend decision from the ledger alone.
    """
    from urllib.parse import urlsplit

    meta = request.meta
    transport = _transport_for(meta)
    return OperationIntent(
        url=request.url,
        domain=(urlsplit(request.url).hostname or "").lower(),
        transport=transport,
        provider=_provider_for(meta, transport),
        workspace_id=getattr(spider, "workspace_id", None),
        scrape_job_id=getattr(spider, "scrape_job_id", None),
        authorization_id=meta.get("authorization_id")
        or getattr(spider, "authorization_id", None),
        http_method=request.method,
        region=meta.get("proxy_country"),
        entitlement_version=_decision_fact(meta, spider, "entitlement_version"),
        budget_decision_version=_decision_fact(meta, spider, "budget_decision_version"),
        breaker_decision=_decision_fact(meta, spider, "breaker_decision"),
        retry_parent_id=retry_parent_id,
    )


def _decision_fact(meta: dict[str, Any], spider: Any, name: str) -> str | None:
    """One authorization decision fact: request ``meta`` first, then spider.

    Same precedence as ``authorization_id``: a request that carries its
    own grant (a re-authorized retry) carries that grant's decisions too,
    and everything else inherits the crawl's.
    """
    value = meta.get(name) or getattr(spider, name, None)
    return str(value) if value else None


class NetLedgerMiddleware:
    """Opens a ledger row before every fetch and closes it after.

    One instance per crawler process. Holds:

    * ``_recorder`` — the single writer (see
      :class:`~app_shared.netledger.recorder.NetLedgerRecorder`);
    * ``_last_operation`` — the previous physical operation per target,
      which is how ``retry_parent_id`` gets chained without threading an
      extra argument through both spiders' dispatch ladders. The access
      engine holds at most one in-flight attempt per target (match lock +
      semaphore), so "the previous operation for this target" is
      unambiguous.
    """

    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler
        if not crawler.settings.getbool("NETLEDGER_ENABLED", True):
            raise NotConfigured(
                "NETLEDGER_ENABLED is False — physical network accounting is OFF "
                "for this crawl; every fetch it makes will be unrecorded"
            )
        from app_shared.costauth import CostAuthorizationService

        # The C3 service is constructed here, not injected per call: the
        # recorder is the component that knows an operation is LIVE, so
        # it is the component that must renew the grant's lease (and
        # settle it at close). Constructing it is cheap — it opens no
        # session until something actually calls through it.
        self._recorder = NetLedgerRecorder(costauth=CostAuthorizationService())
        self._last_operation: dict[Any, uuid.UUID] = {}
        self._flush_limit = crawler.settings.getint("NETLEDGER_FLUSH_BATCH", 500)
        #: How often to renew C3's lease on operations still in flight.
        #: This exists for the BROWSER path: a real Chromium navigation
        #: can outlive C3's lease, and the only component that knows the
        #: fetch is still running is the one holding the socket. `0`
        #: disables the loop.
        self._heartbeat_seconds = crawler.settings.getfloat(
            "NETLEDGER_HEARTBEAT_SECONDS", 30.0
        )
        self._heartbeat_call: Any | None = None

    @classmethod
    def from_crawler(cls, crawler: Any) -> "NetLedgerMiddleware":
        instance = cls(crawler)
        crawler.signals.connect(instance.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(instance.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(instance.bytes_received, signal=signals.bytes_received)
        return instance

    # -- lifecycle ----------------------------------------------------------

    async def spider_opened(self, spider: Any) -> None:
        """Replay whatever the LAST process left in the durable buffer.

        This is the half of the durability contract that a flush-on-close
        cannot provide: a process killed mid-page never reaches its own
        flush, so the events it buffered are only recovered because the
        next process sweeps them.
        """
        self._start_heartbeat()
        try:
            report = await await_in_thread(self._recorder.sweep, limit=self._flush_limit)
        except Exception:  # noqa: BLE001 - a sweep failure must not stop the crawl;
            # the events stay queued for the next process.
            logger.warning("netledger: startup sweep failed", exc_info=True)
            return
        if report.total or report.failed:
            logger.info(
                "netledger: startup sweep wrote %d children, recovered %d closes, "
                "%d still pending",
                report.children_written,
                report.closes_recovered,
                report.failed,
            )

    async def spider_closed(self, spider: Any) -> None:
        """Close anything still open, then drain the buffer.

        An operation still open at spider close is a fetch whose outcome
        nothing reported (a response-chain abort above this middleware,
        or an engine shutdown mid-download). Recording it as an
        unterminated operation is strictly better than leaving a row that
        claims to still be in flight forever.
        """
        self._stop_heartbeat()
        try:
            await await_in_thread(self._close_dangling)
            await await_in_thread(self._recorder.flush, limit=self._flush_limit)
        except Exception:  # noqa: BLE001 - see spider_opened
            logger.warning("netledger: shutdown flush failed", exc_info=True)

    # -- C3 lease renewal ---------------------------------------------------

    def _start_heartbeat(self) -> None:
        if self._heartbeat_seconds <= 0 or self._heartbeat_call is not None:
            return
        from twisted.internet.task import LoopingCall

        self._heartbeat_call = LoopingCall(self._heartbeat_tick)
        # `now=False`: the first tick is one interval away, so a crawl of
        # short fetches never pays for a heartbeat at all.
        self._heartbeat_call.start(self._heartbeat_seconds, now=False)

    def _stop_heartbeat(self) -> None:
        call, self._heartbeat_call = self._heartbeat_call, None
        if call is not None and call.running:
            call.stop()

    def _heartbeat_tick(self) -> Any:
        """Renew every live operation's C3 lease, off the reactor thread.

        Returning the Deferred is what keeps `LoopingCall` from
        re-entering while a slow renewal round is still running.
        """
        return run_in_thread(self._heartbeat_open_operations)

    def _heartbeat_open_operations(self) -> None:
        """Renew the lease of every GRANT this process has work under.

        Per grant, not per operation: one C3 grant authorizes a whole
        batch and this crawl opens an operation per target under it, so
        renewing per operation would be N identical round trips for the
        one lease they share. Keeping that lease alive is what stops the
        sweeper from reaping a batch grant while its later targets are
        still being fetched — the multi-operation case the grant exists
        for (see ``app_shared.costauth.service``'s "One grant, MANY
        operations").
        """
        for authorization_id in self._recorder.open_authorizations:
            self._recorder.heartbeat_authorization(authorization_id)

    def _close_dangling(self) -> None:
        for nrid in self._recorder.open_operations:
            self._recorder.close(
                nrid,
                OperationOutcome(
                    failure_reason="UNTERMINATED_AT_SPIDER_CLOSE",
                    settle_authorization=False,
                ),
            )

    # -- transport-byte capture --------------------------------------------

    def bytes_received(self, data: bytes, request: Any, spider: Any) -> None:
        """Accumulate RAW on-the-wire bytes for this request.

        Scrapy emits this for each chunk as it arrives, before any
        decompression, so the total is the transport-observed COMPRESSED
        count — the same fact B6 records, not a parallel counter.
        """
        request.meta[_META_RAW_BYTES] = request.meta.get(_META_RAW_BYTES, 0) + len(data)

    # -- the boundary -------------------------------------------------------

    async def process_request(self, request: Any, spider: Any) -> None:
        """Open the ledger row. Raises ``IgnoreRequest`` if it cannot.

        The DB write runs off the reactor thread
        (:func:`scrape_core.db.await_in_thread`, ``contracts/
        reactor-safe-db.md``) — a synchronous psycopg call on the reactor
        would stall every other in-flight fetch in this process. The work
        itself lives in :meth:`open_sync` so it is exercisable (and
        testable) without a reactor; this wrapper adds only the
        off-thread hop and the one translation that matters:
        ``LedgerOpenError`` -> ``IgnoreRequest``, i.e. no socket.
        """
        try:
            await await_in_thread(self.open_sync, request, spider)
        except LedgerOpenError as exc:
            logger.error("netledger: refusing to dispatch %s — %s", request.url, exc)
            raise IgnoreRequest(f"network ledger open failed: {exc}") from exc
        return None

    async def process_response(self, request: Any, response: Any, spider: Any) -> Any:
        await self._finish(request, spider, response=response, exception=None)
        return response

    async def process_exception(self, request: Any, exception: Any, spider: Any) -> None:
        await self._finish(request, spider, response=None, exception=exception)
        return None

    # -- reactor-free cores (the middleware's actual behaviour) --------------

    def open_sync(self, request: Any, spider: Any) -> uuid.UUID | None:
        """Open one physical operation for ``request``. Blocking; no reactor.

        Returns the ``network_request_id`` (also stamped onto
        ``request.meta``), or ``None`` when this request already has one.
        Raises :class:`LedgerOpenError` when the row could not be
        written — the caller must not dispatch.
        """
        if request.meta.get(META_OPERATION_ID):
            # A redirect hop (or any re-emission carrying the same meta
            # dict) is the SAME physical operation; opening a second row
            # for one socket would double-count it.
            return None

        key = request.meta.get("match_id") or request.url
        retry_parent_id = (
            self._last_operation.get(key)
            if int(request.meta.get("attempt_number", 1) or 1) > 1
            else None
        )
        intent = operation_intent_for(request, spider, retry_parent_id=retry_parent_id)
        network_request_id = self._recorder.open(intent)

        request.meta[META_OPERATION_ID] = network_request_id
        request.meta[_META_START] = time.monotonic()
        request.meta.setdefault(_META_RAW_BYTES, 0)
        self._last_operation[key] = network_request_id
        return network_request_id

    def close_sync(
        self, request: Any, spider: Any, response: Any, exception: Any
    ) -> uuid.UUID | None:
        """Close this request's operation and flush its children. Blocking.

        Never raises: :meth:`NetLedgerRecorder.close` queues a failed
        close durably rather than propagating, because by this point the
        bytes have already moved.
        """
        network_request_id = request.meta.get(META_OPERATION_ID)
        if network_request_id is None or request.meta.get("_netledger_closed"):
            return None
        # The id STAYS on meta after the close: the spiders' own
        # `_attempt_kwargs_from_meta` carries it onto every `ScrapeResult`
        # the fetch produces — including all five rows of a 5-match
        # fan-out, which is how ONE operation ends up referenced by five
        # `request_attempts`.
        request.meta["_netledger_closed"] = True

        children = self._drain_subresources(request, network_request_id, spider)
        if children:
            # Buffered, never inserted inline: a page's sub-resources are
            # a burst, and one local commit for the burst is what keeps
            # accounting off the per-asset critical path.
            self._recorder.buffer_children(children)
        outcome = self._outcome_for(request, response, exception)
        self._recorder.close(network_request_id, outcome)
        if children:
            self._recorder.flush(limit=self._flush_limit)
        return network_request_id

    async def _finish(
        self, request: Any, spider: Any, *, response: Any, exception: Any
    ) -> None:
        if request.meta.get(META_OPERATION_ID) is None:
            return
        try:
            await await_in_thread(self.close_sync, request, spider, response, exception)
        except Exception:  # noqa: BLE001 - close is a non-raising contract; the
            # recorder already queued whatever it could.
            logger.warning(
                "netledger: close dispatch failed for %s",
                request.meta.get(META_OPERATION_ID),
                exc_info=True,
            )

    def _outcome_for(
        self, request: Any, response: Any, exception: Any
    ) -> OperationOutcome:
        meta = request.meta
        started = meta.get(_META_START)
        duration_ms = int((time.monotonic() - started) * 1000) if started else None

        accumulator = meta.get("_byte_accumulator")
        if accumulator is not None:
            # Browser path: B6b's own per-attempt accumulator is the
            # transport-observed truth. The parent operation carries the
            # MAIN DOCUMENT's bytes; every sub-resource's bytes belong to
            # its own child row, so the two never double-count.
            main_document_bytes, _subresource_bytes = accumulator.finalize()
            bytes_compressed = main_document_bytes
            bytes_decompressed = None
        else:
            raw = meta.get(_META_RAW_BYTES)
            bytes_compressed = int(raw) if raw else None
            bytes_decompressed = len(response.body) if response is not None else None

        transport = _transport_for(meta)
        proxied = _is_proxied(meta)
        cost: int | None = None
        currency: str | None = None
        billing_unit: str | None = None
        billing_rate: int | None = None
        # A BROWSER leg is billed even when it went out DIRECT: the
        # proxy bytes are then zero, but Railway's CPU is not.
        if transport in _PAID_TRANSPORTS:
            from app_shared.costauth import (
                BROWSER_BILLING_RATE_PER_CPU_SECOND,
                BROWSER_CPU_PER_WALL_SECOND,
                PROXY_BILLING_RATE_PER_GIB,
                price_operation_micro_units,
            )

            if transport is NetworkTransport.BROWSER:
                # Two billed resources, one navigation: the proxy's bytes
                # (zero when the browser went out direct — those bytes
                # cost nothing to move) plus Railway's CPU. Wall time
                # UNDER-counts Chromium's compute, so it is scaled to
                # CPU-seconds before it is priced.
                #
                # Only the MAIN DOCUMENT's bytes are here; each
                # sub-resource carries its own on its own child row, so
                # the page's bytes are counted once across the four rows.
                cpu_seconds = (
                    (duration_ms / 1000.0) * BROWSER_CPU_PER_WALL_SECOND
                    if duration_ms
                    else 0.0
                )
                cost = price_operation_micro_units(
                    transport=transport,
                    bytes_on_wire=(bytes_compressed or 0) if proxied else 0,
                    browser_cpu_seconds=cpu_seconds,
                )
                billing_unit = "CPU_SECONDS"
                billing_rate = BROWSER_BILLING_RATE_PER_CPU_SECOND
            else:
                cost = price_operation_micro_units(
                    transport=transport,
                    bytes_on_wire=bytes_compressed or bytes_decompressed,
                    browser_cpu_seconds=None,
                )
                billing_unit = "BYTES"
                billing_rate = PROXY_BILLING_RATE_PER_GIB
            currency = "USD"

        return OperationOutcome(
            bytes_compressed=bytes_compressed,
            bytes_decompressed=bytes_decompressed,
            response_status=(response.status if response is not None else None),
            duration_ms=duration_ms,
            failure_reason=(
                f"{type(exception).__name__}: {exception}"[:500]
                if exception is not None
                else None
            ),
            estimated_cost_micro_units=cost,
            currency=currency,
            billing_unit=billing_unit,
            billing_rate_micro_units=billing_rate,
        )

    def _drain_subresources(
        self, request: Any, parent_id: uuid.UUID, spider: Any
    ) -> list[tuple[OperationIntent, OperationOutcome]]:
        """Turn B6b's per-response observations into child operations.

        Runs strictly AFTER url-safety and the resource-blocking policy:
        a blocked asset is aborted on the Playwright request side and
        never produces a response event, so it is correctly absent here
        rather than specially handled.
        """
        accumulator = request.meta.get("_byte_accumulator")
        if accumulator is None:
            return []
        drain = getattr(accumulator, "drain_subresources", None)
        if drain is None:
            return []
        from urllib.parse import urlsplit

        from app_shared.costauth import (
            PROXY_BILLING_RATE_PER_GIB,
            price_operation_micro_units,
        )

        meta = request.meta
        transport = _transport_for(meta)
        # A sub-resource costs BYTES and nothing else: the navigation's
        # CPU is already priced once on the parent row, and charging it
        # again per asset is how a 4-request page books five browsers'
        # worth of compute. Off a DIRECT browser leg those bytes crossed
        # no paid proxy at all, so the child is unpriced (NULL) rather
        # than floored at one micro-unit — a fabricated non-zero is the
        # same lie as a fabricated zero, just quieter.
        priced = transport is NetworkTransport.PROXY or (
            transport is NetworkTransport.BROWSER and _is_proxied(meta)
        )
        provider = _provider_for(meta, transport)
        workspace_id = getattr(spider, "workspace_id", None)
        scrape_job_id = getattr(spider, "scrape_job_id", None)
        authorization_id = meta.get("authorization_id") or getattr(
            spider, "authorization_id", None
        )
        entitlement_version = _decision_fact(meta, spider, "entitlement_version")
        budget_decision_version = _decision_fact(meta, spider, "budget_decision_version")
        breaker_decision = _decision_fact(meta, spider, "breaker_decision")

        children: list[tuple[OperationIntent, OperationOutcome]] = []
        for observed in drain():
            url = observed.get("url") or request.url
            domain = (urlsplit(url).hostname or "").lower()
            byte_count = observed.get("byte_count")
            cost = (
                price_operation_micro_units(
                    transport=transport,
                    bytes_on_wire=byte_count,
                    browser_cpu_seconds=None,
                )
                if priced
                else None
            )
            children.append(
                (
                    OperationIntent(
                        url=url,
                        domain=domain,
                        transport=transport,
                        provider=provider,
                        workspace_id=workspace_id,
                        scrape_job_id=scrape_job_id,
                        authorization_id=authorization_id,
                        http_method=observed.get("method") or "GET",
                        region=meta.get("proxy_country"),
                        entitlement_version=entitlement_version,
                        budget_decision_version=budget_decision_version,
                        breaker_decision=breaker_decision,
                        parent_operation_id=parent_id,
                    ),
                    OperationOutcome(
                        bytes_compressed=byte_count,
                        response_status=observed.get("status"),
                        extraction_result=observed.get("resource_type"),
                        estimated_cost_micro_units=cost,
                        currency="USD" if cost is not None else None,
                        billing_unit="BYTES" if cost is not None else None,
                        billing_rate_micro_units=(
                            PROXY_BILLING_RATE_PER_GIB if cost is not None else None
                        ),
                        # A sub-resource rides its parent's C3 grant and
                        # does NOT accrue against it: the reservation was
                        # sized per top-level request, so folding every
                        # asset in would settle a request count the batch
                        # never reserved. The asset's real cost is not
                        # lost — it is on this child's own ledger row and
                        # its own tenant allocation, which is the
                        # authoritative per-tenant cost record.
                        settle_authorization=False,
                    ),
                )
            )
        return children
