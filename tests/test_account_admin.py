from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import MistakeAsset, OcrTask, User, UserRole


def _register(client: TestClient, username: str, *, device_name: str = "测试设备") -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": "safe-test-password-123",
            "nickname": username,
            "device_name": device_name,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['access_token']}"}


def _set_role(user_id: str, role: UserRole) -> None:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user is not None
        user.role = role
        db.commit()


def test_device_sessions_are_owned_and_revocable(client: TestClient) -> None:
    first, headers = _register(client, "device_owner_01", device_name="平板 A")
    second = client.post(
        "/api/auth/login",
        json={"username": "device_owner_01", "password": "safe-test-password-123", "device_name": "平板 B"},
    )
    assert second.status_code == 200

    sessions = client.get("/api/auth/sessions", headers=headers)
    assert sessions.status_code == 200
    assert {item["device_name"] for item in sessions.json()} >= {"平板 A", "平板 B"}
    second_session_id = next(item["id"] for item in sessions.json() if item["device_name"] == "平板 B")

    _, other_headers = _register(client, "device_other_01")
    assert client.delete(f"/api/auth/sessions/{second_session_id}", headers=other_headers).status_code == 404
    assert client.delete(f"/api/auth/sessions/{second_session_id}", headers=headers).status_code == 200
    assert client.post(
        "/api/auth/refresh", json={"refresh_token": second.json()["refresh_token"]}
    ).status_code == 401
    assert client.post(
        "/api/auth/refresh", json={"refresh_token": first["refresh_token"]}
    ).status_code == 200


def test_feedback_status_is_private_to_owner(client: TestClient) -> None:
    _, headers = _register(client, "feedback_owner_01")
    created = client.post(
        "/api/feedback",
        json={"category": "product_issue", "content": "横屏时按钮显示异常"},
        headers=headers,
    )
    assert created.status_code == 201
    feedback_id = created.json()["id"]
    listed = client.get("/api/feedback", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == feedback_id and item["status"] == "pending" for item in listed.json())

    _, other_headers = _register(client, "feedback_other_01")
    assert client.get(f"/api/feedback/{feedback_id}", headers=other_headers).status_code == 404
    admin_auth, admin_headers = _register(client, "feedback_admin_01")
    _set_role(admin_auth["user"]["id"], UserRole.admin)
    reviewed = client.patch(
        f"/api/admin/feedback/{feedback_id}",
        json={"status": "resolved", "review_note": "已修复横屏布局"},
        headers=admin_headers,
    )
    assert reviewed.status_code == 200
    owner_view = client.get(f"/api/feedback/{feedback_id}", headers=headers)
    assert owner_view.status_code == 200
    assert owner_view.json()["status"] == "resolved"
    assert owner_view.json()["review_note"] == "已修复横屏布局"


def test_sensitive_admin_endpoints_require_full_admin(client: TestClient, monkeypatch) -> None:
    content_auth, content_headers = _register(client, "content_admin_02")
    admin_auth, admin_headers = _register(client, "system_admin_02")
    student_auth, student_headers = _register(client, "managed_student_02")
    _set_role(content_auth["user"]["id"], UserRole.content_admin)
    _set_role(admin_auth["user"]["id"], UserRole.admin)

    assert client.get("/api/admin/users", headers=content_headers).status_code == 403
    assert client.post(
        f"/api/admin/credits/{student_auth['user']['id']}/adjust",
        json={"amount": 10, "reason": "越权测试"},
        headers=content_headers,
    ).status_code == 403
    users = client.get("/api/admin/users", params={"q": "managed_student_02"}, headers=admin_headers)
    assert users.status_code == 200
    assert users.json()[0]["id"] == student_auth["user"]["id"]

    frozen = client.patch(
        f"/api/admin/users/{student_auth['user']['id']}/status",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert frozen.status_code == 200
    assert frozen.json()["is_active"] is False
    assert client.get("/api/auth/me", headers=student_headers).status_code == 401
    assert client.patch(
        f"/api/admin/users/{student_auth['user']['id']}/status",
        json={"is_active": True},
        headers=admin_headers,
    ).status_code == 200
    assert client.patch(
        f"/api/admin/users/{student_auth['user']['id']}/role",
        json={"role": "content_admin"},
        headers=admin_headers,
    ).json()["role"] == "content_admin"
    assert client.patch(
        f"/api/admin/users/{admin_auth['user']['id']}/status",
        json={"is_active": False},
        headers=admin_headers,
    ).status_code == 409
    adjusted = client.post(
        f"/api/admin/credits/{student_auth['user']['id']}/adjust",
        json={"amount": 10, "reason": "测试调整"},
        headers=admin_headers,
    )
    assert adjusted.status_code == 200
    assert adjusted.json()["balance"] == 1290

    mistake = client.post(
        "/api/mistakes",
        json={"subject": "数学", "question_text": "求二次函数顶点"},
        headers=student_headers,
    )
    # The original access token becomes valid again after the account is unfrozen.
    assert mistake.status_code == 201, mistake.text
    mistake_id = mistake.json()["id"]
    with SessionLocal() as db:
        asset = MistakeAsset(
            mistake_id=mistake_id,
            user_id=student_auth["user"]["id"],
            object_key=f"test/{mistake_id}.jpg",
            mime_type="image/jpeg",
            size_bytes=128,
            checksum_sha256="a" * 64,
            width=64,
            height=64,
            status="queued",
        )
        db.add(asset)
        db.flush()
        task = OcrTask(
            mistake_id=mistake_id,
            asset_id=asset.id,
            user_id=student_auth["user"]["id"],
            status="queued",
            queued_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(task)
        db.commit()
        task_id = task.id

    assert client.get("/api/admin/ocr-tasks", headers=content_headers).status_code == 403
    detail = client.get(f"/api/admin/ocr-tasks/{task_id}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["question_text"] == "求二次函数顶点"
    cancelled = client.post(f"/api/admin/ocr-tasks/{task_id}/cancel", headers=admin_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    monkeypatch.setattr("app.routers.admin.enqueue_ocr_task", lambda _task_id: None)
    retried = client.post(f"/api/admin/ocr-tasks/{task_id}/retry", headers=admin_headers)
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"

    mistakes = client.get("/api/admin/mistakes", params={"q": "二次函数"}, headers=admin_headers)
    assert mistakes.status_code == 200
    assert mistakes.json()[0]["id"] == mistake_id
    reviewed = client.patch(
        f"/api/admin/mistakes/{mistake_id}",
        json={"review_status": "approved", "knowledge_node_id": "quadratic_function"},
        headers=admin_headers,
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["link_status"] == "confirmed"
