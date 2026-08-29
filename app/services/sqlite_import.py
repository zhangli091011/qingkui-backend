"""Import static knowledge content from a SQLite snapshot into PostgreSQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import Base, engine


KNOWLEDGE_TABLES = (
    "knowledge_sources",
    "knowledge_documents",
    "knowledge_chunks",
    "knowledge_nodes",
    "knowledge_node_versions",
    "knowledge_edges",
)

CONFLICT_KEYS = {
    "knowledge_node_versions": ("node_id", "version"),
    "knowledge_edges": ("source_node_id", "target_node_id", "edge_type"),
}


def _coerce_value(column: Any, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    # SQLite stores JSON columns as text. Passing the decoded object lets the
    # PostgreSQL JSON codec preserve the original structure rather than a JSON string.
    if getattr(column.type, "__visit_name__", "") == "JSON" and isinstance(value, str):
        return json.loads(value)
    return value


def import_sqlite_knowledge(snapshot_path: str | Path, *, batch_size: int = 250) -> None:
    """Upsert the knowledge corpus only, leaving users and learning data intact."""
    path = Path(snapshot_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"SQLite snapshot does not exist: {path}")
    if engine.dialect.name != "postgresql":
        raise RuntimeError("The target database must be PostgreSQL")

    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    try:
        with engine.begin() as target:
            for table_name in KNOWLEDGE_TABLES:
                table = Base.metadata.tables[table_name]
                columns = {column.name: column for column in table.columns}
                primary_keys = [column.name for column in table.primary_key.columns]
                conflict_keys = list(CONFLICT_KEYS.get(table_name, tuple(primary_keys)))
                imported = 0
                cursor = source.execute(f"SELECT * FROM {table_name}")
                while rows := cursor.fetchmany(batch_size):
                    batch = [
                        {
                            name: _coerce_value(columns[name], value)
                            for name, value in dict(row).items()
                            if name in columns
                        }
                        for row in rows
                    ]
                    statement = pg_insert(table).values(batch)
                    updates = {
                        column.name: getattr(statement.excluded, column.name)
                        for column in table.columns
                        if column.name not in primary_keys
                    }
                    target.execute(statement.on_conflict_do_update(index_elements=conflict_keys, set_=updates))
                    imported += len(batch)
                print(f"IMPORT {table_name}: {imported}", flush=True)
    finally:
        source.close()
