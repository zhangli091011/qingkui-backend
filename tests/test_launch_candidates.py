import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeNode
from app.services.launch_candidates import materialize_launch_candidates


def test_materialize_launch_candidates_stays_in_draft() -> None:
    document_id = str(uuid.uuid4())
    with SessionLocal() as db:
        document = KnowledgeDocument(
            id=document_id,
            title="第01讲 1.1集合的概念（教师版）",
            subject="数学",
            grade="高一",
            textbook_version="人教A版",
            chapter="1.1 集合的概念",
            document_role="teacher_guide",
            source_type="local_file",
            source_uri=f"oss://test/{document_id}.docx",
            authorization_status="self_owned",
            checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="indexed",
            document_metadata={"document_family": f"candidate-test-{document_id}"},
        )
        db.add(document)
        db.add(
            KnowledgeChunk(
                document_id=document_id,
                sequence=0,
                content=(
                    "第01讲 1.1集合的概念\n"
                    "知识点01：集合的含义\n"
                    "一般地，把研究对象统称为元素，把一些元素组成的总体叫做集合。\n"
                    "题型01 判断元素能否构成集合\n"
                    "根据集合元素的确定性逐项判断给定对象能否构成集合。"
                ),
                content_type="text",
                char_start=0,
                char_end=100,
            )
        )
        db.commit()
        summary = materialize_launch_candidates(
            db,
            subject="数学",
            grade="高一",
            textbook_version="人教A版",
            limit=10,
        )
        assert summary.nodes_created >= 3
        nodes = list(
            db.scalars(
                select(KnowledgeNode).where(KnowledgeNode.source_excerpt.contains("片段"))
            )
        )
        matching = [node for node in nodes if node.chapter == "1.1 集合的概念"]
        assert matching
        assert all(node.review_status == "draft" and not node.is_active for node in matching)
        assert all("待审核" in node.explanation for node in matching)
