from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services.operational_alerts import notify_operational_alerts, webhook_payload


def _summary(status: str = "critical") -> dict:
    now = datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc)
    return {
        "status": status,
        "generated_at": now,
        "window_start": now,
        "metrics": {"model_calls": 5, "model_failed_calls": 2},
        "alerts": [
            {
                "severity": "critical",
                "code": "model_failure_rate",
                "message": "最近模型调用失败率超过阈值",
                "value": 0.4,
                "threshold": 0.2,
            }
        ] if status != "ok" else [],
    }


def test_webhook_payload_contains_only_operational_aggregates(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "pilot")
    payload = webhook_payload(_summary())

    assert payload["event"] == "qingkui.operational_alert"
    assert payload["environment"] == "pilot"
    assert payload["alerts"][0]["code"] == "model_failure_rate"
    assert "user" not in str(payload).lower()
    assert "question" not in str(payload).lower()


def test_notify_posts_once_and_respects_cooldown(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    claims = iter(((None, None, True), (None, None, False)))
    monkeypatch.setattr("app.services.operational_alerts._claim_notification", lambda _summary: next(claims))
    transport = httpx.MockTransport(handler)

    assert notify_operational_alerts(_summary(), webhook_url="https://alerts.example.test/hook", transport=transport) == "sent"
    assert notify_operational_alerts(_summary(), webhook_url="https://alerts.example.test/hook", transport=transport) == "cooldown"
    assert len(requests) == 1


def test_notify_skips_healthy_status(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.operational_alerts._claim_notification",
        lambda _summary: (_ for _ in ()).throw(AssertionError("healthy state must not claim")),
    )
    assert notify_operational_alerts(_summary("ok"), webhook_url="https://alerts.example.test/hook") == "healthy"
