import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.deps import CurrentUser, DbSession
from app.models import KnowledgeNode, KnowledgeStatus, LearningCheckAttempt, LearningEvent, UserKnowledgeState
from app.schemas import (
    LearningCheckResponse,
    LearningCheckResult,
    LearningCheckSubmit,
    KnowledgeStateUpdate,
    LearningEventCreate,
    LearningEventResponse,
    LearningSummary,
    LearningSummaryItem,
)
from app.services.content_safety import moderate_text, moderation_text, record_safety_event


router = APIRouter(prefix="/learning", tags=["学习记录"])


def _enforce_safe_learning_input(db: DbSession, user_id: str, content: str, target_id: str | None) -> None:
    decision = moderate_text(content)
    if decision.allowed:
        return
    record_safety_event(
        db,
        user_id=user_id,
        action="safety.learning_input_blocked",
        decision=decision,
        content=content,
        target_type="knowledge_node",
        target_id=target_id,
    )
    db.commit()
    raise HTTPException(
        status_code=422,
        detail=decision.message,
        headers={"X-Content-Safety": "blocked"},
    )


def _get_or_create_state(db: DbSession, user_id: str, node_id: str) -> UserKnowledgeState:
    state = db.scalar(
        select(UserKnowledgeState).where(
            UserKnowledgeState.user_id == user_id,
            UserKnowledgeState.node_id == node_id,
        )
    )
    if state is None:
        state = UserKnowledgeState(user_id=user_id, node_id=node_id)
        db.add(state)
        db.flush()
    return state


@router.post("/events", response_model=LearningEventResponse, status_code=201)
def create_event(payload: LearningEventCreate, db: DbSession, user: CurrentUser) -> LearningEvent:
    _enforce_safe_learning_input(db, user.id, moderation_text(payload.event_data), payload.node_id)
    if payload.event_type == "completed_check":
        raise HTTPException(status_code=409, detail="理解检查必须通过专用接口由服务端判分")
    state: UserKnowledgeState | None = None
    if payload.node_id:
        if db.get(KnowledgeNode, payload.node_id) is None:
            raise HTTPException(status_code=404, detail="知识点不存在")
        state = _get_or_create_state(db, user.id, payload.node_id)
    if state:
        if payload.event_type == "viewed_node" and state.status == KnowledgeStatus.unexplored:
            state.status = KnowledgeStatus.explored
        elif payload.event_type == "marked_understood":
            state.status = KnowledgeStatus.understood
        elif payload.event_type == "marked_confused":
            state.status = KnowledgeStatus.unstable
        elif payload.event_type == "favorited":
            state.is_favorite = bool(payload.event_data.get("is_favorite", True))
    event = LearningEvent(
        user_id=user.id,
        node_id=payload.node_id,
        event_type=payload.event_type,
        event_data=payload.event_data,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.post("/nodes/{node_id}/checks", response_model=LearningCheckResponse, status_code=201)
def create_check(node_id: str, db: DbSession, user: CurrentUser) -> LearningCheckAttempt:
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    if not node.is_active or node.review_status != "approved" or not node.definition.strip():
        raise HTTPException(status_code=409, detail="该知识点尚未具备可用的理解检查")

    candidates = list(
        db.scalars(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.id != node.id,
                KnowledgeNode.subject == node.subject,
                KnowledgeNode.grade == node.grade,
                KnowledgeNode.is_active.is_(True),
                KnowledgeNode.review_status == "approved",
            )
            .order_by(KnowledgeNode.updated_at.desc())
            .limit(30)
        )
    )
    definitions: list[KnowledgeNode] = []
    seen = {node.definition.strip()}
    for candidate in candidates:
        definition = candidate.definition.strip()
        if definition and definition not in seen:
            definitions.append(candidate)
            seen.add(definition)
    if len(definitions) < 2:
        raise HTTPException(status_code=409, detail="同范围内缺少足够的已审核干扰项")

    selected = secrets.SystemRandom().sample(definitions, min(3, len(definitions))) + [node]
    secrets.SystemRandom().shuffle(selected)
    choices = [{"id": secrets.token_urlsafe(12), "text": item.definition.strip()} for item in selected]
    correct_index = selected.index(node)
    now = datetime.now(timezone.utc)
    attempt = LearningCheckAttempt(
        user_id=user.id,
        node_id=node.id,
        prompt=f"以下哪项是“{node.name}”的准确定义？",
        choices=choices,
        correct_choice_id=choices[correct_index]["id"],
        status="pending",
        expires_at=now + timedelta(minutes=15),
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return attempt


@router.post("/checks/{attempt_id}/submit", response_model=LearningCheckResult)
def submit_check(
    attempt_id: str,
    payload: LearningCheckSubmit,
    db: DbSession,
    user: CurrentUser,
) -> LearningCheckResult:
    attempt = db.scalar(
        select(LearningCheckAttempt).where(
            LearningCheckAttempt.id == attempt_id,
            LearningCheckAttempt.user_id == user.id,
        )
    )
    if attempt is None:
        raise HTTPException(status_code=404, detail="理解检查不存在")
    if attempt.status != "pending":
        raise HTTPException(status_code=409, detail="该理解检查已经提交")
    now = datetime.now(timezone.utc)
    expires_at = attempt.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        attempt.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="理解检查已过期，请重新开始")
    valid_choice_ids = {item["id"] for item in attempt.choices}
    if payload.choice_id not in valid_choice_ids:
        raise HTTPException(status_code=422, detail="选项不属于本次理解检查")

    passed = secrets.compare_digest(payload.choice_id, attempt.correct_choice_id)
    attempt.status = "passed" if passed else "failed"
    attempt.submitted_at = now
    state = _get_or_create_state(db, user.id, attempt.node_id)
    state.status = KnowledgeStatus.verified if passed else KnowledgeStatus.unstable
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=attempt.node_id,
            event_type="completed_check",
            event_data={
                "attempt_id": attempt.id,
                "passed": passed,
                "format": "definition_choice",
            },
        )
    )
    db.commit()
    node = db.get(KnowledgeNode, attempt.node_id)
    assert node is not None
    return LearningCheckResult(
        attempt_id=attempt.id,
        passed=passed,
        status=attempt.status,
        state=LearningSummaryItem(
            id=node.id,
            name=node.name,
            subject=node.subject,
            grade=node.grade,
            chapter=node.chapter,
            definition=node.definition,
            status=state.status,
            updated_at=state.updated_at,
            note=state.note,
            is_favorite=state.is_favorite,
        ),
    )


@router.patch("/nodes/{node_id}/state", response_model=LearningSummaryItem)
def update_state(node_id: str, payload: KnowledgeStateUpdate, db: DbSession, user: CurrentUser) -> LearningSummaryItem:
    _enforce_safe_learning_input(db, user.id, payload.note or "", node_id)
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    if payload.status == KnowledgeStatus.verified:
        raise HTTPException(status_code=409, detail="已验证状态只能由通过的理解检查或练习产生")
    state = _get_or_create_state(db, user.id, node_id)
    state.status = payload.status
    state.note = payload.note
    if payload.is_favorite is not None:
        state.is_favorite = payload.is_favorite
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=node_id,
            event_type="state_updated",
            event_data={"status": payload.status.value},
        )
    )
    db.commit()
    values = {
        **node.__dict__,
        "status": state.status,
        "updated_at": state.updated_at,
        "note": state.note,
        "is_favorite": state.is_favorite,
    }
    return LearningSummaryItem.model_validate(values)


@router.get("/events", response_model=list[LearningEventResponse])
def list_events(db: DbSession, user: CurrentUser, limit: int = 50) -> list[LearningEvent]:
    return list(
        db.scalars(
            select(LearningEvent)
            .where(LearningEvent.user_id == user.id)
            .order_by(LearningEvent.created_at.desc())
            .limit(min(max(limit, 1), 100))
        )
    )


@router.get("/summary", response_model=LearningSummary)
def summary(db: DbSession, user: CurrentUser) -> LearningSummary:
    rows = db.execute(
        select(UserKnowledgeState, KnowledgeNode)
        .join(KnowledgeNode, KnowledgeNode.id == UserKnowledgeState.node_id)
        .where(UserKnowledgeState.user_id == user.id)
        .order_by(UserKnowledgeState.updated_at.desc())
    ).all()
    items = [
        LearningSummaryItem(
            id=node.id,
            name=node.name,
            subject=node.subject,
            grade=node.grade,
            chapter=node.chapter,
            definition=node.definition,
            status=state.status,
            updated_at=state.updated_at,
            note=state.note,
            is_favorite=state.is_favorite,
        )
        for state, node in rows
    ]
    return LearningSummary(
        recent=items[:20],
        review=[item for item in items if item.status in (KnowledgeStatus.unstable, KnowledgeStatus.to_explore)],
        error_prone=[item for item in items if item.status == KnowledgeStatus.error_prone],
        verified=[item for item in items if item.status == KnowledgeStatus.verified],
    )
