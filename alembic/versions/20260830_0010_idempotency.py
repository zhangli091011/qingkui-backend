"""Add persistent request idempotency records."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0010"
down_revision = "20260830_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idempotency_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(120), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "scope", "key", name="uq_idempotency_user_scope_key"),
    )
    op.create_index("ix_idempotency_requests_user_id", "idempotency_requests", ["user_id"])
    op.create_index("ix_idempotency_requests_status", "idempotency_requests", ["status"])


def downgrade() -> None:
    op.drop_table("idempotency_requests")
