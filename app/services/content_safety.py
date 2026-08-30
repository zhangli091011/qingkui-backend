from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    category: str | None = None
    message: str | None = None


class ContentSafetyViolation(ValueError):
    def __init__(self, decision: SafetyDecision) -> None:
        super().__init__(decision.message or "内容未通过安全检查")
        self.decision = decision


_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "sexual_minors",
        re.compile(r"(?:未成年|儿童|小学生|初中生).{0,20}(?:色情|裸照|成人视频|性行为)|(?:色情|裸照|成人视频|性行为).{0,20}(?:未成年|儿童|小学生|初中生)", re.I | re.S),
        "该内容涉及未成年人安全，不能在学习助手中处理。",
    ),
    (
        "violent_threat",
        re.compile(r"(?:我要|我准备|帮我|教我).{0,12}(?:杀死|杀掉|伤害|绑架)(?:他|她|同学|老师|家人|某人)?", re.I | re.S),
        "该内容涉及现实人身安全风险，不能继续处理；如存在紧迫危险，请立即联系可信任的成年人或当地紧急服务。",
    ),
    (
        "self_harm",
        re.compile(r"(?:我想|我准备|怎样|怎么|教我).{0,12}(?:自杀|自残|结束生命)|(?:不想活了|活不下去了)", re.I | re.S),
        "你现在的安全最重要。请立刻联系身边可信任的成年人、家长或老师；如有立即伤害自己的风险，请联系当地紧急服务并不要独处。",
    ),
    (
        "dangerous_instructions",
        re.compile(r"(?:制作|自制|配制|合成|制造).{0,16}(?:炸弹|爆炸物|枪支|冰毒|毒品)|(?:炸弹|爆炸物|枪支|冰毒|毒品).{0,16}(?:教程|配方|步骤|材料清单)", re.I | re.S),
        "该请求包含可能造成现实伤害的操作性内容，不能提供。",
    ),
    (
        "credential_exposure",
        re.compile(r"(?:sk-[A-Za-z0-9._-]{16,}|LTAI[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{12,})"),
        "检测到疑似访问密钥。为保护账户安全，内容未提交；请立即撤销并轮换该密钥。",
    ),
    (
        "prompt_attack",
        re.compile(r"(?:忽略|绕过|覆盖).{0,20}(?:系统规则|安全规则|系统提示|开发者指令).{0,30}(?:密钥|密码|提示词|内部指令|泄露)", re.I | re.S),
        "该内容试图绕过系统安全规则，不能处理。",
    ),
)


def moderate_text(value: str | None) -> SafetyDecision:
    text = (value or "").strip()
    if not text:
        return SafetyDecision(allowed=True)
    for category, pattern, message in _RULES:
        if pattern.search(text):
            return SafetyDecision(allowed=False, category=category, message=message)
    return SafetyDecision(allowed=True)


def require_safe_text(value: str | None) -> None:
    decision = moderate_text(value)
    if not decision.allowed:
        raise ContentSafetyViolation(decision)


def content_fingerprint(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def moderation_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(value)
        else:
            parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(parts)


def record_safety_event(
    db: Session,
    *,
    user_id: str | None,
    action: str,
    decision: SafetyDecision,
    content: str | None,
    target_type: str,
    target_id: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details={
                "category": decision.category,
                "content_sha256": content_fingerprint(content),
                "content_length": len(content or ""),
            },
        )
    )


class BufferedSafetyFilter:
    def __init__(self, holdback_chars: int = 160) -> None:
        self.holdback_chars = max(32, holdback_chars)
        self._pending = ""
        self._complete = ""

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        self._complete += chunk
        require_safe_text(self._complete)
        if len(self._pending) <= self.holdback_chars:
            return ""
        boundary = len(self._pending) - self.holdback_chars
        emitted, self._pending = self._pending[:boundary], self._pending[boundary:]
        return emitted

    def finish(self) -> str:
        require_safe_text(self._complete)
        emitted, self._pending = self._pending, ""
        return emitted
