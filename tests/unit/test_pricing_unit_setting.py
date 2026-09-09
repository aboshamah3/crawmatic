"""The proxy billing unit is a NAMED setting, not an implicit constant
(EPA A8, deep dive §8.3).

Before this, ``costauth.pricing`` divided every priced byte count by a bare
``2**30`` literal. The September 3 pricing note itself confused ``$/GB``
with ``$/GiB`` twice — a real provider-contract ambiguity with nowhere to
record the decision. ``Settings.PROXY_BILLING_UNIT_BYTES`` makes the unit
visible and overridable; these tests pin its default (today's numbers must
not move) and prove an override actually changes the price.
"""

from __future__ import annotations

import math

import pytest

from app_shared.config import get_settings
from app_shared.costauth.pricing import billing_unit_bytes, price_operation_micro_units

GIB = 2**30


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # `get_settings()` is `lru_cache`d — an env override in one test must
    # not leak its cached `Settings` instance into the next.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_default_billing_unit_is_one_gib():
    assert billing_unit_bytes() == GIB == 1_073_741_824


def test_one_gib_at_one_dollar_per_unit_prices_to_one_million_micro_usd():
    # 1 GiB of proxied bytes, at the recorded $1.00/GiB rate, is exactly
    # $1.00 == 1,000,000 micro-USD with the default (one GiB) unit — an
    # exact multiple, so no rounding direction is even exercised here.
    assert price_operation_micro_units(
        transport="PROXY", bytes_on_wire=GIB, browser_cpu_seconds=None
    ) == 1_000_000


def test_billing_unit_bytes_reads_the_env_override(monkeypatch):
    monkeypatch.setenv("PROXY_BILLING_UNIT_BYTES", "1000000000")
    get_settings.cache_clear()
    assert billing_unit_bytes() == 1_000_000_000


def test_a_smaller_billing_unit_prices_the_same_bytes_higher(monkeypatch):
    # The same 1 GiB of bytes, priced against a smaller (decimal GB) unit,
    # costs MORE per byte — proof the unit is a real, visible choice and
    # not a cosmetic rename. Ceiling division (the ledger's documented
    # fail-closed rounding, unchanged by this setting) on
    # 1_073_741_824 * 1_000_000 / 1_000_000_000 == 1_073_741.824 rounds
    # UP to 1_073_742 micro-USD — one micro-unit higher than a plain
    # truncation, which is the direction this ledger always rounds so a
    # byte that was actually billed is never under-booked.
    monkeypatch.setenv("PROXY_BILLING_UNIT_BYTES", "1000000000")
    get_settings.cache_clear()
    assert billing_unit_bytes() == 1_000_000_000

    priced = price_operation_micro_units(
        transport="PROXY", bytes_on_wire=GIB, browser_cpu_seconds=None
    )
    assert priced == math.ceil(GIB * 1_000_000 / 1_000_000_000) == 1_073_742
    assert priced > 1_000_000  # strictly more than the one-GiB-unit price


def test_billing_unit_bytes_is_read_fresh_not_cached_across_settings_reloads(monkeypatch):
    assert billing_unit_bytes() == GIB
    monkeypatch.setenv("PROXY_BILLING_UNIT_BYTES", "2000000000")
    get_settings.cache_clear()
    assert billing_unit_bytes() == 2_000_000_000
