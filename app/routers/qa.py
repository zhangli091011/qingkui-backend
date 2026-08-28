from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
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
from app.schemas import ConversationCreate, ConversationMessageCreate, ConversationResponse, QaResult
from app.services.ai import PROMPT_VERSION, answer_question
from app.services.knowledge import retrieve_nodes


router = APIRouter(prefix="/qa", tags=["AI 问答"])


@router.post("/sessions", response_model=ConversationResponse, status_code=201)
def create_session(payload: ConversationCreate, db: DbSession, user: CurrentUser) -> Conversation:
    if payload.knowledge_node_id and db.get(KnowledgeNode, payload.knowledge_node_id) is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    conversation = Conversation(
        user_id=user.id,
        title=payload.title or "新对话",
        mode=payload.mode,
        knowledge_node_id=payload.knowledge_node_id,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/sessions", response_model=list[ConversationResponse])
def list_sessions(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[Conversation]:
    return list(
        db.scalars(
            select(Conversation)
            .options(selectinload(Conversation.messages))
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
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


@router.post("/sessions/{session_id}/messages", response_model=QaResult)
def send_message(
    session_id: str,
    payload: ConversationMessageCreate,
    db: DbSession,
    user: CurrentUser,
) -> QaResult:
    conversation = db.scalar(
        select(Conversation).where(Conversation.id == session_id, Conversation.user_id == user.id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not settings.ai_ready:
        raise HTTPException(status_code=503, detail="AI 服务尚未配置")

    cost = 2 if payload.help_level.value == "full" else 1
    account = db.scalar(
        select(CreditAccount).where(CreditAccount.user_id == user.id).with_for_update()
    )
    if account is None or account.balance < cost:
        raise HTTPException(status_code=402, detail="额度不足")

    nodes = retrieve_nodes(db, payload.content, conversation.knowledge_node_id)
    user_message = Message(conversation_id=conversation.id, role="student", content=payload.content)
    db.add(user_message)
    db.flush()
    try:
        ai_result = answer_question(payload.content, conversation.mode, payload.help_level, nodes)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用，未扣除额度") from exc

    citations = [
        {
            "node_id": node.id,
            "node_name": node.name,
            "source_title": node.source.title,
            "source_location": node.source.location,
            "excerpt": node.source_excerpt,
        }
        for node in nodes
    ]
    answer = ai_result.answer
    rendered = f"{answer.conclusion}\n\n{answer.explanation}\n\n下一步：{answer.next_step}"
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
        knowledge_version="seed-v1",
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
    db.commit()
    db.refresh(user_message)
    db.refresh(assistant_message)
    db.refresh(account)
    return QaResult(
        conversation_id=conversation.id,
        user_message=user_message,
        assistant_message=assistant_message,
        credits_charged=cost,
        balance=account.balance,
    )
