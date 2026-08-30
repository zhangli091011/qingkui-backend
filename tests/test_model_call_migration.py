from datetime import datetime, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import settings


def test_model_call_migration_backfills_existing_assistant_messages(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{(tmp_path / 'model-call-backfill.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "20260830_0008")
    now = datetime.now(timezone.utc)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO users (
                    id, username, email, nickname, password_hash, role,
                    tenant_id, is_active, created_at, deleted_at
                ) VALUES (
                    'migration-user', 'migration_user', NULL, '迁移测试', 'hash',
                    'student', NULL, true, :now, NULL
                )
                """
            ),
            {"now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO conversations (
                    id, user_id, title, mode, subject, knowledge_node_id, created_at, updated_at
                ) VALUES (
                    'migration-conversation', 'migration-user', '迁移会话', 'knowledge',
                    '数学', NULL, :now, :now
                )
                """
            ),
            {"now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO messages (
                    id, conversation_id, role, content, structured_content, citations,
                    linked_node_ids, provider, model, prompt_version, knowledge_version,
                    input_tokens, output_tokens, created_at
                ) VALUES (
                    'migration-message', 'migration-conversation', 'assistant', '历史回答',
                    NULL, '[]', '[]', 'deepseek', 'legacy-model', 'qa-v1', 'rag-v1',
                    120, 30, :now
                )
                """
            ),
            {"now": now},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT user_id, feature, provider, model, success, input_tokens,
                       output_tokens, latency_ms, reference_id
                FROM model_calls WHERE id = 'migration-message'
                """
            )
        ).one()
    engine.dispose()
    assert tuple(row) == (
        "migration-user",
        "qa_legacy",
        "deepseek",
        "legacy-model",
        True,
        120,
        30,
        0,
        "migration-message",
    )
