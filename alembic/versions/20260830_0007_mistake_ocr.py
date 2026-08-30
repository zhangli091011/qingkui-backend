"""Add the asynchronous OCR mistake workflow."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0007"
down_revision = "20260829_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mistake_problems",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject", sa.String(40), nullable=True),
        sa.Column("question_text", sa.Text(), nullable=True),
        sa.Column("corrected_text", sa.Text(), nullable=True),
        sa.Column("student_work", sa.Text(), nullable=True),
        sa.Column("question_goal", sa.String(1000), nullable=True),
        sa.Column("error_category", sa.String(30), nullable=True),
        sa.Column("error_note", sa.String(2000), nullable=True),
        sa.Column("knowledge_node_id", sa.String(64), sa.ForeignKey("knowledge_nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("link_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("review_status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("study_status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("user_id", "subject", "error_category", "knowledge_node_id", "link_status", "review_status", "study_status", "next_review_at", "created_at"):
        op.create_index(f"ix_mistake_problems_{name}", "mistake_problems", [name])

    op.create_table(
        "mistake_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mistake_id", sa.String(36), sa.ForeignKey("mistake_problems.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="uploading"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("mistake_id", "user_id", "checksum_sha256", "status"):
        op.create_index(f"ix_mistake_assets_{name}", "mistake_assets", [name])

    op.create_table(
        "ocr_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mistake_id", sa.String(36), sa.ForeignKey("mistake_problems.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("mistake_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="uploading"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("formulas", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("mistake_id", "asset_id", "user_id", "status"):
        op.create_index(f"ix_ocr_tasks_{name}", "ocr_tasks", [name])

    op.create_table(
        "mistake_practices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mistake_id", sa.String(36), sa.ForeignKey("mistake_problems.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer_reference", sa.Text(), nullable=True),
        sa.Column("source", sa.String(30), nullable=False, server_default="generated"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("student_answer", sa.Text(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("validation_details", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name in ("mistake_id", "user_id", "status"):
        op.create_index(f"ix_mistake_practices_{name}", "mistake_practices", [name])


def downgrade() -> None:
    op.drop_table("mistake_practices")
    op.drop_table("ocr_tasks")
    op.drop_table("mistake_assets")
    op.drop_table("mistake_problems")
