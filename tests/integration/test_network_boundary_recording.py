"""EPA C4 (READY-005): the ledger is populated AT THE NETWORK BOUNDARY.

C1 built the tables and the triggers. C3 built the authorization gate.
Neither wrote a row when a socket actually opened, and this module is the
proof that C4 does — with a FAKE TRANSPORT throughout. Nothing here
performs a live fetch: every "request" is a hand-built
:class:`scrapy.Request`, every "response" a hand-built
:class:`scrapy.http.Response`, and every Playwright response a plain
object with the three attributes the real byte-capture listener reads.

Two layers, for two different kinds of claim:

* the **offline** class always runs and covers everything that is a
  property of this code rather than of Postgres — that an open failure
  blocks the fetch, that the durable buffer survives a real ``SIGKILL``,
  that a sub-resource's per-asset bytes sum to exactly B6's own
  ``subresource_bytes`` total, and that the fan-out stamps one operation
  id onto every sibling result;
* the **live-Postgres** class (skipped unless
  ``NETWORK_OPS_TEST_DATABASE_URL`` names a reachable scratch database)
  covers what only a real ledger can show — one row per physical
  request, a retry chained by ``retry_parent_id``, 1 parent + 3 children
  whose bytes reconcile, a failed close recovered by the sweeper, and a
  5-match fan-out whose allocations sum EXACTLY to the operation's cost.

``NETWORK_OPS_TEST_DATABASE_URL`` is deliberately the same dedicated
variable C1's own tests use, and is read from ``os.environ`` only:
``.env`` is loaded by pydantic-settings into ``Settings``, never into
``os.environ``, so a configured DSN can never select itself here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app_shared.models.network_operations import (
    FRACTION_SCALE,
    NetworkTransport,
)
from app_shared.netledger.buffer import (
    KIND_CHILD_OPERATION,
    KIND_CLOSE_RECOVERY,
    DurableEventBuffer,
)
from app_shared.netledger.recorder import (
    LedgerOpenError,
    NetLedgerRecorder,
    OperationIntent,
    OperationOutcome,
    canonical_url_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_DSN_ENV = "NETWORK_OPS_TEST_DATABASE_URL"


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeSpider:
    """The attributes the boundary reads off a spider, and nothing else.

    The three decision fields (EPA Phase C F3) arrive as spider ARGUMENTS
    from the Scrapyd POST, exactly like ``authorization_id``, and the real
    spiders expose them under these names.
    """

    def __init__(
        self,
        workspace_id: uuid.UUID | None = None,
        scrape_job_id: uuid.UUID | None = None,
        authorization_id: uuid.UUID | None = None,
        budget_decision_version: str | None = None,
        entitlement_version: str | None = None,
        breaker_decision: str | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.scrape_job_id = scrape_job_id
        self.authorization_id = authorization_id
        self.budget_decision_version = budget_decision_version
        self.entitlement_version = entitlement_version
        self.breaker_decision = breaker_decision


class FakeCostAuth:
    """Records heartbeat/settle calls instead of touching C3's tables.

    ``settle_partial`` is what the boundary actually calls (EPA Phase C
    F1): one grant covers a whole batch, so an operation's close ACCRUES
    its own cost and never terminates the grant. ``settle`` is kept, and
    kept separate, so a test can prove the boundary does not call it.
    """

    def __init__(self) -> None:
        self.heartbeats: list[uuid.UUID] = []
        self.settlements: list[tuple[uuid.UUID, Any]] = []
        self.terminal_settlements: list[tuple[uuid.UUID, Any]] = []

    def heartbeat(self, authorization_id: Any) -> None:
        self.heartbeats.append(authorization_id)

    def settle_partial(self, authorization_id: Any, delta: Any) -> None:
        self.settlements.append((authorization_id, delta))

    def settle(self, authorization_id: Any, actual: Any) -> None:
        self.terminal_settlements.append((authorization_id, actual))


class FakePlaywrightRequest:
    def __init__(self, resource_type: str, method: str = "GET") -> None:
        self.resource_type = resource_type
        self.method = method


class FakePlaywrightResponse:
    """Exactly the surface :mod:`scrape_core.browser.byte_capture` reads."""

    def __init__(self, url: str, resource_type: str, byte_count: int, status: int = 200) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-length": str(byte_count)}
        self.request = FakePlaywrightRequest(resource_type)

    async def body(self) -> bytes:  # pragma: no cover - header path always wins here
        raise AssertionError("content-length was present; body() must not be called")


def make_request(url: str = "https://shop.example.test/p/1", **meta: Any) -> Any:
    import scrapy

    return scrapy.Request(url=url, meta=dict(meta), dont_filter=True)


def make_response(request: Any, body: bytes = b"<html>ok</html>", status: int = 200) -> Any:
    from scrapy.http import HtmlResponse

    return HtmlResponse(
        url=request.url, status=status, body=body, request=request, encoding="utf-8"
    )


def build_middleware(recorder: NetLedgerRecorder) -> Any:
    """A :class:`NetLedgerMiddleware` wired to ``recorder``.

    Built by ``__new__`` + explicit field assignment rather than through
    ``from_crawler``: the constructor's job is to read Scrapy settings and
    build the real recorder, and this test supplies its own recorder on
    purpose. Everything under test — intent construction, retry chaining,
    outcome derivation, sub-resource draining — is untouched.
    """
    from scrape_core.netledger_middleware import NetLedgerMiddleware

    middleware = NetLedgerMiddleware.__new__(NetLedgerMiddleware)
    middleware.crawler = None
    middleware._recorder = recorder
    middleware._last_operation = {}
    middleware._flush_limit = 100
    middleware._heartbeat_seconds = 0.0
    middleware._heartbeat_call = None
    return middleware


# ---------------------------------------------------------------------------
# Offline behaviours
# ---------------------------------------------------------------------------


class TestBoundaryOffline:
    """Properties of the boundary itself — no database involved."""

    def test_open_failure_blocks_the_fetch(self, tmp_path: Path) -> None:
        """A ledger row that cannot be written must not become a socket.

        This is the whole fail-closed posture in one assertion: the
        recorder raises, the middleware translates it to
        ``IgnoreRequest``, and Scrapy therefore never hands the request
        to a download handler.
        """
        from scrapy.exceptions import IgnoreRequest

        import scrape_core.netledger_middleware as mw

        @contextmanager
        def broken_scope() -> Iterator[Session]:
            raise RuntimeError("ledger unreachable")
            yield  # pragma: no cover

        recorder = NetLedgerRecorder(
            broken_scope, buffer=DurableEventBuffer(tmp_path / "buf.sqlite3")
        )
        middleware = build_middleware(recorder)
        request = make_request(match_id=uuid.uuid4())

        # The synchronous core raises the fail-closed signal ...
        with pytest.raises(LedgerOpenError):
            middleware.open_sync(request, FakeSpider())

        # ... and the async wrapper turns it into the one thing Scrapy
        # acts on. `await_in_thread` is replaced by a pass-through so the
        # real translation is exercised without needing a live reactor.
        async def passthrough(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        original = mw.await_in_thread
        mw.await_in_thread = passthrough
        try:
            with pytest.raises(IgnoreRequest):
                asyncio.run(middleware.process_request(make_request(), FakeSpider()))
        finally:
            mw.await_in_thread = original

        # And nothing was stamped onto the request: no phantom identity.
        assert request.meta.get("network_request_id") is None

    def test_buffered_children_survive_a_process_kill(self, tmp_path: Path) -> None:
        """SIGKILL between append and flush must not lose events.

        A child process appends three sub-resources and is then killed
        with ``SIGKILL`` — no atexit, no flush, no chance to clean up.
        The parent then opens the same buffer file and finds all three,
        which is exactly what the next spider process's startup sweep
        does.
        """
        buffer_path = tmp_path / "kill.sqlite3"
        script = textwrap.dedent(
            f"""
            import os, signal, sys
            sys.path[:0] = {json.dumps([str(REPO_ROOT / "libs" / "shared")])}
            from app_shared.netledger.buffer import DurableEventBuffer, KIND_CHILD_OPERATION
            buf = DurableEventBuffer({json.dumps(str(buffer_path))})
            buf.append_many(
                (KIND_CHILD_OPERATION, {{"n": i}}) for i in range(3)
            )
            sys.stdout.write("APPENDED\\n")
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
        )
        assert "APPENDED" in proc.stdout, proc.stderr
        # -9 == killed by SIGKILL: the process really did die without
        # running any shutdown path.
        assert proc.returncode == -9, proc.returncode

        survivor = DurableEventBuffer(buffer_path)
        try:
            assert survivor.pending_count() == 3
            events = survivor.pending()
            assert [event.payload["n"] for event in events] == [0, 1, 2]
        finally:
            survivor.close()

    def test_subresource_details_sum_to_b6_subresource_bytes(self) -> None:
        """Per-asset child bytes must reconcile with B6's own total.

        The ledger's child rows and ``request_attempts.subresource_bytes``
        are two views of ONE measurement. If they can disagree, the
        reconciliation C5 has to perform is impossible — so this asserts
        they are derived from the same numbers.
        """
        from scrape_core.browser.byte_capture import ByteAccumulator

        accumulator = ByteAccumulator()
        responses = [
            FakePlaywrightResponse("https://shop.example.test/p/1", "document", 12_000),
            FakePlaywrightResponse("https://cdn.example.test/a.js", "script", 3_000),
            FakePlaywrightResponse("https://cdn.example.test/b.css", "stylesheet", 1_500),
            FakePlaywrightResponse("https://cdn.example.test/c.png", "image", 700),
        ]
        for response in responses:
            asyncio.run(accumulator.handle_response(response))

        main_document_bytes, subresource_bytes = accumulator.finalize()
        details = accumulator.drain_subresources()

        assert main_document_bytes == 12_000
        assert len(details) == 3, "the document is the parent, never a child row"
        assert sum(item["byte_count"] for item in details) == subresource_bytes
        # Draining does not disturb B6's own contract.
        assert accumulator.finalize() == (main_document_bytes, subresource_bytes)
        # A second drain yields nothing: each asset becomes exactly one row.
        assert accumulator.drain_subresources() == []

    def test_fanout_stamps_one_operation_on_every_sibling_result(self) -> None:
        """Five logical attempts, one physical operation, one cost.

        The deduplicated fetch is the case C1 was built for: five matches
        share one socket, so five ``request_attempts`` rows must all point
        at the SAME ``network_operations`` row rather than at five
        invented ones.
        """
        from scrape_core.targets import _attempt_kwargs_from_meta

        network_request_id = uuid.uuid4()
        meta = {
            "match_id": uuid.uuid4(),
            "attempt_number": 1,
            "network_request_id": network_request_id,
        }
        kwargs = _attempt_kwargs_from_meta(meta)
        assert kwargs["network_operation_id"] == network_request_id

        # `_results_with_siblings` clones the fetcher's kwargs onto every
        # sibling, so all five carry the same id. Assert on the same dict
        # copy semantics the spider relies on.
        sibling_kwargs = [dict(kwargs) for _ in range(4)]
        ids = {kwargs["network_operation_id"]} | {
            item["network_operation_id"] for item in sibling_kwargs
        }
        assert ids == {network_request_id}

        # A request that never dispatched carries no id, and none is
        # fabricated for it.
        assert _attempt_kwargs_from_meta({})["network_operation_id"] is None

    def test_canonical_url_hash_ignores_fragment_and_trailing_slash(self) -> None:
        """The grouping key must not split one URL into several."""
        base = canonical_url_hash("https://Shop.Example.test/p/1")
        assert base == canonical_url_hash("https://shop.example.test/p/1/")
        assert base == canonical_url_hash("https://shop.example.test//p//1#frag")
        assert base != canonical_url_hash("https://shop.example.test/p/2")
        assert base.startswith("sha256:")

    def test_child_without_a_parent_is_refused(self, tmp_path: Path) -> None:
        """A sub-resource with no navigation to belong to is an orphan cost."""
        recorder = NetLedgerRecorder(buffer=DurableEventBuffer(tmp_path / "b.sqlite3"))
        with pytest.raises(ValueError, match="parent_operation_id"):
            recorder.buffer_child(
                OperationIntent(
                    url="https://cdn.example.test/a.js",
                    domain="cdn.example.test",
                    transport=NetworkTransport.BROWSER,
                    provider="browser",
                ),
                OperationOutcome(bytes_compressed=10),
            )

    def test_money_is_never_a_float(self) -> None:
        """§19, at the boundary's own front door."""
        with pytest.raises(TypeError):
            OperationOutcome(estimated_cost_minor_units=1.5, currency="USD")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            OperationOutcome(estimated_cost_minor_units=7)  # no currency
        with pytest.raises(ValueError):
            OperationOutcome(estimated_cost_minor_units=7, currency="dollars")


# ---------------------------------------------------------------------------
# Live-Postgres behaviours
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        pytest.skip(f"{_DSN_ENV} unset — live network-boundary tests skipped")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={dsn}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}")
    eng = create_engine(dsn, future=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def system_scope(engine):  # type: ignore[no-untyped-def]
    """A stand-in for ``get_system_session`` bound to the scratch DB.

    The recorder writes exclusively through this seam because C1's
    deferred allocation-total trigger aggregates ACROSS workspaces and
    fails closed on a tenant connection.
    """

    @contextmanager
    def scope() -> Iterator[Session]:
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    return scope


@pytest.fixture()
def workspace(engine) -> uuid.UUID:  # type: ignore[no-untyped-def]
    ws_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'ACTIVE', now(), now())"
            ),
            {"id": ws_id, "name": f"ws-{ws_id.hex[:8]}", "slug": f"ws-{ws_id.hex[:8]}"},
        )
    return ws_id


def _recorder(system_scope, tmp_path: Path, costauth: Any = None) -> NetLedgerRecorder:
    return NetLedgerRecorder(
        system_scope,
        buffer=DurableEventBuffer(tmp_path / f"buf-{uuid.uuid4().hex}.sqlite3"),
        costauth=costauth,
    )


def _operation(engine, network_request_id: uuid.UUID) -> Any:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT network_request_id, retry_parent_id, parent_operation_id, "
                "       transport, provider, domain, closed_at, bytes_compressed, "
                "       bytes_decompressed, response_status, estimated_cost_minor_units, "
                "       currency, authorization_id "
                "  FROM network_operations WHERE network_request_id = :nrid"
            ),
            {"nrid": network_request_id},
        ).mappings().first()


class TestLedgerAtTheBoundary:
    """What only a real ledger can demonstrate."""

    def test_one_row_per_physical_request(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """Open then close writes exactly one operation, closed exactly once."""
        recorder = _recorder(system_scope, tmp_path)
        middleware = build_middleware(recorder)
        spider = FakeSpider(workspace_id=workspace)
        request = make_request(match_id=uuid.uuid4(), proxy="http://proxy.test:8080")

        network_request_id = middleware.open_sync(request, spider)
        assert network_request_id is not None

        row = _operation(engine, network_request_id)
        assert row is not None, "the row exists BEFORE the socket opens"
        assert row["closed_at"] is None
        assert row["transport"] == NetworkTransport.PROXY.value
        assert row["domain"] == "shop.example.test"

        # A redirect hop re-enters process_request with the same meta:
        # still ONE physical operation, not two.
        assert middleware.open_sync(request, spider) is None

        middleware.close_sync(request, spider, make_response(request), None)
        closed = _operation(engine, network_request_id)
        assert closed["closed_at"] is not None
        assert closed["response_status"] == 200
        assert closed["bytes_decompressed"] == len(b"<html>ok</html>")

        with engine.begin() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM network_operations "
                    " WHERE network_request_id = :nrid"
                ),
                {"nrid": network_request_id},
            ).scalar_one()
        assert count == 1

    def test_a_redirect_chain_is_one_operation_and_loses_no_bytes(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """Every hop's bytes are counted; the chain closes exactly once.

        ``RedirectMiddleware`` short-circuits the response chain, so a
        hop never closes here — it re-enters ``process_request`` with a
        COPY of meta carrying the operation identity and the running byte
        total. Documented in the middleware's docstring, pinned here so
        the behaviour cannot drift silently into either double-counting
        or lost bytes.
        """
        recorder = _recorder(system_scope, tmp_path)
        middleware = build_middleware(recorder)
        spider = FakeSpider(workspace_id=workspace)

        first = make_request(match_id=uuid.uuid4(), proxy="http://proxy.test:8080")
        network_request_id = middleware.open_sync(first, spider)
        middleware.bytes_received(b"x" * 300, first, spider)

        # Scrapy's own redirect re-emission: a copied meta, a new URL.
        hop = first.replace(url="https://shop.example.test/p/1-final")
        assert middleware.open_sync(hop, spider) is None, "one chain, one operation"
        middleware.bytes_received(b"y" * 700, hop, spider)
        middleware.close_sync(hop, spider, make_response(hop), None)

        row = _operation(engine, network_request_id)
        assert row["closed_at"] is not None
        assert row["bytes_compressed"] == 1_000, "both hops counted, none lost"
        with engine.begin() as conn:
            total = conn.execute(
                text(
                    "SELECT COUNT(*) FROM network_operations WHERE domain = :d "
                    "  AND network_request_id = :nrid"
                ),
                {"d": "shop.example.test", "nrid": network_request_id},
            ).scalar_one()
        assert total == 1

    def test_direct_transport_records_no_fabricated_cost(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """A fetch off the fleet's own egress has no provider charge.

        NULL cost (and therefore no allocation rows) is the honest value.
        A fabricated zero would be indistinguishable from "priced at
        zero" and would seed the ledger with allocations nobody owes.
        """
        recorder = _recorder(system_scope, tmp_path)
        middleware = build_middleware(recorder)
        spider = FakeSpider(workspace_id=workspace)
        request = make_request(match_id=uuid.uuid4())

        network_request_id = middleware.open_sync(request, spider)
        middleware.close_sync(request, spider, make_response(request), None)

        row = _operation(engine, network_request_id)
        assert row["transport"] == NetworkTransport.DIRECT.value
        assert row["estimated_cost_minor_units"] is None
        assert row["currency"] is None
        with engine.begin() as conn:
            allocations = conn.execute(
                text(
                    "SELECT COUNT(*) FROM network_operation_allocations "
                    " WHERE operation_id = :nrid"
                ),
                {"nrid": network_request_id},
            ).scalar_one()
        assert allocations == 0

    def test_a_retry_chains_to_the_operation_it_re_attempts(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """A retry is its OWN physical fetch, linked, never an overwrite."""
        recorder = _recorder(system_scope, tmp_path)
        middleware = build_middleware(recorder)
        spider = FakeSpider(workspace_id=workspace)
        match_id = uuid.uuid4()

        first = make_request(match_id=match_id, attempt_number=1)
        first_id = middleware.open_sync(first, spider)
        middleware.close_sync(first, spider, None, TimeoutError("boom"))

        second = make_request(
            match_id=match_id, attempt_number=2, proxy="http://proxy.test:8080"
        )
        second_id = middleware.open_sync(second, spider)
        middleware.close_sync(second, spider, make_response(second), None)

        assert first_id != second_id
        assert _operation(engine, first_id)["retry_parent_id"] is None
        assert _operation(engine, second_id)["retry_parent_id"] == first_id
        # The failed attempt is closed with its own reason, not rewritten.
        assert _operation(engine, first_id)["closed_at"] is not None

    def test_browser_page_yields_one_parent_and_three_children(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """1 navigation + 3 sub-resources == 4 rows whose bytes reconcile.

        This is the canary's 68-vs-147 gap in miniature: before C4 only
        the navigation had a row, and every sub-resource was money spent
        with nothing to point at.
        """
        from scrape_core.browser.byte_capture import ByteAccumulator

        accumulator = ByteAccumulator()
        for response in (
            FakePlaywrightResponse("https://shop.example.test/p/1", "document", 12_000),
            FakePlaywrightResponse("https://cdn.example.test/a.js", "script", 3_000),
            FakePlaywrightResponse("https://cdn.example.test/b.css", "stylesheet", 1_500),
            FakePlaywrightResponse("https://cdn.example.test/c.png", "image", 700),
        ):
            asyncio.run(accumulator.handle_response(response))
        main_document_bytes, subresource_bytes = accumulator.finalize()

        recorder = _recorder(system_scope, tmp_path)
        middleware = build_middleware(recorder)
        spider = FakeSpider(workspace_id=workspace)
        request = make_request(
            match_id=uuid.uuid4(),
            playwright=True,
            playwright_context="proxy:abc",
            _byte_accumulator=accumulator,
        )

        parent_id = middleware.open_sync(request, spider)
        middleware.close_sync(request, spider, make_response(request), None)

        with engine.begin() as conn:
            children = conn.execute(
                text(
                    "SELECT network_request_id, domain, bytes_compressed, transport, "
                    "       closed_at, extraction_result "
                    "  FROM network_operations WHERE parent_operation_id = :pid "
                    " ORDER BY bytes_compressed DESC"
                ),
                {"pid": parent_id},
            ).mappings().all()

        assert len(children) == 3, "one child row per sub-resource"
        parent = _operation(engine, parent_id)
        assert parent["transport"] == NetworkTransport.BROWSER.value
        assert parent["bytes_compressed"] == main_document_bytes
        assert sum(child["bytes_compressed"] for child in children) == subresource_bytes
        # The whole page's transport-observed total, split across exactly
        # four rows with nothing double-counted and nothing lost.
        assert (
            parent["bytes_compressed"]
            + sum(child["bytes_compressed"] for child in children)
            == main_document_bytes + subresource_bytes
        )
        # A sub-resource is opened AND closed in one write — by the time
        # the interception hook sees it the fetch is already over — so a
        # child row is never left dangling open.
        assert all(child["closed_at"] is not None for child in children)
        assert {child["extraction_result"] for child in children} == {
            "script",
            "stylesheet",
            "image",
        }

    def test_close_failure_leaves_a_recovery_event_the_sweeper_resolves(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """Bytes already spent are never silently lost.

        The close is made to fail after the fetch; the operation stays
        visibly OPEN (never a fabricated zero) and the outcome waits in
        the durable queue until a sweep writes it.
        """
        buffer = DurableEventBuffer(tmp_path / "recover.sqlite3")
        recorder = NetLedgerRecorder(system_scope, buffer=buffer)
        intent = OperationIntent(
            url="https://shop.example.test/p/9",
            domain="shop.example.test",
            transport=NetworkTransport.PROXY,
            provider="proxy",
            workspace_id=workspace,
        )
        network_request_id = recorder.open(intent)

        outcome = OperationOutcome(
            bytes_compressed=4_096,
            bytes_decompressed=16_384,
            response_status=200,
            estimated_cost_minor_units=7,
            currency="USD",
        )

        broken = RuntimeError("connection reset during close")

        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise broken

        original_write = recorder._write_close
        recorder._write_close = explode  # type: ignore[method-assign]
        receipt = recorder.close(network_request_id, outcome)
        recorder._write_close = original_write  # type: ignore[method-assign]

        assert receipt.written is False
        assert receipt.deferred is True, "close must NEVER raise; it queues instead"
        assert buffer.pending_count(kind=KIND_CLOSE_RECOVERY) == 1
        assert _operation(engine, network_request_id)["closed_at"] is None, (
            "an unrecorded outcome leaves the operation visibly OPEN rather than "
            "silently closed at zero"
        )

        report = recorder.sweep()
        assert report.closes_recovered == 1
        assert report.failed == 0
        assert buffer.pending_count() == 0

        recovered = _operation(engine, network_request_id)
        assert recovered["closed_at"] is not None
        assert recovered["bytes_compressed"] == 4_096
        assert recovered["bytes_decompressed"] == 16_384
        assert recovered["estimated_cost_minor_units"] == 7

    def test_startup_sweep_flushes_children_a_dead_process_left_behind(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """Buffered children survive the process that buffered them.

        The buffer file is written by one recorder and swept by a
        different one — the same relationship a killed spider process has
        with the one that starts next.
        """
        buffer_path = tmp_path / "handover.sqlite3"
        parent_intent = OperationIntent(
            url="https://shop.example.test/p/7",
            domain="shop.example.test",
            transport=NetworkTransport.BROWSER,
            provider="browser",
            workspace_id=workspace,
        )
        dying = NetLedgerRecorder(system_scope, buffer=DurableEventBuffer(buffer_path))
        parent_id = dying.open(parent_intent)
        dying.buffer_children(
            [
                (
                    OperationIntent(
                        url=f"https://cdn.example.test/asset-{index}.js",
                        domain="cdn.example.test",
                        transport=NetworkTransport.BROWSER,
                        provider="browser",
                        workspace_id=workspace,
                        parent_operation_id=parent_id,
                    ),
                    OperationOutcome(bytes_compressed=100 * (index + 1)),
                )
                for index in range(3)
            ]
        )
        # The process dies here: no flush, no close.
        dying.buffer.close()

        reborn = NetLedgerRecorder(system_scope, buffer=DurableEventBuffer(buffer_path))
        assert reborn.buffer.pending_count(kind=KIND_CHILD_OPERATION) == 3
        report = reborn.sweep()
        assert report.children_written == 3
        assert reborn.buffer.pending_count() == 0

        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT bytes_compressed FROM network_operations "
                    " WHERE parent_operation_id = :pid ORDER BY bytes_compressed"
                ),
                {"pid": parent_id},
            ).scalars().all()
        assert list(rows) == [100, 200, 300]

        # Replaying the SAME child event must not duplicate a row: the
        # child carries its own pre-generated `network_request_id`, so a
        # re-flush after a kill-between-write-and-delete finds the row
        # already there and skips it.
        existing_child_id = _first_child_id(engine, parent_id)
        replay = NetLedgerRecorder(system_scope, buffer=DurableEventBuffer(buffer_path))
        replay.buffer.append(
            KIND_CHILD_OPERATION,
            {
                "intent": {
                    "url": "https://cdn.example.test/asset-0.js",
                    "domain": "cdn.example.test",
                    "transport": "BROWSER",
                    "provider": "browser",
                    "workspace_id": str(workspace),
                    "parent_operation_id": str(parent_id),
                    "network_request_id": str(existing_child_id),
                    "http_method": "GET",
                },
                "outcome": {"bytes_compressed": 100},
            },
        )
        replay_report = replay.sweep()
        assert replay_report.failed == 0
        with engine.begin() as conn:
            after = conn.execute(
                text(
                    "SELECT COUNT(*) FROM network_operations "
                    " WHERE parent_operation_id = :pid"
                ),
                {"pid": parent_id},
            ).scalar_one()
        assert after == 3, "a replayed child is idempotent, never a duplicate row"

    def test_fanout_allocations_sum_exactly(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """One fetch, five logical attempts, allocations summing EXACTLY.

        The 100/3 case is the reason largest-remainder rounding is
        mandatory: naive per-share rounding loses a minor unit, and C1's
        deferred trigger rejects the result at COMMIT.
        """
        recorder = _recorder(system_scope, tmp_path)
        intent = OperationIntent(
            url="https://shop.example.test/p/shared",
            domain="shop.example.test",
            transport=NetworkTransport.PROXY,
            provider="proxy",
            workspace_id=workspace,
            scrape_job_id=uuid.uuid4(),
        )
        network_request_id = recorder.open(intent)

        other_a, other_b = _extra_workspaces(engine, 2)
        receipt = recorder.close(
            network_request_id,
            OperationOutcome(
                bytes_compressed=2_048,
                response_status=200,
                estimated_cost_minor_units=100,
                currency="USD",
                # A genuinely awkward split: three workspaces, equal
                # weights, 100 minor units.
                allocations={workspace: 1, other_a: 1, other_b: 1},
            ),
        )
        assert receipt.written is True

        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT workspace_id, fraction_ppb, allocated_cost_minor_units "
                    "  FROM network_operation_allocations WHERE operation_id = :nrid"
                ),
                {"nrid": network_request_id},
            ).mappings().all()

        assert len(rows) == 3
        assert sum(row["allocated_cost_minor_units"] for row in rows) == 100
        assert sum(row["fraction_ppb"] for row in rows) == FRACTION_SCALE
        # Largest-remainder, not naive rounding: 34/33/33, never 33/33/33.
        assert sorted(row["allocated_cost_minor_units"] for row in rows) == [33, 33, 34]

        # And the five sibling attempts all reference this ONE operation.
        attempts = _write_fanout_attempts(
            engine, workspace, network_request_id, count=5
        )
        assert attempts == 5
        with engine.begin() as conn:
            referencing = conn.execute(
                text(
                    "SELECT COUNT(*) FROM request_attempts "
                    " WHERE network_operation_id = :nrid"
                ),
                {"nrid": network_request_id},
            ).scalar_one()
        assert referencing == 5

    def test_heartbeat_and_settle_reach_c3(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """The boundary owns the live-operation heartbeat and feeds settle."""
        costauth = FakeCostAuth()
        recorder = _recorder(system_scope, tmp_path, costauth=costauth)
        authorization_id = uuid.uuid4()
        network_request_id = recorder.open(
            OperationIntent(
                url="https://shop.example.test/p/live",
                domain="shop.example.test",
                transport=NetworkTransport.BROWSER,
                provider="browser",
                workspace_id=workspace,
                authorization_id=authorization_id,
            )
        )
        assert recorder.open_operations == (network_request_id,)

        assert recorder.heartbeat(network_request_id) is True
        assert costauth.heartbeats == [authorization_id]

        receipt = recorder.close(
            network_request_id,
            OperationOutcome(
                bytes_compressed=9_000,
                response_status=200,
                estimated_cost_minor_units=12,
                currency="USD",
                browser_seconds=4,
            ),
        )
        assert receipt.settled is True
        assert recorder.open_operations == ()
        settled_auth, settled_cost = costauth.settlements[0]
        assert settled_auth == authorization_id
        assert settled_cost.cost_minor_units == 12
        assert settled_cost.bytes_used == 9_000
        assert settled_cost.browser_seconds == 4
        assert _operation(engine, network_request_id)["authorization_id"] == authorization_id
        # ACCRUED, never terminated: the boundary sees one fetch at a time
        # and can never know it is holding the batch's last one.
        assert costauth.terminal_settlements == []

    def test_many_operations_under_one_grant_each_accrue_once(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """EPA Phase C F1, at the boundary that caused it.

        A batch grant covers up to a few hundred targets and this
        boundary opens one operation per target under it. Every close
        must contribute its OWN cost — and a redelivered close must
        contribute nothing further, which the ledger's own
        ``closed_at IS NULL`` update is what decides.
        """
        costauth = FakeCostAuth()
        recorder = _recorder(system_scope, tmp_path, costauth=costauth)
        authorization_id = uuid.uuid4()

        ids = []
        for index in range(4):
            ids.append(
                recorder.open(
                    OperationIntent(
                        url=f"https://shop.example.test/p/{index}",
                        domain="shop.example.test",
                        transport=NetworkTransport.PROXY,
                        provider="proxy",
                        workspace_id=workspace,
                        authorization_id=authorization_id,
                    )
                )
            )

        # One grant, four live operations -> ONE lease to renew, not four.
        assert recorder.open_authorizations == (authorization_id,)

        for index, nrid in enumerate(ids):
            recorder.close(
                nrid,
                OperationOutcome(
                    bytes_compressed=100 * (index + 1),
                    response_status=200,
                    estimated_cost_minor_units=index + 1,
                    currency="USD",
                ),
            )

        assert [auth for auth, _ in costauth.settlements] == [authorization_id] * 4
        assert sum(delta.cost_minor_units for _, delta in costauth.settlements) == 10
        assert costauth.terminal_settlements == []

        # The redelivery: already-closed operations accrue nothing more.
        for nrid in ids:
            receipt = recorder.close(nrid, OperationOutcome(estimated_cost_minor_units=None))
            assert receipt.settled is False
        assert len(costauth.settlements) == 4


# ---------------------------------------------------------------------------
# Small live-DB helpers
# ---------------------------------------------------------------------------


def _first_child_id(engine, parent_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT network_request_id FROM network_operations "
                " WHERE parent_operation_id = :pid ORDER BY bytes_compressed LIMIT 1"
            ),
            {"pid": parent_id},
        ).scalar_one()


def _extra_workspaces(engine, count: int) -> list[uuid.UUID]:  # type: ignore[no-untyped-def]
    ids: list[uuid.UUID] = []
    with engine.begin() as conn:
        for _ in range(count):
            ws_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, slug, status, created_at, "
                    "updated_at) VALUES (:id, :name, :slug, 'ACTIVE', now(), now())"
                ),
                {
                    "id": ws_id,
                    "name": f"ws-{ws_id.hex[:8]}",
                    "slug": f"ws-{ws_id.hex[:8]}",
                },
            )
            ids.append(ws_id)
    return ids


def _write_fanout_attempts(
    engine, workspace_id: uuid.UUID, network_request_id: uuid.UUID, *, count: int
) -> int:  # type: ignore[no-untyped-def]
    """Five sibling ``request_attempts``, all pointing at ONE operation.

    ``request_attempts`` is monthly-partitioned; the partition for this
    month is created on demand so the scratch database needs no
    maintenance job to have run first.
    """
    moment = datetime.now(UTC)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    partition = f"request_attempts_{start:%Y_%m}"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {partition} PARTITION OF request_attempts "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )
        for index in range(count):
            conn.execute(
                text(
                    "INSERT INTO request_attempts "
                    "(id, created_at, workspace_id, match_id, url, attempt_number, "
                    " access_method, success, network_operation_id) "
                    "VALUES (:id, :created_at, :workspace_id, :match_id, :url, "
                    "        :attempt_number, 'PROXY_HTTP', true, :network_operation_id)"
                ),
                {
                    "id": uuid.uuid4(),
                    "created_at": moment,
                    "workspace_id": workspace_id,
                    "match_id": uuid.uuid4(),
                    "url": f"https://shop.example.test/p/shared#{index}",
                    "attempt_number": 1,
                    "network_operation_id": network_request_id,
                },
            )
    return count


@pytest.mark.skipif(not os.environ.get(_DSN_ENV), reason=f"{_DSN_ENV} unset")
class TestCostRollupDimensionsAgainstRealLedgerRows:
    """EPA Phase C F4: the C6 rollup's dimensions, over rows this boundary wrote.

    The aggregators are pure and have their own unit suite; what has no
    unit test at all is the FETCH — a raw ``text()`` statement that must
    read ``transport`` off the operation and join the domain's playbook
    for a real ``profile_version``. A dimension mapping is only as true
    as the SQL that produces it, so this runs the actual statement over
    actual ledger rows.
    """

    def test_the_fetch_reads_transport_and_the_domains_playbook_version(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        from app_shared.netledger.rollups import (
            _fetch_day_rows,
            aggregate_fleet_cost_buckets,
        )

        recorder = _recorder(system_scope, tmp_path)
        domain = f"rollup-{uuid.uuid4().hex[:8]}.example.test"
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO domain_playbooks (id, domain, preferred_access_method, "
                    "method_templates, profile_version, created_at, updated_at) "
                    "VALUES (:id, :domain, 'DIRECT_HTTP', '[]'::jsonb, 9, now(), now())"
                ),
                {"id": uuid.uuid4(), "domain": domain},
            )

        # Two transports on ONE domain and ONE playbook version: the split
        # that must survive into the buckets.
        for transport, provider, cost in (
            (NetworkTransport.PROXY, "proxy", 100),
            (NetworkTransport.BROWSER, "browser", 900),
        ):
            nrid = recorder.open(
                OperationIntent(
                    url=f"https://{domain}/p/{cost}",
                    domain=domain,
                    transport=transport,
                    provider=provider,
                    workspace_id=workspace,
                    # Deliberately present, and deliberately NOT the
                    # grouping key any more: it is near-unique per
                    # operation, so grouping by it shattered the day.
                    budget_decision_version=f"cb1:2026_08:ws{cost}:fleet{cost}",
                    entitlement_version="saas-v7",
                    breaker_decision="CLOSED",
                )
            )
            recorder.close(
                nrid,
                OperationOutcome(
                    bytes_compressed=1_000,
                    response_status=200,
                    estimated_cost_minor_units=cost,
                    currency="USD",
                ),
            )

        today = datetime.now(UTC).date()
        rows = None
        for day in (today, today - timedelta(days=1)):
            # `with`: the fetch opens a read transaction, and an
            # idle-in-transaction connection left behind here would block
            # the next suite's TRUNCATE of these very tables.
            with Session(engine) as session:
                operations, _allocations, _settlements = _fetch_day_rows(session, day)
            mine = [op for op in operations if op.domain == domain]
            if mine:
                rows = mine
                break
        assert rows is not None and len(rows) == 2, rows

        # The decision columns C1 declared and nothing used to populate
        # are on the row now (F3), and the rollup's two dimensions are the
        # real ones (F4).
        assert {row.method for row in rows} == {"PROXY", "BROWSER"}
        assert {row.profile_version for row in rows} == {"9"}

        buckets = aggregate_fleet_cost_buckets(rows, [])
        by_method = {b.method: b for b in buckets}
        assert by_method["BROWSER"].estimated_cost_minor_units == 900
        assert by_method["PROXY"].estimated_cost_minor_units == 100
        assert all(b.profile_version == "9" for b in buckets)

    def test_an_operation_on_a_domain_with_no_playbook_still_appears(
        self, engine, system_scope, workspace, tmp_path: Path
    ) -> None:
        """The LEFT JOIN, and why it is LEFT.

        Uncertified domains are exactly where surprise spend happens. A
        rollup whose join quietly dropped them would hide the spend an
        operator most needs to see, so a missing playbook becomes the
        UNKNOWN sentinel, never a missing row.
        """
        from datetime import timedelta

        from app_shared.models.network_cost_rollups import (
            COST_ROLLUP_UNKNOWN_PROFILE_VERSION,
        )
        from app_shared.netledger.rollups import (
            _fetch_day_rows,
            aggregate_fleet_cost_buckets,
        )

        recorder = _recorder(system_scope, tmp_path)
        domain = f"nopb-{uuid.uuid4().hex[:8]}.example.test"
        nrid = recorder.open(
            OperationIntent(
                url=f"https://{domain}/p/1",
                domain=domain,
                transport=NetworkTransport.DIRECT,
                provider="direct",
                workspace_id=workspace,
            )
        )
        recorder.close(
            nrid,
            OperationOutcome(
                bytes_compressed=500,
                response_status=200,
                estimated_cost_minor_units=3,
                currency="USD",
            ),
        )

        today = datetime.now(UTC).date()
        rows: list[Any] = []
        for day in (today, today - timedelta(days=1)):
            with Session(engine) as session:
                operations, _a, _s = _fetch_day_rows(session, day)
            rows = [op for op in operations if op.domain == domain]
            if rows:
                break
        assert len(rows) == 1, rows
        assert rows[0].method == "DIRECT"
        assert rows[0].profile_version is None

        buckets = aggregate_fleet_cost_buckets(rows, [])
        assert buckets[0].profile_version == COST_ROLLUP_UNKNOWN_PROFILE_VERSION


class TestDecisionFactsReachTheIntent:
    """EPA Phase C F3: the three C1 decision columns finally get a producer.

    ``network_operations`` has carried ``entitlement_version``,
    ``budget_decision_version`` and ``breaker_decision`` since C1 and
    nothing ever wrote them, so all three stood NULL in production: the
    ledger recorded WHICH grant an operation ran under but nothing about
    what that grant had decided, and a spend decision could not be
    reconstructed from the ledger alone.
    """

    def test_spider_arguments_become_operation_intent_fields(self) -> None:
        from scrape_core.netledger_middleware import operation_intent_for

        spider = FakeSpider(
            workspace_id=uuid.uuid4(),
            authorization_id=uuid.uuid4(),
            budget_decision_version="cb1:2026_08:ws41:fleet7",
            entitlement_version="saas-v7",
            breaker_decision="CLOSED",
        )
        intent = operation_intent_for(make_request(), spider)

        assert intent.authorization_id == spider.authorization_id
        assert intent.budget_decision_version == "cb1:2026_08:ws41:fleet7"
        assert intent.entitlement_version == "saas-v7"
        assert intent.breaker_decision == "CLOSED"

    def test_a_request_carrying_its_own_grant_carries_its_own_decisions(self) -> None:
        """Same precedence as ``authorization_id``: meta wins over spider.

        A re-authorized retry carries its own grant, and must therefore
        carry that grant's decisions — never the crawl-wide ones it has
        superseded.
        """
        from scrape_core.netledger_middleware import operation_intent_for

        spider = FakeSpider(
            workspace_id=uuid.uuid4(),
            authorization_id=uuid.uuid4(),
            budget_decision_version="crawl-wide",
            entitlement_version="crawl-wide",
            breaker_decision="CLOSED",
        )
        request = make_request(
            budget_decision_version="per-request",
            entitlement_version="per-request",
        )
        intent = operation_intent_for(request, spider)

        assert intent.budget_decision_version == "per-request"
        assert intent.entitlement_version == "per-request"
        # ...and anything the request does not override still inherits.
        assert intent.breaker_decision == "CLOSED"

    def test_absent_decision_facts_stay_none_rather_than_the_string_none(self) -> None:
        """A legacy dispatch that sends none of them writes SQL NULL.

        The columns are nullable precisely so an un-migrated caller stays
        valid; what must never happen is the literal string ``"None"``
        landing in an audit column.
        """
        from scrape_core.netledger_middleware import operation_intent_for

        intent = operation_intent_for(make_request(), FakeSpider())
        assert intent.budget_decision_version is None
        assert intent.entitlement_version is None
        assert intent.breaker_decision is None
