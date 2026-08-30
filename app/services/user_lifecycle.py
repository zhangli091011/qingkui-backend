from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    ClassMembership,
    Conversation,
    CreditAccount,
    CreditLedger,
    CreditRedemption,
    FeedbackSubmission,
    IdempotencyRequest,
    KnowledgeNodeVersion,
    LearningEvent,
    MistakeAsset,
    MistakeProblem,
    ModelCall,
    RefreshSession,
    SchoolMembership,
    User,
    UserKnowledgeState,
)
from app.services.mistakes import delete_assets


def erase_user_account(db: Session, user: User, *, delete_external_assets: bool = True) -> None:
    if delete_external_assets:
        assets = list(db.scalars(select(MistakeAsset).where(MistakeAsset.user_id == user.id)))
        delete_assets(assets)
    user_id = user.id
    db.execute(delete(FeedbackSubmission).where(FeedbackSubmission.user_id == user_id))
    db.execute(update(FeedbackSubmission).where(FeedbackSubmission.reviewed_by == user_id).values(reviewed_by=None))
    db.execute(delete(MistakeProblem).where(MistakeProblem.user_id == user_id))
    db.execute(delete(Conversation).where(Conversation.user_id == user_id))
    db.execute(delete(UserKnowledgeState).where(UserKnowledgeState.user_id == user_id))
    db.execute(delete(LearningEvent).where(LearningEvent.user_id == user_id))
    db.execute(delete(CreditLedger).where(CreditLedger.user_id == user_id))
    db.execute(delete(CreditRedemption).where(CreditRedemption.user_id == user_id))
    db.execute(delete(CreditAccount).where(CreditAccount.user_id == user_id))
    db.execute(delete(RefreshSession).where(RefreshSession.user_id == user_id))
    db.execute(delete(IdempotencyRequest).where(IdempotencyRequest.user_id == user_id))
    db.execute(delete(ClassMembership).where(ClassMembership.user_id == user_id))
    db.execute(delete(SchoolMembership).where(SchoolMembership.user_id == user_id))
    db.execute(update(ModelCall).where(ModelCall.user_id == user_id).values(user_id=None))
    db.execute(update(KnowledgeNodeVersion).where(KnowledgeNodeVersion.created_by == user_id).values(created_by=None))
    db.execute(update(AuditLog).where(AuditLog.actor_user_id == user_id).values(actor_user_id=None))
    user.username = f"deleted_{user.id[:24]}"
    user.email = None
    user.nickname = "已注销用户"
    user.password_hash = "deleted"
    user.is_active = False
    user.deleted_at = datetime.now(timezone.utc)
