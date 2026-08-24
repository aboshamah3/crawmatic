"""Central final-URL and immutable product-identity validation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from scrape_core.adapters.result import IdentityEvidence, IdentityStatus

__all__ = ["canonicalize_url", "extract_url_identifier", "validate_final_url"]


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "/"))
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), f"{host}{port}", path, parsed.query, ""))


def _patterns(rules: Mapping[str, Any]) -> Sequence[str]:
    value = rules.get("url_id_patterns", rules.get("url_id_pattern", ()))
    if isinstance(value, str):
        return (value,)
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, Sequence) else ()


def extract_url_identifier(url: str, rules: Mapping[str, Any]) -> str | None:
    target = unquote(url)
    for pattern in _patterns(rules):
        try:
            match = re.search(pattern, target, flags=re.IGNORECASE)
        except re.error:
            continue
        if match:
            if "id" in match.groupdict():
                return match.group("id")
            if match.groups():
                return match.group(1)
            return match.group(0)
    return None


def _normalize_identifier(value: str, rules: Mapping[str, Any]) -> str:
    normalized = unquote(value).strip()
    if rules.get("case_sensitive") is not True:
        normalized = normalized.casefold()
    return normalized


def _is_storefront_root(path: str, rules: Mapping[str, Any]) -> bool:
    normalized = re.sub(r"/{2,}", "/", path or "/")
    roots = rules.get("root_paths", ("/",))
    if isinstance(roots, str):
        roots = (roots,)
    if normalized.rstrip("/") in {str(root).rstrip("/") for root in roots}:
        return True
    locale_patterns = rules.get("locale_root_patterns", ())
    if isinstance(locale_patterns, str):
        locale_patterns = (locale_patterns,)
    return any(re.fullmatch(pattern, normalized, flags=re.IGNORECASE) for pattern in locale_patterns)


def validate_final_url(
    requested_url: str,
    final_url: str,
    *,
    expected_identifier: str | None = None,
    rules: Mapping[str, Any] | None = None,
) -> IdentityEvidence:
    """Reject root redirects and cross-product redirects before extraction.

    Locale and slug changes are accepted whenever the configured immutable-ID
    pattern still extracts the same ID.  With no identity rules, an unchanged
    URL is valid and a redirect is left unverified for backward compatibility.
    """
    rules = rules or {}
    requested = urlsplit(canonicalize_url(requested_url))
    final = urlsplit(canonicalize_url(urljoin(requested_url, final_url)))

    allowed_hosts = rules.get("allowed_hosts")
    if isinstance(allowed_hosts, str):
        allowed_hosts = (allowed_hosts,)
    hosts = {str(host).lower() for host in allowed_hosts or (requested.hostname,)}
    if final.hostname not in hosts:
        return IdentityEvidence(
            IdentityStatus.MISMATCH,
            expected_identifier,
            source="final_url",
            reason=f"final host {final.hostname!r} is outside the allowed product hosts",
        )

    requested_is_product = not _is_storefront_root(requested.path, rules)
    if requested_is_product and _is_storefront_root(final.path, rules):
        return IdentityEvidence(
            IdentityStatus.NOT_LISTED,
            expected_identifier,
            source="root_redirect",
            reason="product URL redirected to the storefront root",
        )

    observed = extract_url_identifier(final.geturl(), rules)
    expected = expected_identifier or extract_url_identifier(requested.geturl(), rules)
    if expected is not None and observed is not None:
        if _normalize_identifier(expected, rules) != _normalize_identifier(observed, rules):
            return IdentityEvidence(
                IdentityStatus.MISMATCH,
                expected,
                observed,
                "final_url",
                "final URL identifies a different immutable product",
            )
        return IdentityEvidence(IdentityStatus.VALID, expected, observed, "final_url")
    if expected is not None and rules.get("require_url_identifier", bool(_patterns(rules))):
        return IdentityEvidence(
            IdentityStatus.MISMATCH,
            expected,
            source="final_url",
            reason="final URL contains no verifiable immutable product identifier",
        )
    if requested.geturl() == final.geturl():
        return IdentityEvidence(IdentityStatus.VALID, expected, observed, "unchanged_url")
    return IdentityEvidence(
        IdentityStatus.UNVERIFIED,
        expected,
        observed,
        "final_url",
        "redirect allowed but no immutable identity rule was configured",
    )
