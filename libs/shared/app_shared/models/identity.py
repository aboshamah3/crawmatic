"""Identity ORM models: the first real, workspace-owned application tables.

Per ``data-model.md`` (SPEC-03) — four tables:

* :class:`Workspace` — tenant root, **no** RLS, plain :class:`Base`.
* :class:`User` — workspace-owned, RLS, but with its **own nullable**
  ``workspace_id`` (a cross-workspace ``SUPER_ADMIN`` has no home
  workspace) — cannot use :class:`~app_shared.models.base.WorkspaceScopedBase`
  because that mixin's column is ``NOT NULL``.
* :class:`RefreshToken` — user-owned, **no** RLS (reached only by an
  unforgeable ``token_hash``, never enumerated/filtered by workspace).
* :class:`ApiKey` — workspace-owned, RLS, uses
  :class:`~app_shared.models.base.WorkspaceScopedBase` (mandatory
  ``NOT NULL`` ``workspace_id``); adds the FK to ``workspaces`` via
  ``__table_args__`` since SPEC-02's mixin predates this table.

RLS itself (``emit_rls_policy("users")`` / ``emit_rls_policy("api_keys")``)
is applied in the creating Alembic migration, not here — this module only
declares ORM shape (FR-001…FR-004, §22, §32).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import ApiKeyStatus, UserRole, UserStatus, WorkspaceStatus, enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase


class Workspace(Base, TimestampMixin):
    """Tenant root — NOT workspace-scoped, gets NO RLS policy."""

    __tablename__ = "workspaces"
    __table_args__ = (
        # SPEC-06 promotes this nullable column to a plain FK ->
        # scrape_profiles(id) ON DELETE SET NULL (added via ALTER in the
        # SPEC-06 migration, not here — this module only declares ORM
        # shape). Plain, not composite: a global (workspace_id IS NULL)
        # profile must be assignable by any workspace.
        ForeignKeyConstraint(
            ["default_scrape_profile_id"],
            ["scrape_profiles.id"],
            name="fk_workspaces_default_scrape_profile_id_scrape_profiles",
            ondelete="SET NULL",
        ),
    )

    name: Mapped[str] = mapped_column(Text(), nullable=False)
    slug: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    status: Mapped[WorkspaceStatus] = enum_column(WorkspaceStatus, nullable=False)
    default_scrape_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    # No FK — access_policies lands in a later spec (SPEC-10). Plain
    # dangling nullable id (§22, spec Assumptions).
    default_access_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )


class User(Base, TimestampMixin):
    """Workspace-owned (RLS), but with its own **nullable** ``workspace_id``.

    Cannot use :class:`WorkspaceScopedBase` (NOT NULL column) — a
    ``SUPER_ADMIN`` row has ``workspace_id IS NULL`` (no home workspace),
    which is fail-closed under RLS (``NULL = x`` is never true).
    """

    __tablename__ = "users"

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", name="fk_users_workspace_id_workspaces"),
        nullable=True,
        index=True,
    )
    email: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[UserRole] = enum_column(UserRole, nullable=False)
    status: Mapped[UserStatus] = enum_column(UserStatus, nullable=False)


class RefreshToken(Base):
    """User-owned — NO RLS (reachable only by unforgeable ``token_hash``).

    Declares ``created_at`` directly as :class:`TZDateTime` rather than
    using :class:`TimestampMixin` — this table has no ``updated_at``
    (§22 shape).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", name="fk_refresh_tokens_user_id_users"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(Text(), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)


class ApiKey(Base, WorkspaceScopedBase, TimestampMixin):
    """Workspace-owned (RLS) — mandatory NOT NULL ``workspace_id``."""

    __tablename__ = "api_keys"
    __table_args__ = (
        # SPEC-02's WorkspaceScopedBase intentionally omitted the FK until
        # `workspaces` existed; this spec adds it here.
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_api_keys_workspace_id_workspaces",
        ),
        Index("ix_api_keys_key_prefix", "key_prefix"),
    )

    name: Mapped[str] = mapped_column(Text(), nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text(), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[ApiKeyStatus] = enum_column(ApiKeyStatus, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
