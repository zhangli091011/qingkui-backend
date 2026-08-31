from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from app.config import settings
from app.db import SessionLocal
from app.models import ModelCall, MistakePracticeRound, MistakeProblem, User, UserRole
from app.rate_limit import RateLimitDecision, _client_identity, _limit_for, _route_bucket


def _register(client: TestClient, username: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "observability-pass-123", "nickname": username},
    )
    assert response.status_code == 201
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['access_token']}"}


def test_model_calls_capture_success_failure_tokens_and_admin_aggregation(client: TestClient, monkeypatch) -> None:
    started_at = datetime.now(timezone.utc)
    auth, headers = _register(client, "observability_student_01")
    session = client.post("/api/qa/sessions", json={"mode": "knowledge"}, headers=headers).json()
    succeeded = client.post(
        f"/api/qa/sessions/{session['id']}/messages",
        json={"content": "什么是二次函数？", "help_level": "approach"},
        headers=headers,
    )
    assert succeeded.status_code == 200

    monkeypatch.setattr(
        "app.routers.qa.answer_question",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    before = client.get("/api/credits", headers=headers).json()["balance"]
    failed = client.post(
        f"/api/qa/sessions/{session['id']}/messages",
        json={"content": "再次解释二次函数", "help_level": "approach"},
        headers=headers,
    )
    assert failed.status_code == 502
    assert client.get("/api/credits", headers=headers).json()["balance"] == before

    with SessionLocal() as db:
        calls = list(db.scalars(select(ModelCall).where(ModelCall.user_id == auth["user"]["id"])))
        assert len(calls) == 2
        assert {call.success for call in calls} == {True, False}
        assert all(call.latency_ms >= 0 for call in calls)
        calls[0].input_tokens = 1
        admin = db.get(User, auth["user"]["id"])
        admin.role = UserRole.admin
        db.commit()

    summary = client.get(
        "/api/admin/model-costs",
        params={"feature": "qa_knowledge", "provider": "stub", "start_at": started_at.isoformat()},
        headers=headers,
    )
    assert summary.status_code == 200, summary.text
    row = summary.json()[0]
    assert row["calls"] == 2
    assert row["successful_calls"] == 1
    assert row["failed_calls"] == 1
    assert row["failure_rate"] == 0.5
    assert row["average_latency_ms"] >= 0

    monkeypatch.setattr(settings, "operational_model_failure_min_calls", 1)
    monkeypatch.setattr(settings, "operational_model_failure_rate_threshold", 0.0)
    monkeypatch.setattr(settings, "operational_model_latency_threshold_ms", 0)
    monkeypatch.setattr(settings, "operational_model_tokens_per_call_threshold", 1)
    monkeypatch.setattr(settings, "operational_user_call_burst_threshold", 1)
    alerts = client.get("/api/admin/operational-alerts", headers=headers)
    assert alerts.status_code == 200
    alert_payload = alerts.json()
    assert alert_payload["status"] == "critical"
    assert alert_payload["metrics"]["model_calls"] >= 2
    assert {item["code"] for item in alert_payload["alerts"]} >= {
        "model_failure_rate",
        "model_latency",
        "model_token_spike",
        "user_call_burst",
    }


def test_cost_efficiency_reports_active_student_and_mistake_loop_cost(client: TestClient, monkeypatch) -> None:
    started_at = datetime.now(timezone.utc)
    admin_auth, admin_headers = _register(client, "cost_efficiency_admin_01")
    student_auth, _ = _register(client, "cost_efficiency_student_01")
    with SessionLocal() as db:
        db.get(User, admin_auth["user"]["id"]).role = UserRole.admin
        mistake = MistakeProblem(
            user_id=student_auth["user"]["id"],
            subject="数学",
            question_text="计算 2+2",
            analysis_status="completed",
        )
        db.add(mistake)
        db.flush()
        db.add_all(
            [
                MistakePracticeRound(
                    mistake_id=mistake.id,
                    user_id=student_auth["user"]["id"],
                    round_number=1,
                    review_stage="next_week",
                    status="completed",
                    question_count=3,
                    correct_count=3,
                    authoritative_correct_count=3,
                    completed_at=datetime.now(timezone.utc),
                ),
                ModelCall(
                    user_id=student_auth["user"]["id"],
                    feature="mistake_analysis",
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    success=True,
                    input_tokens=1_000_000,
                    output_tokens=500_000,
                    latency_ms=100,
                    reference_id=mistake.id,
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(settings, "deepseek_input_cost_per_million_cny", 2.0)
    monkeypatch.setattr(settings, "deepseek_output_cost_per_million_cny", 4.0)
    report = client.get(
        "/api/admin/cost-efficiency",
        params={"start_at": started_at.isoformat()},
        headers=admin_headers,
    )
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["active_students"] == 1
    assert payload["started_mistake_loops"] == 1
    assert payload["completed_mistake_loops"] == 1
    assert payload["estimated_model_cost_cny"] == 4.0
    assert payload["estimated_mistake_loop_cost_cny"] == 4.0
    assert payload["estimated_cost_per_active_student_cny"] == 4.0
    assert payload["estimated_cost_per_completed_mistake_loop_cny"] == 4.0
    assert payload["pricing_complete"] is True


def test_cost_efficiency_rejects_invalid_period(client: TestClient) -> None:
    auth, headers = _register(client, "cost_efficiency_admin_02")
    with SessionLocal() as db:
        db.get(User, auth["user"]["id"]).role = UserRole.admin
        db.commit()
    now = datetime.now(timezone.utc)
    response = client.get(
        "/api/admin/cost-efficiency",
        params={"start_at": now.isoformat(), "end_at": now.isoformat()},
        headers=headers,
    )
    assert response.status_code == 422


def test_rate_limit_policy_and_429_response(client: TestClient, monkeypatch) -> None:
    assert _limit_for("POST", "/api/auth/login") == settings.rate_limit_auth_per_minute
    assert _limit_for("POST", "/api/qa/sessions/x/messages/stream") == settings.rate_limit_ai_per_minute
    assert _limit_for("POST", "/api/mistakes/x/images") == settings.rate_limit_upload_per_minute
    assert _route_bucket("POST", "/api/qa/sessions/first/messages") == _route_bucket(
        "POST", "/api/qa/sessions/second/messages"
    )
    assert _route_bucket("POST", "/api/mistakes/first/analyze") == _route_bucket(
        "POST", "/api/mistakes/second/analyze"
    )

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(
        "app.rate_limit._take",
        lambda _key, limit: RateLimitDecision(False, limit, 0, 2_000_000_000),
    )
    response = client.get("/api/knowledge/subjects")
    assert response.status_code == 429
    assert response.json()["detail"] == "请求过于频繁，请稍后重试"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert "retry-after" in response.headers


def test_rate_limit_uses_proxy_overwritten_real_ip(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_trust_proxy_headers", True)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [
                (b"x-forwarded-for", b"198.51.100.99, 203.0.113.7"),
                (b"x-real-ip", b"203.0.113.7"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    assert _client_identity(request) == "ip:203.0.113.7"


def test_rate_limit_store_failure_is_fail_open(client: TestClient, monkeypatch) -> None:
    import redis

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(
        "app.rate_limit._take",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(redis.RedisError("offline")),
    )
    response = client.get("/api/knowledge/subjects")
    assert response.status_code == 401
