"""`EXTRACTION_RANKING_POLICY='shadow'` — the ranker runs, never decides
(EPA C5, F19).

Shadow mode is the whole point of this task's risk posture: the W3.2
ranked path (`extract_ranked`) is exercised on the live path so its
disagreement rate becomes *measurable*, while the value that gets
persisted stays the historical first-hit. Flipping the flag to `'v1'`
is an OWNER decision at C11 — nothing here may anticipate it.

Three properties are pinned:

1. `shadow` returns the first hit, byte-for-byte the same object the
   `off` chain returns.
2. A page where the ranker's winner differs from the first hit produces
   exactly ONE buffered `extraction_shadow_events` record, and
   `scrape_core.pipelines._flush_batch` writes it.
3. Nothing the ranked path can do — an exception included — may change
   or fail the extraction the caller asked for.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app_shared.enums import AccessMethod, ExtractionMethod
from scrape_core import pipelines as pipelines_mod
from scrape_core.extraction import pipeline as extraction_pipeline
from scrape_core.extraction.pipeline import (
    RANKING_POLICY_MODES,
    ShadowEvent,
    drain_shadow_events,
    extract,
    reset_shadow_buffer,
    resolve_ranking_policy_mode,
    shadow_buffer_size,
)
from scrape_core.extraction.result import ExtractionCandidate
from scrape_core.items import ScrapeResult

WORKSPACE_ID = uuid.uuid4()

# A page whose JSON-LD (first in the chain) and CSS selector disagree
# materially about the price: 99.00 vs 10.00, far outside the ranking
# policy's 5% tolerance.
DISAGREEING_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "offers": {"@type": "Offer", "price": "99.00", "priceCurrency": "SAR"}}
</script>
</head><body><span class="price">SAR 10.00</span></body></html>
"""

AGREEING_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "offers": {"@type": "Offer", "price": "99.00", "priceCurrency": "SAR"}}
</script>
</head><body><span class="price">SAR 99.00</span></body></html>
"""


class _Profile:
    id = None
    version = 1
    price_selector = "span.price"
    old_price_selector = None
    currency_selector = None
    stock_selector = None
    price_regex = None
    old_price_regex = None
    currency_regex = None
    stock_regex = None
    price_json_path = None


@pytest.fixture(autouse=True)
def _clean_buffer() -> Any:
    reset_shadow_buffer()
    yield
    reset_shadow_buffer()


# --------------------------------------------------------------------------
# The flag itself
# --------------------------------------------------------------------------


def test_the_three_policy_modes_are_exactly_off_shadow_v1() -> None:
    assert RANKING_POLICY_MODES == ("off", "shadow", "v1")


def test_the_settings_default_is_shadow() -> None:
    from app_shared.config import Settings

    assert Settings.model_fields["EXTRACTION_RANKING_POLICY"].default == "shadow"


def test_an_unreadable_settings_object_falls_back_to_off(monkeypatch: Any) -> None:
    """Fail-open: a telemetry mode must never be the reason extraction stops."""

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(extraction_pipeline, "get_settings", _boom)
    assert resolve_ranking_policy_mode() == "off"


# --------------------------------------------------------------------------
# Shadow returns the first hit
# --------------------------------------------------------------------------


def test_shadow_returns_the_first_hit_not_the_ranker_winner() -> None:
    first_hit = extract(DISAGREEING_HTML, _Profile(), policy_mode="shadow")
    off = extract(DISAGREEING_HTML, _Profile(), policy_mode="off")

    assert first_hit is not None
    assert off is not None
    # JSON-LD is first in the chain, so 99.00 is the first hit under
    # both modes -- shadow changed nothing about what was returned.
    assert first_hit.raw_price_text == off.raw_price_text
    assert first_hit.method is ExtractionMethod.JSON_LD


def test_a_disagreeing_page_buffers_exactly_one_shadow_event() -> None:
    extract(DISAGREEING_HTML, _Profile(), policy_mode="shadow",
            url="https://shop.example.com/p/1")

    assert shadow_buffer_size() == 1
    event = drain_shadow_events()[0]
    assert isinstance(event, ShadowEvent)
    assert event.url == "https://shop.example.com/p/1"
    assert event.domain == "shop.example.com"
    assert event.first_hit_price == Decimal("99.00")
    assert event.disagreement_kind in {"price", "currency", "outcome"}


def test_an_agreeing_page_buffers_nothing() -> None:
    extract(AGREEING_HTML, _Profile(), policy_mode="shadow",
            url="https://shop.example.com/p/1")
    assert shadow_buffer_size() == 0


def test_off_mode_never_runs_the_ranker() -> None:
    extract(DISAGREEING_HTML, _Profile(), policy_mode="off",
            url="https://shop.example.com/p/1")
    assert shadow_buffer_size() == 0


def test_a_ranked_path_explosion_never_reaches_the_caller(monkeypatch: Any) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("ranked path is broken")

    monkeypatch.setattr(extraction_pipeline, "extract_ranked", _boom)
    candidate = extract(DISAGREEING_HTML, _Profile(), policy_mode="shadow")
    assert candidate is not None
    assert candidate.method is ExtractionMethod.JSON_LD
    assert shadow_buffer_size() == 0


def test_the_buffer_is_bounded_and_counts_what_it_drops() -> None:
    from scrape_core.extraction.pipeline import (
        SHADOW_BUFFER_MAX_EVENTS,
        record_shadow_event,
        shadow_events_dropped,
    )

    template = ShadowEvent(
        observed_at=None,
        url="https://shop.example.com/p/1",
        domain="shop.example.com",
        policy_version="v1",
        extractor_version="x",
        profile_version=None,
        disagreement_kind="price",
        first_hit_method="JSON_LD",
        first_hit_price=Decimal("1"),
        first_hit_currency="SAR",
        ranked_outcome="winner",
        ranked_method="CSS",
        ranked_price=Decimal("2"),
        ranked_currency="SAR",
        page_evidence_hash=None,
        detail="",
    )
    for _ in range(SHADOW_BUFFER_MAX_EVENTS + 5):
        record_shadow_event(template)

    assert shadow_buffer_size() == SHADOW_BUFFER_MAX_EVENTS
    assert shadow_events_dropped() >= 5


# --------------------------------------------------------------------------
# Persistence
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

    def add_all(self, items: Any) -> None:
        self.added.extend(items)

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
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
    EVIDENCE_STORE_DIR = None


class _FakeRedis:
    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        return True


def _install_fakes(monkeypatch: Any) -> _FakeSession:
    session = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(session))
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    # Observations reach the DB as a Core `INSERT ... VALUES` built from
    # lowered dicts, not through `session.add_all`, so the ORM instances
    # are captured at the one seam that still sees them.
    monkeypatch.setattr(
        pipelines_mod,
        "_insert_ignoring_replays",
        lambda _session, _model, instances, _keys: session.added.extend(instances),
    )
    return session


def _item() -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=None,
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=True,
        price=Decimal("99.00"),
        currency="SAR",
    )


def test_flush_batch_writes_the_buffered_shadow_events(monkeypatch: Any) -> None:
    session = _install_fakes(monkeypatch)
    extract(DISAGREEING_HTML, _Profile(), policy_mode="shadow",
            url="https://shop.example.com/p/1")

    pipelines_mod._flush_batch(WORKSPACE_ID, [_item()])

    rows = [obj for obj in session.added if type(obj).__name__ == "ExtractionShadowEvent"]
    assert len(rows) == 1
    assert rows[0].url == "https://shop.example.com/p/1"
    assert rows[0].disagreement_kind
    # Drained, not re-read: a second flush must not duplicate the row.
    assert shadow_buffer_size() == 0


def test_the_first_hit_price_is_what_gets_persisted(monkeypatch: Any) -> None:
    session = _install_fakes(monkeypatch)
    candidate = extract(DISAGREEING_HTML, _Profile(), policy_mode="shadow",
                        url="https://shop.example.com/p/1")
    assert candidate is not None

    item = _item()
    pipelines_mod._flush_batch(WORKSPACE_ID, [item])

    observations = [o for o in session.added if type(o).__name__ == "PriceObservation"]
    assert observations[0].price == Decimal("99.00")


def test_a_first_hit_candidate_is_still_an_extraction_candidate() -> None:
    candidate = extract(DISAGREEING_HTML, _Profile(), policy_mode="shadow")
    assert isinstance(candidate, ExtractionCandidate)


# --------------------------------------------------------------------------
# `scripts/run_offer_benchmark.py --from-shadow-events` (the C11 gate input)
# --------------------------------------------------------------------------


def _benchmark_module() -> Any:
    """Load `scripts/run_offer_benchmark.py` (not an importable package).

    Registered in `sys.modules` BEFORE `exec_module`, because
    `dataclasses` resolves a class's annotations through
    `sys.modules[cls.__module__]` and a module that is not there yet
    raises on the first `@dataclass` in the file.
    """
    import importlib.util
    import sys
    from pathlib import Path

    name = "run_offer_benchmark_c5"
    if name in sys.modules:
        return sys.modules[name]
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    spec = importlib.util.spec_from_file_location(
        name, repo_root / "scripts" / "run_offer_benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_events(tmp_path: Any, rows: list[dict[str, Any]]) -> Any:
    import json

    path = tmp_path / "shadow.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_shadow_events_are_summarised_by_kind_and_domain(tmp_path: Any) -> None:
    module = _benchmark_module()
    path = _write_events(
        tmp_path,
        [
            {"disagreement_kind": "price", "domain": "noon.com", "url": "u1",
             "first_hit_price": "10.00", "ranked_price": "20.00"},
            {"disagreement_kind": "outcome", "domain": "noon.com", "url": "u2",
             "first_hit_price": None, "ranked_price": "5.00"},
        ],
    )
    report = module.summarize_shadow_events(
        module.load_shadow_events(path), observations=1000
    )
    assert report.events == 2
    assert report.by_kind == {"price": 1, "outcome": 1}
    assert report.by_domain == {"noon.com": 2}
    assert report.disagreement_rate() == 2 / 1000


def test_labels_adjudicate_who_was_right(tmp_path: Any) -> None:
    import json
    from decimal import Decimal as D

    module = _benchmark_module()
    path = _write_events(
        tmp_path,
        [
            {"disagreement_kind": "price", "domain": "d", "url": "u1",
             "first_hit_price": "10.00", "ranked_price": "20.00"},
            {"disagreement_kind": "price", "domain": "d", "url": "u2",
             "first_hit_price": "7.00", "ranked_price": "9.00"},
            {"disagreement_kind": "price", "domain": "d", "url": "u3",
             "first_hit_price": "1.00", "ranked_price": "2.00"},
        ],
    )
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"url": "u1", "price": "20.00"},   # ranker right
                {"url": "u2", "price": "7.00"},    # first hit right
                {"url": "u3", "price": "99.00"},   # both wrong
            )
        ),
        encoding="utf-8",
    )

    report = module.summarize_shadow_events(
        module.load_shadow_events(path),
        labels=module.load_labeled_prices([labels_path]),
        observations=300,
    )
    assert report.labeled_conflicts == 3
    assert report.ranker_wins == 1
    assert report.first_hit_wins == 1
    # Counted apart: a case where BOTH paths missed is evidence about the
    # page, not about the ranker.
    assert report.both_wrong == 1
    assert report.ranker_win_rate() == 1 / 3
    assert module.load_labeled_prices([labels_path])["u1"] == D("20.00")


def test_an_unknown_denominator_refuses_the_gate(tmp_path: Any) -> None:
    """Unknown is never a pass -- the gate exists to require evidence."""
    module = _benchmark_module()
    path = _write_events(
        tmp_path,
        [{"disagreement_kind": "price", "domain": "d", "url": "u1",
          "first_hit_price": "1.00", "ranked_price": "2.00"}],
    )
    report = module.summarize_shadow_events(module.load_shadow_events(path))
    allowed, reasons = module.decide_shadow_gate(report)
    assert allowed is False
    assert any("disagreement rate unknown" in reason for reason in reasons)
    assert any("ranker win rate unknown" in reason for reason in reasons)


def test_the_gate_thresholds_are_the_plans_own_numbers() -> None:
    module = _benchmark_module()
    thresholds = module.ShadowGateThresholds()
    assert thresholds.max_disagreement_rate == 0.01
    assert thresholds.min_ranker_win_rate == 0.99


def test_a_clean_window_meets_both_thresholds(tmp_path: Any) -> None:
    import json

    module = _benchmark_module()
    path = _write_events(
        tmp_path,
        [{"disagreement_kind": "price", "domain": "d", "url": "u1",
          "first_hit_price": "1.00", "ranked_price": "2.00"}],
    )
    labels_path = tmp_path / "labels.jsonl"
    labels_path.write_text(json.dumps({"url": "u1", "price": "2.00"}), encoding="utf-8")

    report = module.summarize_shadow_events(
        module.load_shadow_events(path),
        labels=module.load_labeled_prices([labels_path]),
        observations=1000,
    )
    allowed, reasons = module.decide_shadow_gate(report)
    assert allowed is True
    assert any("OWNER decision" in reason for reason in reasons)


def test_a_malformed_export_line_is_refused_not_skipped(tmp_path: Any) -> None:
    module = _benchmark_module()
    path = tmp_path / "shadow.jsonl"
    path.write_text('{"disagreement_kind": "price"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_shadow_events(path)


def test_the_cli_accepts_from_shadow_events(tmp_path: Any) -> None:
    module = _benchmark_module()
    path = _write_events(
        tmp_path,
        [{"disagreement_kind": "price", "domain": "d", "url": "u1",
          "first_hit_price": "1.00", "ranked_price": "2.00"}],
    )
    out = tmp_path / "out.json"
    assert module.main(
        ["--from-shadow-events", str(path), "--observations", "500",
         "--json-out", str(out), "--quiet"]
    ) == 0
    import json

    summary = json.loads(out.read_text())
    assert summary["events"] == 1
    assert summary["disagreement_rate"] == 1 / 500
    # No labeled conflict in this window -> the gate refuses rather than
    # passing on an absence of evidence.
    assert summary["c11_thresholds_met"] is False
