from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Conversation,
    AuditLog,
    CreditLedger,
    FeedbackSubmission,
    LearningEvent,
    Message,
    MistakePractice,
    MistakePracticeRound,
    MistakeProblem,
    MistakeAsset,
    OcrTask,
    User,
    UserKnowledgeState,
    KnowledgeStatus,
    UserRole,
)


HELPFUL_TEXT = "该回答对本次学习有帮助"
UNHELPFUL_TEXT = "该回答没有解决我的问题"
STATE_EVENT_TYPES = {
    "state_updated",
    "marked_understood",
    "marked_confused",
    "completed_check",
}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def anonymous_user_id(user_id: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode(),
        f"pilot:{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()[:20]


def normalize_period(start_at: datetime | None, end_at: datetime | None) -> tuple[datetime, datetime]:
    end = end_at or datetime.now(timezone.utc)
    start = start_at or (end - timedelta(days=7))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    if end - start > timedelta(days=366):
        raise HTTPException(status_code=422, detail="单次试点统计范围不能超过 366 天")
    return start, end


def build_pilot_report(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[dict, list[dict]]:
    start, end = normalize_period(start_at, end_at)
    users = list(
        db.scalars(
            select(User)
            .where(
                User.role == UserRole.student,
                User.created_at >= start,
                User.created_at < end,
            )
            .order_by(User.created_at.asc())
        )
    )
    user_ids = [user.id for user in users]
    rows: dict[str, dict] = {
        user.id: {
            "anonymous_id": anonymous_user_id(user.id),
            "registered_at": user.created_at,
            "activated": False,
            "last_activity_at": None,
            "conversation_count": 0,
            "assistant_message_count": 0,
            "graph_exploration_count": 0,
            "knowledge_state_change_count": 0,
            "verified_node_count": 0,
            "credits_spent": 0,
            "helpful_votes": 0,
            "unhelpful_votes": 0,
            "mistake_count": 0,
            "completed_practice_count": 0,
            "mistake_upload_attempts": 0,
            "mistake_upload_successes": 0,
            "ocr_completed_count": 0,
            "ocr_corrected_count": 0,
            "practice_round_count": 0,
            "completed_practice_round_count": 0,
            "second_attempt_count": 0,
            "second_attempt_correct_count": 0,
        }
        for user in users
    }
    activity_by_user: dict[str, list[datetime]] = {user_id: [] for user_id in user_ids}
    if user_ids:
        for conversation in db.scalars(
            select(Conversation).where(
                Conversation.user_id.in_(user_ids),
                Conversation.created_at >= start,
                Conversation.created_at < end,
            )
        ):
            rows[conversation.user_id]["conversation_count"] += 1
            activity_by_user[conversation.user_id].append(conversation.created_at)
        for message, user_id in db.execute(
            select(Message, Conversation.user_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.user_id.in_(user_ids),
                Message.role == "assistant",
                Message.created_at >= start,
                Message.created_at < end,
            )
        ):
            rows[user_id]["assistant_message_count"] += 1
            rows[user_id]["activated"] = True
            activity_by_user[user_id].append(message.created_at)
        for event in db.scalars(
            select(LearningEvent).where(
                LearningEvent.user_id.in_(user_ids),
                LearningEvent.created_at < end,
            )
        ):
            activity_by_user[event.user_id].append(event.created_at)
            if _as_utc(event.created_at) < start:
                continue
            if event.event_type == "asked_question" and event.event_data.get("mode") == "explore":
                rows[event.user_id]["graph_exploration_count"] += 1
            if event.event_type in STATE_EVENT_TYPES:
                rows[event.user_id]["knowledge_state_change_count"] += 1
        for ledger in db.scalars(
            select(CreditLedger).where(
                CreditLedger.user_id.in_(user_ids),
                CreditLedger.created_at >= start,
                CreditLedger.created_at < end,
                CreditLedger.amount < 0,
            )
        ):
            rows[ledger.user_id]["credits_spent"] += abs(ledger.amount)
        for feedback in db.scalars(
            select(FeedbackSubmission).where(
                FeedbackSubmission.user_id.in_(user_ids),
                FeedbackSubmission.created_at >= start,
                FeedbackSubmission.created_at < end,
            )
        ):
            if feedback.content == HELPFUL_TEXT:
                rows[feedback.user_id]["helpful_votes"] += 1
            elif feedback.content == UNHELPFUL_TEXT:
                rows[feedback.user_id]["unhelpful_votes"] += 1
        for mistake in db.scalars(
            select(MistakeProblem).where(
                MistakeProblem.user_id.in_(user_ids),
                MistakeProblem.created_at >= start,
                MistakeProblem.created_at < end,
            )
        ):
            rows[mistake.user_id]["mistake_count"] += 1
        for practice in db.scalars(
            select(MistakePractice).where(
                MistakePractice.user_id.in_(user_ids),
                MistakePractice.status == "completed",
                MistakePractice.completed_at >= start,
                MistakePractice.completed_at < end,
            )
        ):
            rows[practice.user_id]["completed_practice_count"] += 1
        for audit in db.scalars(
            select(AuditLog).where(
                AuditLog.actor_user_id.in_(user_ids),
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
                AuditLog.action.in_(("mistake.image_upload_attempted", "mistake.image_upload_succeeded", "mistake.ocr_confirmed")),
            )
        ):
            if audit.action == "mistake.image_upload_attempted":
                rows[audit.actor_user_id]["mistake_upload_attempts"] += 1
            elif audit.action == "mistake.image_upload_succeeded":
                rows[audit.actor_user_id]["mistake_upload_successes"] += 1
            elif audit.action == "mistake.ocr_confirmed":
                rows[audit.actor_user_id]["ocr_corrected_count"] += 1
        for task in db.scalars(
            select(OcrTask).where(
                OcrTask.user_id.in_(user_ids),
                OcrTask.status == "succeeded",
                OcrTask.completed_at >= start,
                OcrTask.completed_at < end,
            )
        ):
            rows[task.user_id]["ocr_completed_count"] += 1
        for practice_round in db.scalars(
            select(MistakePracticeRound).where(
                MistakePracticeRound.user_id.in_(user_ids),
                MistakePracticeRound.started_at >= start,
                MistakePracticeRound.started_at < end,
            )
        ):
            rows[practice_round.user_id]["practice_round_count"] += 1
            if practice_round.status == "completed":
                rows[practice_round.user_id]["completed_practice_round_count"] += 1
        for mistake in db.scalars(
            select(MistakeProblem).where(
                MistakeProblem.user_id.in_(user_ids),
                MistakeProblem.last_reviewed_at >= start,
                MistakeProblem.last_reviewed_at < end,
                MistakeProblem.second_attempt_correct.is_not(None),
            )
        ):
            rows[mistake.user_id]["second_attempt_count"] += 1
            rows[mistake.user_id]["second_attempt_correct_count"] += int(mistake.second_attempt_correct is True)
        for state in db.scalars(
            select(UserKnowledgeState).where(
                UserKnowledgeState.user_id.in_(user_ids),
                UserKnowledgeState.status == KnowledgeStatus.verified,
            )
        ):
            rows[state.user_id]["verified_node_count"] += 1

    for user_id, timestamps in activity_by_user.items():
        rows[user_id]["last_activity_at"] = max(timestamps) if timestamps else None

    activated = sum(1 for row in rows.values() if row["activated"])
    eligible_users = [user for user in users if _as_utc(user.created_at) <= end - timedelta(days=7)]
    retained = sum(
        1
        for user in eligible_users
        if any(
            _as_utc(timestamp) >= _as_utc(user.created_at) + timedelta(days=7)
            for timestamp in activity_by_user[user.id]
        )
    )
    helpful = sum(row["helpful_votes"] for row in rows.values())
    unhelpful = sum(row["unhelpful_votes"] for row in rows.values())
    votes = helpful + unhelpful
    graph_explorers = sum(1 for row in rows.values() if row["graph_exploration_count"] > 0)
    state_changes = sum(row["knowledge_state_change_count"] for row in rows.values())
    credits_spent = sum(row["credits_spent"] for row in rows.values())
    registered = len(users)
    upload_attempts = sum(row["mistake_upload_attempts"] for row in rows.values())
    upload_successes = sum(row["mistake_upload_successes"] for row in rows.values())
    ocr_completed = sum(row["ocr_completed_count"] for row in rows.values())
    ocr_corrected = sum(row["ocr_corrected_count"] for row in rows.values())
    practice_rounds = sum(row["practice_round_count"] for row in rows.values())
    completed_rounds = sum(row["completed_practice_round_count"] for row in rows.values())
    second_attempts = sum(row["second_attempt_count"] for row in rows.values())
    second_correct = sum(row["second_attempt_correct_count"] for row in rows.values())

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    metrics = {
        "start_at": start,
        "end_at": end,
        "registered_users": registered,
        "activated_users": activated,
        "activation_rate": round(activated / registered, 4) if registered else 0,
        "retention_7d_eligible_users": len(eligible_users),
        "retained_7d_users": retained,
        "retention_7d_rate": round(retained / len(eligible_users), 4) if eligible_users else 0,
        "helpful_votes": helpful,
        "unhelpful_votes": unhelpful,
        "answer_helpfulness_rate": round(helpful / votes, 4) if votes else None,
        "graph_explorers": graph_explorers,
        "graph_exploration_rate": round(graph_explorers / registered, 4) if registered else 0,
        "knowledge_state_changes": state_changes,
        "credits_spent": credits_spent,
        "average_credits_per_active_user": round(credits_spent / activated, 2) if activated else 0,
        "mistake_upload_attempts": upload_attempts,
        "mistake_upload_successes": upload_successes,
        "mistake_upload_success_rate": rate(upload_successes, upload_attempts),
        "ocr_completed_count": ocr_completed,
        "ocr_corrected_count": ocr_corrected,
        "ocr_correction_rate": rate(ocr_corrected, ocr_completed),
        "practice_round_count": practice_rounds,
        "completed_practice_round_count": completed_rounds,
        "practice_completion_rate": rate(completed_rounds, practice_rounds),
        "second_attempt_count": second_attempts,
        "second_attempt_correct_count": second_correct,
        "second_attempt_accuracy": rate(second_correct, second_attempts),
    }
    return metrics, list(rows.values())
