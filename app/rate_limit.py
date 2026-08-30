from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from functools import lru_cache

import redis
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import settings


logger = logging.getLogger("qingkui.rate_limit")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int


@lru_cache(maxsize=1)
def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1)


def _limit_for(method: str, path: str) -> int:
    if method == "POST" and path in {"/api/auth/register", "/api/auth/login", "/api/auth/refresh"}:
        return settings.rate_limit_auth_per_minute
    if method == "POST" and (
        path.endswith("/messages")
        or path.endswith("/messages/stream")
        or path.endswith("/analyze")
    ):
        return settings.rate_limit_ai_per_minute
    if method == "POST" and path.endswith("/images"):
        return settings.rate_limit_upload_per_minute
    return settings.rate_limit_default_per_minute


def _route_bucket(method: str, path: str) -> str:
    if method == "POST" and path in {"/api/auth/register", "/api/auth/login", "/api/auth/refresh"}:
        return path
    if method == "POST" and (
        path.endswith("/messages")
        or path.endswith("/messages/stream")
        or path.endswith("/analyze")
    ):
        return "/api/ai-generation"
    if method == "POST" and path.endswith("/images"):
        return "/api/user-upload"
    return path


def _client_identity(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()[:32]
        return f"token:{digest}"
    address = request.client.host if request.client else "unknown"
    if settings.rate_limit_trust_proxy_headers:
        real_ip = request.headers.get("x-real-ip", "").strip()
        forwarded = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
        address = real_ip or forwarded or address
    return f"ip:{address}"


def _take(key: str, limit: int, *, now: int | None = None) -> RateLimitDecision:
    timestamp = int(time.time()) if now is None else now
    window = timestamp // 60
    reset_at = (window + 1) * 60
    client = _redis_client()
    redis_key = f"qingkui:rate:{window}:{key}"
    pipeline = client.pipeline(transaction=True)
    pipeline.incr(redis_key)
    pipeline.expire(redis_key, max(60, reset_at - timestamp + 5))
    count, _ = pipeline.execute()
    current = int(count)
    return RateLimitDecision(
        allowed=current <= limit,
        limit=limit,
        remaining=max(0, limit - current),
        reset_at=reset_at,
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not settings.rate_limit_enabled or request.url.path in {"/health", "/admin"}:
            return await call_next(request)
        limit = _limit_for(request.method, request.url.path)
        identity = _client_identity(request)
        bucket = _route_bucket(request.method, request.url.path)
        route_key = hashlib.sha256(f"{request.method}:{bucket}".encode()).hexdigest()[:20]
        try:
            decision = _take(f"{identity}:{route_key}", limit)
        except redis.RedisError:
            logger.exception("rate_limit_store_unavailable")
            return await call_next(request)
        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(decision.reset_at),
        }
        if not decision.allowed:
            logger.warning("rate_limit_exceeded method=%s path=%s", request.method, request.url.path)
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后重试"},
                headers={**headers, "Retry-After": str(max(1, decision.reset_at - int(time.time())))},
            )
        response = await call_next(request)
        response.headers.update(headers)
        return response
