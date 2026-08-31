from fastapi.testclient import TestClient

from app.config import settings


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_production_registration_requires_current_privacy_notice(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    payload = {"username": "privacy_new_01", "password": "privacy-pass-123"}
    rejected = client.post("/api/auth/register", json=payload)
    assert rejected.status_code == 422

    accepted = client.post(
        "/api/auth/register",
        json={
            **payload,
            "privacy_consent": True,
            "privacy_notice_version": settings.privacy_notice_version,
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert client.get("/api/credits", headers=_headers(accepted.json()["access_token"])).status_code == 200


def test_existing_student_accepts_updated_notice_before_business_access(client: TestClient, monkeypatch) -> None:
    created = client.post(
        "/api/auth/register",
        json={"username": "privacy_existing_01", "password": "privacy-pass-123"},
    )
    assert created.status_code == 201
    headers = _headers(created.json()["access_token"])
    monkeypatch.setattr(settings, "app_env", "production")

    blocked = client.get("/api/credits", headers=headers)
    assert blocked.status_code == 428
    status = client.get("/api/auth/privacy-consent", headers=headers)
    assert status.status_code == 200
    assert status.json()["required"] is True

    accepted = client.post(
        "/api/auth/privacy-consent",
        json={"accepted": True, "notice_version": settings.privacy_notice_version},
        headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["required"] is False
    assert client.get("/api/credits", headers=headers).status_code == 200
