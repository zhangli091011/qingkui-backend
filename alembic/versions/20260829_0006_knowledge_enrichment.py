"""Enrich local knowledge metadata and formula review state."""

from alembic import op
import sqlalchemy as sa


revision = "20260829_0006"
down_revision = "20260829_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    document_columns = {column["name"] for column in inspector.get_columns("knowledge_documents")}
    chunk_columns = {column["name"] for column in inspector.get_columns("knowledge_chunks")}
    if "chapter" not in document_columns:
        op.add_column("knowledge_documents", sa.Column("chapter", sa.String(120), nullable=True))
        op.create_index("ix_knowledge_documents_chapter", "knowledge_documents", ["chapter"])
    if "document_role" not in document_columns:
        op.add_column("knowledge_documents", sa.Column("document_role", sa.String(40), nullable=True))
        op.create_index("ix_knowledge_documents_document_role", "knowledge_documents", ["document_role"])
    if "formula_review_status" not in chunk_columns:
        op.add_column("knowledge_chunks", sa.Column("formula_review_status", sa.String(20), nullable=True))
        op.create_index("ix_knowledge_chunks_formula_review_status", "knowledge_chunks", ["formula_review_status"])
    if "formula_review_note" not in chunk_columns:
        op.add_column("knowledge_chunks", sa.Column("formula_review_note", sa.String(1000), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    chunk_columns = {column["name"] for column in inspector.get_columns("knowledge_chunks")}
    document_columns = {column["name"] for column in inspector.get_columns("knowledge_documents")}
    if "formula_review_status" in chunk_columns:
        op.drop_index("ix_knowledge_chunks_formula_review_status", table_name="knowledge_chunks")
        op.drop_column("knowledge_chunks", "formula_review_status")
    if "formula_review_note" in chunk_columns:
        op.drop_column("knowledge_chunks", "formula_review_note")
    if "document_role" in document_columns:
        op.drop_index("ix_knowledge_documents_document_role", table_name="knowledge_documents")
        op.drop_column("knowledge_documents", "document_role")
    if "chapter" in document_columns:
        op.drop_index("ix_knowledge_documents_chapter", table_name="knowledge_documents")
        op.drop_column("knowledge_documents", "chapter")
