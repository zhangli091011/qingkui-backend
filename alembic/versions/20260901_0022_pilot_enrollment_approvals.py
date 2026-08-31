"""add pilot enrollment approvals

Revision ID: 20260901_0022
Revises: 20260831_0021
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260901_0022"
down_revision: str | None = "20260831_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pilot_enrollment_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("school_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("school_authorization_confirmed", sa.Boolean(), nullable=False),
        sa.Column("voluntary_participation_confirmed", sa.Boolean(), nullable=False),
        sa.Column("guardian_authorization_required", sa.Boolean(), nullable=False),
        sa.Column("guardian_authorization_confirmed", sa.Boolean(), nullable=False),
        sa.Column("approval_basis", sa.String(length=500), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("school_id", "user_id", name="uq_pilot_enrollment_school_user"),
    )
    op.create_index("ix_pilot_enrollment_approvals_school_id", "pilot_enrollment_approvals", ["school_id"])
    op.create_index("ix_pilot_enrollment_approvals_user_id", "pilot_enrollment_approvals", ["user_id"])
    op.create_index("ix_pilot_enrollment_approvals_status", "pilot_enrollment_approvals", ["status"])
    op.create_index("ix_pilot_enrollment_approvals_approved_by", "pilot_enrollment_approvals", ["approved_by"])


def downgrade() -> None:
    op.drop_index("ix_pilot_enrollment_approvals_approved_by", table_name="pilot_enrollment_approvals")
    op.drop_index("ix_pilot_enrollment_approvals_status", table_name="pilot_enrollment_approvals")
    op.drop_index("ix_pilot_enrollment_approvals_user_id", table_name="pilot_enrollment_approvals")
    op.drop_index("ix_pilot_enrollment_approvals_school_id", table_name="pilot_enrollment_approvals")
    op.drop_table("pilot_enrollment_approvals")
