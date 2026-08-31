from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User, UserPrivacyConsent, UserRole
from app.security import decode_token


bearer = HTTPBearer(auto_error=False)
DbSession = Annotated[Session, Depends(get_db)]


def get_authenticated_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    try:
        payload = decode_token(credentials.credentials, "access")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效") from exc
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账户不可用")
    return user


AuthenticatedUser = Annotated[User, Depends(get_authenticated_user)]


def get_current_user(db: DbSession, user: AuthenticatedUser) -> User:
    if not settings.privacy_consent_enforced or user.role != UserRole.student:
        return user
    accepted = db.scalar(
        select(UserPrivacyConsent.id).where(
            UserPrivacyConsent.user_id == user.id,
            UserPrivacyConsent.notice_version == settings.privacy_notice_version,
            UserPrivacyConsent.withdrawn_at.is_(None),
        )
    )
    if accepted is None:
        raise HTTPException(
            status_code=428,
            detail="请先阅读并同意当前版本的隐私说明。",
            headers={"X-Privacy-Notice-Version": settings.privacy_notice_version},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role not in (UserRole.admin, UserRole.content_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_super_admin(user: CurrentUser) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要系统管理员权限")
    return user


SuperAdminUser = Annotated[User, Depends(require_super_admin)]
