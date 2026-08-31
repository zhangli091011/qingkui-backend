"""Record versioned privacy notice consent."""

from alembic import op
import sqlalchemy as sa


revision = "20260831_0020"
down_revision = "20260831_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_privacy_consents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("notice_version", sa.String(40), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_user_privacy_consents_user_id", "user_privacy_consents", ["user_id"])
    op.create_index("ix_user_privacy_consents_notice_version", "user_privacy_consents", ["notice_version"])


def downgrade() -> None:
    op.drop_table("user_privacy_consents")
