#!/usr/bin/env python3
"""encrypt_proxy_password.py — Fernet-encrypt a proxy password for seeding.

Companion to ``scripts/seed_proxy.sh``. Reads from the environment:

* ``ENCRYPTION_KEYS`` / ``ENCRYPTION_PRIMARY_KEY_VERSION`` — the same
  keyring contract as ``app_shared.config`` (SPEC-10 FR-003, §33);
  typically injected via ``railway run --service api`` so the
  production key never lands on disk or in a transcript.
* ``PROXY_PASSWORD`` — the plaintext to encrypt.

Prints ``<key_version>|<ciphertext>`` on stdout and nothing else, so a
shell can capture it safely. Uses ``cryptography`` directly (no
``app_shared.Settings``) because the full settings model requires
DATABASE_URL etc., which are irrelevant here.
"""

from __future__ import annotations

import os
import sys

from cryptography.fernet import Fernet


def main() -> int:
    raw_keys = os.environ.get("ENCRYPTION_KEYS")
    password = os.environ.get("PROXY_PASSWORD")
    if not raw_keys or not password:
        print("ENCRYPTION_KEYS and PROXY_PASSWORD are required", file=sys.stderr)
        return 1
    keyring: dict[str, str] = {}
    for pair in raw_keys.split(","):
        version, _, key = pair.strip().partition(":")
        if not key:
            print(f"malformed ENCRYPTION_KEYS pair {pair!r}", file=sys.stderr)
            return 1
        keyring[version] = key
    primary = os.environ.get("ENCRYPTION_PRIMARY_KEY_VERSION", "1")
    if primary not in keyring:
        print(f"primary key version {primary} not in ENCRYPTION_KEYS", file=sys.stderr)
        return 1
    token = Fernet(keyring[primary].encode("ascii")).encrypt(password.encode("utf-8"))
    print(f"{primary}|{token.decode('ascii')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
