from app.db import SessionLocal
import uuid

from sqlalchemy import select

from app.models import AuditLog, KnowledgeChunk, KnowledgeDocument, User, UserRole
from app.services.content_governance import governance_report


def _admin_headers(client) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={
            "username": "content_admin_01",
            "password": "content-admin-pass-123",
            "nickname": "内容管理员",
        },
    )
    if response.status_code == 409:
        response = client.post(
            "/api/auth/login",
            json={"username": "content_admin_01", "password": "content-admin-pass-123"},
        )
    assert response.status_code in (200, 201)
    payload = response.json()
    with SessionLocal() as db:
        user = db.get(User, payload["user"]["id"])
        user.role = UserRole.content_admin
        db.commit()
    return {"Authorization": f"Bearer {payload['access_token']}"}


def _node_payload(node_id: str, source_id: str, name: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "subject": "数学",
        "grade": "高一",
        "textbook_version": "人教A版",
        "chapter": "第一章 集合与常用逻辑用语",
        "definition": f"{name}是本测试使用的完整数学定义，用于验证内容发布门。",
        "explanation": f"这里给出{name}的适用范围、推理依据和学习路径，内容长度满足正式审核要求。",
        "common_errors": ["忽略定义域或边界条件"],
        "question_types": ["概念辨析", "综合应用"],
        "source_id": source_id,
        "source_excerpt": "自有课程讲义第一章第 1 节",
        "review_status": "draft",
    }


def test_publication_gate_and_governance_report(client) -> None:
    headers = _admin_headers(client)
    demo_source = client.post(
        "/api/admin/knowledge/sources",
        headers=headers,
        json={
            "title": "内部演示来源",
            "location": "测试",
            "authorization_status": "internal_demo",
        },
    )
    assert demo_source.status_code == 201
    demo_node = client.post(
        "/api/admin/knowledge/nodes",
        headers=headers,
        json=_node_payload("governance_demo_node", demo_source.json()["id"], "演示节点"),
    )
    assert demo_node.status_code == 201
    blocked = client.post(
        "/api/admin/knowledge/nodes/governance_demo_node/publish",
        headers=headers,
        json={"change_note": "不应发布"},
    )
    assert blocked.status_code == 409
    assert "来源未取得正式发布授权" in blocked.json()["detail"]["blockers"]

    source = client.post(
        "/api/admin/knowledge/sources",
        headers=headers,
        json={
            "title": "青葵自有高一数学讲义",
            "publisher": "青葵计划",
            "edition": "2026",
            "location": "第一章",
            "authorization_status": "self_owned",
        },
    )
    assert source.status_code == 201
    left = client.post(
        "/api/admin/knowledge/nodes",
        headers=headers,
        json=_node_payload("governance_left", source.json()["id"], "集合的含义"),
    )
    right = client.post(
        "/api/admin/knowledge/nodes",
        headers=headers,
        json=_node_payload("governance_right", source.json()["id"], "集合的表示"),
    )
    assert left.status_code == right.status_code == 201
    edge = client.post(
        "/api/admin/knowledge/edges",
        headers=headers,
        json={
            "source_node_id": "governance_left",
            "target_node_id": "governance_right",
            "edge_type": "prerequisite",
            "explanation": "理解集合含义后才能准确使用集合表示法。",
        },
    )
    assert edge.status_code == 201
    published = client.post(
        "/api/admin/knowledge/nodes/governance_left/publish",
        headers=headers,
        json={"change_note": "治理测试发布"},
    )
    assert published.status_code == 200
    assert published.json()["review_status"] == "approved"

    report = client.get(
        "/api/admin/knowledge/governance",
        headers=headers,
        params={"subject": "数学", "grade": "高一", "textbook_version": "人教A版"},
    )
    assert report.status_code == 200
    payload = report.json()
    assert payload["nodes"]["total"] >= 3
    assert payload["nodes"]["publishable"] >= 2
    assert payload["relations"]["prerequisite"] >= 1

    queue = client.get(
        "/api/admin/knowledge/review-queue",
        headers=headers,
        params={"candidate_only": "false", "review_status": "draft", "q": "集合"},
    )
    assert queue.status_code == 200
    queue_payload = queue.json()
    assert queue_payload["total"] >= 1
    assert all("blockers" in item and "source_title" in item for item in queue_payload["items"])

    detail = client.get("/api/admin/knowledge/nodes/governance_right", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["name"] == "集合的表示"


def test_formula_review_queue_and_audit_log(client) -> None:
    headers = _admin_headers(client)
    document_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(
            KnowledgeDocument(
                id=document_id,
                title="公式审核测试文档",
                subject="数学",
                grade="高一",
                textbook_version="人教A版",
                chapter="函数",
                document_role="notes",
                source_type="local_file",
                source_uri=f"oss://test/{document_id}.md",
                authorization_status="self_owned",
                checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                status="indexed",
                document_metadata={},
            )
        )
        db.add(
            KnowledgeChunk(
                id=chunk_id,
                document_id=document_id,
                sequence=0,
                content=r"f'(x)=1/x",
                content_type="formula",
                formula_latex=r"f'(x)=1/x",
                formula_source="ocr",
                ocr_confidence=0.61,
                formula_review_status="pending",
                char_start=0,
                char_end=11,
            )
        )
        db.commit()

    queue = client.get(
        "/api/admin/knowledge/formulas",
        headers=headers,
        params={"review_status": "pending", "subject": "数学", "confidence_max": 0.7},
    )
    assert queue.status_code == 200
    assert any(item["id"] == chunk_id for item in queue.json()["items"])

    reviewed = client.patch(
        f"/api/admin/knowledge/formulas/{chunk_id}",
        headers=headers,
        json={
            "review_status": "approved",
            "formula_latex": r"f'(x)=\frac{1}{x}",
            "review_note": "已对照原始页面",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == "approved"
    assert reviewed.json()["formula_latex"] == r"f'(x)=\frac{1}{x}"
    with SessionLocal() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "knowledge_formula.reviewed",
                AuditLog.target_id == chunk_id,
            )
        )
        assert audit is not None
        assert audit.details["formula_updated"] is True


def test_governance_document_gate_is_limited_to_launch_scope(client) -> None:
    in_scope_id = str(uuid.uuid4())
    out_of_scope_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add_all(
            [
                KnowledgeDocument(
                    id=in_scope_id,
                    title="历史首发候选",
                    subject="历史",
                    grade="高一",
                    textbook_version="人教A版",
                    chapter=None,
                    document_role="notes",
                    source_type="local_file",
                    source_uri=f"oss://test/{in_scope_id}.md",
                    authorization_status="self_owned",
                    checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                    status="indexed",
                    document_metadata={},
                ),
                KnowledgeDocument(
                    id=out_of_scope_id,
                    title="历史通用参考",
                    subject="历史",
                    grade="高二",
                    textbook_version="通用课程",
                    chapter=None,
                    document_role=None,
                    source_type="wikibooks_api",
                    source_uri=f"https://example.test/{out_of_scope_id}",
                    authorization_status="self_owned",
                    checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                    status="indexed",
                    document_metadata={},
                ),
            ]
        )
        db.add(
            KnowledgeChunk(
                document_id=out_of_scope_id,
                sequence=0,
                content="待审核公式",
                content_type="formula",
                formula_review_status="pending",
                char_start=0,
                char_end=5,
            )
        )
        db.commit()

        report = governance_report(
            db,
            subject="历史",
            grade="高一",
            textbook_version="人教A版",
        )

    assert report["documents"]["total_for_subject"] == 2
    assert report["documents"]["total_in_launch_scope"] == 1
    assert report["documents"]["missing_metadata"] == 1
    assert report["documents"]["pending_formula_review"] == 0


def test_source_document_management_and_graph_integrity(client) -> None:
    headers = _admin_headers(client)
    source = client.post(
        "/api/admin/knowledge/sources",
        headers=headers,
        json={
            "title": "图谱治理自有来源",
            "publisher": "青葵计划",
            "location": "第一章",
            "authorization_status": "self_owned",
        },
    )
    assert source.status_code == 201
    source_id = source.json()["id"]
    sources = client.get(
        "/api/admin/knowledge/sources",
        headers=headers,
        params={"q": "图谱治理"},
    )
    assert sources.status_code == 200
    assert [item["id"] for item in sources.json()] == [source_id]
    updated_source = client.patch(
        f"/api/admin/knowledge/sources/{source_id}",
        headers=headers,
        json={"edition": "2026 审核版"},
    )
    assert updated_source.status_code == 200
    assert updated_source.json()["edition"] == "2026 审核版"

    document_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(
            KnowledgeDocument(
                id=document_id,
                title="文档管理测试讲义",
                subject="数学",
                grade="高一",
                textbook_version="人教A版",
                chapter="旧章节",
                document_role="notes",
                source_type="local_file",
                source_uri=f"oss://test/{document_id}.md",
                authorization_status="self_owned",
                checksum_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                status="indexed",
                document_metadata={"chapter": "旧章节"},
            )
        )
        db.add_all(
            [
                KnowledgeChunk(
                    document_id=document_id,
                    sequence=0,
                    content="集合的基本概念",
                    content_type="text",
                    char_start=0,
                    char_end=7,
                ),
                KnowledgeChunk(
                    document_id=document_id,
                    sequence=1,
                    content=r"A\subseteq B",
                    content_type="formula",
                    formula_latex=r"A\subseteq B",
                    formula_source="ocr",
                    formula_review_status="pending",
                    char_start=8,
                    char_end=20,
                ),
            ]
        )
        db.commit()

    documents = client.get(
        "/api/admin/knowledge/documents",
        headers=headers,
        params={"q": "文档管理测试", "subject": "数学"},
    )
    assert documents.status_code == 200
    listed = documents.json()["items"]
    assert len(listed) == 1
    assert listed[0]["chunk_count"] == 2
    assert listed[0]["formula_count"] == 1
    assert listed[0]["pending_formula_count"] == 1

    updated_document = client.patch(
        f"/api/admin/knowledge/documents/{document_id}",
        headers=headers,
        json={"chapter": "第一章 集合", "document_role": "teacher_guide"},
    )
    assert updated_document.status_code == 200
    assert updated_document.json()["chapter"] == "第一章 集合"
    assert updated_document.json()["status"] == "text_ready"
    assert updated_document.json()["document_metadata"]["document_role"] == "teacher_guide"

    node_ids = ["integrity_duplicate_a", "integrity_duplicate_b", "integrity_orphan"]
    for node_id in node_ids:
        created = client.post(
            "/api/admin/knowledge/nodes",
            headers=headers,
            json=_node_payload(node_id, source_id, "重复概念" if "duplicate" in node_id else "孤立概念"),
        )
        assert created.status_code == 201
    forward = client.post(
        "/api/admin/knowledge/edges",
        headers=headers,
        json={
            "source_node_id": node_ids[0],
            "target_node_id": node_ids[1],
            "edge_type": "prerequisite",
            "explanation": "完整性测试前向关系",
        },
    )
    reverse = client.post(
        "/api/admin/knowledge/edges",
        headers=headers,
        json={
            "source_node_id": node_ids[1],
            "target_node_id": node_ids[0],
            "edge_type": "prerequisite",
            "explanation": "完整性测试反向关系",
        },
    )
    assert forward.status_code == reverse.status_code == 201
    integrity = client.get(
        "/api/admin/knowledge/integrity",
        headers=headers,
        params={"subject": "数学"},
    )
    assert integrity.status_code == 200
    report = integrity.json()
    assert node_ids[2] in report["orphaned_node_ids"]
    assert node_ids[:2] in report["duplicate_node_groups"]
    assert [node_ids[0], node_ids[1], node_ids[0]] in report["prerequisite_cycles"]

    deleted = client.delete(
        f"/api/admin/knowledge/edges/{reverse.json()['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204
    with SessionLocal() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "knowledge_document.updated",
                AuditLog.target_id == document_id,
            )
        )
        assert audit is not None
        assert audit.details["requires_reindex"] is True


def test_admin_page_exposes_review_workspaces(client) -> None:
    response = client.get("/admin")

    assert response.status_code == 200
    assert "知识候选审核队列" in response.text
    assert "公式审核队列" in response.text
    assert "来源与文档" in response.text
    assert "图谱完整性" in response.text
    assert "用户与权限" in response.text
    assert "OCR 任务审核" in response.text
    assert "错题内容审核" in response.text
    assert "投稿审核" in response.text
    assert "学校、班级与试点准入" in response.text
    assert "活动额度" in response.text
    assert "运行告警" in response.text
    assert "innerHTML" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
