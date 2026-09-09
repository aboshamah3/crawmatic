"""C1/F08 failure classes: timeout phases and `EXTRACTION_FAILED`.

Two codes were doing work they could not support.

`TIMEOUT` blamed nobody: a connect timeout is the proxy vendor's problem,
a TTFB timeout is the host's, and a read timeout is page weight. A5
already splits `request_attempts` into `connect_ms`/`ttfb_ms`/`read_ms`,
so the phase that never completed is knowable and the blame is
attributable.

`PRICE_NOT_FOUND` was claimed for ANY 200 with no price — including
interstitials, consent walls, soft-404s and un-rendered JS shells. That
is the strongest claim in the vocabulary short of `NOT_LISTED` ("we read
this product's genuine page and it carried no price"), and the strategy
optimizer, rediscovery and the domain scorecard all learn from it. Deep
dive §6.1: "200 and fast is not success."
"""

from __future__ import annotations

from app_shared.enums import ScrapeErrorCode

from scrape_core.errors import (
    classify_extraction_outcome,
    classify_http_status,
    classify_timeout_phase,
    page_carries_product_identity,
)


# --- timeout phases ---------------------------------------------------------


def test_never_connected_is_a_connect_timeout() -> None:
    assert classify_timeout_phase() is ScrapeErrorCode.CONNECT_TIMEOUT
    assert (
        classify_timeout_phase(connect_ms=None, ttfb_ms=None, read_ms=None)
        is ScrapeErrorCode.CONNECT_TIMEOUT
    )


def test_connected_but_no_first_byte_is_a_ttfb_timeout() -> None:
    assert (
        classify_timeout_phase(connect_ms=180) is ScrapeErrorCode.TTFB_TIMEOUT
    ), "the request was written; the HOST never answered"


def test_first_byte_but_unfinished_body_is_a_read_timeout() -> None:
    assert (
        classify_timeout_phase(connect_ms=180, ttfb_ms=900)
        is ScrapeErrorCode.READ_TIMEOUT
    )


def test_every_phase_measured_falls_back_to_plain_timeout() -> None:
    """No phase is missing, so nothing may be blamed."""
    assert (
        classify_timeout_phase(connect_ms=180, ttfb_ms=900, read_ms=1200)
        is ScrapeErrorCode.TIMEOUT
    )


def test_zero_is_a_measurement_not_a_missing_phase() -> None:
    """`None` means "never reached"; 0 ms means "instant"."""
    assert (
        classify_timeout_phase(connect_ms=0, ttfb_ms=0) is ScrapeErrorCode.READ_TIMEOUT
    )


# --- product identity -------------------------------------------------------


def test_any_single_identity_signal_is_enough() -> None:
    assert page_carries_product_identity(title="Sony WH-1000XM5 | noon") is True
    assert page_carries_product_identity(product_name="Sony WH-1000XM5") is True
    assert page_carries_product_identity(sku="B09XS7JWHH") is True
    assert page_carries_product_identity(structured_product_data=True) is True


def test_a_blank_title_is_the_absence_of_a_title() -> None:
    assert page_carries_product_identity(title="   ") is False
    assert page_carries_product_identity() is False


# --- PRICE_NOT_FOUND vs EXTRACTION_FAILED -----------------------------------


def test_price_not_found_on_a_matching_product_page_stays_price_not_found() -> None:
    """The listing verdict the optimizer is allowed to learn from."""
    assert (
        classify_extraction_outcome(
            status_code=200,
            price_found=False,
            has_product_identity=True,
            identity_matches_target=True,
        )
        is ScrapeErrorCode.PRICE_NOT_FOUND
    )


def test_two_hundred_with_no_product_title_is_extraction_failed() -> None:
    """A wall, a shell or a soft-404 -- evidence about our ACCESS PATH."""
    assert (
        classify_extraction_outcome(
            status_code=200, price_found=False, has_product_identity=False
        )
        is ScrapeErrorCode.EXTRACTION_FAILED
    )


def test_unchecked_identity_is_not_an_accusation() -> None:
    """`None` means "not checked", never "mismatch"."""
    assert (
        classify_extraction_outcome(
            status_code=200,
            price_found=False,
            has_product_identity=True,
            identity_matches_target=None,
        )
        is ScrapeErrorCode.PRICE_NOT_FOUND
    )


def test_a_proven_different_product_is_an_identity_mismatch() -> None:
    assert (
        classify_extraction_outcome(
            status_code=200,
            price_found=False,
            has_product_identity=True,
            identity_matches_target=False,
        )
        is ScrapeErrorCode.IDENTITY_MISMATCH
    )


def test_a_price_is_a_success() -> None:
    assert (
        classify_extraction_outcome(
            status_code=200, price_found=True, has_product_identity=True
        )
        is None
    )


def test_a_failing_status_always_wins_over_the_body() -> None:
    """A 403 interstitial is a 403, not an extraction problem."""
    for status in (403, 404, 429, 500):
        assert classify_extraction_outcome(
            status_code=status, price_found=False, has_product_identity=False
        ) is classify_http_status(status)


def test_the_new_codes_fit_the_persisted_column() -> None:
    """`error_code` is an app-validated VARCHAR(32), not a PG enum."""
    for code in (
        ScrapeErrorCode.CONNECT_TIMEOUT,
        ScrapeErrorCode.TTFB_TIMEOUT,
        ScrapeErrorCode.READ_TIMEOUT,
        ScrapeErrorCode.EXTRACTION_FAILED,
        ScrapeErrorCode.ATTEMPT_BUDGET_EXHAUSTED,
        ScrapeErrorCode.TARGET_DEADLINE_EXCEEDED,
    ):
        assert code.value == code.name
        assert len(code.value) <= 32, code
