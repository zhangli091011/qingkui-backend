import uuid

from app.db import SessionLocal
from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeNode, KnowledgeSource
from app.services.review_packets import build_content_review_packet, render_content_review_markdown


def test_review_packet_extracts_traceable_suggestions_without_mutating_node(client) -> None:
    suffix = uuid.uuid4().hex
    document_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    node_id = f"review_packet_{suffix}"
    source_uri = f"oss://review-packet/{suffix}.md"
    chapter = f"审核包测试章节-{suffix}"
    with SessionLocal() as db:
        db.add(
            KnowledgeDocument(
                id=document_id,
                title="青葵审核包测试讲义",
                subject="数学",
                grade="高一",
                textbook_version="人教A版",
                chapter=chapter,
                document_role="teacher_guide",
                source_type="local_file",
                source_uri=source_uri,
                authorization_status="self_owned",
                checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                status="indexed",
                document_metadata={},
            )
        )
        db.add(
            KnowledgeSource(
                id=source_id,
                title="青葵审核包测试讲义",
                publisher="青葵计划",
                edition="人教A版",
                location=source_uri,
                authorization_status="self_owned",
            )
        )
        db.add(
            KnowledgeNode(
                id=node_id,
                name="函数定义域审核示例",
                subject="数学",
                grade="高一",
                textbook_version="人教A版",
                chapter=chapter,
                definition="函数定义域是使解析式有意义的全部自变量取值集合。",
                explanation="待审核：该候选由正文结构自动抽取，必须由内容审核员核对。",
                common_errors=[],
                question_types=[],
                source_id=source_id,
                source_excerpt="《青葵审核包测试讲义》片段 0",
                review_status="draft",
                is_active=False,
            )
        )
        db.add_all(
            [
                KnowledgeChunk(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    sequence=0,
                    content=(
                        "知识点 函数定义域审核示例\n"
                        "易错点：分母含有字母时，容易遗漏分母不为零的条件。\n"
                        "题型1：求根式与分式复合函数的定义域。"
                    ),
                    content_type="text",
                    char_start=0,
                    char_end=80,
                ),
                KnowledgeChunk(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    sequence=1,
                    content=r"x\ne 0",
                    content_type="formula",
                    formula_latex=r"x\ne 0",
                    formula_source="bailian_vision_ocr",
                    ocr_confidence=0.62,
                    formula_review_status="pending",
                    char_start=81,
                    char_end=88,
                ),
            ]
        )
        db.commit()

        packet = build_content_review_packet(
            db,
            subject="数学",
            grade="高一",
            textbook_version="人教A版",
            chapter=chapter,
        )

        assert packet["summary"]["nodes"] == 1
        assert packet["summary"]["nodes_with_evidence"] == 1
        assert packet["summary"]["common_error_suggestions"] == 1
        assert packet["summary"]["question_type_suggestions"] == 1
        assert packet["summary"]["pending_formulas_included"] == 1
        item = packet["items"][0]
        assert item["node"]["id"] == node_id
        assert "遗漏分母不为零" in item["suggested_common_errors"][0]["text"]
        assert "复合函数的定义域" in item["suggested_question_types"][0]["text"]
        assert item["pending_formulas"][0]["confidence"] == 0.62
        assert item["review"]["status"] == "pending"
        assert "不得自动写回或发布" in packet["warning"]

        unchanged = db.get(KnowledgeNode, node_id)
        assert unchanged is not None
        assert unchanged.common_errors == []
        assert unchanged.question_types == []
        assert unchanged.review_status == "draft"
        assert unchanged.is_active is False

    markdown = render_content_review_markdown(packet)
    assert "# 知识节点人工审核包" in markdown
    assert node_id in markdown
    assert "审核结论：`pending`" in markdown
