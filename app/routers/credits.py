import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.config import settings
from app.deps import CurrentUser, DbSession, SuperAdminUser
from app.models import (
    AuditLog,
    CreditAccount,
    CreditCampaign,
    CreditCode,
    CreditLedger,
    CreditRedemption,
    School,
    SchoolMembership,
    User,
)
from app.schemas import (
    AdminCreditAdjustment,
    CreditAccountResponse,
    CreditCampaignCreate,
    CreditCampaignCreatedResponse,
    CreditCampaignResponse,
    CreditLedgerResponse,
    CreditRedeemRequest,
    CreditRedeemResponse,
)


router = APIRouter(prefix="/credits", tags=["额度"])
admin_router = APIRouter(prefix="/admin/credits", tags=["管理后台"])


def _campaigns_enabled() -> None:
    if not settings.credit_campaigns_enabled:
        raise HTTPException(status_code=404, detail="活动额度尚未开放")


def _code_hash(code: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode(),
        f"credit-code:{code.strip()}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@router.get("", response_model=CreditAccountResponse)
def account(db: DbSession, user: CurrentUser) -> CreditAccount:
    credit_account = db.get(CreditAccount, user.id)
    if credit_account is None:
        raise HTTPException(status_code=404, detail="额度账户不存在")
    return credit_account


@router.get("/ledger", response_model=list[CreditLedgerResponse])
def ledger(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[CreditLedger]:
    return list(
        db.scalars(
            select(CreditLedger)
            .where(CreditLedger.user_id == user.id)
            .order_by(CreditLedger.created_at.desc())
            .limit(limit)
        )
    )


@router.post("/redeem", response_model=CreditRedeemResponse)
def redeem_credit_code(
    payload: CreditRedeemRequest,
    db: DbSession,
    user: CurrentUser,
) -> CreditRedeemResponse:
    _campaigns_enabled()
    code = db.scalar(
        select(CreditCode).where(CreditCode.code_hash == _code_hash(payload.code)).with_for_update()
    )
    if code is None or not code.is_active or code.use_count >= code.max_uses:
        raise HTTPException(status_code=404, detail="兑换码无效或已用完")
    campaign = db.scalar(
        select(CreditCampaign).where(CreditCampaign.id == code.campaign_id).with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        campaign is None
        or campaign.status != "active"
        or _as_utc(campaign.starts_at) > now
        or _as_utc(campaign.ends_at) <= now
        or campaign.redemption_count >= campaign.max_redemptions
    ):
        raise HTTPException(status_code=404, detail="额度活动不可用或已结束")
    if campaign.school_id is not None:
        membership = db.scalar(
            select(SchoolMembership.id).where(
                SchoolMembership.school_id == campaign.school_id,
                SchoolMembership.user_id == user.id,
                SchoolMembership.status == "active",
            )
        )
        if user.tenant_id != campaign.school_id or membership is None:
            raise HTTPException(status_code=403, detail="该兑换码仅限指定学校成员")
    existing_code_use = db.scalar(
        select(CreditRedemption.id).where(
            CreditRedemption.code_id == code.id,
            CreditRedemption.user_id == user.id,
        )
    )
    if existing_code_use is not None:
        raise HTTPException(status_code=409, detail="该兑换码已兑换")
    user_redemptions = db.scalar(
        select(func.count())
        .select_from(CreditRedemption)
        .where(
            CreditRedemption.campaign_id == campaign.id,
            CreditRedemption.user_id == user.id,
        )
    ) or 0
    if user_redemptions >= campaign.per_user_limit:
        raise HTTPException(status_code=409, detail="已达到本活动个人兑换上限")
    account = db.scalar(
        select(CreditAccount).where(CreditAccount.user_id == user.id).with_for_update()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="额度账户不存在")
    account.balance += campaign.amount
    code.use_count += 1
    campaign.redemption_count += 1
    redemption = CreditRedemption(
        campaign_id=campaign.id,
        code_id=code.id,
        user_id=user.id,
        amount=campaign.amount,
    )
    db.add(redemption)
    db.flush()
    db.add(
        CreditLedger(
            user_id=user.id,
            amount=campaign.amount,
            balance_after=account.balance,
            entry_type="campaign_redemption",
            feature="credit_campaign",
            reference_id=redemption.id,
        )
    )
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="credits.campaign_redeemed",
            target_type="credit_campaign",
            target_id=campaign.id,
            details={"amount": campaign.amount, "school_id": campaign.school_id},
        )
    )
    db.commit()
    return CreditRedeemResponse(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        amount=campaign.amount,
        balance=account.balance,
    )


@admin_router.post(
    "/campaigns",
    response_model=CreditCampaignCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_credit_campaign(
    payload: CreditCampaignCreate,
    db: DbSession,
    admin: SuperAdminUser,
) -> dict:
    _campaigns_enabled()
    if payload.school_id is not None and db.get(School, payload.school_id) is None:
        raise HTTPException(status_code=404, detail="学校不存在")
    starts_at = payload.starts_at or datetime.now(timezone.utc)
    starts_at = _as_utc(starts_at)
    campaign = CreditCampaign(
        name=payload.name.strip(),
        amount=payload.amount,
        school_id=payload.school_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=payload.expires_hours),
        max_redemptions=payload.max_redemptions,
        per_user_limit=payload.per_user_limit,
        created_by=admin.id,
    )
    db.add(campaign)
    db.flush()
    plaintext_codes: list[str] = []
    for _ in range(payload.code_count):
        plaintext = f"QKC-{secrets.token_urlsafe(15)}"
        plaintext_codes.append(plaintext)
        db.add(
            CreditCode(
                campaign_id=campaign.id,
                code_hash=_code_hash(plaintext),
                max_uses=payload.code_max_uses,
            )
        )
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="credits.campaign_created",
            target_type="credit_campaign",
            target_id=campaign.id,
            details={
                "amount": campaign.amount,
                "school_id": campaign.school_id,
                "max_redemptions": campaign.max_redemptions,
                "code_count": payload.code_count,
            },
        )
    )
    db.commit()
    db.refresh(campaign)
    return {
        "id": campaign.id,
        "name": campaign.name,
        "amount": campaign.amount,
        "school_id": campaign.school_id,
        "status": campaign.status,
        "starts_at": campaign.starts_at,
        "ends_at": campaign.ends_at,
        "max_redemptions": campaign.max_redemptions,
        "redemption_count": campaign.redemption_count,
        "per_user_limit": campaign.per_user_limit,
        "created_at": campaign.created_at,
        "codes": plaintext_codes,
    }


@admin_router.get("/campaigns", response_model=list[CreditCampaignResponse])
def list_credit_campaigns(
    db: DbSession,
    _admin: SuperAdminUser,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[CreditCampaign]:
    _campaigns_enabled()
    return list(
        db.scalars(select(CreditCampaign).order_by(CreditCampaign.created_at.desc()).limit(limit))
    )


@admin_router.post("/{user_id}/adjust", response_model=CreditAccountResponse)
def adjust_credit(
    user_id: str,
    payload: AdminCreditAdjustment,
    db: DbSession,
    admin: SuperAdminUser,
) -> CreditAccount:
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    account = db.scalar(select(CreditAccount).where(CreditAccount.user_id == user_id).with_for_update())
    if account is None:
        raise HTTPException(status_code=404, detail="额度账户不存在")
    if account.balance + payload.amount < 0:
        raise HTTPException(status_code=409, detail="调整后余额不能小于零")
    account.balance += payload.amount
    db.add(
        CreditLedger(
            user_id=user_id,
            amount=payload.amount,
            balance_after=account.balance,
            entry_type="admin_adjustment",
            feature="admin",
            reference_id=admin.id,
        )
    )
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="credits.adjusted",
            target_type="user",
            target_id=user_id,
            details={"amount": payload.amount, "reason": payload.reason},
        )
    )
    db.commit()
    db.refresh(account)
    return account
