"""Reconcile databases created by an older create_all/bootstrap path.

Some local snapshots contain the tables introduced by 0011-0016 but miss a
subset of their later columns and constraints.  This migration is deliberately
idempotent so normal PostgreSQL deployments at 0017 remain unchanged.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0018"
down_revision = "20260830_0017"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(bind).get_indexes(table)}


def _foreign_key_columns(bind, table: str) -> set[str]:
    return {
        column
        for item in sa.inspect(bind).get_foreign_keys(table)
        for column in item.get("constrained_columns", [])
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "feedback_submissions" in tables:
        columns = _columns(bind, "feedback_submissions")
        indexes = _indexes(bind, "feedback_submissions")
        foreign_keys = _foreign_key_columns(bind, "feedback_submissions")
        if "reviewed_by" in columns and "ix_feedback_submissions_reviewed_by" not in indexes:
            op.create_index("ix_feedback_submissions_reviewed_by", "feedback_submissions", ["reviewed_by"])
        if "reviewed_by" in columns and "reviewed_by" not in foreign_keys:
            with op.batch_alter_table("feedback_submissions") as batch:
                batch.create_foreign_key(
                    "fk_feedback_submissions_reviewed_by_users",
                    "users",
                    ["reviewed_by"],
                    ["id"],
                )

    if "mistake_problems" in tables:
        columns = _columns(bind, "mistake_problems")
        with op.batch_alter_table("mistake_problems") as batch:
            if "review_stage" not in columns:
                batch.add_column(sa.Column("review_stage", sa.String(30), nullable=False, server_default="correction"))
            if "first_corrected_at" not in columns:
                batch.add_column(sa.Column("first_corrected_at", sa.DateTime(timezone=True), nullable=True))
            if "last_reviewed_at" not in columns:
                batch.add_column(sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True))
            if "second_attempt_correct" not in columns:
                batch.add_column(sa.Column("second_attempt_correct", sa.Boolean(), nullable=True))
            if "review_streak" not in columns:
                batch.add_column(sa.Column("review_streak", sa.Integer(), nullable=False, server_default="0"))
        if "ix_mistake_problems_review_stage" not in _indexes(bind, "mistake_problems"):
            op.create_index("ix_mistake_problems_review_stage", "mistake_problems", ["review_stage"])

    if "mistake_practices" in tables:
        columns = _columns(bind, "mistake_practices")
        foreign_keys = _foreign_key_columns(bind, "mistake_practices")
        indexes = _indexes(bind, "mistake_practices")
        with op.batch_alter_table("mistake_practices") as batch:
            if "round_id" not in columns:
                batch.add_column(sa.Column("round_id", sa.String(36), nullable=True))
            if "position" not in columns:
                batch.add_column(sa.Column("position", sa.Integer(), nullable=True))
            if "hint" not in columns:
                batch.add_column(sa.Column("hint", sa.Text(), nullable=True))
            if "round_id" not in foreign_keys:
                batch.create_foreign_key(
                    "fk_mistake_practices_round_id",
                    "mistake_practice_rounds",
                    ["round_id"],
                    ["id"],
                    ondelete="CASCADE",
                )
            if "uq_mistake_practice_round_position" not in {
                item.get("name") for item in sa.inspect(bind).get_unique_constraints("mistake_practices")
            }:
                batch.create_unique_constraint("uq_mistake_practice_round_position", ["round_id", "position"])
        if "ix_mistake_practices_round_id" not in indexes:
            op.create_index("ix_mistake_practices_round_id", "mistake_practices", ["round_id"])


def downgrade() -> None:
    # This reconciliation is intentionally not destructive.  The authoritative
    # 0015/0016 migrations own downgrade behavior for these fields.
    pass
