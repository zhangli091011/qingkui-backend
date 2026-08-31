"""Persist evidence-backed automated content pre-reviews.

Revision ID: 20260901_0024
Revises: 20260901_0023
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260901_0024"
down_revision: str | None = "20260901_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_auto_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(64), sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_version", sa.Integer(), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="generated"),
        sa.Column("rule_review", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("generation", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("critique", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(40), nullable=True),
        sa.Column("model", sa.String(80), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("node_id", "node_version", "prompt_version", name="uq_content_auto_review_run"),
    )
    op.create_index("ix_content_auto_reviews_node_id", "content_auto_reviews", ["node_id"])
    op.create_index("ix_content_auto_reviews_prompt_version", "content_auto_reviews", ["prompt_version"])
    op.create_index("ix_content_auto_reviews_status", "content_auto_reviews", ["status"])
    op.create_index("ix_content_auto_reviews_evidence_sha256", "content_auto_reviews", ["evidence_sha256"])
    op.create_index("ix_content_auto_reviews_created_at", "content_auto_reviews", ["created_at"])


def downgrade() -> None:
    op.drop_table("content_auto_reviews")
