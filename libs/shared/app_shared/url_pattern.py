"""URL normalization + versioned pattern derivation (`contracts/url-pattern.md`, FR-010/011).

Pure, framework-agnostic — stdlib `urllib.parse` + `re` only. Implements
the §15 algorithm behind a single :data:`URL_PATTERN_ALGORITHM_VERSION`
constant (research D3). Callers pass a URL that has already been
:func:`app_shared.url_safety.validate_competitor_url`'d — these
functions assume a parseable http(s) URL and do not re-validate safety.

Two distinct, non-conflated derivations:

* :func:`normalize_url` — the canonical **identity** URL
  (`normalized_competitor_url`, part of the match unique key): lowercase
  scheme+host, strip `www.`/default-port/fragment/trailing-slash, but
  **keep** the query string (it can distinguish the target product, e.g.
  `?variant=123`).
* :func:`derive_url_pattern` — the versioned **grouping** pattern
  (`url_pattern`): drop scheme+query, generalize id-like segments and
  product slugs, preserve a leading locale prefix.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

URL_PATTERN_ALGORITHM_VERSION: int = 1

_DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}

_LOCALE_PREFIX_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")
_UUID_LIKE_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_PRODUCT_PATH_KEYS = frozenset({"products", "product", "p", "item"})


def normalize_url(url: str) -> str:
    """Canonical **identity** URL: lowercase scheme+host, strip `www.`/default
    port/fragment/trailing-slash, keep the query string."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[len("www.") :]

    port = parsed.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path[:-1]
    if path == "/":
        path = ""

    result = f"{scheme}://{netloc}{path}"
    if parsed.query:
        result = f"{result}?{parsed.query}"
    return result


def _is_id_like(segment: str) -> bool:
    """Version-1 id-like thresholds (`contracts/url-pattern.md`).

    The "long mixed alphanumeric" rule requires the segment to be a
    *contiguous* alphanumeric run (``str.isalnum()``) — a hyphenated
    ordinary slug like ``iphone-15`` is 9 characters and contains both a
    letter and a digit, so without this gate it would be misclassified
    as id-like; real long mixed-alnum ids (opaque hashes like
    ``9f8a7b6c``) never contain separators.
    """
    if segment.isdigit():
        return True
    if _UUID_LIKE_RE.match(segment):
        return True
    if segment.isalnum() and len(segment) >= 8:
        has_letter = any(c.isalpha() for c in segment)
        has_digit = any(c.isdigit() for c in segment)
        if has_letter and has_digit:
            return True
    if len(segment) >= 4:
        digit_count = sum(1 for c in segment if c.isdigit())
        if digit_count / len(segment) >= 0.5:
            return True
    return False


def derive_url_pattern(url: str) -> str:
    """Versioned **grouping** pattern: drop scheme+query, generalize id-like
    segments to `:id` and product slugs after a known product key to `*`,
    preserving a leading locale prefix."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[len("www.") :]

    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path[:-1]

    raw_segments = [seg for seg in path.split("/") if seg]

    segments: list[str] = []
    idx = 0
    if raw_segments and _LOCALE_PREFIX_RE.match(raw_segments[0].lower()):
        segments.append(raw_segments[0].lower())
        idx = 1

    previous_kept_key: str | None = None
    for i in range(idx, len(raw_segments)):
        segment = raw_segments[i]
        if previous_kept_key is not None and previous_kept_key in _PRODUCT_PATH_KEYS:
            segments.append("*")
            previous_kept_key = None
            continue
        if _is_id_like(segment):
            segments.append(":id")
            previous_kept_key = None
        else:
            segments.append(segment)
            previous_kept_key = segment.lower()

    if segments:
        return host + "/" + "/".join(segments)
    return host


def derive_match_url_fields(url: str) -> tuple[str, str, int]:
    """`(normalized_competitor_url, url_pattern, URL_PATTERN_ALGORITHM_VERSION)`."""
    return normalize_url(url), derive_url_pattern(url), URL_PATTERN_ALGORITHM_VERSION
