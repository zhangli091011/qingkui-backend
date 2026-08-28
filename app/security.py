import uuid
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from app.config import settings


password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, password_digest: str) -> bool:
    return password_hash.verify(password, password_digest)


def _create_token(user_id: str, token_type: str, expires_delta: timedelta, jti: str | None = None) -> tuple[str, str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + expires_delta
    token_jti = jti or str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "type": token_type,
        "jti": token_jti,
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256"), token_jti, expires_at


def create_access_token(user_id: str) -> tuple[str, int]:
    lifetime = timedelta(minutes=settings.access_token_minutes)
    token, _, _ = _create_token(user_id, "access", lifetime)
    return token, int(lifetime.total_seconds())


def create_refresh_token(user_id: str) -> tuple[str, str, datetime]:
    return _create_token(user_id, "refresh", timedelta(days=settings.refresh_token_days))


def decode_token(token: str, expected_type: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != expected_type or not payload.get("sub") or not payload.get("jti"):
        raise jwt.InvalidTokenError("Unexpected token type")
    return payload
