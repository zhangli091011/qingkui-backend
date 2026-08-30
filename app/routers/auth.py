from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, select, update

from app.config import settings
from app.deps import CurrentUser, DbSession
from app.models import (
    AuditLog,
    Conversation,
    CreditAccount,
    CreditLedger,
    FeedbackSubmission,
    LearningEvent,
    MistakeAsset,
    MistakeProblem,
    RefreshSession,
    User,
    UserKnowledgeState,
)
from app.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    DeviceSessionResponse,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    UserResponse,
)
from app.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.services.mistakes import delete_assets


router = APIRouter(prefix="/auth", tags=["账户"])


def _session_is_active(session: RefreshSession, now: datetime) -> bool:
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return session.revoked_at is None and expires_at > now


def _issue_tokens(db: DbSession, user: User, device_name: str | None) -> AuthResponse:
    access_token, expires_in = create_access_token(user.id)
    refresh_token, jti, expires_at = create_refresh_token(user.id)
    db.add(
        RefreshSession(
            user_id=user.id,
            token_jti=jti,
            device_name=device_name,
            expires_at=expires_at,
        )
    )
    db.commit()
    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DbSession) -> AuthResponse:
    username = payload.username.lower()
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if payload.email and db.scalar(select(User.id).where(User.email == str(payload.email).lower())):
        raise HTTPException(status_code=409, detail="邮箱已被使用")

    user = User(
        username=username,
        email=str(payload.email).lower() if payload.email else None,
        nickname=payload.nickname or f"青葵同学{username[-4:]}",
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.flush()
    account = CreditAccount(user_id=user.id, balance=settings.initial_credits)
    db.add(account)
    db.flush()
    db.add(
        CreditLedger(
            user_id=user.id,
            amount=settings.initial_credits,
            balance_after=settings.initial_credits,
            entry_type="trial_grant",
            feature="registration",
        )
    )
    db.add(AuditLog(actor_user_id=user.id, action="user.register", target_type="user", target_id=user.id))
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user, payload.device_name)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: DbSession) -> AuthResponse:
    user = db.scalar(select(User).where(User.username == payload.username.lower()))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    db.add(AuditLog(actor_user_id=user.id, action="user.login", target_type="user", target_id=user.id))
    return _issue_tokens(db, user, payload.device_name)


@router.post("/refresh", response_model=AuthResponse)
def refresh(payload: RefreshRequest, db: DbSession) -> AuthResponse:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="刷新凭证无效") from exc
    session = db.scalar(select(RefreshSession).where(RefreshSession.token_jti == claims["jti"]))
    user = db.get(User, claims["sub"])
    now = datetime.now(timezone.utc)
    if (
        session is None
        or session.user_id != claims["sub"]
        or not _session_is_active(session, now)
        or user is None
        or not user.is_active
    ):
        raise HTTPException(status_code=401, detail="刷新凭证已失效")
    session.revoked_at = now
    db.commit()
    return _issue_tokens(db, user, session.device_name)


@router.post("/logout", response_model=MessageResponse)
def logout(payload: LogoutRequest, db: DbSession) -> MessageResponse:
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except jwt.PyJWTError:
        return MessageResponse(message="已退出登录")
    session = db.scalar(select(RefreshSession).where(RefreshSession.token_jti == claims["jti"]))
    if session and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return MessageResponse(message="已退出登录")


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> User:
    return user


@router.get("/sessions", response_model=list[DeviceSessionResponse])
def list_device_sessions(
    db: DbSession,
    user: CurrentUser,
    include_inactive: bool = Query(default=False),
) -> list[dict]:
    now = datetime.now(timezone.utc)
    statement = (
        select(RefreshSession)
        .where(RefreshSession.user_id == user.id)
        .order_by(RefreshSession.created_at.desc())
    )
    if not include_inactive:
        statement = statement.where(RefreshSession.revoked_at.is_(None), RefreshSession.expires_at > now)
    return [
        {
            "id": session.id,
            "device_name": session.device_name,
            "expires_at": session.expires_at,
            "revoked_at": session.revoked_at,
            "created_at": session.created_at,
            "active": _session_is_active(session, now),
        }
        for session in db.scalars(statement)
    ]


@router.delete("/sessions/{session_id}", response_model=MessageResponse)
def revoke_device_session(session_id: str, db: DbSession, user: CurrentUser) -> MessageResponse:
    session = db.scalar(
        select(RefreshSession).where(RefreshSession.id == session_id, RefreshSession.user_id == user.id)
    )
    if session is None:
        raise HTTPException(status_code=404, detail="设备会话不存在")
    if session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="auth.session_revoked",
                target_type="refresh_session",
                target_id=session.id,
                details={"device_name": session.device_name},
            )
        )
        db.commit()
    return MessageResponse(message="设备会话已撤销")


@router.post("/change-password", response_model=MessageResponse)
def change_password(payload: ChangePasswordRequest, db: DbSession, user: CurrentUser) -> MessageResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    user.password_hash = hash_password(payload.new_password)
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.add(AuditLog(actor_user_id=user.id, action="user.password_changed", target_type="user", target_id=user.id))
    db.commit()
    return MessageResponse(message="密码已修改，请重新登录")


@router.delete("/me", response_model=MessageResponse)
def delete_account(db: DbSession, user: CurrentUser) -> MessageResponse:
    user_id = user.id
    mistake_assets = list(db.scalars(select(MistakeAsset).where(MistakeAsset.user_id == user_id)))
    try:
        delete_assets(mistake_assets)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="个人图片存储暂时不可用，账户未注销") from exc
    db.execute(delete(MistakeProblem).where(MistakeProblem.user_id == user_id))
    db.execute(delete(Conversation).where(Conversation.user_id == user_id))
    db.execute(delete(UserKnowledgeState).where(UserKnowledgeState.user_id == user_id))
    db.execute(delete(LearningEvent).where(LearningEvent.user_id == user_id))
    db.execute(delete(CreditLedger).where(CreditLedger.user_id == user_id))
    db.execute(delete(CreditAccount).where(CreditAccount.user_id == user_id))
    db.execute(delete(FeedbackSubmission).where(FeedbackSubmission.user_id == user_id))
    db.execute(delete(RefreshSession).where(RefreshSession.user_id == user_id))
    db.execute(update(AuditLog).where(AuditLog.actor_user_id == user_id).values(actor_user_id=None))
    # usernames are capped at 32 characters; keep the anonymized value unique
    # without overflowing PostgreSQL's VARCHAR constraint.
    user.username = f"deleted_{user.id[:24]}"
    user.email = None
    user.nickname = "已注销用户"
    user.password_hash = "deleted"
    user.is_active = False
    user.deleted_at = datetime.now(timezone.utc)
    db.add(AuditLog(action="user.deleted", target_type="user", target_id=user_id))
    db.commit()
    return MessageResponse(message="账户及个人学习数据已删除")
