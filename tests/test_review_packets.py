import uuid
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AuditLog,
    EdgeType,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeVersion,
    KnowledgeSource,
    User,
    UserRole,
)
from app.services.review_packets import (
    PACKET_SCHEMA,
    apply_content_review_packet,
    build_content_review_packet,
    render_content_review_markdown,
)


def _review_fixture(client, *, publishable: bool = True):
    suffix = uuid.uuid4().hex
    username = f"reviewer_{suffix[:16]}"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "review-pass-123", "nickname": "内容审核员"},
    )
    assert response.status_code == 201
    source_id = str(uuid.uuid4())
    node_id = f"review_apply_{suffix}"
    related_id = f"review_related_{suffix}"
    with SessionLocal() as db:
        reviewer = db.scalar(select(User).where(User.username == username))
        assert reviewer is not None
        reviewer.role = UserRole.content_admin
        db.add(
            KnowledgeSource(
                id=source_id,
                title="人工审核测试教材",
                publisher="青葵计划",
                edition="人教A版",
                location=f"oss://review-apply/{suffix}.md",
                authorization_status="self_owned",
            )
        )
        db.add_all(
            [
                KnowledgeNode(
                    id=node_id,
                    name="审核回写测试知识点",
                    subject="数学",
                    grade="高一",
                    textbook_version="人教A版",
                    chapter="第一章 集合",
                    definition="这是审核前的知识点定义，内容长度符合基础要求。",
                    explanation="这是审核前的知识点解释，用于确认人工审核回写前后的版本变化和审计记录。",
                    common_errors=["容易忽略定义域条件"],
                    question_types=["根据定义判断命题"],
                    source_id=source_id,
                    source_excerpt="人工审核测试教材第 1 页",
                    review_status="draft",
                    is_active=False,
                ),
                KnowledgeNode(
                    id=related_id,
                    name="审核回写关联知识点",
                    subject="数学",
                    grade="高一",
                    textbook_version="人教A版",
                    chapter="第一章 集合",
                    definition="用于建立审核测试所需知识关系的关联节点。",
                    explanation="该节点只为测试发布门中的知识关系要求，不参与当前人工审核包的内容回写。",
                    common_errors=["混淆关联节点"],
                    question_types=["关系判断"],
                    source_id=source_id,
                    source_excerpt="人工审核测试教材第 2 页",
                    review_status="approved",
                    is_active=True,
                ),
            ]
        )
        db.flush()
        if publishable:
            db.add(
                KnowledgeEdge(
                    source_node_id=node_id,
                    target_node_id=related_id,
                    edge_type=EdgeType.related,
                    explanation="人工审核发布门关系测试",
                )
            )
        db.commit()
    return username, node_id


def _completed_packet(node: KnowledgeNode, reviewer: str, decision: str = "approved") -> dict:
    return {
        "schema": PACKET_SCHEMA,
        "scope": {
            "subject": node.subject,
            "grade": node.grade,
            "textbook_version": node.textbook_version,
            "chapter": node.chapter,
        },
        "items": [
            {
                "node": {
                    "id": node.id,
                    "version": node.version,
                    "definition": "人工审核后的定义完整、准确，并且已经对照正式来源逐项核验。",
                    "explanation": "人工审核后的解释包含适用条件、核心含义和必要边界，满足正式内容发布所需的完整性要求。",
                    "common_errors": ["容易忽略题目给出的适用范围"],
                    "question_types": ["根据定义与适用条件判断结论"],
                },
                "review": {
                    "status": "completed",
                    "reviewer": reviewer,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "decision": decision,
                    "notes": "已逐项对照正式教材原文完成审核",
                },
            }
        ],
    }


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


def test_completed_review_packet_publishes_and_records_version_and_audit(client) -> None:
    username, node_id = _review_fixture(client)
    with SessionLocal() as db:
        reviewer = db.scalar(select(User).where(User.username == username))
        node = db.get(KnowledgeNode, node_id)
        assert reviewer is not None and node is not None
        summary = apply_content_review_packet(
            db,
            _completed_packet(node, username),
            reviewer=reviewer,
            publish=True,
        )
        db.commit()

        assert summary.as_dict() == {
            "completed": 1,
            "approved": 1,
            "rejected": 0,
            "changes_requested": 0,
            "published": 1,
            "blocked": {},
            "skipped_pending": 0,
            "stale": [],
            "errors": {},
        }
        db.refresh(node)
        assert node.version == 2
        assert node.review_status == "approved"
        assert node.is_active is True
        assert node.definition.startswith("人工审核后的定义")
        version = db.scalar(
            select(KnowledgeNodeVersion).where(
                KnowledgeNodeVersion.node_id == node_id,
                KnowledgeNodeVersion.version == 2,
            )
        )
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.target_id == node_id,
                AuditLog.action == "knowledge_node.review_approved",
            )
        )
        assert version is not None and version.status == "published"
        assert version.snapshot["review_status"] == "approved"
        assert audit is not None and audit.details["published"] is True


def test_approved_review_with_blockers_remains_an_inactive_draft(client) -> None:
    username, node_id = _review_fixture(client, publishable=False)
    with SessionLocal() as db:
        reviewer = db.scalar(select(User).where(User.username == username))
        node = db.get(KnowledgeNode, node_id)
        assert reviewer is not None and node is not None
        summary = apply_content_review_packet(
            db,
            _completed_packet(node, username),
            reviewer=reviewer,
            publish=True,
        )
        db.commit()

        db.refresh(node)
        assert summary.published == 0
        assert summary.blocked[node_id] == ["缺少知识关系"]
        assert node.review_status == "draft"
        assert node.is_active is False
        assert node.version == 2


def test_rejected_and_changes_requested_reviews_are_versioned(client) -> None:
    rejected_username, rejected_id = _review_fixture(client)
    changes_username, changes_id = _review_fixture(client)
    with SessionLocal() as db:
        rejected_reviewer = db.scalar(select(User).where(User.username == rejected_username))
        changes_reviewer = db.scalar(select(User).where(User.username == changes_username))
        rejected = db.get(KnowledgeNode, rejected_id)
        changes = db.get(KnowledgeNode, changes_id)
        assert rejected_reviewer is not None and changes_reviewer is not None
        assert rejected is not None and changes is not None

        rejected_summary = apply_content_review_packet(
            db,
            _completed_packet(rejected, rejected_username, "rejected"),
            reviewer=rejected_reviewer,
        )
        changes_summary = apply_content_review_packet(
            db,
            _completed_packet(changes, changes_username, "changes_requested"),
            reviewer=changes_reviewer,
        )
        db.commit()

        assert rejected_summary.rejected == 1
        assert rejected.review_status == "archived" and rejected.is_active is False
        assert changes_summary.changes_requested == 1
        assert changes.review_status == "draft" and changes.is_active is False
        statuses = dict(
            db.execute(
                select(KnowledgeNodeVersion.node_id, KnowledgeNodeVersion.status).where(
                    KnowledgeNodeVersion.node_id.in_([rejected_id, changes_id])
                )
            ).all()
        )
        assert statuses == {rejected_id: "withdrawn", changes_id: "changes_requested"}


def test_stale_invalid_and_duplicate_reviews_do_not_mutate_nodes(client) -> None:
    username, node_id = _review_fixture(client)
    with SessionLocal() as db:
        reviewer = db.scalar(select(User).where(User.username == username))
        node = db.get(KnowledgeNode, node_id)
        assert reviewer is not None and node is not None
        original = (node.version, node.definition, node.review_status, node.is_active)

        stale_packet = _completed_packet(node, username)
        stale_packet["items"][0]["node"]["version"] = node.version + 1
        stale = apply_content_review_packet(db, stale_packet, reviewer=reviewer, publish=True)
        assert stale.stale == [node_id]

        invalid_packet = _completed_packet(node, username)
        invalid_packet["items"][0]["review"]["reviewed_at"] = "2026-08-31T12:00:00"
        invalid = apply_content_review_packet(db, invalid_packet, reviewer=reviewer, publish=True)
        assert "timezone" in invalid.errors[node_id]

        duplicate_packet = _completed_packet(node, username)
        duplicate_packet["items"].append(deepcopy(duplicate_packet["items"][0]))
        duplicate = apply_content_review_packet(db, duplicate_packet, reviewer=reviewer, publish=True)
        assert duplicate.errors[node_id] == "node appears more than once in the packet"

        db.flush()
        db.refresh(node)
        assert (node.version, node.definition, node.review_status, node.is_active) == original
        assert db.scalar(
            select(KnowledgeNodeVersion).where(KnowledgeNodeVersion.node_id == node_id)
        ) is None
        assert db.scalar(select(AuditLog).where(AuditLog.target_id == node_id)) is None


def test_review_packet_service_rejects_non_admin_reviewer(client) -> None:
    username, node_id = _review_fixture(client)
    with SessionLocal() as db:
        reviewer = db.scalar(select(User).where(User.username == username))
        node = db.get(KnowledgeNode, node_id)
        assert reviewer is not None and node is not None
        reviewer.role = UserRole.student

        with pytest.raises(ValueError, match="active admin or content_admin"):
            apply_content_review_packet(
                db,
                _completed_packet(node, username),
                reviewer=reviewer,
                publish=True,
            )

        assert node.version == 1
        assert node.review_status == "draft"
        assert node.is_active is False
