from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.deps import CurrentUser, DbSession
from app.models import KnowledgeNode, KnowledgeStatus, LearningEvent, UserKnowledgeState
from app.schemas import (
    KnowledgeStateUpdate,
    LearningEventCreate,
    LearningEventResponse,
    LearningSummary,
    LearningSummaryItem,
)


router = APIRouter(prefix="/learning", tags=["学习记录"])


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
        elif payload.event_type == "completed_check" and payload.event_data.get("passed") is True:
            state.status = KnowledgeStatus.verified
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


@router.patch("/nodes/{node_id}/state", response_model=LearningSummaryItem)
def update_state(node_id: str, payload: KnowledgeStateUpdate, db: DbSession, user: CurrentUser) -> LearningSummaryItem:
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
