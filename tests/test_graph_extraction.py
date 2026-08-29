from pathlib import Path

from app.db import SessionLocal
from app.services.documents import import_document
from app.services.metadata import extract_graph_relations, materialize_graph_nodes


def test_graph_build_materializes_topics_and_edges(tmp_path: Path):
    first = tmp_path / "chemistry-notes-a.txt"
    second = tmp_path / "chemistry-notes-b.txt"
    first.write_text("化学键连接原子。化学平衡需要掌握反应速率基础。", encoding="utf-8")
    second.write_text("化学键决定物质结构。化学平衡与反应速率有关，常见题型需要辨析。", encoding="utf-8")

    with SessionLocal() as db:
        import_document(db, first, "self_owned", subject="化学")
        import_document(db, second, "self_owned", subject="化学")
        nodes_created = materialize_graph_nodes(db, min_document_support=2)
        assert nodes_created >= 2

        summary = extract_graph_relations(db)
        assert summary.nodes_created == 0
        assert summary.candidates >= 1
