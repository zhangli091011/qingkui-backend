"""Ensure reviewer deletion preserves feedback submissions."""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0019"
down_revision = "20260831_0018"
branch_labels = None
depends_on = None


_CONSTRAINT = "fk_feedback_submissions_reviewed_by_users"


def _reviewed_by_foreign_key(bind) -> dict | None:
    for item in sa.inspect(bind).get_foreign_keys("feedback_submissions"):
        if item.get("constrained_columns") == ["reviewed_by"]:
            return item
    return None


def upgrade() -> None:
    bind = op.get_bind()
    if "feedback_submissions" not in sa.inspect(bind).get_table_names():
        return

    existing = _reviewed_by_foreign_key(bind)
    options = (existing or {}).get("options") or {}
    if existing and options.get("ondelete", "").upper() == "SET NULL":
        return

    if bind.dialect.name == "sqlite":
        # SQLite requires table recreation for foreign-key policy changes.
        with op.batch_alter_table("feedback_submissions", recreate="always") as batch:
            if existing and existing.get("name"):
                batch.drop_constraint(existing["name"], type_="foreignkey")
            batch.create_foreign_key(
                _CONSTRAINT,
                "users",
                ["reviewed_by"],
                ["id"],
                ondelete="SET NULL",
            )
        return

    if existing and existing.get("name"):
        op.drop_constraint(existing["name"], "feedback_submissions", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "feedback_submissions",
        "users",
        ["reviewed_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Keep the safer SET NULL policy on downgrade; this migration only repairs
    # legacy schemas and has no independent application-level behavior.
    pass
