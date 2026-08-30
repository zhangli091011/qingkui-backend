"""Add explicit knowledge-node section metadata."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0017"
down_revision = "20260830_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("knowledge_nodes")}
    if "section" not in columns:
        op.add_column("knowledge_nodes", sa.Column("section", sa.String(120), nullable=False, server_default="本章知识点"))
    else:
        op.execute(sa.text("UPDATE knowledge_nodes SET section = '本章知识点' WHERE section IS NULL OR trim(section) = ''"))
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("knowledge_nodes")}
    if "ix_knowledge_nodes_section" not in indexes:
        op.create_index("ix_knowledge_nodes_section", "knowledge_nodes", ["section"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("knowledge_nodes")}
    if "ix_knowledge_nodes_section" in indexes:
        op.drop_index("ix_knowledge_nodes_section", table_name="knowledge_nodes")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("knowledge_nodes")}
    if "section" in columns:
        op.drop_column("knowledge_nodes", "section")
