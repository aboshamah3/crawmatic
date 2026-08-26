"""Credential-encryption key rotation, proved end to end without an outage.

EPA W5.5-L1 item 4, engine half. `tests/unit/test_encryption.py` already
covers the keyring primitives; these cover the OPERATION — the sweep that
moves live rows onto a new key, the concurrency guard that keeps it safe
against a live database, and revocation: what happens when an old key is
removed, both after a completed sweep (nothing) and before one (a loud,
attributable failure, never a silent blank).

No database. The store seam is a session object, so a fake exercises every
branch including the ones Postgres will not produce on demand.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app_shared import config as config_module
from app_shared.security import encryption as enc
from app_shared.security.key_rotation import (
    ENCRYPTED_COLUMNS,
    EncryptedColumn,
    RotationReport,
    rotate_all,
    rotate_column,
    rows_on_old_versions,
)

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
}

KEY_V1 = Fernet.generate_key().decode("ascii")
KEY_V2 = Fernet.generate_key().decode("ascii")

COLUMN = EncryptedColumn("proxy_providers", "password_encrypted", "password_key_version")


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both `get_settings` and `_keyring` are process-wide `lru_cache`
    singletons; a stale one would let one test's keyring leak into another."""
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()


def _env(monkeypatch: pytest.MonkeyPatch, *, keys: str, primary: str) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ENCRYPTION_KEYS", keys)
    monkeypatch.setenv("ENCRYPTION_PRIMARY_KEY_VERSION", primary)
    config_module.get_settings.cache_clear()
    enc._keyring.cache_clear()


def _both_keys_primary_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, keys=f"1:{KEY_V1},2:{KEY_V2}", primary="1")


def _both_keys_primary_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, keys=f"1:{KEY_V1},2:{KEY_V2}", primary="2")


def _only_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 REVOKED — removed from the ring entirely."""
    _env(monkeypatch, keys=f"2:{KEY_V2}", primary="2")


class FakeSession:
    """A tiny in-memory stand-in for the two SQL statements the sweep issues."""

    def __init__(self, rows: list[dict], *, steal_row_ids: set | None = None):
        # {row_id: {"ciphertext": str, "key_version": int}}
        self.rows = {r["row_id"]: dict(r) for r in rows}
        #: Rows another writer will have moved between our SELECT and UPDATE.
        self.steal_row_ids = steal_row_ids or set()
        self.commits = 0

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = params or {}
        if sql.startswith("SELECT count(*)"):
            primary = params["primary"]
            return _One(
                sum(1 for r in self.rows.values() if r["key_version"] != primary)
            )
        if sql.startswith("SELECT"):
            primary = params["primary"]
            pending = [
                {"row_id": rid, **r}
                for rid, r in sorted(self.rows.items())
                if r["key_version"] != primary
            ]
            return _Mappings(pending[: params["batch_size"]])
        # UPDATE
        row_id = params["row_id"]
        if row_id in self.steal_row_ids:
            # Simulate the concurrent rotation: the guard matches nothing.
            self.steal_row_ids.discard(row_id)
            self.rows[row_id]["key_version"] = params["new_version"]
            return _RowCount(0)
        row = self.rows.get(row_id)
        if row is None or row["key_version"] != params["expected_version"]:
            return _RowCount(0)
        row["ciphertext"] = params["ciphertext"]
        row["key_version"] = params["new_version"]
        return _RowCount(1)

    def commit(self):
        self.commits += 1


class _One:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self._rows


class _RowCount:
    def __init__(self, rowcount):
        self.rowcount = rowcount


# --- the register of encrypted columns --------------------------------------


def test_the_register_names_every_encrypted_column_in_the_models() -> None:
    """A third encrypted column added later must not be silently left on an
    old key. This asserts the register against the model layer rather than
    against a hand-kept list."""
    from app_shared.models import ProxyProvider, WebhookEndpoint

    registered = {(c.table, c.ciphertext_column, c.version_column) for c in ENCRYPTED_COLUMNS}
    expected = {
        (ProxyProvider.__tablename__, "password_encrypted", "password_key_version"),
        (WebhookEndpoint.__tablename__, "secret_encrypted", "secret_key_version"),
    }
    assert registered == expected


def test_every_registered_pair_really_exists_on_its_model() -> None:
    from app_shared.models import ProxyProvider, WebhookEndpoint

    by_table = {
        ProxyProvider.__tablename__: ProxyProvider,
        WebhookEndpoint.__tablename__: WebhookEndpoint,
    }
    for column in ENCRYPTED_COLUMNS:
        model = by_table[column.table]
        assert hasattr(model, column.ciphertext_column)
        assert hasattr(model, column.version_column)
        assert hasattr(model, column.id_column)


# --- the sweep --------------------------------------------------------------


def test_sweep_moves_every_old_row_onto_the_new_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _both_keys_primary_v1(monkeypatch)
    secrets = {f"row-{i}": f"proxy-password-{i}" for i in range(5)}
    rows = [
        {"row_id": rid, "ciphertext": enc.encrypt_secret(p).ciphertext, "key_version": 1}
        for rid, p in secrets.items()
    ]

    _both_keys_primary_v2(monkeypatch)  # step 2: promote
    session = FakeSession(rows)
    assert rows_on_old_versions(session, COLUMN, primary_version=2) == 5

    report = rotate_column(session, COLUMN, primary_version=2, batch_size=2)

    assert report.rotated == 5
    assert report.ok
    assert rows_on_old_versions(session, COLUMN, primary_version=2) == 0
    # ...and every plaintext survived the move.
    for rid, plaintext in secrets.items():
        row = session.rows[rid]
        assert row["key_version"] == 2
        assert enc.decrypt_secret(row["ciphertext"], row["key_version"]) == plaintext


def test_sweep_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running a completed sweep must be a no-op, not a second rotation."""
    _both_keys_primary_v1(monkeypatch)
    rows = [
        {"row_id": "a", "ciphertext": enc.encrypt_secret("p").ciphertext, "key_version": 1}
    ]
    _both_keys_primary_v2(monkeypatch)
    session = FakeSession(rows)
    assert rotate_column(session, COLUMN, primary_version=2).rotated == 1
    assert rotate_column(session, COLUMN, primary_version=2).rotated == 0


def test_sweep_batches_and_commits_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupted sweep must leave the rows it already moved moved."""
    _both_keys_primary_v1(monkeypatch)
    rows = [
        {"row_id": f"r{i}", "ciphertext": enc.encrypt_secret("p").ciphertext, "key_version": 1}
        for i in range(7)
    ]
    _both_keys_primary_v2(monkeypatch)
    session = FakeSession(rows)
    rotate_column(session, COLUMN, primary_version=2, batch_size=3)
    # 3 + 3 + 1 rows, then one empty SELECT that breaks the loop.
    assert session.commits == 3


def test_a_concurrently_rotated_row_is_skipped_not_clobbered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overwriting newer ciphertext with older plaintext is the failure mode
    the `WHERE key_version = :expected` guard exists to prevent."""
    _both_keys_primary_v1(monkeypatch)
    rows = [
        {"row_id": "a", "ciphertext": enc.encrypt_secret("pa").ciphertext, "key_version": 1},
        {"row_id": "b", "ciphertext": enc.encrypt_secret("pb").ciphertext, "key_version": 1},
    ]
    _both_keys_primary_v2(monkeypatch)
    session = FakeSession(rows, steal_row_ids={"a"})
    report = rotate_column(session, COLUMN, primary_version=2)
    assert report.skipped_concurrent == 1
    assert report.rotated == 1


def test_rotate_all_sweeps_every_registered_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _both_keys_primary_v1(monkeypatch)
    ciphertext = enc.encrypt_secret("p").ciphertext
    _both_keys_primary_v2(monkeypatch)

    sessions = {
        c.table: FakeSession([{"row_id": "a", "ciphertext": ciphertext, "key_version": 1}])
        for c in ENCRYPTED_COLUMNS
    }

    class Router:
        """One fake per table — the sweep's statements name their own table."""

        def __init__(self):
            self.commits = 0

        def execute(self, statement, params=None):
            sql = str(statement)
            for table, session in sessions.items():
                if table in sql:
                    return session.execute(statement, params)
            raise AssertionError(f"statement named no known table: {sql}")

        def commit(self):
            for session in sessions.values():
                session.commit()

    report = rotate_all(Router(), primary_version=2)
    assert report.rotated == len(ENCRYPTED_COLUMNS)
    assert report.ok


# --- REVOCATION -------------------------------------------------------------


def test_service_continues_after_the_old_key_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: rotation WITHOUT service interruption.

    Encrypt under v1, promote to v2, sweep, then revoke v1 entirely — and the
    credentials are still readable.
    """
    _both_keys_primary_v1(monkeypatch)
    plaintext = "the-proxy-password"
    rows = [
        {"row_id": "a", "ciphertext": enc.encrypt_secret(plaintext).ciphertext, "key_version": 1}
    ]

    _both_keys_primary_v2(monkeypatch)
    session = FakeSession(rows)
    assert rotate_column(session, COLUMN, primary_version=2).ok
    assert rows_on_old_versions(session, COLUMN, primary_version=2) == 0

    _only_v2(monkeypatch)  # step 5: revoke
    row = session.rows["a"]
    assert enc.decrypt_secret(row["ciphertext"], row["key_version"]) == plaintext


def test_revoking_before_the_sweep_makes_the_row_loudly_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revocation must be a failure that is noticed, not a blank password."""
    _both_keys_primary_v1(monkeypatch)
    ciphertext = enc.encrypt_secret("the-proxy-password").ciphertext

    _only_v2(monkeypatch)  # revoked TOO EARLY — step 4's gate was skipped
    with pytest.raises(enc.SecretDecryptionError):
        enc.decrypt_secret(ciphertext, 1)


def test_the_sweep_names_undecryptable_rows_and_blocks_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row on a key that is ALREADY gone must be reported, not re-encrypted
    from a blank — a valid ciphertext of an empty password is worse than an
    unreadable one, because it looks like it works."""
    _both_keys_primary_v1(monkeypatch)
    good = enc.encrypt_secret("good").ciphertext

    _only_v2(monkeypatch)
    session = FakeSession(
        [
            {"row_id": "a", "ciphertext": good, "key_version": 1},  # v1 is gone
            {"row_id": "b", "ciphertext": "not-even-a-token", "key_version": 2},
        ]
    )
    # Only row "a" is on a non-primary version, so only it is swept.
    report = rotate_column(session, COLUMN, primary_version=2)
    assert report.rotated == 0
    assert report.undecryptable == ["proxy_providers:a"]
    assert report.ok is False
    # The row is left exactly as it was — nothing was written over it.
    assert session.rows["a"]["ciphertext"] == good


def test_an_all_undecryptable_batch_stops_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SELECT would return the same rows forever; the sweep must not loop."""
    _both_keys_primary_v1(monkeypatch)
    ciphertext = enc.encrypt_secret("x").ciphertext
    _only_v2(monkeypatch)
    session = FakeSession(
        [{"row_id": f"r{i}", "ciphertext": ciphertext, "key_version": 1} for i in range(3)]
    )
    report = rotate_column(session, COLUMN, primary_version=2, batch_size=2, max_batches=1000)
    assert len(report.undecryptable) == 2  # one batch, then it stopped
    assert session.commits == 1


# --- the report -------------------------------------------------------------


def test_a_clean_report_is_ok_and_a_dirty_one_is_not() -> None:
    assert RotationReport(rotated=3).ok is True
    assert RotationReport(rotated=3, undecryptable=["t:1"]).ok is False


def test_merge_accumulates_every_field() -> None:
    total = RotationReport(rotated=1, skipped_concurrent=1, undecryptable=["a:1"])
    total.merge(RotationReport(rotated=2, skipped_concurrent=3, undecryptable=["b:2"]))
    assert (total.rotated, total.skipped_concurrent) == (3, 4)
    assert total.undecryptable == ["a:1", "b:2"]
