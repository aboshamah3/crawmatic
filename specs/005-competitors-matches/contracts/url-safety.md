# Contract: Save-time URL Safety Validator (`app_shared/url_safety.py`)

Pure, framework-agnostic (stdlib `urllib.parse` + `ipaddress` only). The mandatory §11 **save-time** SSRF control (FR-007/008/009). No DNS resolution — fetch-time authoritative re-resolution + per-redirect re-validation are the SPEC-07 spider's job (research D2).

## API
- `URL_PATTERN` is NOT here (that's `url_pattern.py`). This module exposes:
- `class UnsafeUrlReason(StrEnum)` — `INVALID_URL`, `BAD_SCHEME`, `USERINFO_PRESENT`, `PRIVATE_OR_INTERNAL_IP`, `INTERNAL_HOSTNAME`.
- `class UnsafeUrlError(ValueError)` — carries `reason: UnsafeUrlReason` and a human message; routers map it to `422 {"error":{"code":"UNSAFE_URL","message":...}}`.
- `validate_competitor_url(url: str) -> None` — raises `UnsafeUrlError` on any unsafe URL; returns `None` when safe.
- Deny-list constants: `INTERNAL_HOSTNAMES: frozenset[str]`, `INTERNAL_HOST_SUFFIXES: tuple[str, ...]`.

## Accept rule (all must hold)
1. `urlsplit(url)` parses and yields a non-empty host.
2. `scheme.lower() in {"http","https"}`.
3. No userinfo — `parsed.username is None and parsed.password is None` (no `user:pass@host`).
4. Host is safe:
   - **IP literal** (parses via `ipaddress.ip_address`, IPv6 unbracketed): safe iff `ip.is_global` and not any of `is_loopback`/`is_private`/`is_link_local`/`is_reserved`/`is_multicast`/`is_unspecified`. (This rejects `10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16`, `fe80::/10`, `fc00::/7`, `::1`, `0.0.0.0`, and the metadata literal `169.254.169.254`.)
   - **DNS name** (lowercased): safe iff it is not in `INTERNAL_HOSTNAMES` and does not end with any suffix in `INTERNAL_HOST_SUFFIXES`.

## Deny-list (plan-level, §11 + §4 internal networking)
- `INTERNAL_HOSTNAMES = {"localhost", "postgres", "redis", "pgbouncer", "api", "scheduler", "worker", "scrapyd-http", "scrapyd-browser", "metadata.google.internal"}`.
- `INTERNAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".railway.internal")`.

## Reject → reason mapping
| Case | `UnsafeUrlReason` |
|------|------------------|
| unparseable / no host / relative | `INVALID_URL` |
| scheme not http/https (e.g. `ftp:`, `file:`, `javascript:`, `data:`) | `BAD_SCHEME` |
| `user:pass@host` present | `USERINFO_PRESENT` |
| IP literal in a denied range (private/loopback/link-local/unique-local/reserved/multicast/unspecified/metadata) | `PRIVATE_OR_INTERNAL_IP` |
| internal hostname or internal suffix | `INTERNAL_HOSTNAME` |

## Applied on every write path (FR-009)
Single-match create, match update, and bulk-upsert all validate before storing. On single create/update a rejection is a `422`; in bulk, the row is dropped into `rejected[]` and the safe rows still upsert (`contracts/matches-bulk-upsert.md`).

## Unit tests (no DB, exhaustive corpus)
- **Accept**: `https://competitor.com/p/123`, `http://shop.example.co.uk/ar/products/x`, a public IP literal `https://93.184.216.34/x`, IPv6 global literal `https://[2606:2800:220:1:248:1893:25c8:1946]/x`.
- **Reject** with the mapped reason: `http://localhost/`, `http://127.0.0.1/`, `http://10.0.0.5/`, `http://172.16.0.1/`, `http://192.168.1.1/`, `http://169.254.169.254/latest/meta-data`, `http://[::1]/`, `http://[fe80::1]/`, `http://[fc00::1]/`, `http://postgres:5432/`, `http://api.internal/`, `http://foo.local/`, `http://x.railway.internal/`, `http://user:pass@competitor.com/`, `ftp://competitor.com/`, `file:///etc/passwd`, `javascript:alert(1)`, `not-a-url`, `//competitor.com/x` (no scheme).
- The reason enum on each rejection matches the table above.
