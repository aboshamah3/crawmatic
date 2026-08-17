"""Unit tests for `scripts/onboard_competitor.py` (Task 3.4).

DB-independent throughout, mirroring `tests/unit/test_seed_bootstrap.py`'s
convention: `apply_onboarding` is exercised against a tiny hand-rolled
`_FakeSession` (not a real `sqlalchemy.orm.Session` / not even SQLite --
`ScrapeProfile`/`DomainAccessRule` carry `JSONB` columns that don't
compile under SQLite, confirmed while designing this test, so a fake
session recording `.add()`/`.execute()` calls is the right substitute
here, not a real engine) rather than a live Postgres. Probing is
exercised against a fake `requests`-shaped session (no real network
call), and against real fixture HTML already in the repo
(`tests/fixtures/html/`) for the detection helpers.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.onboard_competitor as onboard  # noqa: E402
from app_shared.enums import AccessStrategy, ExtractionMethod  # noqa: E402
from app_shared.models.access import AccessPolicy, DomainAccessRule  # noqa: E402
from app_shared.models.competitors_matches import Competitor  # noqa: E402
from app_shared.models.scrape_profiles import ScrapeProfile  # noqa: E402

_FIXTURES_HTML = REPO_ROOT / "tests" / "fixtures" / "html"


# --- detection helpers ---------------------------------------------------------


def test_detect_embedded_json_candidate_finds_the_next_data_price_pointer() -> None:
    html = (_FIXTURES_HTML / "embedded_json_next_data.html").read_text(encoding="utf-8")
    hit = onboard.detect_embedded_json_candidate(html)
    assert hit is not None
    pointer, value, _currency = hit
    assert "price" in pointer.lower() or "amount" in pointer.lower()
    assert value  # some numeric-looking string was found


def test_detect_embedded_json_candidate_none_when_no_json_present() -> None:
    assert onboard.detect_embedded_json_candidate("<html><body>hi</body></html>") is None


def test_detect_css_candidate_finds_a_price_selector() -> None:
    html = (_FIXTURES_HTML / "css_only.html").read_text(encoding="utf-8")
    hit = onboard.detect_css_candidate(html)
    assert hit is not None
    selector, text = hit
    assert text and any(ch.isdigit() for ch in text)


def test_detect_css_candidate_none_without_any_matching_selector() -> None:
    assert onboard.detect_css_candidate("<html><body><p>no prices here</p></body></html>") is None


# --- probe_one -----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class _FakeGetSession:
    """Stands in for `requests.Session`: `.get(url, **kwargs)` returns a
    canned `_FakeResponse` (or raises), keyed by URL."""

    def __init__(self, responses: dict[str, _FakeResponse | Exception]) -> None:
        self._responses = responses

    def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
        outcome = self._responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_probe_one_network_error_is_reported_not_raised() -> None:
    session = _FakeGetSession({"http://x.invalid/p": requests.ConnectionError("refused")})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.error == "refused"
    assert result.status_code is None
    assert result.is_blocked is False


def test_probe_one_403_is_blocked() -> None:
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(403, "forbidden")})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.is_blocked is True
    assert result.block_reason == "HTTP 403"


def test_probe_one_429_is_blocked() -> None:
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(429, "slow down")})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.is_blocked is True
    assert result.block_reason == "HTTP 429"


def test_probe_one_200_cloudflare_challenge_page_is_blocked() -> None:
    body = "<html><title>Just a moment...</title><body>checking your browser</body></html>"
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(200, body)})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.is_blocked is True
    assert result.status_code == 200


def test_probe_one_shopify_captcha_bootstrap_script_is_not_a_false_positive_block() -> None:
    """Regression: a first draft's 'captcha' marker false-positived on every
    Shopify storefront's routine `<script id="captcha-bootstrap">` contact-
    form guard (found live against stech.ink, 2026-08-17). A real JSON-LD
    product page carrying that script must NOT be reported as blocked."""
    body = (
        '<html><head><script id="captcha-bootstrap">!function(){}();</script>'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Widget",'
        '"offers":{"@type":"Offer","price":"19.99","priceCurrency":"SAR",'
        '"availability":"https://schema.org/InStock"}}'
        "</script></head><body></body></html>"
    )
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(200, body)})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.is_blocked is False
    assert result.extraction_method == "JSON_LD"
    assert result.price == "19.99"


def test_probe_one_amazon_apology_page_is_blocked() -> None:
    body = (
        "<html><title>نعتذر</title><body>"
        "To discuss automated access to Amazon data please contact "
        "api-services-support@amazon.com."
        "</body></html>"
    )
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(200, body)})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.is_blocked is True


def test_probe_one_jsonld_extraction_reports_price_and_currency() -> None:
    body = (
        '<html><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Widget",'
        '"offers":{"@type":"Offer","price":"42.50","priceCurrency":"SAR",'
        '"availability":"https://schema.org/InStock"}}'
        "</script></html>"
    )
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(200, body)})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.extraction_method == ExtractionMethod.JSON_LD.value
    assert result.price == "42.50"
    assert result.currency == "SAR"


def test_probe_one_non_200_non_blocked_status_reports_http_error() -> None:
    session = _FakeGetSession({"http://x.invalid/p": _FakeResponse(500, "server error")})
    result = onboard.probe_one("http://x.invalid/p", session=session)
    assert result.error == "HTTP 500"
    assert result.is_blocked is False


# --- recommend_access ------------------------------------------------------------


def _outcome(*, is_blocked: bool = False, status_code: int = 200, method: str | None = None) -> onboard.ProbeOutcome:
    return onboard.ProbeOutcome(
        url="http://x.invalid/p",
        status_code=status_code,
        error=None,
        is_blocked=is_blocked,
        block_reason="HTTP 403" if is_blocked else None,
        extraction_method=method,
        price="1.00" if method else None,
        currency="SAR" if method else None,
        detail=None,
    )


def test_recommend_access_all_clean_is_direct_only() -> None:
    outcomes = [_outcome(method="JSON_LD") for _ in range(5)]
    rec = onboard.recommend_access(outcomes)
    assert rec.access_strategy == AccessStrategy.DIRECT_ONLY
    assert rec.max_requests_per_minute == 10
    assert rec.max_concurrent_requests == 1
    assert rec.cooldown_seconds == 2


def test_recommend_access_majority_blocked_is_proxy_first() -> None:
    outcomes = [_outcome(is_blocked=True) for _ in range(3)] + [_outcome(method="JSON_LD") for _ in range(2)]
    rec = onboard.recommend_access(outcomes)
    assert rec.access_strategy == AccessStrategy.PROXY_FIRST


def test_recommend_access_minority_blocked_is_direct_then_proxy() -> None:
    outcomes = [_outcome(is_blocked=True)] + [_outcome(method="JSON_LD") for _ in range(4)]
    rec = onboard.recommend_access(outcomes)
    assert rec.access_strategy == AccessStrategy.DIRECT_THEN_PROXY


def test_recommend_access_no_probes_defaults_direct_only_with_caveat() -> None:
    rec = onboard.recommend_access([])
    assert rec.access_strategy == AccessStrategy.DIRECT_ONLY
    assert "no urls were probed" in rec.rationale.lower()


def test_recommend_access_clean_but_no_strategy_fired_adds_caution() -> None:
    outcomes = [_outcome(method=None) for _ in range(5)]
    rec = onboard.recommend_access(outcomes)
    assert rec.access_strategy == AccessStrategy.DIRECT_ONLY
    assert "CAUTION" in rec.rationale


def test_recommend_access_rate_rule_is_always_the_conservative_default() -> None:
    for outcomes in (
        [_outcome(method="JSON_LD") for _ in range(5)],
        [_outcome(is_blocked=True) for _ in range(5)],
        [_outcome(is_blocked=True), _outcome(method="JSON_LD")],
    ):
        rec = onboard.recommend_access(outcomes)
        assert rec.max_requests_per_minute == 10
        assert rec.max_concurrent_requests == 1
        assert rec.cooldown_seconds == 2


# --- derive_extraction_config ------------------------------------------------------


def test_derive_extraction_config_prefers_jsonld_over_others() -> None:
    outcomes = [
        _outcome(method="CSS_CANDIDATE"),
        _outcome(method="JSON_LD"),
    ]
    config = onboard.derive_extraction_config(outcomes)
    assert config.method == ExtractionMethod.JSON_LD


def test_derive_extraction_config_falls_back_to_embedded_json() -> None:
    outcome = onboard.ProbeOutcome(
        url="u",
        status_code=200,
        error=None,
        is_blocked=False,
        block_reason=None,
        extraction_method="EMBEDDED_JSON_CANDIDATE",
        price="9.99",
        currency="SAR",
        detail="/props/price",
    )
    config = onboard.derive_extraction_config([outcome])
    assert config.method == ExtractionMethod.EMBEDDED_JSON
    assert config.price_json_path == "/props/price"


def test_derive_extraction_config_falls_back_to_css() -> None:
    outcome = onboard.ProbeOutcome(
        url="u",
        status_code=200,
        error=None,
        is_blocked=False,
        block_reason=None,
        extraction_method="CSS_CANDIDATE",
        price="9.99",
        currency=None,
        detail=".price",
    )
    config = onboard.derive_extraction_config([outcome])
    assert config.method == ExtractionMethod.CSS
    assert config.price_selector == ".price"


def test_derive_extraction_config_none_when_nothing_fired() -> None:
    config = onboard.derive_extraction_config([_outcome(method=None)])
    assert config.method is None


# --- build_report smoke test --------------------------------------------------------


def test_build_report_contains_domain_and_tier() -> None:
    outcomes = [_outcome(method="JSON_LD")]
    rec = onboard.recommend_access(outcomes)
    config = onboard.derive_extraction_config(outcomes)
    report = onboard.build_report(
        domain="example.com", outcomes=outcomes, recommendation=rec, extraction_config=config
    )
    assert "example.com" in report
    assert rec.tier_label in report
    assert "JSON_LD" in report


# --- apply_onboarding (FakeSession, DB-independent) ---------------------------------


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeSession:
    """Records `.add()`; `.execute(select(Model)...)` returns the
    caller-configured canned row for that model class (or `None`), mirroring
    `tests/unit/test_rediscovery_runaway.py`'s `_FakeSession` style but
    keyed by target ORM class rather than by raw SQL string."""

    def __init__(self, existing: dict[type, Any] | None = None) -> None:
        self.existing = dict(existing or {})
        self.added: list[Any] = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, statement: Any) -> _FakeResult:
        entity = statement.column_descriptions[0]["entity"]
        return _FakeResult(self.existing.get(entity))

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    def flush(self) -> None:
        self.flush_count += 1

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


def _jsonld_recommendation() -> onboard.Recommendation:
    return onboard.recommend_access([_outcome(method="JSON_LD") for _ in range(5)])


def _jsonld_extraction_config() -> onboard.ExtractionConfig:
    return onboard.derive_extraction_config([_outcome(method="JSON_LD")])


def test_apply_onboarding_creates_all_four_rows_when_nothing_exists() -> None:
    workspace_id = uuid.uuid4()
    session = _FakeSession(
        existing={Competitor: None, AccessPolicy: None, DomainAccessRule: None, ScrapeProfile: None}
    )

    result = onboard.apply_onboarding(
        session,
        workspace_id=workspace_id,
        domain="example.com",
        competitor_name="Example",
        recommendation=_jsonld_recommendation(),
        extraction_config=_jsonld_extraction_config(),
    )

    assert result.competitor_created is True
    assert result.access_policy_created is True
    assert result.domain_access_rule_created is True
    assert result.scrape_profile_created is True
    assert len(session.added) == 4
    # never commits itself -- the caller controls the transaction boundary
    assert session.commit_count == 0

    added_types = {type(obj) for obj in session.added}
    assert added_types == {Competitor, AccessPolicy, DomainAccessRule, ScrapeProfile}

    competitor = next(obj for obj in session.added if isinstance(obj, Competitor))
    assert competitor.default_access_policy_id == result.access_policy_id
    assert competitor.default_scrape_profile_id == result.scrape_profile_id

    rule = next(obj for obj in session.added if isinstance(obj, DomainAccessRule))
    assert rule.max_requests_per_minute == 10
    assert rule.max_concurrent_requests == 1
    assert rule.cooldown_seconds == 2
    assert rule.access_policy_id == result.access_policy_id

    policy = next(obj for obj in session.added if isinstance(obj, AccessPolicy))
    assert policy.strategy == AccessStrategy.DIRECT_ONLY


def test_apply_onboarding_reuses_existing_competitor_and_policy() -> None:
    workspace_id = uuid.uuid4()
    existing_competitor = Competitor(
        id=uuid.uuid4(), workspace_id=workspace_id, name="Example", domain="example.com"
    )
    existing_policy = AccessPolicy(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name="example.com-access-policy",
        strategy=AccessStrategy.DIRECT_ONLY,
    )
    session = _FakeSession(
        existing={
            Competitor: existing_competitor,
            AccessPolicy: existing_policy,
            DomainAccessRule: None,
            ScrapeProfile: None,
        }
    )

    result = onboard.apply_onboarding(
        session,
        workspace_id=workspace_id,
        domain="example.com",
        competitor_name="Example",
        recommendation=_jsonld_recommendation(),
        extraction_config=_jsonld_extraction_config(),
    )

    assert result.competitor_created is False
    assert result.access_policy_created is False
    assert result.domain_access_rule_created is True
    assert result.scrape_profile_created is True
    assert result.competitor_id == existing_competitor.id
    assert result.access_policy_id == existing_policy.id
    # only the two missing rows get INSERTed
    added_types = {type(obj) for obj in session.added}
    assert added_types == {DomainAccessRule, ScrapeProfile}


def test_apply_onboarding_explicit_competitor_id_not_found_raises() -> None:
    workspace_id = uuid.uuid4()
    session = _FakeSession(existing={Competitor: None})

    with pytest.raises(onboard.OnboardingError):
        onboard.apply_onboarding(
            session,
            workspace_id=workspace_id,
            domain="example.com",
            competitor_name="Example",
            recommendation=_jsonld_recommendation(),
            extraction_config=_jsonld_extraction_config(),
            competitor_id=uuid.uuid4(),
        )
    # nothing should have been added -- the lookup failed before any create
    assert session.added == []


# --- CLI plumbing --------------------------------------------------------------------


def test_read_urls_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "https://example.com/a\n\n# a comment\nhttps://example.com/b\n   \n",
        encoding="utf-8",
    )
    urls = onboard._read_urls(urls_file)
    assert urls == ["https://example.com/a", "https://example.com/b"]


def test_parse_args_dry_run_defaults() -> None:
    args = onboard.parse_args(["--domain", "example.com", "--urls", "urls.txt"])
    assert args.domain == "example.com"
    assert args.apply is False
    assert args.workspace_id is None


def test_main_apply_without_workspace_id_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("https://example.com/a\n", encoding="utf-8")

    monkeypatch.setattr(
        onboard,
        "probe_urls",
        lambda urls: [_outcome(method="JSON_LD")],
    )

    exit_code = onboard.main(["--domain", "example.com", "--urls", str(urls_file), "--apply"])
    assert exit_code == 2


def test_main_no_urls_in_file_errors(tmp_path: Path) -> None:
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("# only comments\n", encoding="utf-8")
    exit_code = onboard.main(["--domain", "example.com", "--urls", str(urls_file)])
    assert exit_code == 2


def test_main_dry_run_never_imports_the_database_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-review requirement: dry-run must never touch the database --
    proven here by making `app_shared.database.get_session` explode if
    called, and asserting the dry-run path still returns 0."""
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("https://example.com/a\n", encoding="utf-8")
    monkeypatch.setattr(onboard, "probe_urls", lambda urls: [_outcome(method="JSON_LD")])

    import app_shared.database as database_module

    def _boom() -> None:
        raise AssertionError("dry-run must never open a database session")

    monkeypatch.setattr(database_module, "get_session", _boom)

    exit_code = onboard.main(["--domain", "example.com", "--urls", str(urls_file)])
    assert exit_code == 0
