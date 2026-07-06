"""Variant-selection interaction translation (SPEC-14 T024, US3,
`contracts/variant-selection.md`, `data-model.md` §2).

Two-function split (R2) so the blocking ``value_from`` resolution runs
off-reactor while the translation into Playwright ``PageMethod``s stays a
pure function with no DB/match access:

* :func:`resolve_variant_values` -- called once per target inside
  :func:`scrape_core.targets.load_targets` (off-reactor, DB session
  still open) -- resolves every action's ``value_from`` against the
  match row into a plain ``{action_index: value}`` dict.
* :func:`parse_variant_config` -- pure translation of the tenant-editable
  ``variant_selector_config`` JSON (`scrape_profiles.variant_selector_config`)
  plus the already-resolved values into the ordered
  ``scrapy_playwright.page.PageMethod`` list the spider stamps onto
  ``request.meta["playwright_page_methods"]`` (`scrape_core.browser.page
  .build_page_methods`, T026).

Both raise :class:`VariantConfigError` (never a bare ``KeyError``/
``ValueError``) on any malformed/unsupported config -- the spider's
``errback``/pre-fetch guard (T027) catches this specific type and
classifies it ``SELECTOR_BROKEN`` (R3) before any fetch, never persisting
a partially-interacted price (US3 AS3).

Security (R2, decisive): the ``type`` allowlist below is the *entire*
set of Playwright page methods this config can ever invoke -- notably no
``evaluate``/arbitrary-JS/arbitrary-callable action exists, because
``variant_selector_config`` is tenant-editable DB config and running
arbitrary JS from it would be a code-execution hole.

Import policy note: ``scrapy_playwright.page.PageMethod`` is imported
**lazily** (inside :func:`parse_variant_config` and its helpers), never
at module scope. ``resolve_variant_values`` is called from
``scrape_core.targets.load_targets`` -- shared machinery the *HTTP*
spider also imports (`apps/scrapers`, which has no scrapy-playwright/
playwright dependency, per its ``pyproject.toml``) -- so importing this
module (for ``VariantConfigError``/``resolve_variant_values``) must never
require ``scrapy-playwright`` to be installed. Only `parse_variant_config`
(called solely from ``scrape_core.browser.page.build_page_methods``, the
browser-only project's own request-building path) actually needs
``PageMethod``, so its import is deferred to first use there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app_shared.enums import ScrapeErrorCode

if TYPE_CHECKING:  # pragma: no cover - type-checking only, never imported at runtime here
    from scrapy_playwright.page import PageMethod

__all__ = ["VariantConfigError", "resolve_variant_values", "parse_variant_config"]

_SUPPORTED_VERSION = 1

#: The entire set of Playwright page methods a `variant_selector_config`
#: action may invoke (R2 security) -- anything else (notably `evaluate`)
#: is rejected, never executed.
_ALLOWED_ACTION_TYPES = frozenset(
    {"click", "select_option", "fill", "wait_for_selector", "wait_for_timeout", "wait_for_load_state"}
)
#: Actions that address a page element and therefore require `selector`.
_ELEMENT_ACTION_TYPES = frozenset({"click", "select_option", "fill", "wait_for_selector"})
#: Actions that need a value -- literal `value` or a resolved `value_from`.
_VALUE_ACTION_TYPES = frozenset({"select_option", "fill"})

_OPTIONS_PREFIX = "options."


class VariantConfigError(ValueError):
    """Malformed/unsupported ``variant_selector_config`` -> ``SELECTOR_BROKEN``.

    Carries ``error_code`` (always ``ScrapeErrorCode.SELECTOR_BROKEN``) so
    a caller can read it straight off the exception instance -- mirrors
    the ``SsrfRejectedError``/``RobotsBlockedError`` convention
    ``scrape_core.errors.classify_exception`` already chain-walks for
    (`contracts/variant-selection.md` "Failure -> error code").
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code: ScrapeErrorCode = ScrapeErrorCode.SELECTOR_BROKEN


# --- resolve_variant_values (off-reactor, load_targets) ---------------------


def resolve_variant_values(config: dict[str, Any] | None, match: Any) -> dict[str, str]:
    """Resolve every action's ``value_from`` in ``config`` against ``match``.

    **Off-reactor** -- called inside :func:`scrape_core.targets.load_targets`
    while the DB session is still open, so `parse_variant_config` (which
    may run later, closer to/at request-build time) never needs match
    access itself.

    Returns ``{str(action_index): resolved_value}`` for every action that
    carries a ``value_from`` -- actions with a literal ``value`` (or no
    value at all, e.g. ``click``/``wait_for_selector``) contribute
    nothing here; `parse_variant_config` reads the literal straight off
    the action itself.

    ``value_from`` allowlist (`data-model.md` §2): ``options.<key>`` ->
    ``match.competitor_variant_options[<key>]``; ``identifier`` ->
    ``match.competitor_variant_identifier``; ``sku`` ->
    ``match.competitor_variant_sku``. Any other ``value_from``, or one
    that resolves to ``None``/missing, raises :class:`VariantConfigError`
    (never silently substitutes an empty value).

    ``config is None`` (or carries no ``actions``) -> ``{}`` (FR-004, US3
    AS2 -- no interaction to resolve values for).
    """
    if not config:
        return {}
    actions = config.get("actions")
    if not isinstance(actions, list):
        # Malformed structure -- `parse_variant_config` raises the
        # authoritative `VariantConfigError` for this; resolving values
        # against a non-list is meaningless, so there is nothing to do.
        return {}

    resolved: dict[str, str] = {}
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        value_from = action.get("value_from")
        if value_from is None:
            continue
        resolved[str(index)] = _resolve_value_from(value_from, match, index)
    return resolved


def _resolve_value_from(value_from: str, match: Any, index: int) -> str:
    if value_from == "identifier":
        value = getattr(match, "competitor_variant_identifier", None)
    elif value_from == "sku":
        value = getattr(match, "competitor_variant_sku", None)
    elif isinstance(value_from, str) and value_from.startswith(_OPTIONS_PREFIX):
        key = value_from[len(_OPTIONS_PREFIX) :]
        options = getattr(match, "competitor_variant_options", None)
        value = options.get(key) if isinstance(options, dict) else None
    else:
        raise VariantConfigError(
            f"variant_selector_config: action {index} has an unrecognized value_from {value_from!r}"
        )

    if value is None or value == "":
        raise VariantConfigError(
            f"variant_selector_config: action {index}'s value_from {value_from!r} "
            "resolved to nothing for this match"
        )
    return str(value)


# --- parse_variant_config (pure translation) --------------------------------


def parse_variant_config(
    config: dict[str, Any] | None,
    resolved_values: dict[str, str],
    *,
    timeout_ms: int,
) -> list[PageMethod]:
    """Translate ``config`` + its already-resolved ``value_from`` values
    into the ordered ``PageMethod`` list the spider stamps onto
    ``playwright_page_methods`` -- **pure**, no DB/match access (all
    ``value_from`` resolution already happened in
    :func:`resolve_variant_values`).

    ``config is None`` -> ``[]`` (FR-004, US3 AS2 -- no interaction).
    ``version`` must be ``1``; any other value (including missing) ->
    :class:`VariantConfigError`. ``actions`` execute in order; only the
    allowlisted types below are ever translated -- anything else
    (notably ``evaluate``) raises immediately, never executed (R2).
    Every ``wait_*`` method (including the optional trailing ``settle``)
    carries ``timeout_ms`` (the caller's already-computed effective
    timeout, R10).
    """
    if config is None:
        return []
    # Deferred import (module docstring "Import policy note") -- only
    # this browser-only translation path needs `scrapy-playwright`
    # installed, never `resolve_variant_values`'s HTTP-shared caller.
    from scrapy_playwright.page import PageMethod  # noqa: PLC0415

    if config.get("version") != _SUPPORTED_VERSION:
        raise VariantConfigError(
            f"variant_selector_config: unsupported version {config.get('version')!r} "
            f"(only {_SUPPORTED_VERSION} is supported)"
        )

    actions = config.get("actions")
    if not isinstance(actions, list):
        raise VariantConfigError("variant_selector_config: 'actions' must be a list")

    methods: list[PageMethod] = [
        _build_action_method(action, index, resolved_values, timeout_ms)
        for index, action in enumerate(actions)
    ]

    settle = config.get("settle")
    if settle is not None:
        methods.extend(_build_settle_methods(settle, timeout_ms))

    return methods


def _build_action_method(
    action: Any, index: int, resolved_values: dict[str, str], timeout_ms: int
) -> PageMethod:
    from scrapy_playwright.page import PageMethod  # noqa: PLC0415 - see module docstring

    if not isinstance(action, dict):
        raise VariantConfigError(f"variant_selector_config: action {index} must be an object")

    action_type = action.get("type")
    if action_type not in _ALLOWED_ACTION_TYPES:
        raise VariantConfigError(
            f"variant_selector_config: action {index} has a forbidden/unknown type {action_type!r}"
        )

    selector = action.get("selector")
    if action_type in _ELEMENT_ACTION_TYPES and not selector:
        raise VariantConfigError(
            f"variant_selector_config: action {index} ({action_type}) requires 'selector'"
        )

    if action_type in _VALUE_ACTION_TYPES:
        value = _action_value(action, index, resolved_values)
        return PageMethod(action_type, selector, value)

    if action_type == "click":
        return PageMethod("click", selector)

    if action_type == "wait_for_selector":
        kwargs: dict[str, Any] = {"timeout": timeout_ms}
        state = action.get("state")
        if state is not None:
            kwargs["state"] = state
        return PageMethod("wait_for_selector", selector, **kwargs)

    if action_type == "wait_for_timeout":
        return PageMethod("wait_for_timeout", timeout_ms)

    # action_type == "wait_for_load_state" (the only allowlisted type left)
    state = action.get("state", "load")
    return PageMethod("wait_for_load_state", state, timeout=timeout_ms)


def _action_value(action: dict[str, Any], index: int, resolved_values: dict[str, str]) -> str:
    if action.get("value") is not None:
        return action["value"]
    if action.get("value_from") is not None:
        key = str(index)
        if key not in resolved_values:
            # Should already have been raised by `resolve_variant_values`
            # -- defensive: never silently fall back to a blank value.
            raise VariantConfigError(
                f"variant_selector_config: action {index}'s value_from was never resolved"
            )
        return resolved_values[key]
    raise VariantConfigError(
        f"variant_selector_config: action {index} requires 'value' or 'value_from'"
    )


def _build_settle_methods(settle: Any, timeout_ms: int) -> list[PageMethod]:
    from scrapy_playwright.page import PageMethod  # noqa: PLC0415 - see module docstring

    if not isinstance(settle, dict):
        raise VariantConfigError("variant_selector_config: 'settle' must be an object")

    methods: list[PageMethod] = []
    settle_selector = settle.get("wait_for_selector")
    if settle_selector:
        methods.append(PageMethod("wait_for_selector", settle_selector, timeout=timeout_ms))

    load_state = settle.get("load_state")
    if load_state:
        methods.append(PageMethod("wait_for_load_state", load_state, timeout=timeout_ms))

    return methods
