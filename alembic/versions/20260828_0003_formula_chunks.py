"""Add formula classification fields to knowledge chunks.

Revision ID: 20260828_0003
Revises: 20260828_0002
"""
from alembic import op
import sqlalchemy as sa


revision = "20260828_0003"
down_revision = "20260828_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_chunks")}
    if "content_type" not in columns:
        op.add_column("knowledge_chunks", sa.Column("content_type", sa.String(20), nullable=False, server_default="text"))
        op.create_index("ix_knowledge_chunks_content_type", "knowledge_chunks", ["content_type"])
    if "formula_latex" not in columns:
        op.add_column("knowledge_chunks", sa.Column("formula_latex", sa.Text(), nullable=True))
    if "formula_source" not in columns:
        op.add_column("knowledge_chunks", sa.Column("formula_source", sa.String(30), nullable=True))
    if "ocr_confidence" not in columns:
        op.add_column("knowledge_chunks", sa.Column("ocr_confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("knowledge_chunks")}
    if "ocr_confidence" in columns:
        op.drop_column("knowledge_chunks", "ocr_confidence")
    if "formula_source" in columns:
        op.drop_column("knowledge_chunks", "formula_source")
    if "formula_latex" in columns:
        op.drop_column("knowledge_chunks", "formula_latex")
    if "content_type" in columns:
        op.drop_index("ix_knowledge_chunks_content_type", table_name="knowledge_chunks")
        op.drop_column("knowledge_chunks", "content_type")
