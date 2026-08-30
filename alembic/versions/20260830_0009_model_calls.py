"""Record model-call success, token usage and latency."""

from alembic import op
import sqlalchemy as sa


revision = "20260830_0009"
down_revision = "20260830_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("feature", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("reference_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("user_id", "feature", "provider", "model", "success", "error_code", "reference_id", "created_at"):
        op.create_index(f"ix_model_calls_{name}", "model_calls", [name])
    op.execute(
        sa.text(
            """
            INSERT INTO model_calls (
                id, user_id, feature, provider, model, success,
                input_tokens, output_tokens, latency_ms, error_code,
                reference_id, created_at
            )
            SELECT
                m.id, c.user_id, 'qa_legacy',
                COALESCE(m.provider, 'unknown'), COALESCE(m.model, 'unknown'),
                true, m.input_tokens, m.output_tokens, 0, NULL, m.id, m.created_at
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.role = 'assistant'
            """
        )
    )


def downgrade() -> None:
    op.drop_table("model_calls")
