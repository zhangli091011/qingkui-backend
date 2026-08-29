"""Add subject routing metadata to documents and conversations."""

from alembic import op
import sqlalchemy as sa


revision = "20260829_0004"
down_revision = "20260828_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    document_columns = {column["name"] for column in inspector.get_columns("knowledge_documents")}
    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "subject" not in document_columns:
        op.add_column("knowledge_documents", sa.Column("subject", sa.String(40), nullable=True))
        op.create_index("ix_knowledge_documents_subject", "knowledge_documents", ["subject"])
    if "grade" not in document_columns:
        op.add_column("knowledge_documents", sa.Column("grade", sa.String(40), nullable=True))
        op.create_index("ix_knowledge_documents_grade", "knowledge_documents", ["grade"])
    if "textbook_version" not in document_columns:
        op.add_column("knowledge_documents", sa.Column("textbook_version", sa.String(80), nullable=True))
    if "subject" not in conversation_columns:
        op.add_column("conversations", sa.Column("subject", sa.String(40), nullable=True))
        op.create_index("ix_conversations_subject", "conversations", ["subject"])
    # Existing imported material is the user's high-school mathematics library.
    bind.execute(sa.text("UPDATE knowledge_documents SET subject = '数学' WHERE subject IS NULL"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "subject" in {column["name"] for column in inspector.get_columns("conversations")}:
        op.drop_index("ix_conversations_subject", table_name="conversations")
        op.drop_column("conversations", "subject")
    document_columns = {column["name"] for column in inspector.get_columns("knowledge_documents")}
    for name, index in (("textbook_version", None), ("grade", "ix_knowledge_documents_grade"), ("subject", "ix_knowledge_documents_subject")):
        if name in document_columns:
            if index:
                op.drop_index(index, table_name="knowledge_documents")
            op.drop_column("knowledge_documents", name)
