"""Align learning check node IDs with knowledge node IDs."""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0021"
down_revision = "20260831_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("learning_check_attempts") as batch_op:
        batch_op.alter_column(
            "node_id",
            existing_type=sa.String(36),
            type_=sa.String(64),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("learning_check_attempts") as batch_op:
        batch_op.alter_column(
            "node_id",
            existing_type=sa.String(64),
            type_=sa.String(36),
            existing_nullable=False,
        )
