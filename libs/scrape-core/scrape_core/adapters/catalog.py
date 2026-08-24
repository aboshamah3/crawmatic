"""Configuration-driven exact-ID public catalog JSON adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

from app_shared.enums import ExtractionMethod, StockStatus

from scrape_core.adapters.config import (
    AdapterConfigurationError,
    apply_decimal_transform,
    first_value,
    format_endpoint,
    iter_items,
    resolve_adapter_config,
    resolve_identifier,
)
from scrape_core.adapters.result import (
    AdapterContext,
    AdapterOutcome,
    AdapterRequest,
    AdapterResponse,
    AdapterResult,
    IdentityEvidence,
    IdentityStatus,
)
from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["PublicCatalogJsonAdapter", "find_exact_item"]


def _normalize(value: Any, *, case_sensitive: bool) -> str:
    text = str(value).strip()
    return text if case_sensitive else text.casefold()


def find_exact_item(
    document: Any,
    identifier: str,
    *,
    item_paths: Any,
    identity_paths: Any,
    case_sensitive: bool = False,
) -> tuple[Mapping[str, Any], str] | None:
    expected = _normalize(identifier, case_sensitive=case_sensitive)
    paths = (identity_paths,) if isinstance(identity_paths, str) else identity_paths
    for item in iter_items(document, item_paths):
        for path in paths or ():
            observed = first_value(item, path)
            if observed is not None and _normalize(observed, case_sensitive=case_sensitive) == expected:
                return item, str(observed)
    return None


def _positive(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return Decimal(value) > 0
    except (InvalidOperation, ValueError):
        return False


def _same_money(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return Decimal(left) == Decimal(right)
    except (InvalidOperation, ValueError):
        return left == right


def _stock(value: Any, config: Mapping[str, Any]) -> StockStatus:
    if isinstance(value, bool):
        return StockStatus.IN_STOCK if value else StockStatus.OUT_OF_STOCK
    mapping = config.get("stock_mapping", {})
    if isinstance(mapping, Mapping):
        normalized_mapping = {str(k).casefold(): str(v) for k, v in mapping.items()}
        mapped = normalized_mapping.get(str(value).casefold())
        if mapped:
            try:
                return StockStatus(mapped)
            except ValueError:
                pass
    return StockStatus.UNKNOWN


class PublicCatalogJsonAdapter:
    """Search any catalog payload, accepting only the exact configured ID."""

    def build_request(self, context: AdapterContext) -> AdapterRequest:
        config = resolve_adapter_config(context)
        identifier = resolve_identifier(config, context)
        if not identifier:
            raise AdapterConfigurationError("catalog adapter could not resolve an exact identifier")
        return AdapterRequest(
            format_endpoint(config.get("endpoint_template"), context, identifier),
            headers=config.get("headers", {}),
            follow_redirects=config.get("follow_redirects", True) is not False,
        )

    def adapt(self, response: AdapterResponse, context: AdapterContext, **_: Any) -> AdapterResult:
        config = resolve_adapter_config(context)
        identifier = resolve_identifier(config, context)
        if not identifier:
            raise AdapterConfigurationError("catalog adapter could not resolve an exact identifier")
        try:
            body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return AdapterResult(
                AdapterOutcome.INVALID_RESPONSE,
                response.final_url,
                IdentityEvidence(IdentityStatus.UNVERIFIED, identifier, source="catalog_json"),
                message=f"catalog endpoint returned malformed JSON: {exc}",
            )

        identity_paths = config.get("exact_id_paths", ("uniqueId", "sku", "id"))
        found = find_exact_item(
            document,
            identifier,
            item_paths=config.get("items_paths"),
            identity_paths=identity_paths,
            case_sensitive=config.get("case_sensitive") is True,
        )
        if found is None:
            evidence = IdentityEvidence(
                IdentityStatus.NOT_LISTED,
                identifier,
                source="catalog_exact_match",
                reason="catalog returned no exact identifier match",
            )
            return AdapterResult(AdapterOutcome.NOT_LISTED, response.final_url, evidence, message=evidence.reason)

        item, observed = found
        evidence = IdentityEvidence(
            IdentityStatus.VALID, identifier, observed, "catalog_exact_match"
        )
        fields = config.get("fields", {})
        fields = fields if isinstance(fields, Mapping) else {}
        profile_transforms = (
            getattr(context.profile, "price_transform_rules", None) if context.profile else None
        )
        transforms = config.get("transforms", profile_transforms or {})
        transforms = transforms if isinstance(transforms, Mapping) else {}

        sale_raw = first_value(item, fields.get("sale_price", ("sale_price", "salePrice")))
        regular_raw = first_value(item, fields.get("price", ("price", "sellingPrice")))
        sale = apply_decimal_transform(sale_raw, transforms.get("sale_price", transforms.get("price")))
        regular = apply_decimal_transform(regular_raw, transforms.get("price"))
        selected = sale if _positive(sale) else regular
        if selected is None:
            return AdapterResult(
                AdapterOutcome.NOT_FOUND,
                response.final_url,
                evidence,
                message="exact catalog item contained no usable price field",
            )

        old_price = regular if _positive(regular) and not _same_money(regular, selected) else None
        currency_value = first_value(item, fields.get("currency", ("currency", "currencyCode")))
        currency = str(currency_value) if currency_value else config.get("currency") or context.expected_currency
        stock_value = first_value(item, fields.get("stock", ("is_buyable", "isBuyable", "available")))
        title_value = first_value(item, fields.get("title", ("name", "title")))
        url_value = first_value(item, fields.get("url", ("url", "productUrl")))
        candidate = ExtractionCandidate(
            raw_price_text=selected,
            currency=str(currency) if currency else None,
            method=ExtractionMethod.PLATFORM_JSON,
            confidence=float(config.get("confidence", 1.0)),
            selector_used="catalog exact identifier",
            raw_title=str(title_value) if title_value else None,
            stock=_stock(stock_value, config),
            matched_text=json.dumps(item, default=str),
        )
        canonical_url = None
        if url_value:
            canonical_base = str(config.get("canonical_url_base") or context.target_url)
            canonical_url = urljoin(canonical_base, str(url_value))
        return AdapterResult(
            AdapterOutcome.FOUND,
            response.final_url,
            evidence,
            candidate=candidate,
            old_price_text=old_price,
            canonical_url=canonical_url,
            metadata={"identifier": observed},
        )
