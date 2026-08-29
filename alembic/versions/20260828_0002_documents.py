"""Add document retrieval tables.

Revision ID: 20260828_0002
Revises: 20260828_0001
"""
from alembic import op
import sqlalchemy as sa


revision = "20260828_0002"
down_revision = "20260828_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial migration creates current metadata on a fresh database, so
    # these guards keep both fresh installs and upgrades from older databases valid.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "knowledge_documents" not in tables:
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("source_type", sa.String(40), nullable=False),
            sa.Column("source_uri", sa.String(1000), nullable=False),
            sa.Column("authorization_status", sa.String(40), nullable=False),
            sa.Column("checksum_sha256", sa.String(64), nullable=False),
            sa.Column("mime_type", sa.String(120), nullable=True),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("document_metadata", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_knowledge_documents_title", "knowledge_documents", ["title"])
        op.create_index("ix_knowledge_documents_authorization_status", "knowledge_documents", ["authorization_status"])
        op.create_index("ix_knowledge_documents_checksum_sha256", "knowledge_documents", ["checksum_sha256"], unique=True)
        op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])
    if "knowledge_chunks" not in tables:
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("document_id", sa.String(36), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("char_start", sa.Integer(), nullable=False),
            sa.Column("char_end", sa.Integer(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=True),
            sa.Column("embedding_model", sa.String(80), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("document_id", "sequence"),
        )
        op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
        op.create_index("ix_knowledge_chunks_embedding_model", "knowledge_chunks", ["embedding_model"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_chunks" in tables:
        op.drop_table("knowledge_chunks")
    if "knowledge_documents" in tables:
        op.drop_table("knowledge_documents")
