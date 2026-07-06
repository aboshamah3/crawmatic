"""``scrape_core.browser.variant`` unit tests (SPEC-14 T028, US3,
`contracts/variant-selection.md`, quickstart.md scenario 2).

Pure/off-reactor -- no Chromium, no real browser, no reactor, no DB. A
"fake match" is just a tiny stand-in object carrying the three
``competitor_product_matches`` columns `resolve_variant_values` reads
(`competitor_variant_options`/`_identifier`/`_sku`) -- `resolve_variant_
values` accesses these duck-typed (`getattr`), never imports the real
ORM model, so a plain namespace-like object is enough here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from scrape_core.browser.variant import (
    VariantConfigError,
    parse_variant_config,
    resolve_variant_values,
)

_TIMEOUT_MS = 15000


@dataclass
class _FakeMatch:
    competitor_variant_options: dict[str, Any] | None = field(default_factory=dict)
    competitor_variant_identifier: str | None = None
    competitor_variant_sku: str | None = None


# --- valid round trip: resolve_variant_values -> parse_variant_config -------


def test_valid_config_round_trip_produces_ordered_page_methods_with_resolved_value() -> None:
    """select_option (value_from options.size) + click + wait_for_selector,
    in order, the select_option's value coming from the fake match."""
    config = {
        "version": 1,
        "actions": [
            {"type": "select_option", "selector": "select#size", "value_from": "options.size"},
            {"type": "click", "selector": "button.add-to-cart"},
            {"type": "wait_for_selector", "selector": ".price[data-ready]", "state": "visible"},
        ],
    }
    match = _FakeMatch(competitor_variant_options={"size": "L"})

    resolved = resolve_variant_values(config, match)
    methods = parse_variant_config(config, resolved, timeout_ms=_TIMEOUT_MS)

    assert len(methods) == 3

    m0 = methods[0]
    assert m0.method == "select_option"
    assert m0.args == ("select#size", "L")

    m1 = methods[1]
    assert m1.method == "click"
    assert m1.args == ("button.add-to-cart",)

    m2 = methods[2]
    assert m2.method == "wait_for_selector"
    assert m2.args == (".price[data-ready]",)
    assert m2.kwargs.get("state") == "visible"
    assert m2.kwargs.get("timeout") == _TIMEOUT_MS


def test_value_from_identifier_and_sku_resolve() -> None:
    config = {
        "version": 1,
        "actions": [
            {"type": "fill", "selector": "#variant-identifier", "value_from": "identifier"},
            {"type": "select_option", "selector": "#sku", "value_from": "sku"},
        ],
    }
    match = _FakeMatch(competitor_variant_identifier="VARIANT-123", competitor_variant_sku="SKU-9")

    resolved = resolve_variant_values(config, match)
    methods = parse_variant_config(config, resolved, timeout_ms=_TIMEOUT_MS)

    assert methods[0].args == ("#variant-identifier", "VARIANT-123")
    assert methods[1].args == ("#sku", "SKU-9")


def test_literal_value_needs_no_resolution() -> None:
    config = {
        "version": 1,
        "actions": [{"type": "select_option", "selector": "select#size", "value": "M"}],
    }
    match = _FakeMatch()

    resolved = resolve_variant_values(config, match)
    methods = parse_variant_config(config, resolved, timeout_ms=_TIMEOUT_MS)

    assert methods[0].args == ("select#size", "M")


# --- config is None -> [] (FR-004, US3 AS2) ---------------------------------


def test_none_config_resolves_to_empty_dict_and_empty_page_methods() -> None:
    assert resolve_variant_values(None, _FakeMatch()) == {}
    assert parse_variant_config(None, {}, timeout_ms=_TIMEOUT_MS) == []


# --- forbidden/unknown action type -> VariantConfigError (R2 security) -----


def test_evaluate_action_type_is_forbidden() -> None:
    config = {"version": 1, "actions": [{"type": "evaluate", "expression": "1+1"}]}

    with pytest.raises(VariantConfigError):
        parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)


@pytest.mark.parametrize("action_type", ["route", "add_init_script", "pdf", "goto", "unknown_thing"])
def test_other_non_allowlisted_action_types_are_forbidden(action_type: str) -> None:
    config = {"version": 1, "actions": [{"type": action_type, "selector": "#x"}]}

    with pytest.raises(VariantConfigError):
        parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)


def test_element_action_missing_selector_is_rejected() -> None:
    config = {"version": 1, "actions": [{"type": "click"}]}

    with pytest.raises(VariantConfigError):
        parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)


def test_value_action_missing_value_and_value_from_is_rejected() -> None:
    config = {"version": 1, "actions": [{"type": "fill", "selector": "#x"}]}

    with pytest.raises(VariantConfigError):
        parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)


# --- unresolved value_from (missing option key) -> VariantConfigError ------


def test_unresolved_value_from_missing_option_key_raises_at_resolve_time() -> None:
    config = {
        "version": 1,
        "actions": [{"type": "select_option", "selector": "select#size", "value_from": "options.size"}],
    }
    match = _FakeMatch(competitor_variant_options={"color": "red"})  # no "size" key

    with pytest.raises(VariantConfigError):
        resolve_variant_values(config, match)


def test_value_from_resolving_to_none_raises() -> None:
    config = {
        "version": 1,
        "actions": [{"type": "fill", "selector": "#x", "value_from": "identifier"}],
    }
    match = _FakeMatch(competitor_variant_identifier=None)

    with pytest.raises(VariantConfigError):
        resolve_variant_values(config, match)


def test_unknown_value_from_prefix_raises() -> None:
    config = {
        "version": 1,
        "actions": [{"type": "fill", "selector": "#x", "value_from": "totally_unknown"}],
    }

    with pytest.raises(VariantConfigError):
        resolve_variant_values(config, _FakeMatch())


# --- bad version -> VariantConfigError --------------------------------------


@pytest.mark.parametrize("version", [None, 2, 0, "1"])
def test_bad_version_is_rejected(version: object) -> None:
    config = {"version": version, "actions": []}

    with pytest.raises(VariantConfigError):
        parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)


# --- settle-only (actions: [] + settle) is valid ----------------------------


def test_settle_only_config_is_valid() -> None:
    config = {
        "version": 1,
        "actions": [],
        "settle": {"wait_for_selector": ".price-final", "load_state": "networkidle"},
    }

    methods = parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)

    assert len(methods) == 2
    assert methods[0].method == "wait_for_selector"
    assert methods[0].args == (".price-final",)
    assert methods[0].kwargs.get("timeout") == _TIMEOUT_MS
    assert methods[1].method == "wait_for_load_state"
    assert methods[1].args == ("networkidle",)
    assert methods[1].kwargs.get("timeout") == _TIMEOUT_MS


def test_settle_wait_for_selector_only() -> None:
    config = {"version": 1, "actions": [], "settle": {"wait_for_selector": ".price-final"}}

    methods = parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)

    assert len(methods) == 1
    assert methods[0].method == "wait_for_selector"


def test_actions_plus_settle_ordering() -> None:
    """The settle step(s) always come after every action (contract order)."""
    config = {
        "version": 1,
        "actions": [{"type": "click", "selector": "button.add-to-cart"}],
        "settle": {"load_state": "networkidle"},
    }

    methods = parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)

    assert len(methods) == 2
    assert methods[0].method == "click"
    assert methods[1].method == "wait_for_load_state"


# --- wait_for_timeout / wait_for_load_state carry the effective timeout ----


def test_wait_for_timeout_action_carries_effective_timeout() -> None:
    config = {"version": 1, "actions": [{"type": "wait_for_timeout"}]}

    methods = parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)

    assert methods[0].method == "wait_for_timeout"
    assert methods[0].args == (_TIMEOUT_MS,)


def test_wait_for_load_state_action_carries_effective_timeout() -> None:
    config = {"version": 1, "actions": [{"type": "wait_for_load_state", "state": "domcontentloaded"}]}

    methods = parse_variant_config(config, {}, timeout_ms=_TIMEOUT_MS)

    assert methods[0].method == "wait_for_load_state"
    assert methods[0].args == ("domcontentloaded",)
    assert methods[0].kwargs.get("timeout") == _TIMEOUT_MS


# --- VariantConfigError carries error_code = SELECTOR_BROKEN ---------------


def test_variant_config_error_carries_selector_broken_error_code() -> None:
    from app_shared.enums import ScrapeErrorCode

    try:
        parse_variant_config({"version": 2, "actions": []}, {}, timeout_ms=_TIMEOUT_MS)
    except VariantConfigError as exc:
        assert exc.error_code == ScrapeErrorCode.SELECTOR_BROKEN
    else:
        pytest.fail("expected VariantConfigError")
