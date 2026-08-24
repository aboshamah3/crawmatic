"""Small, strict helpers for tenant-owned adapter configuration."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from scrape_core.adapters.result import AdapterContext

__all__ = [
    "AdapterConfigurationError",
    "apply_decimal_transform",
    "first_value",
    "format_endpoint",
    "get_path",
    "iter_dicts",
    "iter_items",
    "resolve_adapter_config",
    "resolve_identifier",
]


class AdapterConfigurationError(ValueError):
    """Raised before fetching when an adapter configuration is unusable."""


def resolve_adapter_config(context: AdapterContext) -> Mapping[str, Any]:
    config = getattr(context.profile, "adapter_config", None) if context.profile else None
    return config if isinstance(config, Mapping) else {}


def resolve_identifier(config: Mapping[str, Any], context: AdapterContext) -> str | None:
    sources = config.get(
        "identifier_sources",
        ("competitor_variant_identifier", "competitor_variant_sku", "identifier"),
    )
    if isinstance(sources, str):
        sources = (sources,)
    if not isinstance(sources, Iterable):
        raise AdapterConfigurationError("identifier_sources must be a string or list")
    for source in sources:
        if not isinstance(source, str):
            continue
        value = context.value(source)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def format_endpoint(template: Any, context: AdapterContext, identifier: str | None) -> str:
    if not isinstance(template, str) or not template.strip():
        raise AdapterConfigurationError("adapter endpoint_template must be a non-empty string")
    values = dict(context.values)
    values.update(
        identifier=identifier or "",
        identifier_urlencoded=quote(identifier or "", safe=""),
        target_url=context.target_url,
        product_url=context.target_url,
    )
    try:
        return template.format_map(_StrictFormatValues(values))
    except KeyError as exc:
        raise AdapterConfigurationError(
            f"endpoint_template references unknown placeholder {exc.args[0]!r}"
        ) from exc


class _StrictFormatValues(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise KeyError(key)


def get_path(document: Any, path: Any) -> Any:
    """Resolve an RFC-6901 pointer or a dotted field path."""
    if path in (None, "", "/"):
        return document
    if not isinstance(path, str):
        return None
    if path.startswith("/"):
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
    else:
        parts = path.split(".")
    current = document
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def first_value(document: Any, paths: Any) -> Any:
    paths = (paths,) if isinstance(paths, str) else paths
    if not isinstance(paths, Iterable):
        return None
    for path in paths:
        value = get_path(document, path)
        if value is not None and value != "":
            return value
    return None


def iter_dicts(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def iter_items(document: Any, configured_paths: Any) -> Iterator[Mapping[str, Any]]:
    """Yield catalog records from configured containers or recursively."""
    if configured_paths:
        paths = (configured_paths,) if isinstance(configured_paths, str) else configured_paths
        for path in paths:
            value = get_path(document, path)
            if isinstance(value, list):
                yield from (item for item in value if isinstance(item, Mapping))
            elif isinstance(value, Mapping):
                yield value
        return
    yield from iter_dicts(document)


def apply_decimal_transform(value: Any, transform: Any) -> str | None:
    """Apply only declarative decimal transforms; never parse via float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        if isinstance(transform, Mapping):
            if transform.get("divide_by") is not None:
                divisor = Decimal(str(transform["divide_by"]))
                if divisor == 0:
                    raise AdapterConfigurationError("price transform divide_by cannot be zero")
                number /= divisor
            if transform.get("multiply_by") is not None:
                number *= Decimal(str(transform["multiply_by"]))
    except (InvalidOperation, ValueError, TypeError) as exc:
        return None
    return format(number, "f")
