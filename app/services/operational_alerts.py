from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import redis
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ModelCall, OcrTask


logger = logging.getLogger("qingkui.operational_alerts")


def build_operational_alert_summary(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    generated_at = now or datetime.now(timezone.utc)
    window_start = generated_at - timedelta(minutes=max(1, settings.operational_alert_window_minutes))
    calls, failed_calls, average_latency = db.execute(
        select(
            func.count(ModelCall.id),
            func.sum(case((ModelCall.success.is_(False), 1), else_=0)),
            func.avg(ModelCall.latency_ms),
        ).where(ModelCall.created_at >= window_start)
    ).one()
    calls = int(calls or 0)
    failed_calls = int(failed_calls or 0)
    average_latency = float(average_latency or 0)
    failure_rate = failed_calls / calls if calls else 0.0
    recent_call_rows = list(
        db.execute(
            select(ModelCall.user_id, ModelCall.input_tokens, ModelCall.output_tokens).where(
                ModelCall.created_at >= window_start
            )
        )
    )
    max_tokens_per_call = max(
        (int(input_tokens or 0) + int(output_tokens or 0) for _, input_tokens, output_tokens in recent_call_rows),
        default=0,
    )
    calls_by_user = Counter(user_id for user_id, _, _ in recent_call_rows if user_id)
    max_calls_per_user = max(calls_by_user.values(), default=0)

    stuck_before = generated_at - timedelta(minutes=max(1, settings.operational_ocr_stuck_minutes))
    stuck_ocr = db.scalar(
        select(func.count(OcrTask.id)).where(
            OcrTask.status.in_(("queued", "recognizing")),
            func.coalesce(OcrTask.started_at, OcrTask.queued_at, OcrTask.created_at) <= stuck_before,
        )
    ) or 0
    failed_ocr = db.scalar(
        select(func.count(OcrTask.id)).where(
            OcrTask.status == "failed",
            OcrTask.updated_at >= window_start,
        )
    ) or 0
    active_ocr = db.scalar(
        select(func.count(OcrTask.id)).where(OcrTask.status.in_(("queued", "recognizing")))
    ) or 0

    alerts: list[dict[str, Any]] = []
    if (
        calls >= settings.operational_model_failure_min_calls
        and failed_calls > 0
        and failure_rate >= settings.operational_model_failure_rate_threshold
    ):
        alerts.append(
            {
                "severity": "critical",
                "code": "model_failure_rate",
                "message": "最近模型调用失败率超过阈值",
                "value": round(failure_rate, 4),
                "threshold": settings.operational_model_failure_rate_threshold,
            }
        )
    if calls and average_latency >= settings.operational_model_latency_threshold_ms:
        alerts.append(
            {
                "severity": "warning",
                "code": "model_latency",
                "message": "最近模型调用平均延迟超过阈值",
                "value": round(average_latency, 2),
                "threshold": float(settings.operational_model_latency_threshold_ms),
            }
        )
    if max_tokens_per_call >= settings.operational_model_tokens_per_call_threshold:
        alerts.append(
            {
                "severity": "warning",
                "code": "model_token_spike",
                "message": "存在单次 Token 用量异常的模型调用",
                "value": float(max_tokens_per_call),
                "threshold": float(settings.operational_model_tokens_per_call_threshold),
            }
        )
    if max_calls_per_user >= settings.operational_user_call_burst_threshold:
        alerts.append(
            {
                "severity": "warning",
                "code": "user_call_burst",
                "message": "存在单用户短时间调用量异常",
                "value": float(max_calls_per_user),
                "threshold": float(settings.operational_user_call_burst_threshold),
            }
        )
    if failed_ocr >= settings.operational_ocr_failed_threshold:
        alerts.append(
            {
                "severity": "warning",
                "code": "ocr_failures",
                "message": "最近 OCR 失败任务数量超过阈值",
                "value": float(failed_ocr),
                "threshold": float(settings.operational_ocr_failed_threshold),
            }
        )
    if stuck_ocr:
        alerts.append(
            {
                "severity": "critical",
                "code": "ocr_stuck",
                "message": "存在长时间未完成的 OCR 任务",
                "value": float(stuck_ocr),
                "threshold": 0.0,
            }
        )
    status = (
        "critical"
        if any(item["severity"] == "critical" for item in alerts)
        else "warning" if alerts else "ok"
    )
    return {
        "status": status,
        "generated_at": generated_at,
        "window_start": window_start,
        "metrics": {
            "model_calls": calls,
            "model_failed_calls": failed_calls,
            "model_failure_rate": round(failure_rate, 4),
            "model_average_latency_ms": round(average_latency, 2),
            "model_max_tokens_per_call": max_tokens_per_call,
            "model_max_calls_per_user": max_calls_per_user,
            "ocr_active_tasks": int(active_ocr),
            "ocr_failed_tasks": int(failed_ocr),
            "ocr_stuck_tasks": int(stuck_ocr),
        },
        "alerts": alerts,
    }


def webhook_payload(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "qingkui.operational_alert",
        "environment": settings.app_env,
        "status": summary["status"],
        "generated_at": summary["generated_at"].isoformat(),
        "window_start": summary["window_start"].isoformat(),
        "metrics": summary["metrics"],
        "alerts": summary["alerts"],
    }


def _fingerprint(summary: dict[str, Any]) -> str:
    values = sorted((item["severity"], item["code"]) for item in summary["alerts"])
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()[:24]


def _claim_notification(summary: dict[str, Any]) -> tuple[redis.Redis | None, str | None, bool]:
    fingerprint = _fingerprint(summary)
    key = f"qingkui:operational-alert:{fingerprint}"
    try:
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1)
        claimed = bool(
            client.set(
                key,
                summary["generated_at"].isoformat(),
                nx=True,
                ex=max(60, settings.operational_alert_cooldown_minutes * 60),
            )
        )
        return client, key, claimed
    except redis.RedisError:
        logger.exception("operational_alert_deduplication_unavailable")
        return None, None, True


def notify_operational_alerts(
    summary: dict[str, Any],
    *,
    webhook_url: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    if summary["status"] == "ok":
        return "healthy"
    client, key, claimed = _claim_notification(summary)
    if not claimed:
        return "cooldown"
    try:
        with httpx.Client(timeout=settings.operational_alert_webhook_timeout_seconds, transport=transport) as http:
            response = http.post(webhook_url, json=webhook_payload(summary))
            response.raise_for_status()
    except Exception:
        if client is not None and key is not None:
            try:
                client.delete(key)
            except redis.RedisError:
                logger.exception("operational_alert_claim_release_failed")
        raise
    return "sent"
