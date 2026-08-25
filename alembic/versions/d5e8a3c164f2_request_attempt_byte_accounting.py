"""request_attempts.main_document_bytes + subresource_bytes

Revision ID: d5e8a3c164f2
Revises: c9a271f5b6e8
Create Date: 2026-08-25 00:00:00.000000

EPA B6 (2026-08-25, browser resource blocking policy): byte accounting
split per attempt, so a canary run can show *what* the blocklist saved,
not just that the blocklist ran.

Two nullable ``BigInteger`` columns on ``request_attempts`` (one row per
attempted target, FR-013):

* ``main_document_bytes`` — bytes of the page's own top-level document
  response (the one navigation the scraper actually wants).
* ``subresource_bytes`` — bytes of every OTHER response the browser
  loaded while rendering that document (images/media/fonts/XHR/ads/
  analytics/...) — i.e. exactly what :mod:`app_shared.profiles.
  browser_resource_policy` decides to block or let through. Before this
  split, the canary's own investigation (most proxy bytes were Amazon
  browser subresources — media/ad requests, never the priced page
  itself) had no column to point at; this is that column.

Both are **TRANSPORT-OBSERVED** figures ONLY — summed straight from the
byte counts the browser/HTTP client reports for each response it
actually received on the wire for this attempt. They are deliberately
NOT the same fact as provider-billed bytes: compression (gzip/br over
the wire vs. the decompressed byte count a client reports),
CONNECT/TLS handshake overhead on a proxied fetch, a redirect chain's
intermediate responses, and a service worker/cache hit that a browser
never re-fetches over the network are all differences between "what the
transport observed" and "what the proxy provider actually billed for
this attempt". Provider-reconciled bytes are a DISTINCT fact, to be
recorded and reconciled against these two columns in C5 — this
migration adds no provider-reconciliation column and this pair must
never be conflated with one when C5 lands it.

Nullable, no ``server_default``: every attempt row written before this
column existed, and every non-browser (HTTP-only) attempt going
forward, legitimately has no byte breakdown to report — NULL means "not
measured for this attempt", not zero.

``request_attempts`` is a monthly-RANGE-partitioned table
(``2db33dea5e14``); ``ALTER TABLE ... ADD COLUMN`` on the partitioned
**parent** propagates to every existing partition automatically (the
``0fc4c9c9c8b3`` precedent) — no per-partition ``op.execute``.

RLS: unaffected — a plain nullable column addition to an already-RLS'd
table needs no new policy.

Reversible: ``downgrade`` drops both columns. Loses only the byte
breakdown, never an attempt row.

Hand-authored (matches ``app_shared.models.observations.RequestAttempt``
exactly) — this build environment has no live Postgres connection for
autogenerate.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5e8a3c164f2'
down_revision: Union[str, Sequence[str], None] = 'c9a271f5b6e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: request_attempts byte-accounting columns (nullable, transport-observed)."""
    op.add_column(
        "request_attempts",
        sa.Column("main_document_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "request_attempts",
        sa.Column("subresource_bytes", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema: drop the byte-accounting columns."""
    op.drop_column("request_attempts", "subresource_bytes")
    op.drop_column("request_attempts", "main_document_bytes")
