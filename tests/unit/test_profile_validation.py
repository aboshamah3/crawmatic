"""`app_shared/profiles/validation.py` unit tests (SPEC-06 US1 T017, FR-005/006/007, SC-006).

Pure, DB-free. Enum accept/reject; regex compile-ok / uncompilable /
catastrophic-pattern corpus; cookie technical-accept / session-auth-
reject corpus; and the **positive empty-extraction case** — a profile
with a valid mode/adapter but no selectors/xpath/regex is ACCEPTED, not
rejected (spec Edge Cases "all extraction fields empty").

The `validation_rules`/`confidence_rules`/money corpus is added in
US4/T043 (`Extend tests/unit/test_profile_validation.py`) — a minimal
smoke case for each is still included here since `validate_profile`
already wires them into the facade.
"""

from __future__ import annotations

import pytest

from app_shared.profiles.validation import (
    ProfileValidationError,
    coerce_enums,
    compile_regex_or_reject,
    reject_session_cookies,
    validate_confidence_rules,
    validate_profile,
    validate_validation_rules,
)

# --- enum coercion (FR-005) ---------------------------------------------------


def test_coerce_enums_accepts_valid_values() -> None:
    coerce_enums({"mode": "HTTP", "adapter_key": "default_http", "variant_strategy": "PAGE_SINGLE_PRICE"})


def test_coerce_enums_accepts_absent_fields() -> None:
    coerce_enums({})


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "NOT_A_MODE"),
        ("adapter_key", "not_an_adapter"),
        ("variant_strategy", "NOT_A_STRATEGY"),
    ],
)
def test_coerce_enums_rejects_out_of_set_value(field: str, value: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        coerce_enums({field: value})
    assert exc_info.value.field == field
    assert exc_info.value.code == "INVALID_ENUM"


# --- regex compile + ReDoS heuristic (FR-006) ---------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        r"\$(\d+\.\d{2})",
        r"^[A-Z]{3}$",
        r"price:\s*(\d+)",
        r"[a-z0-9]+",
    ],
)
def test_benign_regex_compiles_and_passes(pattern: str) -> None:
    compile_regex_or_reject(pattern, field="price_regex")


def test_uncompilable_regex_is_rejected() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        compile_regex_or_reject(r"(unclosed", field="price_regex")
    assert exc_info.value.field == "price_regex"
    assert exc_info.value.code == "REGEX_UNCOMPILABLE"


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+",
        r"(a*)*",
        r"(a+)*",
        r"(a*)+",
        r"(a|a)+",
    ],
)
def test_catastrophic_backtracking_pattern_is_rejected(pattern: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        compile_regex_or_reject(pattern, field="price_regex")
    assert exc_info.value.field == "price_regex"
    assert exc_info.value.code == "REGEX_CATASTROPHIC"


def test_validate_profile_checks_every_regex_field() -> None:
    for field in ("price_regex", "old_price_regex", "currency_regex", "stock_regex"):
        with pytest.raises(ProfileValidationError) as exc_info:
            validate_profile({field: "(a+)+"})
        assert exc_info.value.field == field


# --- cookies (FR-007, §30) -----------------------------------------------------


@pytest.mark.parametrize(
    "cookies",
    [
        {"currency": "USD"},
        {"cur": "EUR"},
        {"lang": "en"},
        {"locale": "en-US"},
        {"country": "DE"},
        [{"name": "currency", "value": "USD"}, {"name": "locale", "value": "en"}],
    ],
)
def test_technical_cookies_are_accepted(cookies) -> None:
    reject_session_cookies(cookies)


@pytest.mark.parametrize(
    "name",
    [
        "session",
        "sessionid",
        "sid",
        "sess",
        "PHPSESSID",
        "JSESSIONID",
        "ASP.NET_SessionId",
        "connect.sid",
        "auth",
        "Authorization",
        "token",
        "access_token",
        "refresh_token",
        "jwt",
        "csrf",
        "xsrf",
        "remember",
        "remember_me",
        "login",
        "logged_in",
        "user",
        "uid",
        "account",
        "my_auth_cookie",
        "x-csrf-token",
    ],
)
def test_session_auth_cookie_names_are_rejected_dict_shape(name: str) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        reject_session_cookies({name: "value"})
    assert exc_info.value.field == "cookies"
    assert exc_info.value.code == "FORBIDDEN_COOKIE"


def test_session_auth_cookie_names_are_rejected_list_shape() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        reject_session_cookies([{"name": "sessionid", "value": "abc"}])
    assert exc_info.value.code == "FORBIDDEN_COOKIE"


def test_cookies_none_is_accepted() -> None:
    reject_session_cookies(None)


@pytest.mark.parametrize("bad_shape", ["not-a-dict-or-list", 42, [{"missing_name": "x"}], [1, 2]])
def test_malformed_cookies_shape_is_rejected(bad_shape) -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        reject_session_cookies(bad_shape)
    assert exc_info.value.code == "INVALID_SHAPE"


# --- validation_rules / confidence_rules smoke (full corpus in T043) ---------


def test_validation_rules_valid_bundle_passes() -> None:
    validate_validation_rules(
        {"required_currency": "USD", "min_price": "1.00", "max_price": "999.99"}
    )


def test_validation_rules_none_passes() -> None:
    validate_validation_rules(None)


def test_confidence_rules_valid_bundle_passes() -> None:
    validate_confidence_rules({"css": 0.85, "regex": 0.75})


def test_confidence_rules_none_passes() -> None:
    validate_confidence_rules(None)


# --- positive empty-extraction case (spec Edge Cases) -------------------------


def test_profile_with_no_selectors_xpath_regex_is_accepted() -> None:
    """A profile with a valid mode/adapter but NO selectors/xpath/regex
    must be ACCEPTED, not rejected — later extraction may rely on
    JSON-LD/platform patterns alone."""
    validate_profile(
        {
            "mode": "HTTP",
            "adapter_key": "jsonld_first",
            "variant_strategy": "PAGE_SINGLE_PRICE",
            "price_selector": None,
            "price_xpath": None,
            "price_regex": None,
            "old_price_selector": None,
            "old_price_xpath": None,
            "old_price_regex": None,
            "currency_selector": None,
            "currency_xpath": None,
            "currency_regex": None,
            "stock_selector": None,
            "stock_xpath": None,
            "stock_regex": None,
            "title_selector": None,
            "title_xpath": None,
        }
    )


def test_profile_with_no_fields_at_all_is_accepted() -> None:
    validate_profile({})


def test_validate_profile_facade_raises_on_first_offending_field() -> None:
    with pytest.raises(ProfileValidationError) as exc_info:
        validate_profile({"mode": "NOT_A_MODE", "price_regex": "(a+)+"})
    assert exc_info.value.code == "INVALID_ENUM"
