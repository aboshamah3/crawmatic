"""2026-10-05 noon-stall fix 7: a claimed target whose access policy never
resolved (``NONE_RESOLVED``) must leave the run terminal.

`load_targets` claims the whole batch ``STARTED`` before attempt 1 is
decided. The spiders used to drop a ``NONE_RESOLVED`` target silently (no
result row, no status write), so it stayed ``STARTED``: the ended-run reaper
and the dispatcher re-sent it every ~15 minutes until the 12 h job deadline
-- the same loop as fix 1. Now it (and its dedup siblings, which resolved
the same chain) is finalized ``SKIPPED`` + ``TARGET_UNRESOLVED`` through the
single ``mark_target`` writer. No result row: a ``POLICY_BLOCKED`` result
would record the competitor's availability as BLOCKED, which is not true.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app_shared.enums import RobotsPolicy, ScrapeErrorCode, ScrapeTargetStatus
from app_shared.jobs.targets import REFUSAL_FINALIZABLE_TARGET_STATUSES

import scrape_core.targets as targets_mod
from scrape_core.targets import AdmissionContext, SpiderTarget, _DispatchDecision, _LoadedTargets

from price_monitor.spiders import generic_price_spider as gps
from price_monitor_browser.spiders import generic_browser_price_spider as gbps


WORKSPACE_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _target(**overrides: Any) -> SpiderTarget:
    return SpiderTarget(
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        url="https://shop.example.com/product/1",
        profile=None,
        robots_policy=RobotsPolicy.RESPECT,
        access_policy=None,
        **overrides,
    )


async def _direct(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


def test_mark_unresolved_skips_every_match_with_the_single_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    session = object()

    @contextmanager
    def _txn(workspace_id: Any) -> Any:
        assert workspace_id == WORKSPACE_ID
        yield session

    def _mark(s: Any, **kwargs: Any) -> None:
        assert s is session
        calls.append(kwargs)

    monkeypatch.setattr(targets_mod, "workspace_txn", _txn)
    monkeypatch.setattr(targets_mod, "mark_target", _mark)

    ids = [uuid.uuid4(), uuid.uuid4()]
    targets_mod._mark_targets_unresolved_skipped(WORKSPACE_ID, JOB_ID, ids)

    assert [c["match_id"] for c in calls] == ids
    for c in calls:
        assert c["status"] is ScrapeTargetStatus.SKIPPED
        assert c["error_code"] is ScrapeErrorCode.TARGET_UNRESOLVED
        assert c["scrape_job_id"] == JOB_ID
        # STARTED included: the batch was claimed before attempt 1.
        assert ScrapeTargetStatus.STARTED in c["only_if_status"]
        assert tuple(c["only_if_status"]) == tuple(REFUSAL_FINALIZABLE_TARGET_STATUSES)


def test_skip_unresolved_target_finalizes_target_and_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, Any, list[uuid.UUID]]] = []
    monkeypatch.setattr(targets_mod, "await_in_thread", _direct)
    monkeypatch.setattr(
        targets_mod,
        "_mark_targets_unresolved_skipped",
        lambda ws, job, ids: seen.append((ws, job, list(ids))),
    )
    sibling = _target()
    target = _target(sibling_targets=[sibling])
    ctx = AdmissionContext(
        workspace_id=WORKSPACE_ID, scrape_job_id=JOB_ID, requeue_state_by_match_id={}
    )

    asyncio.run(targets_mod.skip_unresolved_target(ctx, target))

    assert seen == [(WORKSPACE_ID, JOB_ID, [target.match_id, sibling.match_id])]


def test_skip_unresolved_target_without_job_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any) -> None:
        raise AssertionError("no job, nothing to mark")

    monkeypatch.setattr(targets_mod, "await_in_thread", _direct)
    monkeypatch.setattr(targets_mod, "_mark_targets_unresolved_skipped", _boom)
    ctx = AdmissionContext(
        workspace_id=WORKSPACE_ID, scrape_job_id=None, requeue_state_by_match_id={}
    )

    asyncio.run(targets_mod.skip_unresolved_target(ctx, _target()))


async def _drain(agen: Any) -> list[Any]:
    return [item async for item in agen]


def _unresolved_decision(*args: Any, **kwargs: Any) -> Any:
    async def _inner() -> _DispatchDecision:
        return _DispatchDecision(plan=None, proxy=None, skip_error_code=None)

    return _inner()


@pytest.mark.parametrize(
    ("module", "spider_cls"),
    [(gps, gps.GenericPriceSpider), (gbps, gbps.GenericBrowserPriceSpider)],
    ids=["http", "browser"],
)
def test_spider_finalizes_an_unresolved_target(
    monkeypatch: pytest.MonkeyPatch, module: Any, spider_cls: Any
) -> None:
    target = _target()
    spider = spider_cls(
        workspace_id=str(WORKSPACE_ID),
        match_ids=str(target.match_id),
        scrape_job_id=str(JOB_ID),
    )

    async def _load(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return _LoadedTargets(targets=[target])

    skipped: list[uuid.UUID] = []

    async def _skip(ctx: AdmissionContext, t: SpiderTarget) -> None:
        assert ctx.scrape_job_id == JOB_ID
        skipped.append(t.match_id)

    monkeypatch.setattr(
        "app_shared.config.get_settings", lambda: SimpleNamespace(SCRAPE_URL_DEDUP=False)
    )
    monkeypatch.setattr(module, "await_in_thread", _load)
    monkeypatch.setattr(module, "prepare_dispatch_with_backoff", _unresolved_decision)
    monkeypatch.setattr(module, "skip_unresolved_target", _skip)

    results = asyncio.run(_drain(spider.start()))

    assert results == []
    assert skipped == [target.match_id]
