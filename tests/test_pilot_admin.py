from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import (
    Conversation,
    FeedbackSubmission,
    LearningEvent,
    ModelCall,
    User,
    UserRole,
)


def _register(client: TestClient, username: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "pilot-test-password-123", "nickname": username},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['access_token']}"}


def _set_role(user_id: str, role: UserRole) -> None:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        user.role = role
        db.commit()


def test_pilot_metrics_and_export_are_super_admin_only_and_anonymous(client: TestClient) -> None:
    admin, admin_headers = _register(client, "pilot_metrics_admin_01")
    content_admin, content_headers = _register(client, "pilot_metrics_content_01")
    _set_role(admin["user"]["id"], UserRole.admin)
    _set_role(content_admin["user"]["id"], UserRole.content_admin)
    started_at = datetime.now(timezone.utc)
    student, student_headers = _register(client, "pilot_metrics_student_01")

    session = client.post(
        "/api/qa/sessions",
        json={"mode": "explore", "subject": "数学"},
        headers=student_headers,
    ).json()
    answer = client.post(
        f"/api/qa/sessions/{session['id']}/messages",
        json={"content": "二次函数与判别式有什么关系？", "help_level": "approach"},
        headers=student_headers,
    )
    assert answer.status_code == 200
    feedback = client.post(
        "/api/feedback",
        json={
            "category": "other",
            "content": "该回答对本次学习有帮助",
            "message_id": answer.json()["assistant_message"]["id"],
        },
        headers=student_headers,
    )
    assert feedback.status_code == 201
    event = client.patch(
        "/api/learning/nodes/quadratic_function/state",
        json={"status": "understood"},
        headers=student_headers,
    )
    assert event.status_code == 200
    params = {
        "start_at": started_at.isoformat(),
        "end_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    }

    assert client.get("/api/admin/pilot/metrics", params=params, headers=content_headers).status_code == 403
    metrics = client.get("/api/admin/pilot/metrics", params=params, headers=admin_headers)
    assert metrics.status_code == 200, metrics.text
    values = metrics.json()
    assert values["registered_users"] == 1
    assert values["activated_users"] == 1
    assert values["activation_rate"] == 1
    assert values["helpful_votes"] == 1
    assert values["answer_helpfulness_rate"] == 1
    assert values["graph_explorers"] == 1
    assert values["knowledge_state_changes"] >= 1
    assert values["credits_spent"] == 1

    exported = client.get("/api/admin/pilot/export", params=params, headers=admin_headers)
    assert exported.status_code == 200
    body = exported.text
    payload = exported.json()
    assert len(payload["users"]) == 1
    assert payload["users"][0]["anonymous_id"]
    assert payload["users"][0]["assistant_message_count"] == 1
    assert "pilot_metrics_student_01" not in body
    assert "二次函数与判别式" not in body
    assert "该回答对本次学习有帮助" not in body
    assert "nickname" not in payload["users"][0]
    assert "user_id" not in payload["users"][0]


def test_pilot_cleanup_requires_exact_preview_and_erases_personal_data(client: TestClient) -> None:
    admin, admin_headers = _register(client, "pilot_cleanup_admin_01")
    student, student_headers = _register(client, "pilot_cleanup_student_01")
    admin_id = admin["user"]["id"]
    student_id = student["user"]["id"]
    _set_role(admin_id, UserRole.admin)
    session = client.post("/api/qa/sessions", json={"mode": "knowledge"}, headers=student_headers).json()
    answer = client.post(
        f"/api/qa/sessions/{session['id']}/messages",
        json={"content": "解释判别式", "help_level": "approach"},
        headers=student_headers,
    )
    assert answer.status_code == 200
    assert client.post(
        "/api/feedback",
        json={
            "category": "answer_error",
            "content": "该回答没有解决我的问题",
            "message_id": answer.json()["assistant_message"]["id"],
        },
        headers=student_headers,
    ).status_code == 201

    duplicate = client.post(
        "/api/admin/pilot/cleanup/preview",
        json={"user_ids": [student_id, student_id]},
        headers=admin_headers,
    )
    assert duplicate.status_code == 422
    blocked = client.post(
        "/api/admin/pilot/cleanup/preview",
        json={"user_ids": [student_id, admin_id]},
        headers=admin_headers,
    )
    assert blocked.status_code == 200
    assert blocked.json()["eligible_count"] == 1
    assert blocked.json()["blocked_count"] == 1

    preview = client.post(
        "/api/admin/pilot/cleanup/preview",
        json={"user_ids": [student_id]},
        headers=admin_headers,
    )
    assert preview.status_code == 200
    anonymous_id = preview.json()["candidates"][0]["anonymous_id"]
    assert client.post(
        "/api/admin/pilot/cleanup",
        json={"user_ids": [student_id], "expected_count": 1, "confirmation": "wrong"},
        headers=admin_headers,
    ).status_code == 409
    assert client.post(
        "/api/admin/pilot/cleanup",
        json={"user_ids": [student_id], "expected_count": 2, "confirmation": "DELETE_PILOT_DATA"},
        headers=admin_headers,
    ).status_code == 409

    cleaned = client.post(
        "/api/admin/pilot/cleanup",
        json={"user_ids": [student_id], "expected_count": 1, "confirmation": "DELETE_PILOT_DATA"},
        headers=admin_headers,
    )
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json() == {"deleted_count": 1, "anonymous_ids": [anonymous_id]}
    assert client.get("/api/auth/me", headers=student_headers).status_code == 401
    with SessionLocal() as db:
        user = db.get(User, student_id)
        assert user.deleted_at is not None
        assert user.username.startswith("deleted_")
        assert db.scalar(select(func.count()).select_from(Conversation).where(Conversation.user_id == student_id)) == 0
        assert db.scalar(select(func.count()).select_from(FeedbackSubmission).where(FeedbackSubmission.user_id == student_id)) == 0
        assert db.scalar(select(func.count()).select_from(LearningEvent).where(LearningEvent.user_id == student_id)) == 0
        calls = list(db.scalars(select(ModelCall).where(ModelCall.reference_id == answer.json()["assistant_message"]["id"])))
        assert len(calls) == 1
        assert calls[0].user_id is None
