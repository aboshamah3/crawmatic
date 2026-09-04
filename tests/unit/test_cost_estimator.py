"""What a scrape actually costs (H4/B2).

Before this module the ledger priced every paid operation with a
per-DOMAIN average and a floor of one CENT. Both halves were wrong in
the same direction:

* the floor booked ``$0.01`` for a request whose real proxy cost was
  ``$0.00000046`` — a **2,174x** over-statement on the direct-site rate
  and **47x** on amazon.sa's;
* the per-domain average could not see that the same domain costs a
  hundred times more through a browser than through plain HTTP, because
  what the provider bills is BYTES, not requests.

So the unit of pricing here is the unit the provider invoices:
proxied bytes at ``$1.00/GiB`` (the recorded DataImpulse pool rate) and
browser CPU-seconds at Railway's ``$20/vCPU-month``. These tests pin the
arithmetic, not the implementation.
"""

from __future__ import annotations

import math

import pytest

from app_shared.costauth.pricing import (
    BROWSER_CPU_PER_WALL_SECOND,
    ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST,
    ESTIMATED_BYTES_PER_REQUEST,
    MICRO_USD_PER_BROWSER_CPU_SECOND,
    estimate_reservation_micro_units,
    price_operation_micro_units,
)

GIB = 2**30


# --- pricing one settled operation ---------------------------------------


def test_direct_is_not_priced() -> None:
    """A fetch off the fleet's own egress has NO provider charge.

    ``None``, never a fabricated zero and never a floor: pricing fleet
    egress is reconciliation's problem, not a number to invent at the
    boundary.
    """
    assert (
        price_operation_micro_units(
            transport="DIRECT", bytes_on_wire=500_000, browser_cpu_seconds=None
        )
        is None
    )


def test_proxy_priced_by_bytes_at_one_dollar_per_gib() -> None:
    """113,900 bytes is ~106 micro-USD — not the 10,000 the floor booked."""
    assert price_operation_micro_units(
        transport="PROXY", bytes_on_wire=113_900, browser_cpu_seconds=None
    ) == math.ceil(113_900 * 1e6 / GIB)


def test_proxy_floor_is_one_micro_unit_not_one_cent() -> None:
    """The floor survives H4 — at a MILLIONTH of its old size.

    A request that consumed real bytes must not book literally zero (that
    is how the 2026-08-12 rediscovery loop stayed invisible), but one
    micro-USD is a rounding artefact where one cent was a 2,174x lie.
    """
    assert (
        price_operation_micro_units(
            transport="PROXY", bytes_on_wire=1, browser_cpu_seconds=None
        )
        == 1
    )


def test_proxy_with_unknown_bytes_falls_back_to_the_floor() -> None:
    """Unobserved bytes are not free bytes — they are the floor."""
    assert (
        price_operation_micro_units(
            transport="PROXY", bytes_on_wire=None, browser_cpu_seconds=None
        )
        == 1
    )


def test_browser_prices_cpu_seconds_plus_proxied_bytes() -> None:
    """A proxied browser leg pays for BOTH legs of what it consumed."""
    expected = math.ceil(2_530_000 * 1e6 / GIB) + math.ceil(7.7 * 19)
    assert (
        price_operation_micro_units(
            transport="BROWSER", bytes_on_wire=2_530_000, browser_cpu_seconds=7.7
        )
        == expected
    )


def test_browser_off_the_direct_leg_prices_cpu_only() -> None:
    """No proxy, no proxy bill — the compute is still real and is charged."""
    assert price_operation_micro_units(
        transport="BROWSER", bytes_on_wire=0, browser_cpu_seconds=7.7
    ) == math.ceil(7.7 * 19)


def test_browser_cpu_rate_is_the_measured_railway_rate() -> None:
    """$20/vCPU-month == $0.000019/CPU-second == 19 micro-USD."""
    assert MICRO_USD_PER_BROWSER_CPU_SECOND == 19
    assert BROWSER_CPU_PER_WALL_SECOND == 1.8


# --- pricing a reservation (the ceiling, before anything is observed) -----


def test_reservation_for_twenty_amazon_http_requests_is_a_fraction_of_a_cent() -> None:
    """20 proxied fetches reserve ~4,657 micro-USD (~$0.0047), not $0.20."""
    assert estimate_reservation_micro_units(
        transport="PROXY", requests=20
    ) == math.ceil(20 * 250_000 * 1e6 / GIB)


def test_browser_reservation_includes_cpu_seconds_per_request() -> None:
    """A browser batch reserves its compute as well as its bytes."""
    requests = 3
    expected = math.ceil(
        requests * ESTIMATED_BYTES_PER_REQUEST["BROWSER"] * 1e6 / GIB
    ) + math.ceil(requests * ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST * 19)
    assert estimate_reservation_micro_units(transport="BROWSER", requests=requests) == expected


def test_direct_reservation_is_zero_not_none() -> None:
    """A reservation is a CEILING and must be an int — nothing to reserve is 0."""
    assert estimate_reservation_micro_units(transport="DIRECT", requests=20) == 0


def test_reservation_never_reserves_nothing_for_a_paid_batch() -> None:
    """A ceiling of zero is not a ceiling."""
    assert estimate_reservation_micro_units(transport="PROXY", requests=0) >= 1


# --- strictness ----------------------------------------------------------


def test_unknown_transport_is_refused_not_guessed() -> None:
    """Guessing a transport is guessing a bill."""
    with pytest.raises(ValueError, match="transport"):
        price_operation_micro_units(
            transport="CARRIER_PIGEON", bytes_on_wire=10, browser_cpu_seconds=None
        )
    with pytest.raises(ValueError, match="transport"):
        estimate_reservation_micro_units(transport="CARRIER_PIGEON", requests=1)


def test_the_transport_enum_itself_is_accepted() -> None:
    """Call sites hold a ``NetworkTransport``; they must not have to stringify it."""
    from app_shared.models.network_operations import NetworkTransport

    assert price_operation_micro_units(
        transport=NetworkTransport.PROXY, bytes_on_wire=113_900, browser_cpu_seconds=None
    ) == price_operation_micro_units(
        transport="PROXY", bytes_on_wire=113_900, browser_cpu_seconds=None
    )
    assert (
        price_operation_micro_units(
            transport=NetworkTransport.DIRECT, bytes_on_wire=1, browser_cpu_seconds=None
        )
        is None
    )


def test_prices_are_integers_never_floats() -> None:
    """§19: money is an integer count of micro-USD, all the way down."""
    priced = price_operation_micro_units(
        transport="BROWSER", bytes_on_wire=2_530_000, browser_cpu_seconds=7.7
    )
    assert isinstance(priced, int) and not isinstance(priced, bool)
    assert isinstance(estimate_reservation_micro_units(transport="PROXY", requests=20), int)
