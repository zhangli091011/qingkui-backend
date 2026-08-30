"""Add scoped credit campaigns and hashed redemption codes."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0012"
down_revision = "20260830_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credit_campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("school_id", sa.String(36), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False),
        sa.Column("redemption_count", sa.Integer(), nullable=False),
        sa.Column("per_user_limit", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("school_id", "status", "starts_at", "ends_at", "created_by"):
        op.create_index(f"ix_credit_campaigns_{name}", "credit_campaigns", [name])
    op.create_table(
        "credit_codes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("credit_campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credit_codes_campaign_id", "credit_codes", ["campaign_id"])
    op.create_index("ix_credit_codes_code_hash", "credit_codes", ["code_hash"], unique=True)
    op.create_index("ix_credit_codes_is_active", "credit_codes", ["is_active"])
    op.create_table(
        "credit_redemptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("credit_campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_id", sa.String(36), sa.ForeignKey("credit_codes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code_id", "user_id", name="uq_credit_code_user"),
    )
    for name in ("campaign_id", "code_id", "user_id", "created_at"):
        op.create_index(f"ix_credit_redemptions_{name}", "credit_redemptions", [name])


def downgrade() -> None:
    op.drop_table("credit_redemptions")
    op.drop_table("credit_codes")
    op.drop_table("credit_campaigns")
