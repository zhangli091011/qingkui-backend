"""Role-aware aggregate workspace APIs.

The workspace is intentionally an aggregation boundary: teachers receive no
student question text or image keys, while admins can drill into existing
admin endpoints after the same permission checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import case, distinct, func, or_, select

from app.config import settings
from app.deps import AuthenticatedUser, DbSession
from app.models import (
    AuditLog,
    ClassMembership,
    ContentContribution,
    CreditLedger,
    FeedbackSubmission,
    KnowledgeDocument,
    KnowledgeNode,
    LearningEvent,
    ModelCall,
    MistakePractice,
    MistakePracticeRound,
    MistakeProblem,
    OcrTask,
    SchoolClass,
    SchoolMembership,
    User,
    UserRole,
)
from app.services.operational_alerts import build_operational_alert_summary
from app.services.release_readiness import build_release_readiness_report


router = APIRouter(prefix="/workspace", tags=["统一工作台"])


def _parse_range(days: int, start_at: str | None, end_at: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if end_at:
        try:
            end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="end_at 必须是 ISO-8601 时间") from exc
        if end.tzinfo is None:
            raise HTTPException(status_code=422, detail="end_at 必须包含时区")
        now = min(now, end.astimezone(timezone.utc))
    if start_at:
        try:
            start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="start_at 必须是 ISO-8601 时间") from exc
        if start.tzinfo is None:
            raise HTTPException(status_code=422, detail="start_at 必须包含时区")
        start = start.astimezone(timezone.utc)
    else:
        start = now - timedelta(days=max(1, min(days, 365)))
    if start >= now:
        raise HTTPException(status_code=422, detail="时间范围必须满足 start_at < end_at")
    return start, now


def _roles(db: DbSession, user: User) -> list[str]:
    roles = {user.role.value if hasattr(user.role, "value") else str(user.role)}
    roles.update(
        db.scalars(
            select(SchoolMembership.role).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.status == "active",
            )
        )
    )
    roles.update(
        db.scalars(
            select(ClassMembership.role).where(
                ClassMembership.user_id == user.id,
                ClassMembership.status == "active",
            )
        )
    )
    return sorted(str(role) for role in roles)


def _scope_user_ids(
    db: DbSession,
    user: User,
    *,
    school_id: str | None,
    class_id: str | None,
) -> tuple[set[str] | None, str]:
    role = user.role.value if hasattr(user.role, "value") else str(user.role)
    if role in {UserRole.admin.value, UserRole.content_admin.value}:
        if class_id:
            ids = set(
                db.scalars(
                    select(ClassMembership.user_id).where(
                        ClassMembership.class_id == class_id,
                        ClassMembership.status == "active",
                    )
                )
            )
            return ids, "admin_class_scope"
        if school_id:
            ids = set(
                db.scalars(
                    select(SchoolMembership.user_id).where(
                        SchoolMembership.school_id == school_id,
                        SchoolMembership.status == "active",
                    )
                )
            )
            return ids, "admin_school_scope"
        return None, "all_authorized_data"

    if role != UserRole.student.value:
        class_query = select(ClassMembership.class_id).where(
            ClassMembership.user_id == user.id,
            ClassMembership.role.in_(("teacher", "school_admin", "owner")),
            ClassMembership.status == "active",
        )
        class_ids = set(db.scalars(class_query))
        if school_id:
            class_ids &= set(
                db.scalars(select(SchoolClass.id).where(SchoolClass.school_id == school_id))
            )
        if class_id:
            class_ids &= {class_id}
        if not class_ids:
            return set(), "teacher_empty_scope"
        ids = set(
            db.scalars(
                select(ClassMembership.user_id).where(
                    ClassMembership.class_id.in_(class_ids),
                    ClassMembership.status == "active",
                )
            )
        )
        return ids, "teacher_aggregate_scope"
    return {user.id}, "self_only"


def _where_user(column, user_ids: set[str] | None):
    return column.in_(user_ids) if user_ids is not None else None


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _utc_datetime(value: datetime | None) -> datetime | None:
    """Normalize SQLite's naive timestamps before comparing with UTC bounds."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _metric_payload(db: DbSession, user_ids: set[str] | None, start: datetime, end: datetime) -> dict[str, Any]:
    event_filter = [LearningEvent.created_at >= start, LearningEvent.created_at < end]
    mistake_filter = [MistakeProblem.created_at >= start, MistakeProblem.created_at < end]
    ocr_filter = [OcrTask.created_at >= start, OcrTask.created_at < end]
    if user_ids is not None:
        event_filter.append(LearningEvent.user_id.in_(user_ids))
        mistake_filter.append(MistakeProblem.user_id.in_(user_ids))
        ocr_filter.append(OcrTask.user_id.in_(user_ids))

    active_students = int(db.scalar(select(func.count(distinct(LearningEvent.user_id))).where(*event_filter)) or 0)
    events = list(db.scalars(select(LearningEvent).where(*event_filter)))
    active_ids = {item.user_id for item in events}
    midpoint = start + (end - start) / 2
    previous_ids = set(
        db.scalars(
            select(distinct(LearningEvent.user_id)).where(
                LearningEvent.created_at >= start - (end - start),
                LearningEvent.created_at < midpoint,
                *([LearningEvent.user_id.in_(user_ids)] if user_ids is not None else []),
            )
        )
    )
    returning_ids = active_ids & previous_ids

    mistakes = list(db.scalars(select(MistakeProblem).where(*mistake_filter)))
    ocr_tasks = list(db.scalars(select(OcrTask).where(*ocr_filter)))
    upload_attempts = int(
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "mistake.image_upload_attempted",
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
                *([AuditLog.actor_user_id.in_(user_ids)] if user_ids is not None else []),
            )
        )
        or 0
    )
    upload_successes = int(
        db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "mistake.image_upload_succeeded",
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
                *([AuditLog.actor_user_id.in_(user_ids)] if user_ids is not None else []),
            )
        )
        or 0
    )
    analysis_completed = sum(item.analysis_status == "completed" for item in mistakes)
    practice_rounds = list(
        db.scalars(
            select(MistakePracticeRound).where(
                MistakePracticeRound.started_at >= start,
                MistakePracticeRound.started_at < end,
                *([MistakePracticeRound.user_id.in_(user_ids)] if user_ids is not None else []),
            )
        )
    )
    completed_rounds = [item for item in practice_rounds if item.status == "completed"]
    second_rounds = [item for item in completed_rounds if item.review_stage in {"next_day", "next_week"}]
    second_correct = sum(item.authoritative_correct_count >= item.question_count for item in second_rounds)
    model_calls = list(
        db.scalars(
            select(ModelCall).where(
                ModelCall.created_at >= start,
                ModelCall.created_at < end,
                *([ModelCall.user_id.in_(user_ids)] if user_ids is not None else []),
            )
        )
    )
    feedback = list(
        db.scalars(
            select(FeedbackSubmission).where(
                FeedbackSubmission.created_at >= start,
                FeedbackSubmission.created_at < end,
                *([FeedbackSubmission.user_id.in_(user_ids)] if user_ids is not None else []),
            )
        )
    )
    helpful = [item for item in feedback if item.category in {"helpful", "answer_helpful"}]
    unhelpful = [item for item in feedback if item.category in {"unhelpful", "answer_unhelpful"}]
    return {
        "active_students": active_students,
        "seven_day_return_rate": _ratio(len(returning_ids), len(previous_ids)),
        "mistake_upload_success_rate": _ratio(upload_successes, upload_attempts),
        "ocr_correction_rate": _ratio(sum(item.status == "succeeded" for item in ocr_tasks), len(ocr_tasks)),
        "ocr_queue": {
            "queued": sum(item.status == "queued" for item in ocr_tasks),
            "processing": sum(item.status in {"recognizing", "processing"} for item in ocr_tasks),
            "failed": sum(item.status == "failed" for item in ocr_tasks),
            "needs_review": sum(item.requires_review for item in ocr_tasks),
        },
        "mistake_analysis_completion_rate": _ratio(analysis_completed, len(mistakes)),
        "same_practice_completion_rate": _ratio(sum(item.status == "completed" for item in practice_rounds), len(practice_rounds)),
        "second_attempt_accuracy": _ratio(second_correct, len(second_rounds)),
        "ai_helpful_rate": _ratio(len(helpful), len(helpful) + len(unhelpful)),
        "ai_calls": len(model_calls),
        "ai_failed_calls": sum(not item.success for item in model_calls),
        "ai_failure_rate": _ratio(sum(not item.success for item in model_calls), len(model_calls)),
        "average_user_cost_tokens": round(
            sum((item.input_tokens or 0) + (item.output_tokens or 0) for item in model_calls) / max(1, len(active_ids)),
            2,
        ),
        "mistakes_created": len(mistakes),
        "error_categories": {
            category: sum(item.error_category == category for item in mistakes)
            for category in ("concept", "reading", "method", "calculation", "expression", "other")
            if any(item.error_category == category for item in mistakes)
        },
        "due_reviews": sum(
            (review_at := _utc_datetime(item.next_review_at)) is not None
            and review_at <= end
            and item.study_status != "mastered"
            for item in mistakes
        ),
    }


def _content_metrics(db: DbSession, user: User) -> dict[str, Any]:
    if user.role not in (UserRole.admin, UserRole.content_admin):
        return {"pending_content": None, "pending_formulas": None, "approved_nodes": None}
    pending_content = int(
        db.scalar(select(func.count(KnowledgeNode.id)).where(KnowledgeNode.review_status == "draft", KnowledgeNode.is_active.is_(False))) or 0
    )
    pending_formulas = int(
        db.scalar(select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.status.in_(("pending", "processing", "failed")))) or 0
    )
    approved_nodes = int(
        db.scalar(select(func.count(KnowledgeNode.id)).where(KnowledgeNode.review_status == "approved", KnowledgeNode.is_active.is_(True))) or 0
    )
    return {"pending_content": pending_content, "pending_formulas": pending_formulas, "approved_nodes": approved_nodes}


def _dashboard(
    db: DbSession,
    user: User,
    *,
    days: int,
    start_at: str | None,
    end_at: str | None,
    school_id: str | None,
    class_id: str | None,
    grade: str | None,
    subject: str | None,
    textbook_version: str | None,
) -> dict[str, Any]:
    start, end = _parse_range(days, start_at, end_at)
    user_ids, scope = _scope_user_ids(db, user, school_id=school_id, class_id=class_id)
    metrics = _metric_payload(db, user_ids, start, end)
    alerts = build_operational_alert_summary(db, now=end)
    governance = _content_metrics(db, user)
    if grade or subject or textbook_version:
        # These dimensions are currently content filters. Learning events retain
        # no subject column, so do not claim a false filtered student metric.
        governance["filter_note"] = "学习事件尚未记录学科/教材维度，内容指标按筛选条件由专用接口下钻。"
    return {
        "schema": "qingkui-workspace-dashboard-v1",
        "generated_at": datetime.now(timezone.utc),
        "range": {"start_at": start, "end_at": end, "days": (end - start).days or 1},
        "filters": {
            "school_id": school_id,
            "class_id": class_id,
            "grade": grade,
            "subject": subject,
            "textbook_version": textbook_version,
            "environment": settings.app_env,
        },
        "viewer": {"user_id": user.id, "username": user.username, "roles": _roles(db, user), "scope": scope},
        "metrics": {**metrics, **governance, "security_events": 0},
        "alerts": alerts,
    }


@router.get("/me")
def workspace_me(db: DbSession, user: AuthenticatedUser) -> dict[str, Any]:
    user_ids, scope = _scope_user_ids(db, user, school_id=None, class_id=None)
    memberships = list(
        db.execute(
            select(SchoolMembership.school_id, SchoolMembership.role).where(
                SchoolMembership.user_id == user.id,
                SchoolMembership.status == "active",
            )
        )
    )
    return {
        "schema": "qingkui-workspace-me-v1",
        "user": {"id": user.id, "username": user.username, "nickname": user.nickname, "role": user.role.value},
        "roles": _roles(db, user),
        "scope": scope,
        "school_ids": sorted({row[0] for row in memberships}),
        "capabilities": {
            "student_workspace": True,
            "teacher_workspace": user.role != UserRole.student or any(row[1] in {"teacher", "school_admin"} for row in memberships),
            "content_workspace": user.role in (UserRole.admin, UserRole.content_admin),
            "operations_workspace": user.role == UserRole.admin,
            "raw_student_content": user.role == UserRole.admin,
        },
        "data_scope_user_count": None if user_ids is None else len(user_ids),
    }


@router.get("/dashboard")
def workspace_dashboard(
    db: DbSession,
    user: AuthenticatedUser,
    days: int = Query(default=7, ge=1, le=365),
    start_at: str | None = None,
    end_at: str | None = None,
    school_id: str | None = None,
    class_id: str | None = None,
    grade: str | None = None,
    subject: str | None = None,
    textbook_version: str | None = None,
) -> dict[str, Any]:
    return _dashboard(
        db,
        user,
        days=days,
        start_at=start_at,
        end_at=end_at,
        school_id=school_id,
        class_id=class_id,
        grade=grade,
        subject=subject,
        textbook_version=textbook_version,
    )


@router.get("/metrics")
def workspace_metrics(
    db: DbSession,
    user: AuthenticatedUser,
    days: int = Query(default=7, ge=1, le=365),
    start_at: str | None = None,
    end_at: str | None = None,
    school_id: str | None = None,
    class_id: str | None = None,
) -> dict[str, Any]:
    start, end = _parse_range(days, start_at, end_at)
    user_ids, scope = _scope_user_ids(db, user, school_id=school_id, class_id=class_id)
    return {"schema": "qingkui-workspace-metrics-v1", "scope": scope, "range": {"start_at": start, "end_at": end}, "metrics": _metric_payload(db, user_ids, start, end)}


@router.get("/alerts")
def workspace_alerts(db: DbSession, user: AuthenticatedUser) -> dict[str, Any]:
    if user.role == UserRole.student:
        return {"schema": "qingkui-workspace-alerts-v1", "status": "ok", "alerts": [], "restricted": True}
    summary = build_operational_alert_summary(db)
    failed_release = []
    if user.role == UserRole.admin:
        readiness = build_release_readiness_report(db)
        failed_release = [item for item in readiness["checks"] if not item["passed"]]
    return {"schema": "qingkui-workspace-alerts-v1", **summary, "release_blockers": failed_release}


@router.get("/tasks")
def workspace_tasks(
    db: DbSession,
    user: AuthenticatedUser,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    user_ids, scope = _scope_user_ids(db, user, school_id=None, class_id=None)
    ocr_query = select(OcrTask).order_by(OcrTask.created_at.desc()).limit(limit)
    if user_ids is not None:
        ocr_query = ocr_query.where(OcrTask.user_id.in_(user_ids))
    if status:
        ocr_query = ocr_query.where(OcrTask.status == status)
    ocr = list(db.scalars(ocr_query))
    contributions = []
    if user.role in (UserRole.admin, UserRole.content_admin):
        contribution_query = select(ContentContribution).order_by(ContentContribution.created_at.desc()).limit(limit)
        if status:
            contribution_query = contribution_query.where(ContentContribution.status == status)
        contributions = list(db.scalars(contribution_query))
    items = [
        {
            "task_type": "ocr",
            "id": item.id,
            "status": item.status,
            "requires_review": item.requires_review,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "error_code": item.error_code,
            "scope": scope,
        }
        for item in ocr
    ]
    if user.role in (UserRole.admin, UserRole.content_admin):
        items.extend(
            {
                "task_type": "contribution_review",
                "id": item.id,
                "status": item.status,
                "requires_review": item.status in {"queued", "screened", "review_failed"},
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "error_code": None,
                "scope": "admin",
            }
            for item in contributions
        )
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {"schema": "qingkui-workspace-tasks-v1", "scope": scope, "items": items[:limit], "privacy": {"student_text_and_images_hidden": user.role != UserRole.admin}}


@router.get("/activity")
def workspace_activity(db: DbSession, user: AuthenticatedUser, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    user_ids, scope = _scope_user_ids(db, user, school_id=None, class_id=None)
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if user_ids is not None:
        query = query.where(AuditLog.actor_user_id.in_(user_ids))
    rows = list(db.scalars(query))
    return {
        "schema": "qingkui-workspace-activity-v1",
        "scope": scope,
        "items": [
            {"id": row.id, "action": row.action, "target_type": row.target_type, "target_id": row.target_id, "created_at": row.created_at}
            for row in rows
        ],
    }
