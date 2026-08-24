"""versioned strategy methods and reproducible profile attempts

Revision ID: 6e4a9c8f2d10
Revises: 88d894a2e23b
Create Date: 2026-08-24 00:00:00.000000

Purely additive Phase-1 migration. Existing profile configuration, learned
preferred columns, and free-text method stats remain intact. Each current
preferred access method is backfilled as priority-zero ``PROVEN`` and linked
to the best currently assigned profile when one exists; a nullable profile
reference deliberately preserves legacy preferences that predate profiles.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app_shared.models import emit_global_readable_rls_policy, emit_rls_policy

revision: str = "6e4a9c8f2d10"
down_revision: Union[str, Sequence[str], None] = "88d894a2e23b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "domain_playbooks",
        sa.Column(
            "method_templates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Reusable profile configuration is versioned without replacing its
    # backward-compatible mutable fast-path row.
    op.add_column(
        "scrape_profiles",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "scrape_profiles",
        sa.Column(
            "adapter_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_table(
        "scrape_profile_revisions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scrape_profile_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scrape_profile_revisions"),
        sa.UniqueConstraint(
            "scrape_profile_id",
            "version",
            name="uq_scrape_profile_revisions_profile_version",
        ),
        sa.ForeignKeyConstraint(
            ["scrape_profile_id"],
            ["scrape_profiles.id"],
            name="fk_spr_profile_id_scrape_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_scrape_profile_revisions_workspace_id_workspaces",
        ),
    )
    op.create_index(
        "ix_scrape_profile_revisions_workspace_id",
        "scrape_profile_revisions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_scrape_profile_revisions_scrape_profile_id",
        "scrape_profile_revisions",
        ["scrape_profile_id"],
    )
    op.execute(
        """
        INSERT INTO scrape_profile_revisions
            (id, workspace_id, scrape_profile_id, version, snapshot, created_at, updated_at)
        SELECT gen_random_uuid(), sp.workspace_id, sp.id, sp.version,
               to_jsonb(sp) - 'id' - 'workspace_id' - 'version' - 'created_at' - 'updated_at',
               now(), now()
        FROM scrape_profiles sp
        """
    )
    for statement in emit_global_readable_rls_policy("scrape_profile_revisions"):
        op.execute(statement)

    # Composite parent key makes a method structurally incapable of pointing
    # at another workspace's domain-strategy profile.
    op.create_unique_constraint(
        "uq_dsp_workspace_id_id", "domain_strategy_profiles", ["workspace_id", "id"]
    )
    op.create_table(
        "domain_strategy_methods",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("domain_strategy_profile_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("scrape_profile_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scrape_profile_version", sa.Integer(), nullable=True),
        sa.Column("access_method", sa.String(length=32), nullable=False),
        sa.Column("extraction_method", sa.String(length=32), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("method_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "enter_on",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "fallback_on",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("proof_state", sa.String(length=32), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_canary_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proof_sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("circuit_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("circuit_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("supersedes_method_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_domain_strategy_methods"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "domain_strategy_profile_id"],
            ["domain_strategy_profiles.workspace_id", "domain_strategy_profiles.id"],
            name="fk_dsm_workspace_profile_domain_strategy_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scrape_profile_id"],
            ["scrape_profiles.id"],
            name="fk_dsm_scrape_profile_id_scrape_profiles",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_method_id"],
            ["domain_strategy_methods.id"],
            name="fk_dsm_supersedes_domain_strategy_methods",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_domain_strategy_methods_workspace_id_workspaces",
        ),
    )
    op.create_index(
        "ix_domain_strategy_methods_workspace_id",
        "domain_strategy_methods",
        ["workspace_id"],
    )
    op.create_index(
        "ix_domain_strategy_methods_domain_strategy_profile_id",
        "domain_strategy_methods",
        ["domain_strategy_profile_id"],
    )
    op.create_index(
        "ix_domain_strategy_methods_scrape_profile_id",
        "domain_strategy_methods",
        ["scrape_profile_id"],
    )
    op.create_index(
        "uq_dsm_profile_active_priority",
        "domain_strategy_methods",
        ["domain_strategy_profile_id", "priority"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_index(
        "ix_dsm_profile_enabled_priority",
        "domain_strategy_methods",
        ["domain_strategy_profile_id", "enabled", "priority"],
    )
    op.add_column(
        "domain_strategy_profiles",
        sa.Column("preferred_method_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_domain_strategy_profiles_preferred_method_id",
        "domain_strategy_profiles",
        ["preferred_method_id"],
    )

    # Preserve current learned winners as a first candidate rather than
    # overwriting them. Competitor profile wins, then workspace default;
    # missing legacy assignments stay visible with a NULL profile reference.
    op.execute(
        """
        INSERT INTO domain_strategy_methods
            (id, workspace_id, domain_strategy_profile_id, scrape_profile_id,
             scrape_profile_version, access_method, extraction_method, priority,
             method_version, enter_on, fallback_on, enabled, proof_state, proof_sample_size,
             created_at, updated_at)
        SELECT gen_random_uuid(), dsp.workspace_id, dsp.id,
               COALESCE(c.default_scrape_profile_id, w.default_scrape_profile_id),
               sp.version, dsp.preferred_access_method,
               dsp.preferred_extraction_method, 0, 1, '[]'::jsonb, '[]'::jsonb, true,
               'PROVEN', dsp.confirmed_success_count, now(), now()
        FROM domain_strategy_profiles dsp
        JOIN competitors c
          ON c.id = dsp.competitor_id AND c.workspace_id = dsp.workspace_id
        JOIN workspaces w ON w.id = dsp.workspace_id
        LEFT JOIN scrape_profiles sp
          ON sp.id = COALESCE(c.default_scrape_profile_id, w.default_scrape_profile_id)
        WHERE dsp.preferred_access_method IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE domain_strategy_profiles dsp
        SET preferred_method_id = dsm.id
        FROM domain_strategy_methods dsm
        WHERE dsm.domain_strategy_profile_id = dsp.id
          AND dsm.priority = 0 AND dsm.retired_at IS NULL
        """
    )
    op.create_foreign_key(
        "fk_dsp_preferred_method_domain_strategy_methods",
        "domain_strategy_profiles",
        "domain_strategy_methods",
        ["preferred_method_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Supplement, never replace, the historic free-text stats key.
    op.add_column(
        "strategy_attempt_stats",
        sa.Column("strategy_method_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.drop_constraint(
        "uq_sas_profile_method_type_name",
        "strategy_attempt_stats",
        type_="unique",
    )
    op.create_index(
        "uq_sas_profile_method_type_name",
        "strategy_attempt_stats",
        ["domain_strategy_profile_id", "method_type", "method_name"],
        unique=True,
        postgresql_where=sa.text("strategy_method_id IS NULL"),
    )
    op.create_index(
        "uq_sas_strategy_method_type",
        "strategy_attempt_stats",
        ["strategy_method_id", "method_type"],
        unique=True,
        postgresql_where=sa.text("strategy_method_id IS NOT NULL"),
    )
    op.create_index(
        "ix_strategy_attempt_stats_strategy_method_id",
        "strategy_attempt_stats",
        ["strategy_method_id"],
    )
    op.create_foreign_key(
        "fk_sas_strategy_method_domain_strategy_methods",
        "strategy_attempt_stats",
        "domain_strategy_methods",
        ["strategy_method_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE strategy_attempt_stats sas
        SET strategy_method_id = dsm.id
        FROM domain_strategy_methods dsm
        WHERE dsm.domain_strategy_profile_id = sas.domain_strategy_profile_id
          AND dsm.priority = 0
          AND ((sas.method_type = 'ACCESS' AND sas.method_name = dsm.access_method)
            OR (sas.method_type = 'EXTRACTION' AND sas.method_name = dsm.extraction_method))
        """
    )
    for statement in emit_rls_policy("domain_strategy_methods"):
        op.execute(statement)

    # Request-attempt audit: exact combined method and immutable profile
    # revision, adapter/identity result, redirect destination, terminality.
    for column in (
        sa.Column("strategy_method_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scrape_profile_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scrape_profile_version", sa.Integer(), nullable=True),
        sa.Column("adapter_key", sa.String(length=32), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("identity_validation_result", sa.Text(), nullable=True),
        sa.Column("terminal_for_target", sa.Boolean(), nullable=False, server_default=sa.true()),
    ):
        op.add_column("request_attempts", column)
    op.create_index(
        "ix_request_attempts_strategy_method_id", "request_attempts", ["strategy_method_id"]
    )
    op.create_index(
        "ix_request_attempts_scrape_profile_id", "request_attempts", ["scrape_profile_id"]
    )
    op.create_foreign_key(
        "fk_request_attempts_strategy_method_id_domain_strategy_methods",
        "request_attempts",
        "domain_strategy_methods",
        ["strategy_method_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_request_attempts_scrape_profile_id_scrape_profiles",
        "request_attempts",
        "scrape_profiles",
        ["scrape_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Durable strategy-chain handoff state for HTTP/browser node changes.
    op.add_column(
        "scrape_job_targets",
        sa.Column("current_strategy_method_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scrape_job_targets", sa.Column("chain_token", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "scrape_job_targets",
        sa.Column("strategy_url_override", sa.Text(), nullable=True),
    )
    op.add_column(
        "scrape_job_targets",
        sa.Column("strategy_attempt_ordinal", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_foreign_key(
        "fk_sjt_current_strategy_method_dsm",
        "scrape_job_targets",
        "domain_strategy_methods",
        ["current_strategy_method_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_scrape_job_targets_current_strategy_method_id",
        "scrape_job_targets",
        ["current_strategy_method_id"],
    )
    op.create_index(
        "ix_scrape_job_targets_chain_token", "scrape_job_targets", ["chain_token"]
    )


def downgrade() -> None:
    op.drop_index("ix_scrape_job_targets_chain_token", table_name="scrape_job_targets")
    op.drop_index(
        "ix_scrape_job_targets_current_strategy_method_id", table_name="scrape_job_targets"
    )
    op.drop_constraint(
        "fk_sjt_current_strategy_method_dsm", "scrape_job_targets", type_="foreignkey"
    )
    op.drop_column("scrape_job_targets", "strategy_attempt_ordinal")
    op.drop_column("scrape_job_targets", "strategy_url_override")
    op.drop_column("scrape_job_targets", "chain_token")
    op.drop_column("scrape_job_targets", "current_strategy_method_id")

    op.drop_constraint(
        "fk_request_attempts_scrape_profile_id_scrape_profiles",
        "request_attempts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_request_attempts_strategy_method_id_domain_strategy_methods",
        "request_attempts",
        type_="foreignkey",
    )
    op.drop_index("ix_request_attempts_scrape_profile_id", table_name="request_attempts")
    op.drop_index("ix_request_attempts_strategy_method_id", table_name="request_attempts")
    for name in (
        "terminal_for_target", "identity_validation_result", "final_url", "adapter_key",
        "scrape_profile_version", "scrape_profile_id", "strategy_method_id",
    ):
        op.drop_column("request_attempts", name)

    op.drop_constraint(
        "fk_sas_strategy_method_domain_strategy_methods",
        "strategy_attempt_stats",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_strategy_attempt_stats_strategy_method_id",
        table_name="strategy_attempt_stats",
    )
    op.drop_index(
        "uq_sas_strategy_method_type",
        table_name="strategy_attempt_stats",
    )
    op.drop_index(
        "uq_sas_profile_method_type_name",
        table_name="strategy_attempt_stats",
    )
    # A downgrade collapses version-specific rows back into the legacy
    # free-text grain. Keep one representative per old key before restoring
    # its unique constraint (downgrade is necessarily lossy at this boundary).
    op.execute(
        """
        DELETE FROM strategy_attempt_stats newer
        USING strategy_attempt_stats keeper
        WHERE newer.domain_strategy_profile_id = keeper.domain_strategy_profile_id
          AND newer.method_type = keeper.method_type
          AND newer.method_name = keeper.method_name
          AND newer.id::text > keeper.id::text
        """
    )
    op.drop_column("strategy_attempt_stats", "strategy_method_id")
    op.create_unique_constraint(
        "uq_sas_profile_method_type_name",
        "strategy_attempt_stats",
        ["domain_strategy_profile_id", "method_type", "method_name"],
    )
    op.drop_constraint(
        "fk_dsp_preferred_method_domain_strategy_methods",
        "domain_strategy_profiles",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_domain_strategy_profiles_preferred_method_id",
        table_name="domain_strategy_profiles",
    )
    op.drop_column("domain_strategy_profiles", "preferred_method_id")
    op.drop_table("domain_strategy_methods")
    op.drop_constraint("uq_dsp_workspace_id_id", "domain_strategy_profiles", type_="unique")
    op.drop_table("scrape_profile_revisions")
    op.drop_column("scrape_profiles", "adapter_config")
    op.drop_column("scrape_profiles", "version")
    op.drop_column("domain_playbooks", "method_templates")
