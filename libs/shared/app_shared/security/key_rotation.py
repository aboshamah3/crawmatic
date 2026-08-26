"""Rotating the credential-encryption keyring without an interruption.

EPA W5.5-L1 item 4, engine half.

:mod:`app_shared.security.encryption` already has versioned keys and the
per-row primitive (:func:`~app_shared.security.encryption.reencrypt_secret`).
What it did not have is the thing that makes rotation an *operation* rather
than a capability: a sweep that walks the rows still on an old version, and a
statement of what "revoked" means and when it is safe.

WHAT IS ACTUALLY ENCRYPTED HERE
-------------------------------
Two column pairs, both `(ciphertext, key_version)`:

* ``proxy_providers.password_encrypted`` / ``password_key_version`` — the
  upstream proxy provider credentials (SPEC-10 FR-003).
* ``webhook_endpoints.secret_encrypted`` / ``secret_key_version`` — the
  per-endpoint signing secret.

Both are declared in :data:`ENCRYPTED_COLUMNS`, which is what a sweep walks
and what a coverage test asserts against the model layer, so a third
encrypted column added later cannot be silently left behind on an old key.

THE PROCEDURE (this is the runbook; the code below implements step 3)
---------------------------------------------------------------------
1. **Add** the new key to ``ENCRYPTION_KEYS`` alongside the old one, WITHOUT
   moving ``ENCRYPTION_PRIMARY_KEY_VERSION``. Deploy. Nothing changes: old
   rows decrypt under the old version, new writes still use the old primary.
   This step exists so that the new key is present everywhere before anything
   depends on it — the alternative, flipping both at once, means any process
   that has not yet restarted writes ciphertext its neighbours cannot read.
2. **Promote**: set ``ENCRYPTION_PRIMARY_KEY_VERSION`` to the new version.
   Deploy. New writes use the new key; existing rows are untouched and still
   decrypt, because the old key is still in the ring. THERE IS NO WINDOW IN
   WHICH A CREDENTIAL CANNOT BE READ — that is the whole reason the ring is
   a map and not a single key.
3. **Sweep**: run :func:`rotate_all` (or :func:`rotate_column` per column) to
   re-encrypt every row still on an old version. Idempotent, resumable, and
   batched, because it runs against a live database.
4. **Verify**: :func:`rows_on_old_versions` returns 0 for every column.
5. **Revoke**: remove the old key from ``ENCRYPTION_KEYS``. Only now. After
   this, any row that the sweep missed is permanently unreadable — which is
   why step 4 is a gate and not a suggestion, and why
   :func:`decrypt_secret` raises :class:`SecretDecryptionError` rather than
   returning a blank: a revoked key must be a loud failure, never a silently
   empty password that would be sent to a proxy as if it were real.

WHY THE SWEEP RE-READS THE VERSION IT JUST WROTE
------------------------------------------------
Each row is re-encrypted and its version column updated in the same UPDATE,
guarded by ``WHERE key_version = <old>``. If another writer rotated the row
between our read and our write, the guard matches nothing and we skip it
rather than clobbering newer ciphertext with older plaintext.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

from app_shared.security.encryption import (
    SecretDecryptionError,
    encrypt_secret,
)


@dataclass(frozen=True)
class EncryptedColumn:
    """One `(ciphertext, key_version)` column pair on one table."""

    table: str
    ciphertext_column: str
    version_column: str
    #: Primary key column, used to address a single row in the UPDATE.
    id_column: str = "id"


#: Every credential this system encrypts at rest. A sweep walks this; a test
#: asserts it against the models, so a new encrypted column cannot be
#: forgotten by a future rotation.
ENCRYPTED_COLUMNS: tuple[EncryptedColumn, ...] = (
    EncryptedColumn("proxy_providers", "password_encrypted", "password_key_version"),
    EncryptedColumn("webhook_endpoints", "secret_encrypted", "secret_key_version"),
)


@dataclass
class RotationReport:
    """What one sweep did. Every number is actionable, none is decorative."""

    #: Rows re-encrypted onto the primary version.
    rotated: int = 0
    #: Rows another writer moved between our read and our write. Not an error.
    skipped_concurrent: int = 0
    #: Rows whose ciphertext could not be decrypted at all — an old key is
    #: ALREADY missing from the ring, or the row is corrupt. These block
    #: revocation and are named individually so an operator can act on them.
    undecryptable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing blocks retiring the old key."""
        return not self.undecryptable

    def merge(self, other: "RotationReport") -> "RotationReport":
        self.rotated += other.rotated
        self.skipped_concurrent += other.skipped_concurrent
        self.undecryptable.extend(other.undecryptable)
        return self


def _select_stmt(column: EncryptedColumn):
    return text(
        f"SELECT {column.id_column} AS row_id, "
        f"{column.ciphertext_column} AS ciphertext, "
        f"{column.version_column} AS key_version "
        f"FROM {column.table} "
        f"WHERE {column.ciphertext_column} IS NOT NULL "
        f"AND {column.version_column} IS NOT NULL "
        f"AND {column.version_column} <> :primary "
        f"ORDER BY {column.id_column} "
        f"LIMIT :batch_size"
    )


def _update_stmt(column: EncryptedColumn):
    # The `WHERE ... key_version = :expected_version` guard is what makes the
    # sweep safe to run against a live database: a row somebody else rotated
    # underneath us matches nothing and is skipped, never overwritten.
    return text(
        f"UPDATE {column.table} SET "
        f"{column.ciphertext_column} = :ciphertext, "
        f"{column.version_column} = :new_version "
        f"WHERE {column.id_column} = :row_id "
        f"AND {column.version_column} = :expected_version"
    )


def _count_stmt(column: EncryptedColumn):
    return text(
        f"SELECT count(*) FROM {column.table} "
        f"WHERE {column.ciphertext_column} IS NOT NULL "
        f"AND {column.version_column} IS NOT NULL "
        f"AND {column.version_column} <> :primary"
    )


def rows_on_old_versions(session, column: EncryptedColumn, *, primary_version: int) -> int:
    """How many rows still hold ciphertext under a non-primary key version.

    Step 4's gate: this must be 0 for every column before the old key is
    removed from ``ENCRYPTION_KEYS``.
    """
    return int(session.execute(_count_stmt(column), {"primary": primary_version}).scalar_one())


def rotate_column(
    session,
    column: EncryptedColumn,
    *,
    primary_version: int,
    batch_size: int = 100,
    max_batches: int = 1000,
) -> RotationReport:
    """Re-encrypt every row of one column onto ``primary_version``.

    Batched and committed per batch, so a sweep interrupted halfway leaves
    the rows it already moved moved — this is resumable, and re-running it is
    a no-op for rows already on the primary version.
    """
    report = RotationReport()
    select_stmt = _select_stmt(column)
    update_stmt = _update_stmt(column)

    for _ in range(max_batches):
        rows = list(
            session.execute(
                select_stmt, {"primary": primary_version, "batch_size": batch_size}
            ).mappings()
        )
        if not rows:
            break

        progressed = False
        for row in rows:
            try:
                fresh = encrypt_secret(
                    _decrypt(row["ciphertext"], int(row["key_version"]))
                )
            except SecretDecryptionError:
                # An old key is already gone from the ring, or the row is
                # corrupt. Named, not swallowed, and NOT re-encrypted from a
                # blank — writing a valid ciphertext of an empty password
                # would turn an unreadable credential into a working-looking
                # wrong one.
                report.undecryptable.append(f"{column.table}:{row['row_id']}")
                continue

            result = session.execute(
                update_stmt,
                {
                    "ciphertext": fresh.ciphertext,
                    "new_version": fresh.key_version,
                    "row_id": row["row_id"],
                    "expected_version": int(row["key_version"]),
                },
            )
            if getattr(result, "rowcount", 1) == 0:
                report.skipped_concurrent += 1
            else:
                report.rotated += 1
            progressed = True

        session.commit()
        if not progressed:
            # Every row in this batch was undecryptable, so the next SELECT
            # would return the same rows forever. Stop rather than spin.
            break

    return report


def _decrypt(ciphertext: str, key_version: int) -> str:
    """Indirection point so a test can substitute the keyring cheaply."""
    from app_shared.security.encryption import decrypt_secret

    return decrypt_secret(ciphertext, key_version)


def rotate_all(
    session,
    *,
    primary_version: int,
    columns: tuple[EncryptedColumn, ...] = ENCRYPTED_COLUMNS,
    batch_size: int = 100,
) -> RotationReport:
    """Sweep every encrypted column. Step 3 of the procedure above."""
    total = RotationReport()
    for column in columns:
        total.merge(
            rotate_column(
                session, column, primary_version=primary_version, batch_size=batch_size
            )
        )
    return total
