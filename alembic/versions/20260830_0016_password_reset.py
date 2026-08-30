"""Add one-time password reset tokens."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0016"
down_revision = "20260830_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("user_id", "token_hash", "expires_at"):
        op.create_index(f"ix_password_reset_tokens_{name}", "password_reset_tokens", [name])


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
