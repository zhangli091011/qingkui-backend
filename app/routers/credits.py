from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.deps import AdminUser, CurrentUser, DbSession
from app.models import AuditLog, CreditAccount, CreditLedger, User
from app.schemas import AdminCreditAdjustment, CreditAccountResponse, CreditLedgerResponse


router = APIRouter(prefix="/credits", tags=["额度"])
admin_router = APIRouter(prefix="/admin/credits", tags=["管理后台"])


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


@admin_router.post("/{user_id}/adjust", response_model=CreditAccountResponse)
def adjust_credit(
    user_id: str,
    payload: AdminCreditAdjustment,
    db: DbSession,
    admin: AdminUser,
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
