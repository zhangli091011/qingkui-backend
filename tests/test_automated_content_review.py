import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AuditLog,
    ContentAutoReview,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeNode,
    KnowledgeNodeVersion,
    KnowledgeSource,
)
from app.services.automated_content_review import auto_review_launch_content


def test_auto_review_improves_only_inactive_draft_and_keeps_human_gate(client) -> None:
    suffix = uuid.uuid4().hex
    document_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    node_id = f"auto_review_{suffix}"
    chapter = f"自动预审章节-{suffix}"
    uri = f"oss://auto-review/{suffix}.md"
    with SessionLocal() as db:
        db.add(
            KnowledgeDocument(
                id=document_id,
                title="自动预审数学讲义",
                subject="数学",
                grade="高一",
                textbook_version="人教A版",
                chapter=chapter,
                document_role="teacher_guide",
                source_type="local_file",
                source_uri=uri,
                authorization_status="self_owned",
                checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                status="indexed",
                document_metadata={},
            )
        )
        db.add(
            KnowledgeSource(
                id=source_id,
                title="自动预审数学讲义",
                publisher="青葵计划",
                edition="人教A版",
                location=uri,
                authorization_status="self_owned",
            )
        )
        db.add(
            KnowledgeNode(
                id=node_id,
                name="集合相等",
                subject="数学",
                grade="高一",
                textbook_version="人教A版",
                chapter=chapter,
                definition="集合相等是待审核概念。",
                explanation="待审核：由正文结构自动抽取。",
                common_errors=[],
                question_types=[],
                source_id=source_id,
                source_excerpt="自动预审数学讲义第 1 节",
                review_status="draft",
                is_active=False,
            )
        )
        db.add(
            KnowledgeChunk(
                id=str(uuid.uuid4()),
                document_id=document_id,
                sequence=0,
                content="知识点 集合相等。两个集合所含元素完全相同，则两个集合相等。易错点：只比较元素个数。题型：判断两个集合是否相等。",
                content_type="text",
                char_start=0,
                char_end=60,
            )
        )
        db.commit()

        summary, results = auto_review_launch_content(
            db,
            subject="数学",
            grade="高一",
            textbook_version="人教A版",
            chapter=chapter,
            workers=1,
            apply_drafts=True,
        )
        db.commit()

        node = db.get(KnowledgeNode, node_id)
        assert summary.auto_applied == 1
        assert results[0]["status"] == "applied_to_draft"
        assert node is not None
        assert node.version == 2
        assert node.review_status == "draft"
        assert node.is_active is False
        assert "待审核" not in node.definition + node.explanation
        assert node.common_errors and node.question_types
        record = db.scalar(select(ContentAutoReview).where(ContentAutoReview.node_id == node_id))
        version = db.scalar(select(KnowledgeNodeVersion).where(KnowledgeNodeVersion.node_id == node_id))
        audit = db.scalar(select(AuditLog).where(AuditLog.target_id == node_id))
        assert record is not None and record.status == "applied_to_draft"
        assert version is not None and version.status == "draft"
        assert version.snapshot["review_status"] == "draft"
        assert version.snapshot["is_active"] is False
        assert audit is not None and audit.details["published"] is False
