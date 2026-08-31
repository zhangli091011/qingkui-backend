import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import CurrentUser, DbSession
from app.models import (
    AuditLog,
    CreditAccount,
    CreditLedger,
    KnowledgeNode,
    KnowledgeStatus,
    LearningEvent,
    MistakeAsset,
    MistakePractice,
    MistakePracticeRound,
    MistakeProblem,
    OcrTask,
    UserKnowledgeState,
    new_id,
)
from app.schemas import (
    MessageResponse,
    MistakeCreate,
    MistakeAnalysisData,
    MistakeAnalysisResponse,
    MistakePracticeResponse,
    MistakePracticeRoundResponse,
    MistakeResponse,
    MistakeWeeklyReview,
    MistakeUpdate,
    OcrCorrection,
    OcrTaskResponse,
    PracticeCreate,
    PracticeSubmit,
    WeeklyMistakeLink,
)
from app.services.knowledge import retrieve_chunks, retrieve_nodes
from app.services.content_safety import moderate_text, moderation_text, record_safety_event
from app.services.mistake_analysis import PROMPT_VERSION as ANALYSIS_PROMPT_VERSION
from app.services.mistake_analysis import analyze_mistake_content, generate_similar_practices
from app.services.mistakes import delete_assets, enqueue_ocr_task, store_mistake_image
from app.services.model_calls import add_model_call, start_model_timer
from app.services.object_storage import delete_private_object, get_private_bytes
from app.services.practice_validation import validate_practice_answer


router = APIRouter(prefix="/mistakes", tags=["错题本"])


def _require_ai_available() -> None:
    if not settings.ai_enabled:
        raise HTTPException(status_code=503, detail="AI 功能正在维护，已保存的错题不会丢失")
    if not settings.ai_ready:
        raise HTTPException(status_code=503, detail="AI 服务尚未配置")


def _enforce_safe_mistake_input(
    db: DbSession,
    user_id: str,
    content: str,
    *,
    target_id: str | None = None,
) -> None:
    decision = moderate_text(content)
    if decision.allowed:
        return
    record_safety_event(
        db,
        user_id=user_id,
        action="safety.mistake_input_blocked",
        decision=decision,
        content=content,
        target_type="mistake",
        target_id=target_id,
    )
    db.commit()
    raise HTTPException(
        status_code=422,
        detail=decision.message,
        headers={"X-Content-Safety": "blocked"},
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _round_loader():
    return selectinload(MistakeProblem.practice_rounds).selectinload(MistakePracticeRound.practices)


def _owned_mistake(db: DbSession, mistake_id: str, user_id: str, *, detail: bool = False) -> MistakeProblem:
    query = select(MistakeProblem).where(MistakeProblem.id == mistake_id, MistakeProblem.user_id == user_id)
    if detail:
        query = query.options(
            selectinload(MistakeProblem.assets),
            selectinload(MistakeProblem.ocr_tasks),
            selectinload(MistakeProblem.practices),
            _round_loader(),
        )
    mistake = db.scalar(query)
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    return mistake


@router.post("", response_model=MistakeResponse, status_code=201)
def create_mistake(payload: MistakeCreate, db: DbSession, user: CurrentUser) -> MistakeProblem:
    if not any((payload.question_text, payload.student_work, payload.question_goal)):
        raise HTTPException(status_code=422, detail="请至少填写题目、作答过程或提问目标")
    _enforce_safe_mistake_input(
        db,
        user.id,
        moderation_text(payload.question_text, payload.student_work, payload.question_goal, payload.error_category),
    )
    mistake = MistakeProblem(user_id=user.id, **payload.model_dump())
    db.add(mistake)
    db.flush()
    db.add(AuditLog(actor_user_id=user.id, action="mistake.created", target_type="mistake", target_id=mistake.id))
    db.commit()
    return _owned_mistake(db, mistake.id, user.id, detail=True)


@router.get("", response_model=list[MistakeResponse])
def list_mistakes(
    db: DbSession,
    user: CurrentUser,
    error_category: str | None = None,
    study_status: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> list[MistakeProblem]:
    query = (
        select(MistakeProblem)
        .where(MistakeProblem.user_id == user.id)
        .options(
            selectinload(MistakeProblem.assets),
            selectinload(MistakeProblem.ocr_tasks),
            selectinload(MistakeProblem.practices),
            _round_loader(),
        )
        .order_by(MistakeProblem.updated_at.desc())
        .limit(limit)
    )
    if error_category:
        query = query.where(MistakeProblem.error_category == error_category)
    if study_status:
        query = query.where(MistakeProblem.study_status == study_status)
    return list(db.scalars(query))


@router.get("/review/weekly", response_model=MistakeWeeklyReview)
def weekly_review(
    db: DbSession,
    user: CurrentUser,
    week_start: date | None = None,
) -> MistakeWeeklyReview:
    today = datetime.now(timezone.utc).date()
    start_date = week_start or (today - timedelta(days=today.weekday()))
    end_date = start_date + timedelta(days=7)
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date, time.min, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    mistakes = list(db.scalars(select(MistakeProblem).where(MistakeProblem.user_id == user.id)))
    rounds = list(
        db.scalars(
            select(MistakePracticeRound)
            .where(MistakePracticeRound.user_id == user.id)
            .options(selectinload(MistakePracticeRound.practices))
        )
    )
    new_items = [item for item in mistakes if start_at <= _utc(item.created_at) < end_at]
    category_counts = Counter(item.error_category or "unclassified" for item in new_items)
    weak_counts = Counter(
        item.knowledge_node_id
        for item in mistakes
        if item.study_status != "mastered" and item.knowledge_node_id
    )
    weak_nodes = []
    if weak_counts:
        names = dict(
            db.execute(
                select(KnowledgeNode.id, KnowledgeNode.name).where(KnowledgeNode.id.in_(weak_counts.keys()))
            ).all()
        )
        weak_nodes = [
            {"knowledge_node_id": node_id, "name": names.get(node_id, node_id), "mistake_count": count}
            for node_id, count in weak_counts.most_common(5)
        ]

    active_rounds = {item.mistake_id: item for item in rounds if item.status == "active"}
    due_items = [
        item
        for item in mistakes
        if item.study_status != "mastered"
        and item.next_review_at is not None
        and _utc(item.next_review_at) <= now
    ]
    due_links = [
        WeeklyMistakeLink(
            mistake_id=item.id,
            practice_round_id=active_rounds.get(item.id).id if active_rounds.get(item.id) else None,
            knowledge_node_id=item.knowledge_node_id,
            title=(item.corrected_text or item.question_text or "图片错题")[:120],
            review_stage=item.review_stage,
            next_review_at=item.next_review_at,
        )
        for item in sorted(due_items, key=lambda value: _utc(value.next_review_at))
    ]

    weekly_rounds = [item for item in rounds if start_at <= _utc(item.started_at) < end_at]
    completed = [item for item in weekly_rounds if item.status == "completed"]
    submitted = [practice for item in weekly_rounds for practice in item.practices if practice.status == "completed"]
    authoritative = [practice for practice in submitted if practice.validation_details.get("authoritative") is True]
    second_attempts = [
        item for item in mistakes
        if item.second_attempt_correct is not None and item.last_reviewed_at and start_at <= _utc(item.last_reviewed_at) < end_at
    ]
    eligible_followups = [
        item for item in mistakes
        if item.first_corrected_at is not None and _utc(item.first_corrected_at) <= min(now, end_at) - timedelta(days=7)
    ]
    next_week_completed_ids = {
        item.mistake_id for item in rounds if item.review_stage == "next_week" and item.status == "completed"
    }
    upload_events = list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.actor_user_id == user.id,
                AuditLog.action.in_(("mistake.image_upload_attempted", "mistake.image_upload_succeeded", "mistake.ocr_confirmed")),
                AuditLog.created_at >= start_at,
                AuditLog.created_at < end_at,
            )
        )
    )
    upload_attempts = sum(item.action == "mistake.image_upload_attempted" for item in upload_events)
    upload_successes = sum(item.action == "mistake.image_upload_succeeded" for item in upload_events)
    ocr_confirmed = sum(item.action == "mistake.ocr_confirmed" for item in upload_events)
    ocr_completed = db.scalar(
        select(func.count(OcrTask.id)).where(
            OcrTask.user_id == user.id,
            OcrTask.status == "succeeded",
            OcrTask.completed_at >= start_at,
            OcrTask.completed_at < end_at,
        )
    ) or 0

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return MistakeWeeklyReview(
        week_start=start_date,
        week_end=end_date,
        new_mistakes=len(new_items),
        error_categories=dict(category_counts),
        weak_knowledge_points=weak_nodes,
        due_reviews=due_links,
        upload_success_rate=ratio(upload_successes, upload_attempts),
        ocr_correction_rate=ratio(ocr_confirmed, ocr_completed),
        practice_completion_rate=ratio(len(completed), len(weekly_rounds)),
        authoritative_accuracy=ratio(sum(item.is_correct is True for item in authoritative), len(authoritative)),
        second_attempt_accuracy=ratio(sum(item.second_attempt_correct is True for item in second_attempts), len(second_attempts)),
        seven_day_followup_rate=ratio(
            sum(item.id in next_week_completed_ids for item in eligible_followups), len(eligible_followups)
        ),
    )


@router.get("/{mistake_id}", response_model=MistakeResponse)
def get_mistake(mistake_id: str, db: DbSession, user: CurrentUser) -> MistakeProblem:
    return _owned_mistake(db, mistake_id, user.id, detail=True)


@router.patch("/{mistake_id}", response_model=MistakeResponse)
def update_mistake(mistake_id: str, payload: MistakeUpdate, db: DbSession, user: CurrentUser) -> MistakeProblem:
    mistake = _owned_mistake(db, mistake_id, user.id)
    values = payload.model_dump(exclude_unset=True)
    _enforce_safe_mistake_input(db, user.id, moderation_text(values), target_id=mistake.id)
    node_id = values.get("knowledge_node_id")
    if node_id is not None:
        if not db.scalar(select(KnowledgeNode.id).where(KnowledgeNode.id == node_id, KnowledgeNode.is_active.is_(True))):
            raise HTTPException(status_code=404, detail="知识点不存在")
        values["link_status"] = "confirmed"
    for key, value in values.items():
        setattr(mistake, key, value)
    if values.keys() & {"subject", "question_text", "corrected_text", "student_work", "question_goal"}:
        mistake.analysis = {}
        mistake.analysis_status = "not_started"
        mistake.analysis_provider = None
        mistake.analysis_model = None
        mistake.analyzed_at = None
    db.add(AuditLog(actor_user_id=user.id, action="mistake.updated", target_type="mistake", target_id=mistake.id))
    db.commit()
    return _owned_mistake(db, mistake_id, user.id, detail=True)


@router.delete("/{mistake_id}", response_model=MessageResponse)
def delete_mistake(mistake_id: str, db: DbSession, user: CurrentUser) -> MessageResponse:
    mistake = _owned_mistake(db, mistake_id, user.id, detail=True)
    try:
        delete_assets(list(mistake.assets))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="图片存储暂时不可用，错题未删除") from exc
    target_id = mistake.id
    db.delete(mistake)
    db.add(AuditLog(actor_user_id=user.id, action="mistake.deleted", target_type="mistake", target_id=target_id))
    db.commit()
    return MessageResponse(message="错题及图片已删除")


@router.post("/{mistake_id}/images", response_model=OcrTaskResponse, status_code=202)
async def upload_image(
    mistake_id: str,
    db: DbSession,
    user: CurrentUser,
    image: UploadFile = File(...),
) -> OcrTask:
    mistake = _owned_mistake(db, mistake_id, user.id)
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="mistake.image_upload_attempted",
            target_type="mistake",
            target_id=mistake.id,
            details={"filename": image.filename, "content_type": image.content_type},
        )
    )
    payload = await image.read(settings.user_upload_max_bytes + 1)
    asset_id = new_id()
    try:
        clean, object_key = store_mistake_image(
            user_id=user.id, mistake_id=mistake.id, asset_id=asset_id, payload=payload
        )
    except ValueError as exc:
        db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        db.commit()
        raise HTTPException(status_code=503, detail="图片存储暂时不可用") from exc
    asset = MistakeAsset(
        id=asset_id,
        mistake_id=mistake.id,
        user_id=user.id,
        object_key=object_key,
        mime_type=clean.mime_type,
        size_bytes=len(clean.payload),
        checksum_sha256=clean.checksum_sha256,
        width=clean.width,
        height=clean.height,
        status="queued",
    )
    task = OcrTask(
        mistake_id=mistake.id,
        asset_id=asset.id,
        user_id=user.id,
        status="queued",
        queued_at=datetime.now(timezone.utc),
    )
    db.add_all((asset, task))
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="mistake.image_upload_succeeded",
            target_type="mistake_asset",
            target_id=asset.id,
            details={"size_bytes": asset.size_bytes, "mime_type": asset.mime_type},
        )
    )
    db.commit()
    db.refresh(task)
    try:
        enqueue_ocr_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error_code = "queue_unavailable"
        task.error_message = "OCR 队列暂时不可用"
        task.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=503, detail="OCR 队列暂时不可用，可稍后重试") from exc
    return task


@router.get("/{mistake_id}/images/{asset_id}")
def download_image(mistake_id: str, asset_id: str, db: DbSession, user: CurrentUser) -> Response:
    _owned_mistake(db, mistake_id, user.id)
    asset = db.scalar(
        select(MistakeAsset).where(
            MistakeAsset.id == asset_id,
            MistakeAsset.mistake_id == mistake_id,
            MistakeAsset.user_id == user.id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        payload = get_private_bytes(asset.object_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="图片存储暂时不可用") from exc
    return Response(payload, media_type=asset.mime_type, headers={"Cache-Control": "private, no-store"})


@router.delete("/{mistake_id}/images/{asset_id}", response_model=MessageResponse)
def delete_image(mistake_id: str, asset_id: str, db: DbSession, user: CurrentUser) -> MessageResponse:
    _owned_mistake(db, mistake_id, user.id)
    asset = db.scalar(select(MistakeAsset).where(MistakeAsset.id == asset_id, MistakeAsset.user_id == user.id))
    if asset is None or asset.mistake_id != mistake_id:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        delete_private_object(asset.object_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="图片存储暂时不可用，图片未删除") from exc
    db.delete(asset)
    db.commit()
    return MessageResponse(message="图片已删除")


@router.get("/{mistake_id}/ocr/{task_id}", response_model=OcrTaskResponse)
def get_ocr_task(mistake_id: str, task_id: str, db: DbSession, user: CurrentUser) -> OcrTask:
    _owned_mistake(db, mistake_id, user.id)
    task = db.scalar(select(OcrTask).where(OcrTask.id == task_id, OcrTask.user_id == user.id))
    if task is None or task.mistake_id != mistake_id:
        raise HTTPException(status_code=404, detail="OCR 任务不存在")
    return task


@router.post("/{mistake_id}/ocr/{task_id}/cancel", response_model=OcrTaskResponse)
def cancel_ocr_task(mistake_id: str, task_id: str, db: DbSession, user: CurrentUser) -> OcrTask:
    task = get_ocr_task(mistake_id, task_id, db, user)
    if task.status in {"succeeded", "failed", "cancelled", "blocked"}:
        raise HTTPException(status_code=409, detail="当前任务状态不能取消")
    task.cancel_requested = True
    if task.status == "queued":
        task.status = "cancelled"
        task.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return task


@router.post("/{mistake_id}/ocr/{task_id}/retry", response_model=OcrTaskResponse, status_code=202)
def retry_ocr_task(mistake_id: str, task_id: str, db: DbSession, user: CurrentUser) -> OcrTask:
    task = get_ocr_task(mistake_id, task_id, db, user)
    if task.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    task.status = "queued"
    task.attempts = 0
    task.cancel_requested = False
    task.error_code = None
    task.error_message = None
    task.result_text = None
    task.formulas = []
    task.confidence = None
    task.requires_review = True
    task.review_reasons = []
    task.completed_at = None
    task.queued_at = datetime.now(timezone.utc)
    db.commit()
    try:
        enqueue_ocr_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error_code = "queue_unavailable"
        task.error_message = "OCR 队列暂时不可用"
        task.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=503, detail="OCR 队列暂时不可用") from exc
    db.refresh(task)
    return task


@router.post("/{mistake_id}/ocr/{task_id}/confirm", response_model=MistakeResponse)
def confirm_ocr(
    mistake_id: str, task_id: str, payload: OcrCorrection, db: DbSession, user: CurrentUser
) -> MistakeProblem:
    mistake = _owned_mistake(db, mistake_id, user.id)
    task = get_ocr_task(mistake_id, task_id, db, user)
    if task.status != "succeeded":
        raise HTTPException(status_code=409, detail="OCR 尚未成功完成")
    _enforce_safe_mistake_input(db, user.id, payload.corrected_text, target_id=mistake.id)
    mistake.corrected_text = payload.corrected_text
    mistake.question_text = payload.corrected_text
    mistake.review_status = "confirmed"
    mistake.analysis = {}
    mistake.analysis_status = "not_started"
    mistake.analysis_provider = None
    mistake.analysis_model = None
    mistake.analyzed_at = None
    task.requires_review = False
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="mistake.ocr_confirmed",
            target_type="ocr_task",
            target_id=task.id,
            details={"review_reasons": task.review_reasons},
        )
    )
    db.commit()
    return _owned_mistake(db, mistake_id, user.id, detail=True)


@router.post("/{mistake_id}/analyze", response_model=MistakeAnalysisResponse)
def analyze_mistake(mistake_id: str, db: DbSession, user: CurrentUser) -> MistakeAnalysisResponse:
    mistake = db.scalar(
        select(MistakeProblem)
        .where(MistakeProblem.id == mistake_id, MistakeProblem.user_id == user.id)
        .options(selectinload(MistakeProblem.ocr_tasks))
        .with_for_update()
    )
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    question = (mistake.corrected_text or mistake.question_text or "").strip()
    if not question:
        raise HTTPException(status_code=409, detail="请先补充或确认题目文本")
    if any(task.status == "succeeded" and task.requires_review for task in mistake.ocr_tasks) and not mistake.corrected_text:
        raise HTTPException(status_code=409, detail="OCR 结果存在置信度或完整性风险，必须先人工校对")
    _enforce_safe_mistake_input(
        db,
        user.id,
        moderation_text(question, mistake.student_work, mistake.question_goal),
        target_id=mistake.id,
    )

    account = db.scalar(
        select(CreditAccount).where(CreditAccount.user_id == user.id).with_for_update()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="额度账户不存在")
    if mistake.analysis_status == "completed" and mistake.analysis:
        return MistakeAnalysisResponse(
            mistake_id=mistake.id,
            analysis=MistakeAnalysisData.model_validate(mistake.analysis),
            credits_charged=0,
            balance=account.balance,
            provider=mistake.analysis_provider or "unknown",
            model=mistake.analysis_model or "unknown",
        )
    _require_ai_available()
    cost = 2
    if account.balance < cost:
        raise HTTPException(status_code=402, detail="额度不足")

    nodes = retrieve_nodes(db, question, mistake.knowledge_node_id, limit=8, subject=mistake.subject)
    chunks = retrieve_chunks(db, question, limit=8, subject=mistake.subject)
    mistake.analysis_status = "processing"
    db.flush()
    model_started_at = start_model_timer()
    try:
        ai_result = analyze_mistake_content(
            question=question,
            student_work=mistake.student_work,
            question_goal=mistake.question_goal,
            nodes=nodes,
            chunks=chunks,
        )
    except RuntimeError as exc:
        db.rollback()
        add_model_call(
            db,
            user_id=user.id,
            feature="mistake_analysis",
            provider=settings.ai_provider,
            model=settings.deepseek_model if settings.ai_provider == "deepseek" else "grounded-stub",
            success=False,
            started_at=model_started_at,
            error_code="provider_error",
            reference_id=mistake.id,
        )
        db.commit()
        raise HTTPException(status_code=502, detail="错题分析服务暂时不可用，未扣除额度") from exc

    candidate_ids = {node.id for node in nodes}
    analysis = ai_result.analysis
    output_content = moderation_text(analysis.model_dump())
    output_decision = moderate_text(output_content)
    if not output_decision.allowed:
        db.rollback()
        record_safety_event(
            db,
            user_id=user.id,
            action="safety.mistake_output_blocked",
            decision=output_decision,
            content=output_content,
            target_type="mistake",
            target_id=mistake_id,
        )
        add_model_call(
            db,
            user_id=user.id,
            feature="mistake_analysis",
            provider=ai_result.provider,
            model=ai_result.model,
            success=False,
            started_at=model_started_at,
            error_code="output_safety_blocked",
            reference_id=mistake_id,
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail="分析结果触发内容安全保护，未扣除额度。",
            headers={"X-Content-Safety": "blocked"},
        )
    if analysis.suggested_node_id not in candidate_ids:
        analysis = analysis.model_copy(
            update={"suggested_node_id": None, "node_confidence": 0, "uncertain": True}
        )
    values = analysis.model_dump()
    mistake.analysis = {**values, "prompt_version": ANALYSIS_PROMPT_VERSION}
    mistake.analysis_status = "completed"
    mistake.analysis_provider = ai_result.provider
    mistake.analysis_model = ai_result.model
    mistake.analyzed_at = datetime.now(timezone.utc)
    mistake.error_category = analysis.error_category
    mistake.error_note = analysis.error_note
    if analysis.suggested_node_id:
        mistake.knowledge_node_id = analysis.suggested_node_id
        mistake.link_status = "pending"
    account.balance -= cost
    db.add(
        CreditLedger(
            user_id=user.id,
            amount=-cost,
            balance_after=account.balance,
            entry_type="mistake_analysis_charge",
            feature="mistake_analysis",
            reference_id=mistake.id,
            provider=ai_result.provider,
            model=ai_result.model,
        )
    )
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=mistake.knowledge_node_id,
            event_type="analyzed_mistake",
            event_data={
                "mistake_id": mistake.id,
                "error_category": analysis.error_category,
                "node_confidence": analysis.node_confidence,
            },
        )
    )
    add_model_call(
        db,
        user_id=user.id,
        feature="mistake_analysis",
        provider=ai_result.provider,
        model=ai_result.model,
        success=True,
        started_at=model_started_at,
        input_tokens=ai_result.input_tokens,
        output_tokens=ai_result.output_tokens,
        reference_id=mistake.id,
    )
    db.commit()
    return MistakeAnalysisResponse(
        mistake_id=mistake.id,
        analysis=analysis,
        credits_charged=cost,
        balance=account.balance,
        provider=ai_result.provider,
        model=ai_result.model,
    )


@router.post("/{mistake_id}/practices", response_model=MistakePracticeResponse, status_code=201)
def create_practice(
    mistake_id: str, payload: PracticeCreate, db: DbSession, user: CurrentUser
) -> MistakePractice:
    mistake = _owned_mistake(db, mistake_id, user.id)
    source_question = mistake.corrected_text or mistake.question_text
    question = payload.question_text or (f"请重新分析并解答这道同类题：\n{source_question}" if source_question else None)
    if not question:
        raise HTTPException(status_code=409, detail="请先补充或确认题目文本")
    _enforce_safe_mistake_input(
        db,
        user.id,
        moderation_text(question, payload.answer_reference),
        target_id=mistake.id,
    )
    practice = MistakePractice(
        mistake_id=mistake.id,
        user_id=user.id,
        question_text=question,
        answer_reference=payload.answer_reference,
        source="manual" if payload.question_text else "derived",
    )
    db.add(practice)
    db.commit()
    db.refresh(practice)
    return practice


def _practice_candidates(analysis: dict, excluded_questions: set[str] | None = None) -> list[dict[str, str]]:
    excluded = {re.sub(r"\s+", "", value).casefold() for value in (excluded_questions or set())}
    candidates = analysis.get("similar_practices") or []
    if not isinstance(candidates, list):
        return []
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in candidates:
        if not isinstance(value, dict):
            continue
        question = str(value.get("question") or "").strip()
        hint = str(value.get("hint") or "").strip()
        answer = str(value.get("answer_reference") or "").strip()
        normalized = re.sub(r"\s+", "", question).casefold()
        if not question or not hint or not answer or normalized in seen or normalized in excluded:
            continue
        seen.add(normalized)
        unique.append({"question": question, "hint": hint, "answer_reference": answer})
    legacy_question = str(analysis.get("similar_question") or "").strip()
    legacy_answer = str(analysis.get("answer_reference") or "").strip()
    if len(unique) < 2 and legacy_question and legacy_answer:
        legacy_values = [
            {
                "question": legacy_question,
                "hint": "先整理题目条件并确定适用的方法。",
                "answer_reference": legacy_answer,
            },
            {
                "question": f"请先写出关键条件，再完成这道同类题：{legacy_question}",
                "hint": "把已知量、未知量和限制条件分开列出。",
                "answer_reference": legacy_answer,
            },
            {
                "question": f"请换一种检验顺序解答并核对结果：{legacy_question}",
                "hint": "得出答案后代回原条件检查边界和符号。",
                "answer_reference": legacy_answer,
            },
        ]
        for value in legacy_values:
            normalized = re.sub(r"\s+", "", value["question"]).casefold()
            if normalized not in seen and normalized not in excluded:
                seen.add(normalized)
                unique.append(value)
    return unique[:3]


@router.post("/{mistake_id}/practices/generate", response_model=MistakePracticeRoundResponse, status_code=201)
def generate_practice(mistake_id: str, db: DbSession, user: CurrentUser) -> MistakePracticeRound:
    mistake = db.scalar(
        select(MistakeProblem)
        .where(MistakeProblem.id == mistake_id, MistakeProblem.user_id == user.id)
        .with_for_update()
    )
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    if mistake.analysis_status != "completed" or not mistake.analysis:
        raise HTTPException(status_code=409, detail="请先完成错题分析")
    existing = db.scalar(
        select(MistakePracticeRound)
        .where(
            MistakePracticeRound.mistake_id == mistake.id,
            MistakePracticeRound.user_id == user.id,
            MistakePracticeRound.status == "active",
        )
        .options(selectinload(MistakePracticeRound.practices))
        .order_by(MistakePracticeRound.round_number.desc())
    )
    if existing is not None:
        return existing
    _require_ai_available()
    if mistake.review_stage == "completed" or mistake.study_status == "mastered":
        raise HTTPException(status_code=409, detail="该错题已完成隔周复习")
    now = datetime.now(timezone.utc)
    if mistake.next_review_at is not None and _utc(mistake.next_review_at) > now:
        raise HTTPException(status_code=409, detail=f"下一轮复习将在 {_utc(mistake.next_review_at).isoformat()} 开放")
    prior_practices = list(
        db.scalars(
            select(MistakePractice.question_text).where(
                MistakePractice.mistake_id == mistake.id,
                MistakePractice.user_id == user.id,
            )
        )
    )
    # Once a round has been completed, never fall back to the legacy single-
    # practice fields: those fields can recreate the original question. New
    # rounds must come from the fresh-variant generator.
    candidates = [] if prior_practices else _practice_candidates(mistake.analysis)
    generated_provider = "analysis"
    generated_model = "mistake-analysis"
    generated_usage: tuple[int | None, int | None] = (None, None)
    if len(candidates) < 2:
        source_question = (mistake.corrected_text or mistake.question_text or "").strip()
        generation_started_at = start_model_timer()
        try:
            generated = generate_similar_practices(
                question=source_question,
                diagnosis=str(mistake.analysis.get("diagnosis") or ""),
                error_category=mistake.error_category,
                review_stage=mistake.review_stage,
                excluded_questions=set(prior_practices),
                nodes=retrieve_nodes(db, source_question, mistake.knowledge_node_id, limit=8, subject=mistake.subject),
                chunks=retrieve_chunks(db, source_question, limit=8, subject=mistake.subject),
            )
        except RuntimeError as exc:
            db.rollback()
            raise HTTPException(status_code=502, detail="变式练习生成服务暂时不可用，请稍后重试") from exc
        candidates = [
            {
                "question": item.question,
                "hint": item.hint,
                "answer_reference": item.answer_reference,
            }
            for item in generated.practices
        ]
        generated_provider = generated.provider
        generated_model = generated.model
        generated_usage = (generated.input_tokens, generated.output_tokens)
    if len(candidates) < 2:
        raise HTTPException(status_code=409, detail="无法生成两道不重复的有效练习，请补充题目后重试")
    candidates = candidates[:3]
    generated_text = moderation_text(
        *[part for value in candidates for part in (value["question"], value["hint"], value["answer_reference"])]
    )
    generated_decision = moderate_text(generated_text)
    if not generated_decision.allowed:
        db.rollback()
        record_safety_event(
            db,
            user_id=user.id,
            action="safety.mistake_practice_output_blocked",
            decision=generated_decision,
            content=generated_text,
            target_type="mistake",
            target_id=mistake.id,
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail="生成的练习触发内容安全保护，未保存。请稍后重试。",
            headers={"X-Content-Safety": "blocked"},
        )
    round_number = (db.scalar(
        select(func.max(MistakePracticeRound.round_number)).where(MistakePracticeRound.mistake_id == mistake.id)
    ) or 0) + 1
    practice_round = MistakePracticeRound(
        id=new_id(),
        mistake_id=mistake.id,
        user_id=user.id,
        round_number=round_number,
        review_stage=mistake.review_stage,
        question_count=len(candidates),
    )
    db.add(practice_round)
    db.add_all(
        MistakePractice(
            mistake_id=mistake.id,
            user_id=user.id,
            round_id=practice_round.id,
            position=index,
            question_text=value["question"],
            hint=value["hint"],
            answer_reference=value["answer_reference"],
            source="ai_analysis",
        )
        for index, value in enumerate(candidates, start=1)
    )
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="mistake.practice_round_started",
            target_type="mistake_practice_round",
            target_id=practice_round.id,
            details={
                "mistake_id": mistake.id,
                "review_stage": mistake.review_stage,
                "question_count": len(candidates),
                "provider": generated_provider,
            },
        )
    )
    if generated_provider != "analysis":
        add_model_call(
            db,
            user_id=user.id,
            feature="mistake_practice_generation",
            provider=generated_provider,
            model=generated_model,
            success=True,
            started_at=generation_started_at,
            input_tokens=generated_usage[0],
            output_tokens=generated_usage[1],
            reference_id=practice_round.id,
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent_round = db.scalar(
            select(MistakePracticeRound)
            .where(
                MistakePracticeRound.mistake_id == mistake_id,
                MistakePracticeRound.user_id == user.id,
                MistakePracticeRound.status == "active",
            )
            .options(selectinload(MistakePracticeRound.practices))
            .order_by(MistakePracticeRound.round_number.desc())
        )
        if concurrent_round is None:
            raise
        return concurrent_round
    return db.scalar(
        select(MistakePracticeRound)
        .where(MistakePracticeRound.id == practice_round.id)
        .options(selectinload(MistakePracticeRound.practices))
    )


@router.post("/{mistake_id}/practices/{practice_id}/submit", response_model=MistakePracticeResponse)
def submit_practice(
    mistake_id: str,
    practice_id: str,
    payload: PracticeSubmit,
    db: DbSession,
    user: CurrentUser,
) -> MistakePractice:
    mistake = db.scalar(
        select(MistakeProblem)
        .where(MistakeProblem.id == mistake_id, MistakeProblem.user_id == user.id)
        .with_for_update()
    )
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    _enforce_safe_mistake_input(db, user.id, payload.student_answer, target_id=mistake.id)
    practice = db.scalar(
        select(MistakePractice).where(
            MistakePractice.id == practice_id,
            MistakePractice.mistake_id == mistake.id,
            MistakePractice.user_id == user.id,
        )
    )
    if practice is None:
        raise HTTPException(status_code=404, detail="练习不存在")
    if practice.status == "completed":
        raise HTTPException(status_code=409, detail="该练习已经提交，不能重复作答")
    server_correct, validation = validate_practice_answer(payload.student_answer, practice.answer_reference)
    authoritative = bool(validation.get("authoritative"))
    resolved_correct = server_correct
    if resolved_correct is None and payload.is_correct is not None:
        resolved_correct = payload.is_correct
        validation = {
            "method": "self_report",
            "authoritative": False,
            "self_reported_correct": payload.is_correct,
        }
    if payload.validation_details:
        validation["client"] = payload.validation_details
    practice.student_answer = payload.student_answer
    practice.is_correct = resolved_correct
    practice.validation_details = validation
    practice.status = "completed"
    now = datetime.now(timezone.utc)
    practice.completed_at = now
    mistake.attempt_count += 1
    mistake.last_reviewed_at = now
    mistake.study_status = "reviewing"
    completed_stage: str | None = None
    stage_passed = False
    practice_round = None
    if practice.round_id:
        practice_round = db.scalar(
            select(MistakePracticeRound)
            .where(
                MistakePracticeRound.id == practice.round_id,
                MistakePracticeRound.mistake_id == mistake.id,
                MistakePracticeRound.user_id == user.id,
            )
            .options(selectinload(MistakePracticeRound.practices))
            .with_for_update()
        )
    db.flush()
    if practice_round is not None and all(item.status == "completed" for item in practice_round.practices):
        practice_round.status = "completed"
        practice_round.completed_at = now
        practice_round.correct_count = sum(item.is_correct is True for item in practice_round.practices)
        practice_round.authoritative_correct_count = sum(
            item.is_correct is True and item.validation_details.get("authoritative") is True
            for item in practice_round.practices
        )
        completed_stage = practice_round.review_stage
        stage_passed = practice_round.authoritative_correct_count == practice_round.question_count
        if stage_passed:
            mistake.review_streak += 1
            if completed_stage == "correction":
                mistake.first_corrected_at = mistake.first_corrected_at or now
                mistake.review_stage = "next_day"
                mistake.next_review_at = now + timedelta(days=1)
            elif completed_stage == "next_day":
                mistake.second_attempt_correct = True
                mistake.review_stage = "next_week"
                mistake.next_review_at = now + timedelta(days=7)
            elif completed_stage == "next_week":
                mistake.review_stage = "completed"
                mistake.next_review_at = None
                mistake.study_status = "mastered"
        else:
            if completed_stage == "next_day":
                mistake.second_attempt_correct = False
            mistake.review_streak = 0
            mistake.next_review_at = now + timedelta(days=1)
    elif practice_round is None:
        stage_passed = resolved_correct is True and authoritative
        if stage_passed:
            mistake.first_corrected_at = mistake.first_corrected_at or now
            mistake.review_stage = "next_day"
            mistake.review_streak = 1
            mistake.next_review_at = now + timedelta(days=1)

    if mistake.study_status == "mastered" and mistake.knowledge_node_id:
        state = db.scalar(
            select(UserKnowledgeState).where(
                UserKnowledgeState.user_id == user.id,
                UserKnowledgeState.node_id == mistake.knowledge_node_id,
            )
        )
        if state is None:
            state = UserKnowledgeState(user_id=user.id, node_id=mistake.knowledge_node_id)
            db.add(state)
        state.status = KnowledgeStatus.verified
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=mistake.knowledge_node_id,
            event_type="completed_mistake_practice",
            event_data={
                "mistake_id": mistake.id,
                "practice_id": practice.id,
                "practice_round_id": practice.round_id,
                "review_stage": completed_stage,
                "round_passed": stage_passed if completed_stage else None,
                "passed": resolved_correct if authoritative else None,
                "validation_method": validation["method"],
            },
        )
    )
    db.commit()
    db.refresh(practice)
    return practice
