from app.db import SessionLocal
from app.models import User, UserRole


def _admin_headers(client) -> dict[str, str]:
    response = client.post(
        "/api/auth/register",
        json={
            "username": "content_admin_01",
            "password": "content-admin-pass-123",
            "nickname": "内容管理员",
        },
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
