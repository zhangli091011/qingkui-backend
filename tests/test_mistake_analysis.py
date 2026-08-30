from fastapi.testclient import TestClient

from app.services.practice_validation import validate_practice_answer


def _create(client: TestClient, headers: dict[str, str], *, suffix: str = "") -> dict:
    response = client.post(
        "/api/mistakes",
        json={
            "subject": "数学",
            "question_text": f"计算 2+2 的值{suffix}",
            "student_work": "我写成了 5",
            "question_goal": "找出计算错误",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_analysis_is_grounded_charged_once_and_generates_one_practice(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（分析）")
    before = client.get("/api/credits", headers=headers).json()["balance"]

    analyzed = client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text
    payload = analyzed.json()
    assert payload["credits_charged"] == 2
    assert payload["balance"] == before - 2
    assert payload["analysis"]["error_category"] in {
        "concept",
        "reading",
        "method",
        "calculation",
        "expression",
    }

    repeated = client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["credits_charged"] == 0
    assert repeated.json()["balance"] == before - 2

    first = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    second = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["analysis_status"] == "completed"
    assert detail["analysis"]["similar_question"]
    assert len(detail["practices"]) == 1


def test_editing_source_content_invalidates_analysis(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（失效）")
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers).status_code == 200
    updated = client.patch(
        f"/api/mistakes/{mistake['id']}",
        json={"student_work": "重新写成 4"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["analysis_status"] == "not_started"
    assert updated.json()["analysis"] == {}
    assert client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers).status_code == 409


def test_analysis_failure_does_not_charge_and_is_cross_user_private(client: TestClient, account, monkeypatch) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（失败）")
    before = client.get("/api/credits", headers=headers).json()["balance"]
    monkeypatch.setattr(
        "app.routers.mistakes.analyze_mistake_content",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    failed = client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers)
    assert failed.status_code == 502
    assert client.get("/api/credits", headers=headers).json()["balance"] == before
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["analysis_status"] == "not_started"

    other = client.post(
        "/api/auth/register",
        json={"username": "analysis_other_01", "password": "analysis-pass-123", "nickname": "其他同学"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=other_headers).status_code == 404


def test_server_numeric_validation_overrides_client_and_controls_mastery(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（校验）")
    practice = client.post(
        f"/api/mistakes/{mistake['id']}/practices",
        json={"question_text": "计算 2+2", "answer_reference": "答案：4"},
        headers=headers,
    )
    assert practice.status_code == 201
    submitted = client.post(
        f"/api/mistakes/{mistake['id']}/practices/{practice.json()['id']}/submit",
        json={"student_answer": "2+2", "is_correct": False, "validation_details": {"claimed": "wrong"}},
        headers=headers,
    )
    assert submitted.status_code == 200
    result = submitted.json()
    assert result["is_correct"] is True
    assert result["validation_details"]["method"] == "numeric_expression"
    assert result["validation_details"]["authoritative"] is True
    assert result["validation_details"]["client"] == {"claimed": "wrong"}
    assert client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()["study_status"] == "mastered"


def test_non_numeric_self_report_never_counts_as_verified_mastery(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（自评）")
    practice = client.post(
        f"/api/mistakes/{mistake['id']}/practices",
        json={"question_text": "说明理由", "answer_reference": "应写出完整推理过程"},
        headers=headers,
    ).json()
    submitted = client.post(
        f"/api/mistakes/{mistake['id']}/practices/{practice['id']}/submit",
        json={"student_answer": "我已经理解", "is_correct": True},
        headers=headers,
    )
    assert submitted.status_code == 200
    assert submitted.json()["validation_details"]["authoritative"] is False
    assert client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()["study_status"] == "reviewing"


def test_answer_validator_rejects_executable_expressions() -> None:
    result, details = validate_practice_answer("__import__('os').system('whoami')", "答案：4")
    assert result is None
    assert details["authoritative"] is False
