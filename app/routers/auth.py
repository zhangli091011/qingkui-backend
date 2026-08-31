from datetime import datetime, timedelta, timezone

import jwt
import hashlib
import secrets
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update

from app.config import settings
from app.deps import AuthenticatedUser, CurrentUser, DbSession
from app.models import (
    AuditLog,
    CreditAccount,
    CreditLedger,
    RefreshSession,
    PasswordResetToken,
    User,
    UserPrivacyConsent,
)
from app.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    DeviceSessionResponse,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    PasswordResetRequest,
    PasswordResetConfirm,
    PasswordResetRequestResponse,
    PrivacyConsentRequest,
    PrivacyConsentResponse,
    RegisterRequest,
    UserResponse,
)
from app.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from app.services.user_lifecycle import erase_user_account
from app.services.email_delivery import send_password_reset_email


router = APIRouter(prefix="/auth", tags=["账户"])


def _reset_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_is_active(session: RefreshSession, now: datetime) -> bool:
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return session.revoked_at is None and expires_at > now


def _current_privacy_consent(db: DbSession, user_id: str) -> UserPrivacyConsent | None:
    return db.scalar(
        select(UserPrivacyConsent)
        .where(
            UserPrivacyConsent.user_id == user_id,
            UserPrivacyConsent.notice_version == settings.privacy_notice_version,
            UserPrivacyConsent.withdrawn_at.is_(None),
        )
        .order_by(UserPrivacyConsent.accepted_at.desc())
    )


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
    if settings.app_env in {"pilot", "production"} and (
        not payload.privacy_consent or payload.privacy_notice_version != settings.privacy_notice_version
    ):
        raise HTTPException(
            status_code=422,
            detail="请阅读并同意当前版本的隐私说明后再注册；应用版本过旧时请先更新。",
        )
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
    if payload.privacy_consent:
        db.add(
            UserPrivacyConsent(
                user_id=user.id,
                notice_version=payload.privacy_notice_version or settings.privacy_notice_version,
            )
        )
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
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="user.register",
            target_type="user",
            target_id=user.id,
            details={"privacy_notice_version": payload.privacy_notice_version if payload.privacy_consent else None},
        )
    )
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


@router.get("/privacy-consent", response_model=PrivacyConsentResponse)
def privacy_consent_status(db: DbSession, user: AuthenticatedUser) -> PrivacyConsentResponse:
    consent = _current_privacy_consent(db, user.id)
    return PrivacyConsentResponse(
        required=consent is None,
        required_version=settings.privacy_notice_version,
        accepted_version=consent.notice_version if consent else None,
        accepted_at=consent.accepted_at if consent else None,
    )


@router.post("/privacy-consent", response_model=PrivacyConsentResponse)
def accept_privacy_consent(
    payload: PrivacyConsentRequest,
    db: DbSession,
    user: AuthenticatedUser,
) -> PrivacyConsentResponse:
    if not payload.accepted or payload.notice_version != settings.privacy_notice_version:
        raise HTTPException(status_code=422, detail="只能接受当前版本的隐私说明。")
    consent = _current_privacy_consent(db, user.id)
    if consent is None:
        consent = UserPrivacyConsent(user_id=user.id, notice_version=settings.privacy_notice_version)
        db.add(consent)
        db.flush()
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="privacy.consent_accepted",
                target_type="privacy_consent",
                target_id=consent.id,
                details={"notice_version": settings.privacy_notice_version},
            )
        )
        db.commit()
        db.refresh(consent)
    return PrivacyConsentResponse(
        required=False,
        required_version=settings.privacy_notice_version,
        accepted_version=consent.notice_version,
        accepted_at=consent.accepted_at,
    )


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


@router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_password_reset(payload: PasswordResetRequest, db: DbSession) -> PasswordResetRequestResponse:
    generic = "如果该邮箱已绑定账户，重置邮件将很快发送。"
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    if user is None:
        return PasswordResetRequestResponse(message=generic)
    now = datetime.now(timezone.utc)
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    token = secrets.token_urlsafe(32)
    record = PasswordResetToken(
        user_id=user.id,
        token_hash=_reset_hash(token),
        expires_at=now + timedelta(minutes=settings.password_reset_minutes),
    )
    db.add(record)
    db.flush()
    try:
        send_password_reset_email(email, token)
    except RuntimeError:
        db.delete(record)
        db.add(AuditLog(actor_user_id=user.id, action="auth.password_reset_delivery_failed", target_type="user", target_id=user.id))
        db.commit()
        return PasswordResetRequestResponse(message=generic)
    db.add(AuditLog(actor_user_id=user.id, action="auth.password_reset_requested", target_type="user", target_id=user.id))
    db.commit()
    return PasswordResetRequestResponse(
        message=generic,
        reset_token=token if settings.app_env == "test" else None,
    )


@router.post("/password-reset/confirm", response_model=MessageResponse)
def confirm_password_reset(payload: PasswordResetConfirm, db: DbSession) -> MessageResponse:
    now = datetime.now(timezone.utc)
    record = db.scalar(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == _reset_hash(payload.token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .with_for_update()
    )
    if record is None:
        raise HTTPException(status_code=400, detail="重置令牌无效或已过期")
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="重置令牌无效或已过期")
    user.password_hash = hash_password(payload.new_password)
    record.used_at = now
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.id != record.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.add(AuditLog(actor_user_id=user.id, action="auth.password_reset_completed", target_type="user", target_id=user.id))
    db.commit()
    return MessageResponse(message="密码已重置，请使用新密码登录")


@router.delete("/me", response_model=MessageResponse)
def delete_account(db: DbSession, user: CurrentUser) -> MessageResponse:
    user_id = user.id
    try:
        erase_user_account(db, user)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="个人图片存储暂时不可用，账户未注销") from exc
    db.add(AuditLog(action="user.deleted", target_type="user", target_id=user_id))
    db.commit()
    return MessageResponse(message="账户及个人学习数据已删除")
