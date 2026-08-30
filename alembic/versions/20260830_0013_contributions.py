"""Add isolated content contribution review and delayed rewards."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0013"
down_revision = "20260830_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_contributions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("school_id", sa.String(36), sa.ForeignKey("schools.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contribution_type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("ai_review", sa.JSON(), nullable=False),
        sa.Column("ai_provider", sa.String(40), nullable=True),
        sa.Column("ai_model", sa.String(80), nullable=True),
        sa.Column("review_note", sa.String(2000), nullable=True),
        sa.Column("reviewed_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reward_amount", sa.Integer(), nullable=False),
        sa.Column("reward_status", sa.String(20), nullable=False),
        sa.Column("reward_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in (
        "user_id",
        "school_id",
        "contribution_type",
        "status",
        "reviewed_by",
        "reward_status",
        "reward_available_at",
        "created_at",
    ):
        op.create_index(f"ix_content_contributions_{name}", "content_contributions", [name])


def downgrade() -> None:
    op.drop_table("content_contributions")
