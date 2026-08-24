"""Exact immutable-ID URL repair over JSON or HTML search responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import urljoin

from parsel import Selector

from scrape_core.adapters.catalog import find_exact_item
from scrape_core.adapters.config import (
    AdapterConfigurationError,
    first_value,
    format_endpoint,
    resolve_adapter_config,
    resolve_identifier,
)
from scrape_core.adapters.identity import extract_url_identifier, validate_final_url
from scrape_core.adapters.result import (
    AdapterContext,
    AdapterOutcome,
    AdapterRequest,
    AdapterResponse,
    AdapterResult,
    IdentityEvidence,
    IdentityStatus,
)

__all__ = ["ExactIdUrlRepairAdapter"]


class ExactIdUrlRepairAdapter:
    """Return a new canonical URL only when the immutable ID is exact."""

    def build_request(self, context: AdapterContext) -> AdapterRequest:
        config = resolve_adapter_config(context)
        identifier = resolve_identifier(config, context)
        if not identifier:
            raise AdapterConfigurationError("repair adapter could not resolve an exact identifier")
        template = config.get("repair_endpoint_template", config.get("endpoint_template"))
        return AdapterRequest(
            format_endpoint(template, context, identifier),
            headers=config.get("headers", {}),
            follow_redirects=config.get("follow_redirects", True) is not False,
        )

    def adapt(self, response: AdapterResponse, context: AdapterContext, **_: object) -> AdapterResult:
        config = resolve_adapter_config(context)
        identifier = resolve_identifier(config, context)
        if not identifier:
            raise AdapterConfigurationError("repair adapter could not resolve an exact identifier")
        response_format = str(config.get("response_format", "json")).casefold()
        repaired_url: str | None
        if response_format == "json":
            repaired_url = self._from_json(response, config, identifier)
        elif response_format == "html":
            repaired_url = self._from_html(response, config, identifier)
        else:
            raise AdapterConfigurationError("repair response_format must be 'json' or 'html'")

        if not repaired_url:
            evidence = IdentityEvidence(
                IdentityStatus.NOT_LISTED,
                identifier,
                source="repair_exact_match",
                reason="repair search returned no exact immutable-ID URL",
            )
            return AdapterResult(AdapterOutcome.NOT_LISTED, response.final_url, evidence, message=evidence.reason)

        rules = config.get("identity_rules", {})
        rules = rules if isinstance(rules, Mapping) else {}
        evidence = validate_final_url(
            context.target_url,
            repaired_url,
            expected_identifier=identifier,
            rules={**rules, "require_url_identifier": True},
        )
        if evidence.status != IdentityStatus.VALID:
            return AdapterResult(
                AdapterOutcome.IDENTITY_MISMATCH,
                response.final_url,
                evidence,
                message=evidence.reason,
            )
        return AdapterResult(
            AdapterOutcome.REPAIRED,
            response.final_url,
            evidence,
            canonical_url=repaired_url,
        )

    @staticmethod
    def _from_json(response: AdapterResponse, config: Mapping[str, object], identifier: str) -> str | None:
        try:
            body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None
        found = find_exact_item(
            document,
            identifier,
            item_paths=config.get("items_paths"),
            identity_paths=config.get("exact_id_paths", ("uniqueId", "sku", "id")),
            case_sensitive=config.get("case_sensitive") is True,
        )
        if found is None:
            return None
        return_value = first_value(found[0], config.get("url_paths", ("url", "productUrl")))
        return urljoin(response.final_url, str(return_value)) if return_value else None

    @staticmethod
    def _from_html(response: AdapterResponse, config: Mapping[str, object], identifier: str) -> str | None:
        body = response.body.decode("utf-8", errors="replace") if isinstance(response.body, bytes) else response.body
        selector = Selector(text=body, type="html")
        link_selector = config.get("link_selector", "a[href]::attr(href)")
        if not isinstance(link_selector, str):
            raise AdapterConfigurationError("repair link_selector must be a CSS selector")
        rules = config.get("identity_rules", {})
        rules = rules if isinstance(rules, Mapping) else {}
        for href in selector.css(link_selector).getall():
            absolute = urljoin(response.final_url, href)
            observed = extract_url_identifier(absolute, rules)
            if observed and observed.casefold() == identifier.casefold():
                return absolute
        return None
