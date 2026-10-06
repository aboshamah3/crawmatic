"""Canonical competitor/host domain form (security E3/E4).

One spelling per host: IDNA (ASCII/punycode) encoded, lowercase, no trailing
dot, a single leading ``www.`` removed. Anything that is not a bare host name
(ports, paths, userinfo, query/fragment, whitespace, IP literals) is rejected
with ``ValueError`` so a competitor row can never name something other than a
registrable-style host.
"""

from __future__ import annotations

import ipaddress

_FORBIDDEN_CHARS = frozenset("/\\@:?#[]%")
_MAX_HOST_LEN = 253
_MAX_LABEL_LEN = 63


def canonical_domain(raw: str) -> str:
    """Return the canonical form of ``raw`` or raise ``ValueError``."""
    if not isinstance(raw, str):
        raise ValueError("domain must be a string")
    value = raw.strip()
    if not value:
        raise ValueError("domain must not be empty")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("domain must not contain whitespace or control characters")
    if any(ch in _FORBIDDEN_CHARS for ch in value):
        raise ValueError("domain must be a bare host (no port, path, userinfo or query)")
    value = value.lower().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    if not value or value.startswith(".") or ".." in value:
        raise ValueError("domain is not a valid host name")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are not allowed as a domain")
    try:
        ascii_host = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"domain is not valid IDNA: {exc}") from exc
    ascii_host = ascii_host.lower()
    labels = ascii_host.split(".")
    if len(ascii_host) > _MAX_HOST_LEN or any(not lab or len(lab) > _MAX_LABEL_LEN for lab in labels):
        raise ValueError("domain label or length out of range")
    if labels[-1].isdigit():
        # 127.1, 2130706433 and friends: numeric TLDs are never real hosts.
        raise ValueError("numeric host names are not allowed as a domain")
    if any(lab.startswith("-") or lab.endswith("-") for lab in labels):
        raise ValueError("domain label must not start or end with a hyphen")
    return ascii_host


def url_host_belongs_to_domain(url: str | None, competitor_domain: str | None) -> bool:
    """True when ``url``'s host is ``competitor_domain`` or one of its subdomains.

    Fails closed: an unparsable URL/domain is a mismatch.
    """
    from urllib.parse import urlsplit

    if not url or not competitor_domain:
        return False
    try:
        host = canonical_domain(urlsplit(url).hostname or "")
        domain = canonical_domain(competitor_domain)
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


def plan_competitor_domain_rewrite(
    rows: "list[tuple[object, object, str]]",
) -> "tuple[list[tuple[object, str]], list[tuple[object, str, list[object]]], list[tuple[object, str]]]":
    """Plan a ``competitors.domain`` rewrite from ``(id, workspace_id, domain)`` rows.

    Returns ``(updates, collisions, invalid)``:

    * ``updates``: ``(id, canonical)`` for rows whose stored spelling differs;
    * ``collisions``: ``(workspace_id, canonical, [ids...])`` where two or more
      rows of one workspace canonicalise to the same domain (never merged);
    * ``invalid``: ``(id, domain)`` that ``canonical_domain`` rejects.
    """
    groups: dict[tuple[object, str], list[object]] = {}
    updates: list[tuple[object, str]] = []
    invalid: list[tuple[object, str]] = []
    for row_id, workspace_id, domain in rows:
        try:
            canon = canonical_domain(domain)
        except ValueError:
            invalid.append((row_id, domain))
            continue
        groups.setdefault((workspace_id, canon), []).append(row_id)
        if canon != domain:
            updates.append((row_id, canon))
    collisions = [(ws, canon, ids) for (ws, canon), ids in groups.items() if len(ids) > 1]
    return updates, collisions, invalid
