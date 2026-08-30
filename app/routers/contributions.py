from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.config import settings
from app.deps import AdminUser, CurrentUser, DbSession, SuperAdminUser
from app.models import AuditLog, ContentContribution, CreditAccount, CreditLedger
from app.schemas import (
    AdminContributionResponse,
    ContributionCreate,
    ContributionResponse,
    ContributionReview,
    ContributionSettlementResponse,
)
from app.services.contributions import enqueue_contribution_review
from app.services.content_safety import moderate_text, moderation_text, record_safety_event


router = APIRouter(prefix="/contributions", tags=["内容投稿"])
admin_router = APIRouter(prefix="/admin/contributions", tags=["管理后台"])


def _contributions_enabled() -> None:
    if not settings.contributions_enabled:
        raise HTTPException(status_code=404, detail="内容投稿尚未开放")


def _enqueue_or_mark_failed(db: DbSession, contribution: ContentContribution) -> None:
    try:
        enqueue_contribution_review(contribution.id)
    except Exception as exc:
        contribution.status = "review_failed"
        contribution.ai_review = {
            "error_code": "queue_unavailable",
            "prompt_version": "contribution-screen-v1",
        }
        db.add(
            AuditLog(
                actor_user_id=contribution.user_id,
                action="contribution.queue_failed",
                target_type="content_contribution",
                target_id=contribution.id,
                details={"error_type": type(exc).__name__},
            )
        )
        db.commit()
        raise HTTPException(status_code=503, detail="初审队列暂时不可用，请稍后重试") from exc


@router.post("", response_model=ContributionResponse, status_code=status.HTTP_201_CREATED)
def create_contribution(
    payload: ContributionCreate,
    db: DbSession,
    user: CurrentUser,
) -> ContentContribution:
    _contributions_enabled()
    content = moderation_text(payload.title, payload.content, payload.source_reference)
    decision = moderate_text(content)
    if not decision.allowed:
        record_safety_event(
            db,
            user_id=user.id,
            action="safety.contribution_input_blocked",
            decision=decision,
            content=content,
            target_type="content_contribution",
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail=decision.message,
            headers={"X-Content-Safety": "blocked"},
        )
    contribution = ContentContribution(
        user_id=user.id,
        school_id=user.tenant_id,
        contribution_type=payload.contribution_type,
        title=payload.title.strip(),
        content=payload.content.strip(),
        source_reference=payload.source_reference.strip() if payload.source_reference else None,
    )
    db.add(contribution)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="contribution.submitted",
            target_type="content_contribution",
            target_id=contribution.id,
            details={"contribution_type": contribution.contribution_type, "school_id": contribution.school_id},
        )
    )
    db.commit()
    db.refresh(contribution)
    _enqueue_or_mark_failed(db, contribution)
    return contribution


@router.get("", response_model=list[ContributionResponse])
def list_my_contributions(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ContentContribution]:
    _contributions_enabled()
    return list(
        db.scalars(
            select(ContentContribution)
            .where(ContentContribution.user_id == user.id)
            .order_by(ContentContribution.created_at.desc())
            .limit(limit)
        )
    )


@router.delete("/{contribution_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_contribution(
    contribution_id: str,
    db: DbSession,
    user: CurrentUser,
) -> None:
    _contributions_enabled()
    contribution = db.get(ContentContribution, contribution_id)
    if contribution is None or contribution.user_id != user.id:
        raise HTTPException(status_code=404, detail="投稿不存在")
    if contribution.status in {"approved", "rejected"}:
        raise HTTPException(status_code=409, detail="已完成审核的投稿不能删除")
    db.delete(contribution)
    db.commit()


@admin_router.get("", response_model=list[AdminContributionResponse])
def list_contributions(
    db: DbSession,
    _admin: AdminUser,
    contribution_status: str | None = None,
    school_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ContentContribution]:
    _contributions_enabled()
    statement = select(ContentContribution).order_by(ContentContribution.created_at.desc()).limit(limit)
    if contribution_status:
        statement = statement.where(ContentContribution.status == contribution_status)
    if school_id:
        statement = statement.where(ContentContribution.school_id == school_id)
    return list(db.scalars(statement))


@admin_router.post("/{contribution_id}/retry", response_model=AdminContributionResponse)
def retry_contribution_screening(
    contribution_id: str,
    db: DbSession,
    admin: AdminUser,
) -> ContentContribution:
    _contributions_enabled()
    contribution = db.get(ContentContribution, contribution_id)
    if contribution is None:
        raise HTTPException(status_code=404, detail="投稿不存在")
    if contribution.status != "review_failed":
        raise HTTPException(status_code=409, detail="只有初审失败的投稿可以重试")
    contribution.status = "queued"
    contribution.ai_review = {}
    contribution.ai_provider = None
    contribution.ai_model = None
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="contribution.screening_retried",
            target_type="content_contribution",
            target_id=contribution.id,
        )
    )
    db.commit()
    db.refresh(contribution)
    _enqueue_or_mark_failed(db, contribution)
    return contribution


@admin_router.post("/{contribution_id}/review", response_model=AdminContributionResponse)
def review_contribution(
    contribution_id: str,
    payload: ContributionReview,
    db: DbSession,
    admin: AdminUser,
) -> ContentContribution:
    _contributions_enabled()
    contribution = db.scalar(
        select(ContentContribution)
        .where(ContentContribution.id == contribution_id)
        .with_for_update()
    )
    if contribution is None:
        raise HTTPException(status_code=404, detail="投稿不存在")
    if contribution.status != "screened":
        raise HTTPException(status_code=409, detail="投稿必须先完成 AI 初审")
    now = datetime.now(timezone.utc)
    contribution.status = payload.decision
    contribution.review_note = payload.review_note.strip()
    contribution.reviewed_by = admin.id
    contribution.reviewed_at = now
    contribution.reward_amount = 0
    contribution.reward_available_at = None
    contribution.reward_status = "disabled"
    if (
        payload.decision == "approved"
        and settings.contribution_rewards_enabled
        and payload.reward_amount > 0
    ):
        contribution.reward_amount = payload.reward_amount
        contribution.reward_status = "pending"
        contribution.reward_available_at = now + timedelta(hours=settings.contribution_reward_delay_hours)
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action=f"contribution.{payload.decision}",
            target_type="content_contribution",
            target_id=contribution.id,
            details={
                "reward_amount": contribution.reward_amount,
                "reward_status": contribution.reward_status,
                "formal_knowledge_inserted": False,
            },
        )
    )
    db.commit()
    db.refresh(contribution)
    return contribution


@admin_router.post("/rewards/settle", response_model=ContributionSettlementResponse)
def settle_contribution_rewards(
    db: DbSession,
    admin: SuperAdminUser,
) -> ContributionSettlementResponse:
    _contributions_enabled()
    if not settings.contribution_rewards_enabled:
        raise HTTPException(status_code=404, detail="投稿奖励尚未开放")
    now = datetime.now(timezone.utc)
    contributions = list(
        db.scalars(
            select(ContentContribution)
            .where(
                ContentContribution.status == "approved",
                ContentContribution.reward_status == "pending",
                ContentContribution.reward_available_at <= now,
            )
            .order_by(ContentContribution.reward_available_at, ContentContribution.id)
            .with_for_update()
        )
    )
    settled_count = 0
    total_credits = 0
    for contribution in contributions:
        account = db.scalar(
            select(CreditAccount)
            .where(CreditAccount.user_id == contribution.user_id)
            .with_for_update()
        )
        if account is None:
            contribution.reward_status = "account_missing"
            continue
        account.balance += contribution.reward_amount
        contribution.reward_status = "settled"
        db.add(
            CreditLedger(
                user_id=contribution.user_id,
                amount=contribution.reward_amount,
                balance_after=account.balance,
                entry_type="contribution_reward",
                feature="content_contribution",
                reference_id=contribution.id,
            )
        )
        settled_count += 1
        total_credits += contribution.reward_amount
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="contribution.rewards_settled",
            target_type="content_contribution_batch",
            details={"settled_count": settled_count, "total_credits": total_credits},
        )
    )
    db.commit()
    return ContributionSettlementResponse(settled_count=settled_count, total_credits=total_credits)
