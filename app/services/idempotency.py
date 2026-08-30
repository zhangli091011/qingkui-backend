from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IdempotencyRequest


KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
PROCESSING_TIMEOUT = timedelta(minutes=5)


@dataclass(frozen=True)
class IdempotencyReservation:
    request_id: str
    acquired: bool
    response_body: dict[str, Any] | None = None


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _validate_key(key: str) -> str:
    normalized = key.strip()
    if not KEY_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key 必须为 8-128 位字母、数字或 . _ : -",
        )
    return normalized


def reserve_idempotency(
    db: Session,
    *,
    user_id: str,
    scope: str,
    key: str | None,
    request_hash: str,
) -> IdempotencyReservation | None:
    if key is None:
        return None
    normalized = _validate_key(key)
    record = IdempotencyRequest(
        user_id=user_id,
        scope=scope,
        key=normalized,
        request_hash=request_hash,
        status="processing",
    )
    db.add(record)
    try:
        db.commit()
        return IdempotencyReservation(record.id, acquired=True)
    except IntegrityError:
        db.rollback()

    existing = db.scalar(
        select(IdempotencyRequest)
        .where(
            IdempotencyRequest.user_id == user_id,
            IdempotencyRequest.scope == scope,
            IdempotencyRequest.key == normalized,
        )
        .with_for_update()
    )
    if existing is None:  # pragma: no cover - defensive against external deletion races
        raise HTTPException(status_code=409, detail="幂等请求状态已变化，请重试")
    if existing.request_hash != request_hash:
        raise HTTPException(status_code=409, detail="同一 Idempotency-Key 不能用于不同请求")
    if existing.status == "completed" and existing.response_body is not None:
        return IdempotencyReservation(existing.id, acquired=False, response_body=existing.response_body)
    now = datetime.now(timezone.utc)
    if existing.status == "processing" and _as_utc(existing.updated_at) > now - PROCESSING_TIMEOUT:
        raise HTTPException(
            status_code=409,
            detail="相同请求正在处理中，请稍后重试",
            headers={"Retry-After": "2"},
        )
    existing.status = "processing"
    existing.response_body = None
    existing.updated_at = now
    db.commit()
    return IdempotencyReservation(existing.id, acquired=True)


def complete_idempotency(db: Session, request_id: str | None, response_body: dict[str, Any]) -> None:
    if request_id is None:
        return
    record = db.get(IdempotencyRequest, request_id)
    if record is None:
        raise RuntimeError("Idempotency reservation disappeared")
    record.status = "completed"
    record.response_body = response_body
    record.updated_at = datetime.now(timezone.utc)


def fail_idempotency(db: Session, request_id: str | None) -> None:
    if request_id is None:
        return
    record = db.get(IdempotencyRequest, request_id)
    if record is None:
        return
    record.status = "failed"
    record.response_body = None
    record.updated_at = datetime.now(timezone.utc)
