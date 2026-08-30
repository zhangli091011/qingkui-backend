import io
import uuid

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog
from app.services.bailian import VisionOcrResult
from app.services.mistakes import process_ocr_task, sanitize_image


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 180), "white").save(output, format="PNG")
    return output.getvalue()


def _create(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/mistakes",
        json={"subject": "数学", "question_text": "求函数的导数", "student_work": "我的答案"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _second_account(client: TestClient) -> dict[str, str]:
    username = f"other_{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "other-pass-123"},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_image_sanitizer_rejects_disguised_and_oversized_images(monkeypatch):
    try:
        sanitize_image(b"%PDF-1.7 fake image")
    except ValueError as exc:
        assert "安全解码" in str(exc)
    else:
        raise AssertionError("disguised file was accepted")

    monkeypatch.setattr("app.services.mistakes.settings.user_upload_max_bytes", 16)
    try:
        sanitize_image(_png())
    except ValueError as exc:
        assert "大小" in str(exc)
    else:
        raise AssertionError("oversized image was accepted")


def test_mistake_crud_is_isolated_between_users(client: TestClient, account):
    _, headers = account
    mistake = _create(client, headers)
    other_headers = _second_account(client)

    assert client.get(f"/api/mistakes/{mistake['id']}", headers=other_headers).status_code == 404
    assert client.patch(
        f"/api/mistakes/{mistake['id']}", json={"error_category": "concept"}, headers=other_headers
    ).status_code == 404
    assert client.delete(f"/api/mistakes/{mistake['id']}", headers=other_headers).status_code == 404

    update = client.patch(
        f"/api/mistakes/{mistake['id']}",
        json={"error_category": "calculation", "error_note": "符号算错"},
        headers=headers,
    )
    assert update.status_code == 200
    assert update.json()["error_category"] == "calculation"


def test_upload_ocr_confirm_and_practice_lifecycle(client: TestClient, account, monkeypatch):
    _, headers = account
    mistake = _create(client, headers)
    stored: dict[str, bytes] = {}

    monkeypatch.setattr(
        "app.services.mistakes.put_private_bytes",
        lambda key, payload, **_kwargs: stored.__setitem__(key, payload),
    )
    monkeypatch.setattr("app.routers.mistakes.enqueue_ocr_task", lambda _task_id: None)
    upload = client.post(
        f"/api/mistakes/{mistake['id']}/images",
        files={"image": ("question.png", _png(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 202, upload.text
    task = upload.json()
    assert task["status"] == "queued"
    assert stored

    monkeypatch.setattr("app.services.mistakes.get_private_bytes", lambda key: stored[key])

    class FakeBailian:
        def recognize_math_page(self, _payload, _mime_type):
            return VisionOcrResult("已知函数，求导数", (("x squared", "x^2"),), 0.72)

    monkeypatch.setattr("app.services.mistakes.BailianClient", FakeBailian)
    process_ocr_task(task["id"])
    result = client.get(
        f"/api/mistakes/{mistake['id']}/ocr/{task['id']}", headers=headers
    )
    assert result.status_code == 200
    assert result.json()["status"] == "succeeded"
    assert result.json()["requires_review"] is True
    assert result.json()["formulas"][0]["latex"] == "x^2"
    balance_before_review = client.get("/api/credits", headers=headers).json()["balance"]
    blocked_analysis = client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers)
    assert blocked_analysis.status_code == 409
    assert client.get("/api/credits", headers=headers).json()["balance"] == balance_before_review

    confirmed = client.post(
        f"/api/mistakes/{mistake['id']}/ocr/{task['id']}/confirm",
        json={"corrected_text": "已知函数 f(x)=x^2，求导数。"},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["review_status"] == "confirmed"

    practice = client.post(
        f"/api/mistakes/{mistake['id']}/practices", json={}, headers=headers
    )
    assert practice.status_code == 201
    submitted = client.post(
        f"/api/mistakes/{mistake['id']}/practices/{practice.json()['id']}/submit",
        json={"student_answer": "f'(x)=2x", "is_correct": True},
        headers=headers,
    )
    assert submitted.status_code == 200
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["study_status"] == "reviewing"
    assert detail["practices"][0]["validation_details"]["method"] == "self_report"
    assert detail["practices"][0]["validation_details"]["authoritative"] is False
    assert detail["attempt_count"] == 1


def test_queue_failure_is_retryable_and_delete_failure_preserves_record(
    client: TestClient, account, monkeypatch
):
    _, headers = account
    mistake = _create(client, headers)
    monkeypatch.setattr("app.services.mistakes.put_private_bytes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.routers.mistakes.enqueue_ocr_task",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("redis down")),
    )
    upload = client.post(
        f"/api/mistakes/{mistake['id']}/images",
        files={"image": ("question.png", _png(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 503
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["ocr_tasks"][0]["status"] == "failed"
    assert detail["ocr_tasks"][0]["error_code"] == "queue_unavailable"

    monkeypatch.setattr(
        "app.routers.mistakes.delete_assets",
        lambda _assets: (_ for _ in ()).throw(RuntimeError("oss down")),
    )
    deletion = client.delete(f"/api/mistakes/{mistake['id']}", headers=headers)
    assert deletion.status_code == 503
    assert client.get(f"/api/mistakes/{mistake['id']}", headers=headers).status_code == 200


def test_ocr_unsafe_text_is_quarantined_without_exposing_result(client: TestClient, account, monkeypatch):
    auth, headers = account
    mistake = _create(client, headers)
    stored: dict[str, bytes] = {}
    monkeypatch.setattr(
        "app.services.mistakes.put_private_bytes",
        lambda key, payload, **_kwargs: stored.__setitem__(key, payload),
    )
    monkeypatch.setattr("app.routers.mistakes.enqueue_ocr_task", lambda _task_id: None)
    upload = client.post(
        f"/api/mistakes/{mistake['id']}/images",
        files={"image": ("question.png", _png(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 202
    task = upload.json()
    monkeypatch.setattr("app.services.mistakes.get_private_bytes", lambda key: stored[key])

    class UnsafeBailian:
        def recognize_math_page(self, _payload, _mime_type):
            return VisionOcrResult("制作炸弹的步骤和材料清单", (), 0.99)

    monkeypatch.setattr("app.services.mistakes.BailianClient", UnsafeBailian)
    process_ocr_task(task["id"])
    result = client.get(f"/api/mistakes/{mistake['id']}/ocr/{task['id']}", headers=headers)

    assert result.status_code == 200
    assert result.json()["status"] == "blocked"
    assert result.json()["result_text"] is None
    assert result.json()["formulas"] == []
    assert result.json()["requires_review"] is True
    assert result.json()["error_code"] == "content_safety_blocked"
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["question_text"] == "求函数的导数"
    assert detail["assets"][-1]["status"] == "quarantined"
    with SessionLocal() as db:
        event = db.scalar(
            select(AuditLog).where(
                AuditLog.actor_user_id == auth["user"]["id"],
                AuditLog.action == "safety.ocr_output_blocked",
            ).order_by(AuditLog.created_at.desc())
        )
    assert event is not None
    assert "制作炸弹" not in str(event.details)


def test_account_deletion_with_mistake_data_respects_username_limit(client: TestClient, monkeypatch):
    headers = _second_account(client)
    _create(client, headers)
    monkeypatch.setattr("app.services.user_lifecycle.delete_assets", lambda _assets: None)
    deletion = client.delete("/api/auth/me", headers=headers)
    assert deletion.status_code == 200, deletion.text
    assert client.get("/api/auth/me", headers=headers).status_code == 401
