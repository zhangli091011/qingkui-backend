import json
import re

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal
from app.deps import CurrentUser, DbSession
from app.models import (
    Conversation,
    CreditAccount,
    CreditLedger,
    KnowledgeNode,
    KnowledgeStatus,
    LearningEvent,
    Message,
    UserKnowledgeState,
)
from app.schemas import (
    ConversationCreate,
    ConversationMessageCreate,
    ConversationResponse,
    QaIntentOption,
    QaIntentRequest,
    QaIntentResult,
    QaResult,
)
from app.services.ai import AiStreamState, PROMPT_VERSION, answer_question, stream_answer_text
from app.services.content_safety import (
    BufferedSafetyFilter,
    ContentSafetyViolation,
    moderate_text,
    record_safety_event,
)
from app.services.knowledge import RetrievedChunk, retrieve_chunks, retrieve_nodes
from app.services.idempotency import (
    complete_idempotency,
    fail_idempotency,
    payload_hash,
    reserve_idempotency,
)
from app.services.model_calls import add_model_call, start_model_timer
from app.subjects import normalize_subject, resolve_subject


router = APIRouter(prefix="/qa", tags=["AI 问答"])


def _require_ai_available() -> None:
    if not settings.ai_enabled:
        raise HTTPException(status_code=503, detail="AI 功能正在维护，学习记录和知识图谱仍可正常使用")
    if not settings.ai_ready:
        raise HTTPException(status_code=503, detail="AI 服务尚未配置")


_VAGUE_QUESTIONS = {
    "帮我看看", "这个怎么做", "这道怎么做", "不会", "我不会", "讲一下", "解释一下",
    "为什么", "怎么办", "帮帮我", "看一下", "怎么弄", "这是什么",
}


def _needs_clarification(content: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?：:；;]", "", content).casefold()
    if normalized in _VAGUE_QUESTIONS:
        return True
    return len(normalized) <= 10 and bool(
        re.fullmatch(r"(?:这个|这道题?|它)?(?:怎么做|怎么看|讲一下|解释一下|为什么|不会|看一下)", normalized)
    )


@router.post("/intent", response_model=QaIntentResult)
def clarify_intent(payload: QaIntentRequest, db: DbSession, user: CurrentUser) -> QaIntentResult:
    decision = moderate_text(payload.content)
    if not decision.allowed:
        record_safety_event(
            db, user_id=user.id, action="safety.qa_input_blocked", decision=decision,
            content=payload.content, target_type="qa_intent",
        )
        db.commit()
        raise HTTPException(status_code=422, detail=decision.message, headers={"X-Content-Safety": "blocked"})
    subject = resolve_subject(payload.content)
    if not _needs_clarification(payload.content):
        return QaIntentResult(needs_clarification=False, subject=subject)
    return QaIntentResult(
        needs_clarification=True,
        subject=subject,
        prompt="你希望我怎样帮助你？",
        options=[
            QaIntentOption(id="explain", label="解释概念", instruction="解释相关概念、定义和关键性质", mode="knowledge"),
            QaIntentOption(id="solve", label="分析题目", instruction="分析题目条件并给出解题思路", mode="problem"),
            QaIntentOption(id="diagnose", label="检查错误", instruction="检查我的作答并定位错误原因", mode="error"),
            QaIntentOption(id="review", label="安排复习", instruction="梳理薄弱点并制定复习顺序", mode="review"),
        ],
    )


def _enforce_safe_input(db: DbSession, user_id: str, content: str, conversation_id: str) -> None:
    decision = moderate_text(content)
    if decision.allowed:
        return
    record_safety_event(
        db,
        user_id=user_id,
        action="safety.qa_input_blocked",
        decision=decision,
        content=content,
        target_type="conversation",
        target_id=conversation_id,
    )
    db.commit()
    raise HTTPException(
        status_code=422,
        detail=decision.message,
        headers={"X-Content-Safety": "blocked"},
    )


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _message_request_hash(payload: ConversationMessageCreate) -> str:
    return payload_hash(payload.model_dump(mode="json"))


def _cached_stream(result: QaResult):
    yield _sse(
        "meta",
        {
            "conversation_id": result.conversation_id,
            "credits_required": result.credits_charged,
            "cached": True,
        },
    )
    if result.assistant_message.content:
        yield _sse("delta", {"content": result.assistant_message.content, "cached": True})
    yield _sse("done", json.loads(result.model_dump_json()))


def _citations(nodes: list[KnowledgeNode], chunks: list[RetrievedChunk]) -> list[dict]:
    node_citations = [
        {
            "node_id": node.id,
            "node_name": node.name,
            "source_title": node.source.title,
            "source_location": node.source.location,
            "excerpt": node.source_excerpt,
        }
        for node in nodes
    ]
    document_citations = [
        {
            "node_id": f"document:{item.chunk.document.id}:{item.chunk.sequence}",
            "node_name": item.chunk.document.title,
            "source_title": item.chunk.document.title,
            "source_location": item.chunk.document.source_uri,
            "excerpt": item.chunk.content[:300],
            "document_id": item.chunk.document.id,
            "chunk_id": item.chunk.id,
            "score": round(item.score, 6),
        }
        for item in chunks
    ]
    return node_citations + document_citations


@router.post("/sessions", response_model=ConversationResponse, status_code=201)
def create_session(payload: ConversationCreate, db: DbSession, user: CurrentUser) -> Conversation:
    if payload.knowledge_node_id and db.get(KnowledgeNode, payload.knowledge_node_id) is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    conversation = Conversation(
        user_id=user.id,
        title=payload.title or "新对话",
        mode=payload.mode,
        knowledge_node_id=payload.knowledge_node_id,
        subject=normalize_subject(payload.subject),
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/sessions", response_model=list[ConversationResponse])
def list_sessions(
    db: DbSession,
    user: CurrentUser,
    q: str | None = Query(default=None, min_length=1, max_length=120),
    limit: int = Query(default=30, ge=1, le=100),
) -> list[Conversation]:
    statement = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.user_id == user.id)
    )
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            Conversation.title.ilike(pattern) | Conversation.messages.any(Message.content.ilike(pattern))
        )
    return list(
        db.scalars(
            statement.order_by(Conversation.updated_at.desc()).limit(limit)
        )
    )


@router.get("/sessions/{session_id}", response_model=ConversationResponse)
def get_session(session_id: str, db: DbSession, user: CurrentUser) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == session_id, Conversation.user_id == user.id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, db: DbSession, user: CurrentUser) -> None:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == session_id, Conversation.user_id == user.id)
    )
    if conversation is None:
        # Do not reveal whether another user's session exists.
        raise HTTPException(status_code=404, detail="会话不存在")
    db.delete(conversation)
    db.commit()


@router.post("/sessions/{session_id}/messages", response_model=QaResult)
def send_message(
    session_id: str,
    payload: ConversationMessageCreate,
    db: DbSession,
    user: CurrentUser,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> QaResult:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == session_id, Conversation.user_id == user.id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    _enforce_safe_input(db, user.id, payload.content, conversation.id)
    _require_ai_available()

    reservation = reserve_idempotency(
        db,
        user_id=user.id,
        scope=f"qa:{conversation.id}",
        key=idempotency_key,
        request_hash=_message_request_hash(payload),
    )
    if reservation is not None and not reservation.acquired:
        return QaResult.model_validate(reservation.response_body)
    idempotency_request_id = reservation.request_id if reservation is not None else None

    cost = 2 if payload.help_level.value == "full" else 1
    account = db.scalar(
        select(CreditAccount).where(CreditAccount.user_id == user.id).with_for_update()
    )
    if account is None or account.balance < cost:
        fail_idempotency(db, idempotency_request_id)
        db.commit()
        raise HTTPException(status_code=402, detail="额度不足")

    subject = resolve_subject(payload.content) or normalize_subject(conversation.subject)
    if subject and conversation.subject != subject:
        conversation.subject = subject
    nodes = retrieve_nodes(db, payload.content, conversation.knowledge_node_id, subject=subject)
    chunks = retrieve_chunks(db, payload.content, subject=subject)
    user_message = Message(conversation_id=conversation.id, role="student", content=payload.content)
    db.add(user_message)
    db.flush()
    model_started_at = start_model_timer()
    try:
        ai_result = answer_question(payload.content, conversation.mode, payload.help_level, nodes, chunks)
    except RuntimeError as exc:
        db.rollback()
        add_model_call(
            db,
            user_id=user.id,
            feature=f"qa_{conversation.mode.value}",
            provider=settings.ai_provider,
            model=settings.deepseek_model if settings.ai_provider == "deepseek" else "grounded-stub",
            success=False,
            started_at=model_started_at,
            error_code="provider_error",
            reference_id=conversation.id,
        )
        fail_idempotency(db, idempotency_request_id)
        db.commit()
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用，未扣除额度") from exc

    citations = _citations(nodes, chunks)
    answer = ai_result.answer
    rendered = f"{answer.conclusion}\n\n{answer.explanation}\n\n下一步：{answer.next_step}"
    output_decision = moderate_text(rendered)
    if not output_decision.allowed:
        db.rollback()
        fail_idempotency(db, idempotency_request_id)
        record_safety_event(
            db,
            user_id=user.id,
            action="safety.qa_output_blocked",
            decision=output_decision,
            content=rendered,
            target_type="conversation",
            target_id=conversation.id,
        )
        add_model_call(
            db,
            user_id=user.id,
            feature=f"qa_{conversation.mode.value}",
            provider=ai_result.provider,
            model=ai_result.model,
            success=False,
            started_at=model_started_at,
            error_code="output_safety_blocked",
            reference_id=conversation.id,
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail="回答触发内容安全保护，未扣除额度。请换一种学习问题表述。",
            headers={"X-Content-Safety": "blocked"},
        )
    assistant_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=rendered,
        structured_content=answer.model_dump(),
        citations=citations,
        linked_node_ids=[node.id for node in nodes],
        provider=ai_result.provider,
        model=ai_result.model,
        prompt_version=PROMPT_VERSION,
        knowledge_version="rag-v1",
        input_tokens=ai_result.input_tokens,
        output_tokens=ai_result.output_tokens,
    )
    db.add(assistant_message)
    db.flush()
    if conversation.title == "新对话":
        conversation.title = payload.content[:30]

    for node in nodes:
        state = db.scalar(
            select(UserKnowledgeState).where(
                UserKnowledgeState.user_id == user.id,
                UserKnowledgeState.node_id == node.id,
            )
        )
        if state is None:
            state = UserKnowledgeState(user_id=user.id, node_id=node.id, status=KnowledgeStatus.explored)
            db.add(state)
        elif state.status == KnowledgeStatus.unexplored:
            state.status = KnowledgeStatus.explored
    db.add(
        LearningEvent(
            user_id=user.id,
            node_id=nodes[0].id if nodes else None,
            event_type="asked_question",
            event_data={"session_id": conversation.id, "mode": conversation.mode.value},
        )
    )
    account.balance -= cost
    db.add(
        CreditLedger(
            user_id=user.id,
            amount=-cost,
            balance_after=account.balance,
            entry_type="qa_charge",
            feature=f"qa_{conversation.mode.value}",
            reference_id=assistant_message.id,
            provider=ai_result.provider,
            model=ai_result.model,
        )
    )
    add_model_call(
        db,
        user_id=user.id,
        feature=f"qa_{conversation.mode.value}",
        provider=ai_result.provider,
        model=ai_result.model,
        success=True,
        started_at=model_started_at,
        input_tokens=ai_result.input_tokens,
        output_tokens=ai_result.output_tokens,
        reference_id=assistant_message.id,
    )
    db.flush()
    db.refresh(user_message)
    db.refresh(assistant_message)
    db.refresh(account)
    result = QaResult(
        conversation_id=conversation.id,
        user_message=user_message,
        assistant_message=assistant_message,
        credits_charged=cost,
        balance=account.balance,
        subject=subject,
    )
    complete_idempotency(db, idempotency_request_id, result.model_dump(mode="json"))
    db.commit()
    return result


def _stream_message(
    session_id: str,
    user_id: str,
    payload: ConversationMessageCreate,
    idempotency_request_id: str | None = None,
):
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(Conversation.id == session_id, Conversation.user_id == user_id)
        )
        if conversation is None:
            yield _sse("error", {"status": 404, "detail": "会话不存在"})
            return
        cost = 2 if payload.help_level.value == "full" else 1
        account = db.scalar(select(CreditAccount).where(CreditAccount.user_id == user_id).with_for_update())
        if account is None or account.balance < cost:
            fail_idempotency(db, idempotency_request_id)
            db.commit()
            yield _sse("error", {"status": 402, "detail": "额度不足"})
            return
        try:
            yield _sse("meta", {"conversation_id": conversation.id, "credits_required": cost})
        except GeneratorExit:
            db.rollback()
            fail_idempotency(db, idempotency_request_id)
            db.commit()
            raise
        subject = resolve_subject(payload.content) or normalize_subject(conversation.subject)
        if subject and conversation.subject != subject:
            conversation.subject = subject
        nodes = retrieve_nodes(db, payload.content, conversation.knowledge_node_id, subject=subject)
        chunks = retrieve_chunks(db, payload.content, subject=subject)
        citations = _citations(nodes, chunks)
        state = AiStreamState()
        safety_filter = BufferedSafetyFilter()
        model_started_at = start_model_timer()
        try:
            for raw_chunk in stream_answer_text(
                payload.content,
                conversation.mode,
                payload.help_level,
                nodes,
                chunks,
                state,
            ):
                chunk = safety_filter.feed(raw_chunk)
                if chunk:
                    yield _sse("delta", {"content": chunk})
            tail = safety_filter.finish()
            if tail:
                yield _sse("delta", {"content": tail})
            if state.truncated:
                # Keep the persisted answer honest when the provider stops at
                # max_tokens. The client can offer a retry instead of showing
                # an indistinguishable partial answer.
                notice = "\n\n（回答达到长度上限，内容可能不完整，可点击“重新回答”。）"
                state.content += notice
                yield _sse("delta", {"content": notice})
        except ContentSafetyViolation as exc:
            db.rollback()
            fail_idempotency(db, idempotency_request_id)
            record_safety_event(
                db,
                user_id=user_id,
                action="safety.qa_output_blocked",
                decision=exc.decision,
                content=state.content,
                target_type="conversation",
                target_id=conversation.id,
            )
            add_model_call(
                db,
                user_id=user_id,
                feature=f"qa_{conversation.mode.value}",
                provider=state.provider or settings.ai_provider,
                model=state.model or settings.deepseek_model,
                success=False,
                started_at=model_started_at,
                error_code="output_safety_blocked",
                reference_id=conversation.id,
            )
            db.commit()
            yield _sse(
                "error",
                {"status": 422, "detail": "回答触发内容安全保护，未扣除额度。请换一种学习问题表述。"},
            )
            return
        except GeneratorExit:
            db.rollback()
            fail_idempotency(db, idempotency_request_id)
            db.commit()
            raise
        except RuntimeError:
            db.rollback()
            add_model_call(
                db,
                user_id=user_id,
                feature=f"qa_{conversation.mode.value}",
                provider=settings.ai_provider,
                model=settings.deepseek_model if settings.ai_provider == "deepseek" else "grounded-stub",
                success=False,
                started_at=model_started_at,
                error_code="provider_error",
                reference_id=conversation.id,
            )
            fail_idempotency(db, idempotency_request_id)
            db.commit()
            yield _sse("error", {"status": 502, "detail": "AI 服务暂时不可用，未扣除额度"})
            return

        user_message = Message(conversation_id=conversation.id, role="student", content=payload.content)
        assistant_message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=state.content,
            structured_content={
                "conclusion": state.content,
                "explanation": "",
                "evidence": [item.chunk.document.title for item in chunks[:3]] or [node.name for node in nodes[:3]],
                "next_step": "点击“重新回答”获取完整内容。" if state.truncated else "继续提出一个具体问题。",
                "uncertain": state.truncated,
                "truncated": state.truncated,
            },
            citations=citations,
            linked_node_ids=[node.id for node in nodes],
            provider=state.provider,
            model=state.model,
            prompt_version=PROMPT_VERSION,
            knowledge_version="rag-v1",
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
        )
        db.add_all((user_message, assistant_message))
        db.flush()
        if conversation.title == "新对话":
            conversation.title = payload.content[:30]
        for node in nodes:
            knowledge_state = db.scalar(
                select(UserKnowledgeState).where(
                    UserKnowledgeState.user_id == user_id,
                    UserKnowledgeState.node_id == node.id,
                )
            )
            if knowledge_state is None:
                db.add(UserKnowledgeState(user_id=user_id, node_id=node.id, status=KnowledgeStatus.explored))
            elif knowledge_state.status == KnowledgeStatus.unexplored:
                knowledge_state.status = KnowledgeStatus.explored
        db.add(
            LearningEvent(
                user_id=user_id,
                node_id=nodes[0].id if nodes else None,
                event_type="asked_question",
                event_data={"session_id": conversation.id, "mode": conversation.mode.value, "stream": True},
            )
        )
        account.balance -= cost
        db.add(
            CreditLedger(
                user_id=user_id,
                amount=-cost,
                balance_after=account.balance,
                entry_type="qa_charge",
                feature=f"qa_{conversation.mode.value}",
                reference_id=assistant_message.id,
                provider=state.provider,
                model=state.model,
            )
        )
        add_model_call(
            db,
            user_id=user_id,
            feature=f"qa_{conversation.mode.value}",
            provider=state.provider,
            model=state.model,
            success=True,
            started_at=model_started_at,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            reference_id=assistant_message.id,
        )
        db.flush()
        db.refresh(user_message)
        db.refresh(assistant_message)
        db.refresh(account)
        result = QaResult(
            conversation_id=conversation.id,
            user_message=user_message,
            assistant_message=assistant_message,
            credits_charged=cost,
            balance=account.balance,
            subject=subject,
        )
        complete_idempotency(db, idempotency_request_id, result.model_dump(mode="json"))
        db.commit()
        yield _sse("done", json.loads(result.model_dump_json()))


@router.post("/sessions/{session_id}/messages/stream")
def stream_message(
    session_id: str,
    payload: ConversationMessageCreate,
    db: DbSession,
    user: CurrentUser,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> StreamingResponse:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == session_id, Conversation.user_id == user.id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    _enforce_safe_input(db, user.id, payload.content, conversation.id)
    _require_ai_available()
    reservation = reserve_idempotency(
        db,
        user_id=user.id,
        scope=f"qa:{conversation.id}",
        key=idempotency_key,
        request_hash=_message_request_hash(payload),
    )
    if reservation is not None and not reservation.acquired:
        cached = QaResult.model_validate(reservation.response_body)
        return StreamingResponse(
            _cached_stream(cached),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Idempotency-Replayed": "true"},
        )
    idempotency_request_id = reservation.request_id if reservation is not None else None
    cost = 2 if payload.help_level.value == "full" else 1
    account = db.scalar(select(CreditAccount).where(CreditAccount.user_id == user.id))
    if account is None or account.balance < cost:
        fail_idempotency(db, idempotency_request_id)
        db.commit()
        raise HTTPException(status_code=402, detail="额度不足")
    return StreamingResponse(
        _stream_message(session_id, user.id, payload, idempotency_request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
