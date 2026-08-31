import io
import json
import uuid

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.db import SessionLocal
from app.models import AuditLog
from app.services.bailian import BailianClient, VisionOcrResult, _extract_embedded_formulas, _merge_formulas
from app.services.documents import classify_ocr_text
from app.services.mistakes import (
    _append_missing_formula_text,
    ocr_review_reasons,
    process_ocr_task,
    sanitize_image,
)


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


def test_embedded_latex_is_extracted_and_deduplicated():
    text = (
        "设 $x \\in \\mathbb{R}$，且 \\(x^2=1\\)。"
        "重复公式为 $$x^2=1$$，命题为 \\[\\forall x \\in A\\]。"
    )
    embedded = _extract_embedded_formulas(text)
    merged = _merge_formulas((("x squared equals one", "x ^ 2 = 1"),), embedded)

    assert [latex for _, latex in embedded] == ["x \\in \\mathbb{R}", "x^2=1", "\\forall x \\in A"]
    assert [latex for _, latex in merged] == ["x ^ 2 = 1", "x \\in \\mathbb{R}", "\\forall x \\in A"]


def test_vision_response_extracts_embedded_formula_and_structure_warning(monkeypatch):
    response_content = json.dumps(
        {
            "text": "6. 已知 \\(x \\in \\mathbb{R}\\)，求集合。",
            "formulas": [],
            "confidence": "0.90",
            "structure": {"content_may_be_missing": True, "uncertain": False},
        },
        ensure_ascii=False,
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": response_content}}]}

    class FakeHttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.services.bailian.settings.dashscope_api_key", "test-key")
    monkeypatch.setattr("app.services.bailian.httpx.Client", FakeHttpClient)
    result = BailianClient().recognize_math_page(_png())

    assert result.confidence == 0.90
    assert result.formulas == (("x \\in \\mathbb{R}", "x \\in \\mathbb{R}"),)
    assert result.review_warnings == ("model_detected_omission",)


def test_formula_text_is_not_appended_when_already_embedded():
    text = "已知 \\(x^2=1\\)，求实数 x。"
    combined = _append_missing_formula_text(text, (("x squared equals one", "x^2=1"),))

    assert combined == text
    assert combined.count("x^2=1") == 1


def test_document_ocr_keeps_formula_in_place_without_duplicate_chunk():
    parts = classify_ocr_text(
        "已知 \\(x^2=1\\)，求 x。",
        (("x squared equals one", "x ^ 2 = 1"),),
    )
    formulas = [part for part in parts if part.content_type == "formula"]

    assert len(formulas) == 1
    assert formulas[0].formula_latex == "x^2=1"
    assert formulas[0].formula_source == "bailian_vision_ocr"


def test_document_formula_classifier_accepts_set_and_quantifier_notation():
    parts = classify_ocr_text(
        "命题为 \\(\\forall x \\in \\mathbb{R}\\)，结论成立。",
        (("for every real x", "\\forall x \\in \\mathbb{R}"),),
    )
    formulas = [part for part in parts if part.content_type == "formula"]

    assert len(formulas) == 1
    assert formulas[0].formula_source == "bailian_vision_ocr"


def test_structural_review_reasons_override_high_confidence():
    reasons = ocr_review_reasons("5. 已知集合 A。\n7. 判断下列命题：（1）命题甲；（3）命题丙。", 0.90)

    assert "low_confidence" not in reasons
    assert "multiple_questions" in reasons
    assert "question_number_gap" in reasons
    assert "subquestion_number_gap" in reasons


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
    assert result.json()["review_reasons"] == ["low_confidence"]
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
