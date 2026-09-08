"""The structured offer contract reaching the persisted row (EPA C5, F19).

`ScrapeResult` gains the offer/provenance quintet the plan names
(`offer`, `extractor_version`, `profile_version`, `confidence`,
`provenance`) and `scrape_core.pipelines._flush_batch` writes them onto
the `price_observations` row alongside the W3.1 `offer_*` superset --
including `offer_raw_evidence_hash`, which is produced by
`app_shared.observations.evidence_store.store_evidence` against the
configured `EVIDENCE_STORE_DIR` so that a hash on a persisted row
actually resolves back to the bytes it was computed from
(`docs/RETENTION_POLICY.md` §2.1: "a hash pointing at deleted data
proves nothing" -- a hash pointing at data that was never stored proves
even less).

Fakes only, same harness shape as
`tests/unit/test_pipeline_needs_review_sidecar.py`: no DB, no Redis, no
reactor. Every item carries `scrape_job_id=None` so the
target-terminalization/finalize paths stay inert.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app_shared.enums import AccessMethod, ScrapeErrorCode, StockStatus
from app_shared.observations.evidence_store import resolve_hash
from app_shared.observations.offer_observation import OfferObservation
from scrape_core import pipelines as pipelines_mod
from scrape_core.items import (
    EXTRACTOR_VERSION,
    PROVENANCE_FIRST_HIT,
    PROVENANCE_NONE,
    ScrapeResult,
)
from scrape_core.pipelines import _flush_batch, availability_state_for

WORKSPACE_ID = uuid.uuid4()

HTML = b"<html><body><span class='price'>SAR 99.00</span></body></html>"


# --------------------------------------------------------------------------
# Fakes (mirrors tests/unit/test_pipeline_needs_review_sidecar.py)
# --------------------------------------------------------------------------


class _FakeResult:
    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[tuple[str, Any]] = []

    def add_all(self, items: Any) -> None:
        self.added.extend(items)

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.executed.append((str(stmt), params))
        return _FakeResult()


class _FakeWorkspaceTxn:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class _FakeSettings:
    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85

    def __init__(self, evidence_store_dir: str | None = None) -> None:
        self.EVIDENCE_STORE_DIR = evidence_store_dir


class _FakeRedis:
    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        return True


def _install_fakes(monkeypatch: Any, *, evidence_store_dir: str | None = None) -> _FakeSession:
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(
        pipelines_mod, "get_settings", lambda: _FakeSettings(evidence_store_dir)
    )
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    # Observations reach the DB as a Core `INSERT ... VALUES` built from
    # lowered dicts (`_row_values`), not through `session.add_all`, so
    # the ORM instances are captured at the one seam that still sees
    # them -- asserting on the lowered dict would test the lowering, not
    # the projection this task adds.
    monkeypatch.setattr(
        pipelines_mod,
        "_insert_ignoring_replays",
        lambda _session, _model, instances, _keys: session.added.extend(instances),
    )
    return session


def _observations(session: _FakeSession) -> list[Any]:
    return [obj for obj in session.added if type(obj).__name__ == "PriceObservation"]


def _offer(observation_id: uuid.UUID, **overrides: Any) -> OfferObservation:
    payload: dict[str, Any] = {
        "observation_id": observation_id,
        "source_url": "https://shop.example.com/p/1",
        "domain": "shop.example.com",
        "observed_at": datetime.now(timezone.utc),
        "currency": "SAR",
        "item_price": Decimal("99.00"),
        "seller_name": "Example Store",
        "stock_status": StockStatus.IN_STOCK,
    }
    payload.update(overrides)
    return OfferObservation(**payload)


def _result(**overrides: Any) -> ScrapeResult:
    payload: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "match_id": uuid.uuid4(),
        "product_id": uuid.uuid4(),
        "product_variant_id": uuid.uuid4(),
        "competitor_id": uuid.uuid4(),
        "scrape_job_id": None,
        "url": "https://shop.example.com/p/1",
        "access_method": AccessMethod.DIRECT_HTTP,
        "success": True,
        "price": Decimal("99.00"),
        "currency": "SAR",
        "stock_status": StockStatus.IN_STOCK,
    }
    payload.update(overrides)
    return ScrapeResult(**payload)


# --------------------------------------------------------------------------
# The transport contract
# --------------------------------------------------------------------------


def test_scrape_result_carries_the_offer_provenance_quintet() -> None:
    item = _result()
    assert item.offer is None
    assert item.extractor_version == EXTRACTOR_VERSION
    assert item.profile_version == 0
    assert item.confidence == Decimal("0")
    assert item.provenance == PROVENANCE_NONE


def test_offer_is_an_offer_observation_when_supplied() -> None:
    observation_id = uuid.uuid4()
    item = _result(offer=_offer(observation_id))
    assert isinstance(item.offer, OfferObservation)
    assert item.offer.observation_id == observation_id


# --------------------------------------------------------------------------
# Persistence: offer_* columns + a resolvable evidence hash
# --------------------------------------------------------------------------


def test_persisted_observation_has_a_resolvable_offer_raw_evidence_hash(
    monkeypatch: Any, tmp_path: Path
) -> None:
    session = _install_fakes(monkeypatch, evidence_store_dir=str(tmp_path))
    item = _result(offer=_offer(uuid.uuid4()), raw_evidence=HTML)

    _flush_batch(WORKSPACE_ID, [item])

    observation = _observations(session)[0]
    assert observation.offer_raw_evidence_hash is not None
    # THE property this task exists for: the hash on the row resolves
    # back to the exact bytes it was computed from.
    assert resolve_hash(observation.offer_raw_evidence_hash, store_dir=tmp_path) == HTML


def test_offer_columns_are_populated_from_the_offer_observation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    session = _install_fakes(monkeypatch, evidence_store_dir=str(tmp_path))
    item = _result(
        offer=_offer(uuid.uuid4(), market="SA", seller_type="marketplace"),
        raw_evidence=HTML,
    )

    _flush_batch(WORKSPACE_ID, [item])

    observation = _observations(session)[0]
    assert observation.offer_source_url == "https://shop.example.com/p/1"
    assert observation.offer_domain == "shop.example.com"
    assert observation.offer_market == "SA"
    assert observation.offer_seller_name == "Example Store"
    assert observation.offer_seller_type == "marketplace"
    assert observation.offer_item_price == Decimal("99.00")


def test_provenance_quintet_reaches_the_row(monkeypatch: Any, tmp_path: Path) -> None:
    session = _install_fakes(monkeypatch, evidence_store_dir=str(tmp_path))
    item = _result(
        extractor_version="scrape-core-extraction-test",
        profile_version=7,
        confidence=Decimal("0.9100"),
        provenance=PROVENANCE_FIRST_HIT,
    )

    _flush_batch(WORKSPACE_ID, [item])

    observation = _observations(session)[0]
    assert observation.extractor_version == "scrape-core-extraction-test"
    assert observation.profile_version == 7
    assert observation.confidence == Decimal("0.9100")
    assert observation.provenance == PROVENANCE_FIRST_HIT


def test_no_evidence_store_dir_configured_writes_no_blob_and_no_hash(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """An unmounted volume must not silently produce an unresolvable hash.

    Recording a content address for bytes nobody stored is exactly the
    failure `docs/RETENTION_POLICY.md` §2.1 warns about, so with no
    store configured the column stays NULL rather than carrying a
    reference that can never be replayed.
    """
    session = _install_fakes(monkeypatch, evidence_store_dir=None)
    item = _result(raw_evidence=HTML)

    _flush_batch(WORKSPACE_ID, [item])

    observation = _observations(session)[0]
    assert observation.offer_raw_evidence_hash is None
    assert list(tmp_path.iterdir()) == []


def test_an_item_with_no_raw_evidence_keeps_the_offers_own_hash(
    monkeypatch: Any, tmp_path: Path
) -> None:
    session = _install_fakes(monkeypatch, evidence_store_dir=str(tmp_path))
    known_hash = "a" * 64
    item = _result(offer=_offer(uuid.uuid4(), raw_evidence_hash=known_hash))

    _flush_batch(WORKSPACE_ID, [item])

    assert _observations(session)[0].offer_raw_evidence_hash == known_hash


# --------------------------------------------------------------------------
# availability_state
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("success", "stock_status", "error_code", "expected"),
    [
        (True, StockStatus.IN_STOCK, None, "available"),
        (True, StockStatus.OUT_OF_STOCK, None, "unavailable"),
        (False, StockStatus.OUT_OF_STOCK, ScrapeErrorCode.PRICE_NOT_FOUND, "unavailable"),
        (False, None, ScrapeErrorCode.BLOCKED, "blocked"),
        (False, None, ScrapeErrorCode.HTTP_403, "blocked"),
        (False, None, ScrapeErrorCode.POLICY_BLOCKED, "blocked"),
        (False, None, ScrapeErrorCode.PRICE_NOT_FOUND, None),
        (True, StockStatus.UNKNOWN, None, "available"),
    ],
)
def test_availability_state_derivation(
    success: bool, stock_status: Any, error_code: Any, expected: str | None
) -> None:
    assert availability_state_for(_result(
        success=success, stock_status=stock_status, error_code=error_code
    )) == expected


def test_expired_offer_is_stale_and_a_conditional_promotion_is_conditional() -> None:
    past = datetime.now(timezone.utc) - timedelta(days=1)
    stale = _result(offer=_offer(uuid.uuid4(), expires_at=past))
    assert availability_state_for(stale) == "stale"

    conditional = _result(
        offer=_offer(
            uuid.uuid4(),
            promotion={"coupon_code": "SAVE10", "coupon_requirement": "clip coupon"},
        )
    )
    assert availability_state_for(conditional) == "conditional"


def test_availability_state_is_written_to_the_row(monkeypatch: Any, tmp_path: Path) -> None:
    session = _install_fakes(monkeypatch, evidence_store_dir=str(tmp_path))
    item = _result(success=False, stock_status=StockStatus.OUT_OF_STOCK,
                   price=None, error_code=ScrapeErrorCode.PRICE_NOT_FOUND)

    _flush_batch(WORKSPACE_ID, [item])

    assert _observations(session)[0].availability_state == "unavailable"
