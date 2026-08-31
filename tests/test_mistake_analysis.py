from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.config import settings
from app.models import AuditLog, MistakeAsset, MistakePracticeRound, ModelCall, MistakeProblem, OcrTask, utc_now
from app.services.practice_validation import numeric_reference_is_consistent, validate_practice_answer


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


def test_analysis_is_grounded_charged_once_and_generates_idempotent_round(client: TestClient, account) -> None:
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
    assert first.json()["review_stage"] == "correction"
    assert first.json()["question_count"] == 3
    assert len(first.json()["practices"]) == 3
    assert all(item["hint"] for item in first.json()["practices"])
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["analysis_status"] == "completed"
    assert detail["analysis"]["similar_question"]
    assert len(detail["practices"]) == 3
    assert len(detail["practice_rounds"]) == 1


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


def test_ai_kill_switch_preserves_mistake_and_blocks_analysis_and_generation(
    client: TestClient,
    account,
    monkeypatch,
) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（停机门）")
    initial_balance = client.get("/api/credits", headers=headers).json()["balance"]
    monkeypatch.setattr(settings, "ai_enabled", False)

    blocked_analysis = client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers)

    assert blocked_analysis.status_code == 503
    assert "维护" in blocked_analysis.json()["detail"]
    assert client.get("/api/credits", headers=headers).json()["balance"] == initial_balance
    assert client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()["analysis_status"] == "not_started"

    monkeypatch.setattr(settings, "ai_enabled", True)
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers).status_code == 200
    balance_after_analysis = client.get("/api/credits", headers=headers).json()["balance"]
    monkeypatch.setattr(settings, "ai_enabled", False)

    blocked_generation = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)

    assert blocked_generation.status_code == 503
    assert "维护" in blocked_generation.json()["detail"]
    assert client.get("/api/credits", headers=headers).json()["balance"] == balance_after_analysis
    assert client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()["practice_rounds"] == []


def test_server_numeric_validation_overrides_client_and_schedules_review(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（校验）")
    linked = client.patch(
        f"/api/mistakes/{mistake['id']}",
        json={"knowledge_node_id": "quadratic_function"},
        headers=headers,
    )
    assert linked.status_code == 200
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
    detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
    assert detail["study_status"] == "reviewing"
    assert detail["review_stage"] == "next_day"
    assert detail["next_review_at"] is not None
    summary = client.get("/api/learning/summary", headers=headers).json()
    assert not any(item["id"] == "quadratic_function" for item in summary["verified"])


def test_three_stage_rounds_require_authoritative_answers_before_mastery(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（三阶段）")
    assert client.patch(
        f"/api/mistakes/{mistake['id']}",
        json={"knowledge_node_id": "quadratic_function"},
        headers=headers,
    ).status_code == 200
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers).status_code == 200
    with SessionLocal() as db:
        row = db.scalar(select(MistakeProblem).where(MistakeProblem.id == mistake["id"]))
        row.analysis = {
            **row.analysis,
            "similar_practices": [
                {"question": "计算 2+2", "hint": "直接计算", "answer_reference": "答案：4"},
                {"question": "计算 1+3", "hint": "合并整数", "answer_reference": "答案：4"},
                {"question": "计算 8/2", "hint": "先做除法", "answer_reference": "答案：4"},
            ],
        }
        db.commit()

    stage_expectations = [
        ("correction", "next_day", "reviewing"),
        ("next_day", "next_week", "reviewing"),
        ("next_week", "completed", "mastered"),
    ]
    for expected_stage, next_stage, status in stage_expectations:
        with SessionLocal() as db:
            row = db.scalar(select(MistakeProblem).where(MistakeProblem.id == mistake["id"]))
            row.next_review_at = None
            db.commit()
        generated = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
        assert generated.status_code == 201, generated.text
        assert generated.json()["review_stage"] == expected_stage
        for practice in generated.json()["practices"]:
            answer = practice["answer_reference"].split("：", 1)[-1]
            submitted = client.post(
                f"/api/mistakes/{mistake['id']}/practices/{practice['id']}/submit",
                json={"student_answer": answer, "is_correct": False},
                headers=headers,
            )
            assert submitted.status_code == 200, submitted.text
        repeated = client.post(
            f"/api/mistakes/{mistake['id']}/practices/{generated.json()['practices'][0]['id']}/submit",
            json={"student_answer": "4"},
            headers=headers,
        )
        assert repeated.status_code == 409
        detail = client.get(f"/api/mistakes/{mistake['id']}", headers=headers).json()
        assert detail["review_stage"] == next_stage
        assert detail["study_status"] == status

    assert detail["second_attempt_correct"] is True
    assert detail["review_streak"] == 3
    assert detail["next_review_at"] is None
    summary = client.get("/api/learning/summary", headers=headers).json()
    assert any(item["id"] == "quadratic_function" for item in summary["verified"])

    weekly = client.get("/api/mistakes/review/weekly", headers=headers)
    assert weekly.status_code == 200, weekly.text
    assert weekly.json()["practice_completion_rate"] > 0
    assert weekly.json()["authoritative_accuracy"] == 1.0
    assert weekly.json()["second_attempt_accuracy"] == 1.0


def test_later_review_round_generates_fresh_questions_without_duplicates(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（去重）")
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers).status_code == 200
    with SessionLocal() as db:
        row = db.scalar(select(MistakeProblem).where(MistakeProblem.id == mistake["id"]))
        row.analysis = {
            **row.analysis,
            "similar_practices": [
                {"question": "计算 2+2", "hint": "直接计算", "answer_reference": "答案：4"},
                {"question": "计算 1+3", "hint": "合并整数", "answer_reference": "答案：4"},
                {"question": "计算 8/2", "hint": "先做除法", "answer_reference": "答案：4"},
            ],
        }
        db.commit()

    first = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    assert first.status_code == 201, first.text
    first_questions = {item["question_text"] for item in first.json()["practices"]}
    for practice in first.json()["practices"]:
        submitted = client.post(
            f"/api/mistakes/{mistake['id']}/practices/{practice['id']}/submit",
            json={"student_answer": "4"},
            headers=headers,
        )
        assert submitted.status_code == 200, submitted.text

    with SessionLocal() as db:
        row = db.scalar(select(MistakeProblem).where(MistakeProblem.id == mistake["id"]))
        row.next_review_at = None
        db.commit()
    second = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    assert second.status_code == 201, second.text
    second_questions = {item["question_text"] for item in second.json()["practices"]}
    assert first_questions.isdisjoint(second_questions)
    assert len(second_questions) == second.json()["question_count"]


def test_failed_later_round_does_not_create_round_or_model_call(client: TestClient, account, monkeypatch) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（生成失败）")
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers).status_code == 200
    first = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    assert first.status_code == 201
    for practice in first.json()["practices"]:
        assert client.post(
            f"/api/mistakes/{mistake['id']}/practices/{practice['id']}/submit",
            json={"student_answer": "4"},
            headers=headers,
        ).status_code == 200
    with SessionLocal() as db:
        row = db.scalar(select(MistakeProblem).where(MistakeProblem.id == mistake["id"]))
        row.next_review_at = None
        before_rounds = len(list(db.scalars(select(MistakePracticeRound).where(MistakePracticeRound.mistake_id == row.id))))
        before_calls = len(
            list(db.scalars(select(ModelCall).where(ModelCall.feature == "mistake_practice_generation")))
        )
        db.commit()
    monkeypatch.setattr(
        "app.routers.mistakes.generate_similar_practices",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    failed = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    assert failed.status_code == 502
    with SessionLocal() as db:
        after_rounds = len(list(db.scalars(select(MistakePracticeRound).where(MistakePracticeRound.mistake_id == mistake["id"]))))
        after_calls = len(
            list(db.scalars(select(ModelCall).where(ModelCall.feature == "mistake_practice_generation")))
        )
    assert after_rounds == before_rounds
    assert after_calls == before_calls


def test_practice_rounds_and_weekly_review_are_cross_user_private(client: TestClient, account) -> None:
    _, headers = account
    mistake = _create(client, headers, suffix="（权限）")
    assert client.post(f"/api/mistakes/{mistake['id']}/analyze", headers=headers).status_code == 200
    practice_round = client.post(f"/api/mistakes/{mistake['id']}/practices/generate", headers=headers)
    assert practice_round.status_code == 201

    other = client.post(
        "/api/auth/register",
        json={"username": "practice_other_01", "password": "practice-pass-123", "nickname": "另一位同学"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.get(f"/api/mistakes/{mistake['id']}", headers=other_headers).status_code == 404
    assert client.post(
        f"/api/mistakes/{mistake['id']}/practices/generate", headers=other_headers
    ).status_code == 404
    other_weekly = client.get("/api/mistakes/review/weekly", headers=other_headers)
    assert other_weekly.status_code == 200
    assert other_weekly.json()["new_mistakes"] == 0
    assert other_weekly.json()["due_reviews"] == []


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


def test_generated_numeric_reference_must_match_question() -> None:
    assert numeric_reference_is_consistent("计算 3+5 的值", "答案：8") is True
    assert numeric_reference_is_consistent("计算 3+5 的值", "答案：9") is False
    assert numeric_reference_is_consistent("证明正弦定理", "应写出完整证明") is None


def test_weekly_review_reports_upload_and_ocr_correction_rates(client: TestClient, account) -> None:
    auth, headers = account
    mistake = _create(client, headers, suffix="（周指标）")
    with SessionLocal() as db:
        asset = MistakeAsset(
            mistake_id=mistake["id"],
            user_id=auth["user"]["id"],
            object_key=f"test/{mistake['id']}.jpg",
            mime_type="image/jpeg",
            size_bytes=100,
            checksum_sha256="a" * 64,
            width=10,
            height=10,
            status="stored",
        )
        db.add(asset)
        db.flush()
        db.add(
            OcrTask(
                mistake_id=mistake["id"],
                asset_id=asset.id,
                user_id=auth["user"]["id"],
                status="succeeded",
                completed_at=utc_now(),
            )
        )
        db.add_all(
            [
                AuditLog(actor_user_id=auth["user"]["id"], action="mistake.image_upload_attempted", target_type="mistake", target_id=mistake["id"]),
                AuditLog(actor_user_id=auth["user"]["id"], action="mistake.image_upload_succeeded", target_type="mistake", target_id=mistake["id"]),
                AuditLog(actor_user_id=auth["user"]["id"], action="mistake.ocr_confirmed", target_type="ocr_task", target_id=mistake["id"]),
            ]
        )
        db.commit()
    report = client.get("/api/mistakes/review/weekly", headers=headers)
    assert report.status_code == 200, report.text
    assert report.json()["upload_success_rate"] == 1.0
    assert report.json()["ocr_correction_rate"] == 1.0
