"""Add knowledge version history and moderation metadata."""

import json
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260829_0005"
down_revision = "20260829_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "knowledge_node_versions" not in tables:
        op.create_table(
            "knowledge_node_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("node_id", sa.String(64), sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("change_note", sa.String(255), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
            sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("node_id", "version"),
        )
        op.create_index("ix_knowledge_node_versions_node_id", "knowledge_node_versions", ["node_id"])
        op.create_index("ix_knowledge_node_versions_status", "knowledge_node_versions", ["status"])
        op.create_index("ix_knowledge_node_versions_created_by", "knowledge_node_versions", ["created_by"])
        nodes = sa.table(
            "knowledge_nodes",
            sa.column("id", sa.String), sa.column("name", sa.String), sa.column("subject", sa.String),
            sa.column("grade", sa.String), sa.column("textbook_version", sa.String), sa.column("chapter", sa.String),
            sa.column("definition", sa.Text), sa.column("explanation", sa.Text), sa.column("common_errors", sa.JSON),
            sa.column("question_types", sa.JSON), sa.column("source_id", sa.String), sa.column("source_excerpt", sa.String),
            sa.column("version", sa.Integer), sa.column("review_status", sa.String), sa.column("is_active", sa.Boolean),
        )
        versions = sa.table(
            "knowledge_node_versions",
            sa.column("id", sa.String), sa.column("node_id", sa.String), sa.column("version", sa.Integer),
            sa.column("snapshot", sa.JSON), sa.column("status", sa.String), sa.column("created_at", sa.DateTime),
            sa.column("change_note", sa.String),
        )
        existing_ids = {row[0] for row in bind.execute(sa.text("SELECT node_id FROM knowledge_node_versions"))}
        for row in bind.execute(sa.select(nodes)):
            if row.id in existing_ids:
                continue
            snapshot = {key: getattr(row, key) for key in ("id", "name", "subject", "grade", "textbook_version", "chapter", "definition", "explanation", "common_errors", "question_types", "source_id", "source_excerpt", "review_status", "is_active")}
            bind.execute(sa.insert(versions).values(id=str(uuid.uuid4()), node_id=row.id, version=row.version or 1, snapshot=json.loads(json.dumps(snapshot, ensure_ascii=False, default=str)), status="published" if row.review_status == "approved" and row.is_active else "draft", created_at=datetime.now(timezone.utc), change_note="迁移生成的初始版本"))
    feedback_columns = {column["name"] for column in inspector.get_columns("feedback_submissions")}
    if "review_note" not in feedback_columns:
        op.add_column("feedback_submissions", sa.Column("review_note", sa.String(1000), nullable=True))
    if "reviewed_by" not in feedback_columns:
        # SQLite cannot ALTER a table by adding a column with a foreign-key
        # constraint. The ORM still exposes the relationship; deployments on
        # PostgreSQL can enforce it separately with the normal schema tooling.
        op.add_column("feedback_submissions", sa.Column("reviewed_by", sa.String(36), nullable=True))
        op.create_index("ix_feedback_submissions_reviewed_by", "feedback_submissions", ["reviewed_by"])
    if "reviewed_at" not in feedback_columns:
        op.add_column("feedback_submissions", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "feedback_submissions" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("feedback_submissions")}
        if "reviewed_by" in columns:
            op.drop_index("ix_feedback_submissions_reviewed_by", table_name="feedback_submissions")
            op.drop_column("feedback_submissions", "reviewed_by")
        for name in ("reviewed_at", "review_note"):
            if name in columns:
                op.drop_column("feedback_submissions", name)
    if "knowledge_node_versions" in inspector.get_table_names():
        op.drop_index("ix_knowledge_node_versions_created_by", table_name="knowledge_node_versions")
        op.drop_index("ix_knowledge_node_versions_status", table_name="knowledge_node_versions")
        op.drop_index("ix_knowledge_node_versions_node_id", table_name="knowledge_node_versions")
        op.drop_table("knowledge_node_versions")
