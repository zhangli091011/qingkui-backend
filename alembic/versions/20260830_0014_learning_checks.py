"""Add server-validated understanding checks."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0014"
down_revision = "20260830_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_check_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("prompt", sa.String(500), nullable=False),
        sa.Column("choices", sa.JSON(), nullable=False),
        sa.Column("correct_choice_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("user_id", "node_id", "status", "expires_at", "created_at"):
        op.create_index(f"ix_learning_check_attempts_{name}", "learning_check_attempts", [name])


def downgrade() -> None:
    op.drop_table("learning_check_attempts")
