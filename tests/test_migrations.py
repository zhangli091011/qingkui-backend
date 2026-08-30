from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import settings


def test_full_migration_round_trip_on_empty_database(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{(tmp_path / 'migration-round-trip.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "20260830_0011"
        assert "idempotency_requests" in inspect(connection).get_table_names()
    engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("select version_num from alembic_version")).scalar_one() == "20260830_0011"
        assert "idempotency_requests" in inspect(connection).get_table_names()
    engine.dispose()
