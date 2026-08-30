from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import CurrentUser, DbSession
from app.models import (
    AuditLog,
    CreditAccount,
    CreditLedger,
    KnowledgeNode,
    LearningEvent,
    MistakeAsset,
    MistakePractice,
    MistakeProblem,
    OcrTask,
    new_id,
)
from app.schemas import (
    MessageResponse,
    MistakeCreate,
    MistakeAnalysisData,
    MistakeAnalysisResponse,
    MistakePracticeResponse,
    MistakeResponse,
    MistakeUpdate,
    OcrCorrection,
    OcrTaskResponse,
    PracticeCreate,
    PracticeSubmit,
)
from app.services.knowledge import retrieve_chunks, retrieve_nodes
from app.services.mistake_analysis import PROMPT_VERSION as ANALYSIS_PROMPT_VERSION
from app.services.mistake_analysis import analyze_mistake_content
from app.services.mistakes import delete_assets, enqueue_ocr_task, store_mistake_image
from app.services.object_storage import delete_private_object, get_private_bytes
from app.services.practice_validation import validate_practice_answer


router = APIRouter(prefix="/mistakes", tags=["错题本"])


def _owned_mistake(db: DbSession, mistake_id: str, user_id: str, *, detail: bool = False) -> MistakeProblem:
    query = select(MistakeProblem).where(MistakeProblem.id == mistake_id, MistakeProblem.user_id == user_id)
    if detail:
        query = query.options(
            selectinload(MistakeProblem.assets),
            selectinload(MistakeProblem.ocr_tasks),
            selectinload(MistakeProblem.practices),
        )
    mistake = db.scalar(query)
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    return mistake


@router.post("", response_model=MistakeResponse, status_code=201)
def create_mistake(payload: MistakeCreate, db: DbSession, user: CurrentUser) -> MistakeProblem:
    if not any((payload.question_text, payload.student_work, payload.question_goal)):
        raise HTTPException(status_code=422, detail="请至少填写题目、作答过程或提问目标")
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
        )
        .order_by(MistakeProblem.updated_at.desc())
        .limit(limit)
    )
    if error_category:
        query = query.where(MistakeProblem.error_category == error_category)
    if study_status:
        query = query.where(MistakeProblem.study_status == study_status)
    return list(db.scalars(query))


@router.get("/{mistake_id}", response_model=MistakeResponse)
def get_mistake(mistake_id: str, db: DbSession, user: CurrentUser) -> MistakeProblem:
    return _owned_mistake(db, mistake_id, user.id, detail=True)


@router.patch("/{mistake_id}", response_model=MistakeResponse)
def update_mistake(mistake_id: str, payload: MistakeUpdate, db: DbSession, user: CurrentUser) -> MistakeProblem:
    mistake = _owned_mistake(db, mistake_id, user.id)
    values = payload.model_dump(exclude_unset=True)
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
    payload = await image.read(settings.user_upload_max_bytes + 1)
    asset_id = new_id()
    try:
        clean, object_key = store_mistake_image(
            user_id=user.id, mistake_id=mistake.id, asset_id=asset_id, payload=payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
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
    if task.status in {"succeeded", "failed", "cancelled"}:
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
    mistake.corrected_text = payload.corrected_text
    mistake.question_text = payload.corrected_text
    mistake.review_status = "confirmed"
    mistake.analysis = {}
    mistake.analysis_status = "not_started"
    mistake.analysis_provider = None
    mistake.analysis_model = None
    mistake.analyzed_at = None
    task.requires_review = False
    db.add(AuditLog(actor_user_id=user.id, action="mistake.ocr_confirmed", target_type="ocr_task", target_id=task.id))
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
        raise HTTPException(status_code=409, detail="低置信度 OCR 必须先人工校对")

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
    cost = 2
    if account.balance < cost:
        raise HTTPException(status_code=402, detail="额度不足")

    nodes = retrieve_nodes(db, question, mistake.knowledge_node_id, limit=8, subject=mistake.subject)
    chunks = retrieve_chunks(db, question, limit=8, subject=mistake.subject)
    mistake.analysis_status = "processing"
    db.flush()
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
        raise HTTPException(status_code=502, detail="错题分析服务暂时不可用，未扣除额度") from exc

    candidate_ids = {node.id for node in nodes}
    analysis = ai_result.analysis
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


@router.post("/{mistake_id}/practices/generate", response_model=MistakePracticeResponse, status_code=201)
def generate_practice(mistake_id: str, db: DbSession, user: CurrentUser) -> MistakePractice:
    mistake = _owned_mistake(db, mistake_id, user.id)
    if mistake.analysis_status != "completed" or not mistake.analysis:
        raise HTTPException(status_code=409, detail="请先完成错题分析")
    question = str(mistake.analysis.get("similar_question") or "").strip()
    answer_reference = str(mistake.analysis.get("answer_reference") or "").strip()
    if not question or not answer_reference:
        raise HTTPException(status_code=409, detail="分析结果没有可用的同类练习")
    existing = db.scalar(
        select(MistakePractice)
        .where(
            MistakePractice.mistake_id == mistake.id,
            MistakePractice.user_id == user.id,
            MistakePractice.source == "ai_analysis",
            MistakePractice.question_text == question,
        )
        .order_by(MistakePractice.created_at.desc())
    )
    if existing is not None:
        return existing
    practice = MistakePractice(
        mistake_id=mistake.id,
        user_id=user.id,
        question_text=question,
        answer_reference=answer_reference,
        source="ai_analysis",
    )
    db.add(practice)
    db.commit()
    db.refresh(practice)
    return practice


@router.post("/{mistake_id}/practices/{practice_id}/submit", response_model=MistakePracticeResponse)
def submit_practice(
    mistake_id: str,
    practice_id: str,
    payload: PracticeSubmit,
    db: DbSession,
    user: CurrentUser,
) -> MistakePractice:
    mistake = _owned_mistake(db, mistake_id, user.id)
    practice = db.scalar(
        select(MistakePractice).where(
            MistakePractice.id == practice_id,
            MistakePractice.mistake_id == mistake.id,
            MistakePractice.user_id == user.id,
        )
    )
    if practice is None:
        raise HTTPException(status_code=404, detail="练习不存在")
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
    practice.completed_at = datetime.now(timezone.utc)
    mistake.attempt_count += 1
    mistake.study_status = "mastered" if resolved_correct is True and authoritative else "reviewing"
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=mistake.knowledge_node_id,
            event_type="completed_mistake_practice",
            event_data={
                "mistake_id": mistake.id,
                "practice_id": practice.id,
                "passed": resolved_correct if authoritative else None,
                "validation_method": validation["method"],
            },
        )
    )
    db.commit()
    db.refresh(practice)
    return practice
