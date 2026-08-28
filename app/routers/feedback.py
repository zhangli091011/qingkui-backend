from fastapi import APIRouter, HTTPException

from app.deps import CurrentUser, DbSession
from sqlalchemy import select

from app.models import Conversation, FeedbackSubmission, KnowledgeNode, LearningEvent, Message
from app.schemas import FeedbackCreate, FeedbackResponse


router = APIRouter(prefix="/feedback", tags=["问题反馈"])


@router.post("", response_model=FeedbackResponse, status_code=201)
def submit_feedback(payload: FeedbackCreate, db: DbSession, user: CurrentUser) -> FeedbackSubmission:
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
    feedback = FeedbackSubmission(
        user_id=user.id,
        node_id=payload.node_id,
        message_id=payload.message_id,
        category=payload.category,
        content=payload.content,
    )
    db.add(feedback)
    db.flush()
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
