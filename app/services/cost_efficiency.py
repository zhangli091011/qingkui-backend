from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    CreditLedger,
    ModelCall,
    MistakePracticeRound,
    OcrTask,
    User,
    UserRole,
)


def _period(start_at: datetime | None, end_at: datetime | None) -> tuple[datetime, datetime]:
    end = end_at or datetime.now(timezone.utc)
    start = start_at or end - timedelta(days=30)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    if end - start > timedelta(days=366):
        raise HTTPException(status_code=422, detail="单次成本统计范围不能超过 366 天")
    return start, end


def _model_cost(call: ModelCall) -> tuple[float, bool]:
    input_tokens = max(0, int(call.input_tokens or 0))
    output_tokens = max(0, int(call.output_tokens or 0))
    if call.provider == "stub":
        return 0.0, True
    if call.provider == "deepseek":
        input_price = settings.deepseek_input_cost_per_million_cny
        output_price = settings.deepseek_output_cost_per_million_cny
        priced = (input_tokens == 0 or input_price > 0) and (
            output_tokens == 0 or output_price > 0
        )
        cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        return cost, priced
    return 0.0, input_tokens == 0 and output_tokens == 0


def build_cost_efficiency_report(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict:
    start, end = _period(start_at, end_at)
    calls = list(
        db.scalars(
            select(ModelCall).where(ModelCall.created_at >= start, ModelCall.created_at < end)
        )
    )
    successful_ocr = list(
        db.scalars(
            select(OcrTask).where(
                OcrTask.status == "succeeded",
                OcrTask.completed_at >= start,
                OcrTask.completed_at < end,
            )
        )
    )
    rounds = list(
        db.scalars(
            select(MistakePracticeRound).where(
                MistakePracticeRound.started_at >= start,
                MistakePracticeRound.started_at < end,
            )
        )
    )
    completed_loop_ids = set(
        db.scalars(
            select(MistakePracticeRound.mistake_id).where(
                MistakePracticeRound.review_stage == "next_week",
                MistakePracticeRound.status == "completed",
                MistakePracticeRound.completed_at >= start,
                MistakePracticeRound.completed_at < end,
            )
        )
    )

    candidate_user_ids = {
        value
        for value in (
            [call.user_id for call in calls]
            + [task.user_id for task in successful_ocr]
            + [practice_round.user_id for practice_round in rounds]
        )
        if value
    }
    active_students = 0
    if candidate_user_ids:
        active_students = len(
            set(
                db.scalars(
                    select(User.id).where(
                        User.id.in_(candidate_user_ids),
                        User.role == UserRole.student,
                        User.is_active.is_(True),
                    )
                )
            )
        )

    model_cost = 0.0
    mistake_model_cost = 0.0
    unpriced_calls = 0
    for call in calls:
        call_cost, priced = _model_cost(call)
        model_cost += call_cost
        if call.feature in {"mistake_analysis", "mistake_practice_generation"}:
            mistake_model_cost += call_cost
        unpriced_calls += int(not priced)
    ocr_cost = len(successful_ocr) * settings.dashscope_ocr_cost_per_task_cny
    total_cost = model_cost + ocr_cost
    mistake_loop_cost = mistake_model_cost + ocr_cost
    credits_spent = abs(
        sum(
            db.scalars(
                select(CreditLedger.amount).where(
                    CreditLedger.amount < 0,
                    CreditLedger.created_at >= start,
                    CreditLedger.created_at < end,
                )
            )
        )
    )

    started_loop_ids = {task.mistake_id for task in successful_ocr}
    started_loop_ids.update(practice_round.mistake_id for practice_round in rounds)
    started_loop_ids.update(
        call.reference_id
        for call in calls
        if call.feature == "mistake_analysis" and call.reference_id
    )

    def money(value: float) -> float:
        return round(value, 6)

    return {
        "start_at": start,
        "end_at": end,
        "active_students": active_students,
        "model_calls": len(calls),
        "input_tokens": sum(max(0, int(call.input_tokens or 0)) for call in calls),
        "output_tokens": sum(max(0, int(call.output_tokens or 0)) for call in calls),
        "successful_ocr_tasks": len(successful_ocr),
        "credits_spent": credits_spent,
        "estimated_model_cost_cny": money(model_cost),
        "estimated_ocr_cost_cny": money(ocr_cost),
        "estimated_total_cost_cny": money(total_cost),
        "estimated_cost_per_active_student_cny": money(total_cost / active_students) if active_students else 0.0,
        "estimated_mistake_loop_cost_cny": money(mistake_loop_cost),
        "started_mistake_loops": len(started_loop_ids),
        "completed_mistake_loops": len(completed_loop_ids),
        "estimated_cost_per_started_mistake_loop_cny": money(mistake_loop_cost / len(started_loop_ids)) if started_loop_ids else 0.0,
        "estimated_cost_per_completed_mistake_loop_cny": money(mistake_loop_cost / len(completed_loop_ids)) if completed_loop_ids else None,
        "unpriced_model_calls": unpriced_calls,
        "pricing_complete": unpriced_calls == 0 and (
            not successful_ocr or settings.dashscope_ocr_cost_per_task_cny > 0
        ),
    }
