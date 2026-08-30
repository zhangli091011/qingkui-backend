"""Add staged mistake practice rounds and review scheduling."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0015"
down_revision = "20260830_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mistake_problems", sa.Column("review_stage", sa.String(30), nullable=False, server_default="correction"))
    op.add_column("mistake_problems", sa.Column("first_corrected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("mistake_problems", sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("mistake_problems", sa.Column("second_attempt_correct", sa.Boolean(), nullable=True))
    op.add_column("mistake_problems", sa.Column("review_streak", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_mistake_problems_review_stage", "mistake_problems", ["review_stage"])

    op.create_table(
        "mistake_practice_rounds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mistake_id", sa.String(36), sa.ForeignKey("mistake_problems.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("review_stage", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("correct_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("authoritative_correct_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("mistake_id", "round_number", name="uq_mistake_practice_round_number"),
    )
    for name in ("mistake_id", "user_id", "review_stage", "status", "started_at"):
        op.create_index(f"ix_mistake_practice_rounds_{name}", "mistake_practice_rounds", [name])

    with op.batch_alter_table("mistake_practices") as batch:
        batch.add_column(sa.Column("round_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("position", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("hint", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_mistake_practices_round_id",
            "mistake_practice_rounds",
            ["round_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_mistake_practices_round_id", ["round_id"])
        batch.create_unique_constraint("uq_mistake_practice_round_position", ["round_id", "position"])


def downgrade() -> None:
    with op.batch_alter_table("mistake_practices") as batch:
        batch.drop_constraint("uq_mistake_practice_round_position", type_="unique")
        batch.drop_index("ix_mistake_practices_round_id")
        batch.drop_constraint("fk_mistake_practices_round_id", type_="foreignkey")
        batch.drop_column("hint")
        batch.drop_column("position")
        batch.drop_column("round_id")
    op.drop_table("mistake_practice_rounds")
    op.drop_index("ix_mistake_problems_review_stage", table_name="mistake_problems")
    op.drop_column("mistake_problems", "review_streak")
    op.drop_column("mistake_problems", "second_attempt_correct")
    op.drop_column("mistake_problems", "last_reviewed_at")
    op.drop_column("mistake_problems", "first_corrected_at")
    op.drop_column("mistake_problems", "review_stage")
