"""Persist structured mistake analysis results."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0008"
down_revision = "20260830_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mistake_problems", sa.Column("analysis", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column(
        "mistake_problems",
        sa.Column("analysis_status", sa.String(30), nullable=False, server_default="not_started"),
    )
    op.add_column("mistake_problems", sa.Column("analysis_provider", sa.String(40), nullable=True))
    op.add_column("mistake_problems", sa.Column("analysis_model", sa.String(80), nullable=True))
    op.add_column("mistake_problems", sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_mistake_problems_analysis_status", "mistake_problems", ["analysis_status"])


def downgrade() -> None:
    op.drop_index("ix_mistake_problems_analysis_status", table_name="mistake_problems")
    op.drop_column("mistake_problems", "analyzed_at")
    op.drop_column("mistake_problems", "analysis_model")
    op.drop_column("mistake_problems", "analysis_provider")
    op.drop_column("mistake_problems", "analysis_status")
    op.drop_column("mistake_problems", "analysis")
