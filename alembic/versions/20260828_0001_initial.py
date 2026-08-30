"""Initial Qingkui schema.

Revision ID: 20260828_0001
Revises:
"""
from alembic import op

from app.db import Base
from app import models  # noqa: F401


revision = "20260828_0001"
down_revision = None
branch_labels = None
depends_on = None

INITIAL_TABLES = (
    "users",
    "refresh_sessions",
    "knowledge_sources",
    "knowledge_nodes",
    "knowledge_edges",
    "user_knowledge_states",
    "conversations",
    "messages",
    "learning_events",
    "credit_accounts",
    "credit_ledger",
    "feedback_submissions",
    "audit_logs",
)


def upgrade() -> None:
    # Freeze the first revision to the tables that existed when it was
    # released. Using every table in current ORM metadata makes fresh installs
    # create future tables before their own migrations run.
    Base.metadata.create_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in INITIAL_TABLES],
    )


def downgrade() -> None:
    Base.metadata.drop_all(
        bind=op.get_bind(),
        tables=[Base.metadata.tables[name] for name in reversed(INITIAL_TABLES)],
    )
