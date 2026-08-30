from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.requests import Request

from app.config import settings
from app.db import SessionLocal
from app.models import ModelCall, User, UserRole
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
