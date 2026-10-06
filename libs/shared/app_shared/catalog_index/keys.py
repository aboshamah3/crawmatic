"""Keys and text helpers for the catalog index. Pure: no DB, no I/O.

ONE DEFINITION, TWO CALLERS. `scripts/load_catalog_index.py` derives the
keys it stores for every index row with these functions, and
`app_shared.catalog_index.lookup` derives the keys it searches for with
the same functions. A change here changes both sides; the stored keys
only follow after the next index load.

Three kinds of evidence, strongest first:

* a BARCODE key: a check-digit-valid GTIN, padded to 14 digits. The
  `gtin`/`barcode` field may hold GTIN-8/12/13/14; `sku` and `mpn` count
  only at 12 to 14 digits, because Salla stores put the EAN in `sku`
  while other stores put short internal numbers there.
* a MODEL key: a manufacturer-style code (letters and at least two
  digits, five or more characters once separators are stripped) from
  `mpn`, `sku` or the title. Measurements, dimensions and spec ratings
  are refused.
* TITLE tokens, compared by Jaccard overlap, with a hard refusal when
  the two titles carry different numbers (473 ml is not 236 ml).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app_shared.domains import canonical_domain

__all__ = [
    "KIND_BARCODE",
    "KIND_MODEL",
    "barcode_keys",
    "gtin_key",
    "host_of",
    "jaccard",
    "model_keys",
    "normalise_domain",
    "numbers",
    "numbers_conflict",
    "store_domain_of_url",
    "title_query_text",
    "title_tokens",
]

KIND_BARCODE = "g"
KIND_MODEL = "m"
MAX_MODEL_KEYS = 8
#: Hosts whose first path segment names the store (`salla.sa/<store>`).
PATH_STORE_HOSTS = frozenset({"salla.sa"})

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_STRICT_LENGTHS = frozenset({8, 12, 13, 14})
_LOOSE_LENGTHS = frozenset({12, 13, 14})
_DIGITS_RE = re.compile(r"[0-9]+")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]*[A-Za-z0-9]")
_MEASURE_RE = re.compile(
    r"^[0-9]+(?:ml|l|g|gm|kg|mg|oz|lb|lbs|cm|mm|m|km|in|inch|ft|gb|tb|mb|kb|w|kw|v|a|mah|ah|"
    r"hz|khz|mhz|ghz|fps|mp|rpm|btu|pcs|pc|pk|x|k|cc|ton|kbps|mbps|gbps|lm|db|nm|bar|psi)$"
)
_DIMENSION_RE = re.compile(r"^[0-9]+(?:x[0-9]+)+[a-z]*$")
_SPEC_RE = re.compile(
    r"^(?:(?:spf|ip|usb|uv|no|vol|size|pack|type|gen|iso|din|ddr|wifi|bt|hd|uhd|fhd|air|pro|max|mini|plus)"
    r"[0-9]+[a-z]?|[0-9]+w[0-9]+)$"
)


def _clean(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).translate(_ARABIC_DIGITS).strip()


def gtin_check_ok(digits: str) -> bool:
    """GS1 mod-10 check over a digit string (last digit is the check)."""
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == int(digits[-1])


def gtin_key(value: object, *, strict: bool) -> str | None:
    """`value` as a 14-digit GTIN key, or None. `strict=True` is the
    barcode field (GTIN-8 allowed); `strict=False` is sku/mpn (12 to 14
    digits only). A trailing `.0` (a spreadsheet float) is dropped."""
    text = re.sub(r"\.0+$", "", _clean(value))
    text = re.sub(r"[\s\-]", "", text)
    if not _DIGITS_RE.fullmatch(text):
        return None
    if len(text) not in (_STRICT_LENGTHS if strict else _LOOSE_LENGTHS):
        return None
    if len(set(text)) == 1 or not gtin_check_ok(text):
        return None
    return text.zfill(14)


def barcode_keys(*, gtin: object = None, sku: object = None, mpn: object = None) -> list[str]:
    out: list[str] = []
    for value, strict in ((gtin, True), (sku, False), (mpn, False)):
        key = gtin_key(value, strict=strict)
        if key and key not in out:
            out.append(key)
    return out


def norm_code(token: object) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean(token).lower())


def _is_model_code(code: str) -> bool:
    if len(code) < 5:
        return False
    if not any(c.isalpha() for c in code) or sum(c.isdigit() for c in code) < 2:
        return False
    return not (_MEASURE_RE.match(code) or _DIMENSION_RE.match(code) or _SPEC_RE.match(code))


def model_keys(*, sku: object = None, mpn: object = None, title: object = None) -> list[str]:
    """Model-code keys, mpn first, then sku, then title tokens; unique,
    at most `MAX_MODEL_KEYS`."""
    out: list[str] = []

    def add(code: str) -> None:
        if _is_model_code(code) and code not in out and len(out) < MAX_MODEL_KEYS:
            out.append(code)

    add(norm_code(mpn))
    add(norm_code(sku))
    for token in _TOKEN_RE.findall(_clean(title)):
        add(norm_code(token))
    return out


def title_tokens(title: object) -> list[str]:
    return [t for t in _WORD_RE.findall(_clean(title).lower()) if len(t) >= 2]


def numbers(title: object) -> frozenset[str]:
    """Every number in a title, `1.50` and `1.5` reading the same."""
    out = set()
    for n in _NUMBER_RE.findall(_clean(title)):
        out.add(n.rstrip("0").rstrip(".") if "." in n else n.lstrip("0") or "0")
    return frozenset(out)


def numbers_conflict(a: object, b: object) -> bool:
    """Both titles carry numbers and the sets differ: a different size,
    count or model. One side having none is not a conflict."""
    na, nb = numbers(a), numbers(b)
    return bool(na) and bool(nb) and na != nb


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def title_query_text(title: object, brand: object = None, *, max_terms: int = 4) -> str | None:
    """Up to `max_terms` distinctive title words for `plainto_tsquery`
    (which ANDs them): the brand's first word, then Latin words, then the
    rest, in title order. None without a brand or with fewer than three
    words: "women's bag" and "New Balance" are categories, not products,
    and every store has one."""
    words = [t for t in dict.fromkeys(title_tokens(title)) if len(t) >= 3 and not t.isdigit()]
    chosen = [t for t in words if t.isascii()] + [t for t in words if not t.isascii()]
    brand_words = [t for t in title_tokens(brand) if len(t) >= 3][:1]
    for word in brand_words:
        if word in chosen:
            chosen.remove(word)
        chosen.insert(0, word)
    chosen = chosen[:max_terms]
    return " ".join(chosen) if brand_words and len(chosen) >= 3 else None


def normalise_domain(value: object) -> str:
    """`https://WWW.Shop.com/` -> `shop.com`; `salla.sa/Store/` -> `salla.sa/store`."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return text[4:] if text.startswith("www.") else text


def host_of(value: object) -> str:
    """The bare host of a domain or URL (`salla.sa/store` -> `salla.sa`)."""
    host = normalise_domain(value).split("/", 1)[0].split(":", 1)[0]
    try:
        return canonical_domain(host)
    except ValueError:
        return host


def store_domain_of_url(url: object) -> str:
    """The index `domain` a product URL belongs to: the host, or
    `host/<first segment>` on a path-store host."""
    text = normalise_domain(url)
    host = text.split("/", 1)[0].split(":", 1)[0]
    if host in PATH_STORE_HOSTS:
        segments = [s for s in text.split("/")[1:] if s]
        if segments:
            return f"{host}/{segments[0]}"
    return host
