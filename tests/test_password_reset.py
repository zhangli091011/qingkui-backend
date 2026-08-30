from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import PasswordResetToken, RefreshSession, User


def test_password_reset_is_private_one_time_and_revokes_sessions(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "email_provider", "console")
    username = f"reset_{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "password": "old-password-123", "email": email},
    )
    assert registered.status_code == 201
    unknown = client.post("/api/auth/password-reset/request", json={"email": "missing@example.com"})
    requested = client.post("/api/auth/password-reset/request", json={"email": email})
    assert unknown.status_code == requested.status_code == 202
    assert unknown.json()["message"] == requested.json()["message"]
    token = requested.json()["reset_token"]
    assert token

    confirmed = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "new-password-456"},
    )
    assert confirmed.status_code == 200
    assert client.post("/api/auth/login", json={"username": username, "password": "old-password-123"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": username, "password": "new-password-456"}).status_code == 200
    assert client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "another-password-789"},
    ).status_code == 400
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user is not None
        old_sessions = list(db.scalars(select(RefreshSession).where(RefreshSession.user_id == user.id).order_by(RefreshSession.created_at)))
    assert old_sessions[0].revoked_at is not None


def test_expired_password_reset_token_is_rejected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "email_provider", "console")
    username = f"expired_{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"
    client.post("/api/auth/register", json={"username": username, "password": "old-password-123", "email": email})
    requested = client.post("/api/auth/password-reset/request", json={"email": email})
    token = requested.json()["reset_token"]
    with SessionLocal() as db:
        record = db.scalar(select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None)))
        assert record is not None
        record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "new-password-456"},
    ).status_code == 400
