from fastapi import APIRouter, HTTPException, Query

from app.deps import CurrentUser, DbSession
from sqlalchemy import select

from app.models import Conversation, FeedbackSubmission, KnowledgeNode, KnowledgeStatus, LearningEvent, Message, UserKnowledgeState
from app.schemas import FeedbackCreate, FeedbackResponse
from app.services.content_safety import moderate_text, record_safety_event


router = APIRouter(prefix="/feedback", tags=["问题反馈"])


@router.post("", response_model=FeedbackResponse, status_code=201)
def submit_feedback(payload: FeedbackCreate, db: DbSession, user: CurrentUser) -> FeedbackSubmission:
    decision = moderate_text(payload.content)
    if not decision.allowed:
        record_safety_event(
            db,
            user_id=user.id,
            action="safety.feedback_input_blocked",
            decision=decision,
            content=payload.content,
            target_type="feedback",
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail=decision.message,
            headers={"X-Content-Safety": "blocked"},
        )
    if payload.node_id and db.get(KnowledgeNode, payload.node_id) is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    if payload.message_id:
        owned_message = db.scalar(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.id == payload.message_id, Conversation.user_id == user.id)
        )
        if owned_message is None:
            raise HTTPException(status_code=404, detail="回答不存在")
    else:
        owned_message = None
    feedback = FeedbackSubmission(
        user_id=user.id,
        node_id=payload.node_id,
        message_id=payload.message_id,
        category=payload.category,
        content=payload.content,
    )
    db.add(feedback)
    db.flush()
    if payload.category == "review_request" and owned_message is not None:
        # A review request is an actionable learning signal, not only a ticket.
        # The assistant stores linked node IDs as server-generated JSON.
        linked_node_ids = owned_message.linked_node_ids or []
        node_id = linked_node_ids[0] if linked_node_ids else None
        if node_id and db.get(KnowledgeNode, node_id) is not None:
            state = db.scalar(
                select(UserKnowledgeState).where(
                    UserKnowledgeState.user_id == user.id,
                    UserKnowledgeState.node_id == node_id,
                )
            )
            if state is None:
                state = UserKnowledgeState(user_id=user.id, node_id=node_id, status=KnowledgeStatus.unstable)
                db.add(state)
            else:
                # A deliberate review request can reopen even a previously
                # verified node; the verification event remains auditable.
                state.status = KnowledgeStatus.unstable
            db.add(
                LearningEvent(
                    user_id=user.id,
                    node_id=node_id,
                    event_type="marked_confused",
                    event_data={"feedback_id": feedback.id, "message_id": owned_message.id},
                )
            )
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=payload.node_id,
            event_type="submitted_feedback",
            event_data={"feedback_id": feedback.id, "category": payload.category},
        )
    )
    db.commit()
    db.refresh(feedback)
    return feedback


@router.get("", response_model=list[FeedbackResponse])
def list_my_feedback(
    db: DbSession,
    user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[FeedbackSubmission]:
    statement = (
        select(FeedbackSubmission)
        .where(FeedbackSubmission.user_id == user.id)
        .order_by(FeedbackSubmission.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        statement = statement.where(FeedbackSubmission.status == status_filter)
    return list(db.scalars(statement))


@router.get("/{feedback_id}", response_model=FeedbackResponse)
def get_my_feedback(feedback_id: str, db: DbSession, user: CurrentUser) -> FeedbackSubmission:
    feedback = db.scalar(
        select(FeedbackSubmission).where(
            FeedbackSubmission.id == feedback_id,
            FeedbackSubmission.user_id == user.id,
        )
    )
    if feedback is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    return feedback
