from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.deps import AdminUser, DbSession
from app.models import AuditLog, CreditLedger, FeedbackSubmission, KnowledgeEdge, KnowledgeNode, KnowledgeNodeVersion, KnowledgeSource, Message
from app.schemas import (
    AdminFeedbackResponse,
    FeedbackReview,
    KnowledgeEdgeCreate,
    KnowledgeEdgeResponse,
    KnowledgeEdgeUpdate,
    KnowledgeNodeAction,
    KnowledgeNodeCreate,
    KnowledgeNodeDetail,
    KnowledgeNodeRestore,
    KnowledgeNodeUpdate,
    KnowledgeNodeVersionResponse,
    AuditLogResponse,
    ModelCostResponse,
    KnowledgeSourceCreate,
    SourceResponse,
)


router = APIRouter(prefix="/admin", tags=["管理后台"])


def _snapshot(node: KnowledgeNode) -> dict:
    return {
        "id": node.id,
        "name": node.name,
        "subject": node.subject,
        "grade": node.grade,
        "textbook_version": node.textbook_version,
        "chapter": node.chapter,
        "definition": node.definition,
        "explanation": node.explanation,
        "common_errors": list(node.common_errors or []),
        "question_types": list(node.question_types or []),
        "source_id": node.source_id,
        "source_excerpt": node.source_excerpt,
        "review_status": node.review_status,
        "is_active": node.is_active,
    }


def _add_version(db, node: KnowledgeNode, admin_id: str, *, status: str | None = None, change_note: str | None = None) -> KnowledgeNodeVersion:
    effective_status = status or ("published" if node.review_status == "approved" and node.is_active else "withdrawn" if node.review_status == "archived" else "draft")
    now = datetime.now(timezone.utc)
    version = KnowledgeNodeVersion(
        node_id=node.id,
        version=node.version,
        snapshot=_snapshot(node),
        change_note=change_note,
        status=effective_status,
        created_by=admin_id,
        published_at=now if effective_status == "published" else None,
        withdrawn_at=now if effective_status == "withdrawn" else None,
    )
    db.add(version)
    return version


@router.get("/knowledge/nodes", response_model=list[KnowledgeNodeDetail])
def list_nodes(
    db: DbSession,
    _admin: AdminUser,
    chapter: str | None = None,
    review_status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[KnowledgeNode]:
    statement = select(KnowledgeNode).options(joinedload(KnowledgeNode.source)).order_by(KnowledgeNode.chapter, KnowledgeNode.name).limit(limit)
    if chapter:
        statement = statement.where(KnowledgeNode.chapter == chapter)
    if review_status:
        statement = statement.where(KnowledgeNode.review_status == review_status)
    return list(db.scalars(statement))


@router.get("/knowledge/chapters", response_model=list[str])
def list_chapters(db: DbSession, _admin: AdminUser, subject: str | None = None) -> list[str]:
    statement = select(KnowledgeNode.chapter).distinct().order_by(KnowledgeNode.chapter)
    if subject:
        statement = statement.where(KnowledgeNode.subject == subject)
    return list(db.scalars(statement))


@router.post("/knowledge/sources", response_model=SourceResponse, status_code=201)
def create_source(payload: KnowledgeSourceCreate, db: DbSession, admin: AdminUser) -> KnowledgeSource:
    source = KnowledgeSource(**payload.model_dump())
    db.add(source)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="knowledge_source.created",
            target_type="knowledge_source",
            target_id=source.id,
        )
    )
    db.commit()
    db.refresh(source)
    return source


@router.post("/knowledge/nodes", response_model=KnowledgeNodeDetail, status_code=201)
def create_node(payload: KnowledgeNodeCreate, db: DbSession, admin: AdminUser) -> KnowledgeNode:
    if db.get(KnowledgeNode, payload.id):
        raise HTTPException(status_code=409, detail="知识点 ID 已存在")
    source = db.get(KnowledgeSource, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="内容来源不存在")
    if payload.review_status == "approved" and source.authorization_status not in ("authorized", "self_owned", "self_owned_demo"):
        raise HTTPException(status_code=409, detail="未确认授权的来源不能发布到正式库")
    node = KnowledgeNode(**payload.model_dump())
    db.add(node)
    db.flush()
    _add_version(db, node, admin.id, change_note="初始版本")
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="knowledge_node.created",
            target_type="knowledge_node",
            target_id=node.id,
            details={"version": node.version, "review_status": node.review_status},
        )
    )
    db.commit()
    return db.scalar(select(KnowledgeNode).options(joinedload(KnowledgeNode.source)).where(KnowledgeNode.id == node.id))


@router.patch("/knowledge/nodes/{node_id}", response_model=KnowledgeNodeDetail)
def update_node(
    node_id: str,
    payload: KnowledgeNodeUpdate,
    db: DbSession,
    admin: AdminUser,
) -> KnowledgeNode:
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    changes = payload.model_dump(exclude_unset=True)
    source_id = changes.get("source_id", node.source_id)
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="内容来源不存在")
    review_status = changes.get("review_status", node.review_status)
    if review_status == "approved" and source.authorization_status not in ("authorized", "self_owned", "self_owned_demo"):
        raise HTTPException(status_code=409, detail="未确认授权的来源不能发布到正式库")
    for key, value in changes.items():
        setattr(node, key, value)
    node.version += 1
    _add_version(db, node, admin.id, change_note="管理员编辑")
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="knowledge_node.updated",
            target_type="knowledge_node",
            target_id=node.id,
            details={"fields": sorted(changes), "version": node.version},
        )
    )
    db.commit()
    return db.scalar(select(KnowledgeNode).options(joinedload(KnowledgeNode.source)).where(KnowledgeNode.id == node.id))


@router.post("/knowledge/edges", response_model=KnowledgeEdgeResponse, status_code=201)
def create_edge(payload: KnowledgeEdgeCreate, db: DbSession, admin: AdminUser) -> KnowledgeEdge:
    if payload.source_node_id == payload.target_node_id:
        raise HTTPException(status_code=409, detail="知识关系不能指向自身")
    if db.get(KnowledgeNode, payload.source_node_id) is None or db.get(KnowledgeNode, payload.target_node_id) is None:
        raise HTTPException(status_code=404, detail="关系节点不存在")
    edge = KnowledgeEdge(**payload.model_dump())
    db.add(edge)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="重复的知识关系") from exc
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="knowledge_edge.created",
            target_type="knowledge_edge",
            target_id=edge.id,
        )
    )
    db.commit()
    db.refresh(edge)
    return edge


@router.get("/knowledge/edges", response_model=list[KnowledgeEdgeResponse])
def list_edges(
    db: DbSession,
    _admin: AdminUser,
    node_id: str | None = None,
    edge_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[KnowledgeEdge]:
    statement = select(KnowledgeEdge).order_by(KnowledgeEdge.id).limit(limit)
    if node_id:
        statement = statement.where((KnowledgeEdge.source_node_id == node_id) | (KnowledgeEdge.target_node_id == node_id))
    if edge_type:
        statement = statement.where(KnowledgeEdge.edge_type == edge_type)
    return list(db.scalars(statement))


@router.patch("/knowledge/edges/{edge_id}", response_model=KnowledgeEdgeResponse)
def update_edge(edge_id: str, payload: KnowledgeEdgeUpdate, db: DbSession, admin: AdminUser) -> KnowledgeEdge:
    edge = db.get(KnowledgeEdge, edge_id)
    if edge is None:
        raise HTTPException(status_code=404, detail="知识关系不存在")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(edge, key, value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="重复的知识关系") from exc
    db.add(AuditLog(actor_user_id=admin.id, action="knowledge_edge.updated", target_type="knowledge_edge", target_id=edge.id, details={"fields": sorted(changes)}))
    db.commit()
    db.refresh(edge)
    return edge


@router.delete("/knowledge/edges/{edge_id}", status_code=204)
def delete_edge(edge_id: str, db: DbSession, admin: AdminUser) -> None:
    edge = db.get(KnowledgeEdge, edge_id)
    if edge is None:
        raise HTTPException(status_code=404, detail="知识关系不存在")
    db.delete(edge)
    db.add(AuditLog(actor_user_id=admin.id, action="knowledge_edge.deleted", target_type="knowledge_edge", target_id=edge.id))
    db.commit()


@router.get("/knowledge/nodes/{node_id}/versions", response_model=list[KnowledgeNodeVersionResponse])
def list_node_versions(node_id: str, db: DbSession, _admin: AdminUser, limit: int = Query(default=100, ge=1, le=500)) -> list[KnowledgeNodeVersion]:
    if db.get(KnowledgeNode, node_id) is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    return list(db.scalars(select(KnowledgeNodeVersion).where(KnowledgeNodeVersion.node_id == node_id).order_by(KnowledgeNodeVersion.version.desc()).limit(limit)))


@router.get("/knowledge/nodes/{node_id}/versions/{version}", response_model=KnowledgeNodeVersionResponse)
def get_node_version(node_id: str, version: int, db: DbSession, _admin: AdminUser) -> KnowledgeNodeVersion:
    item = db.scalar(select(KnowledgeNodeVersion).where(KnowledgeNodeVersion.node_id == node_id, KnowledgeNodeVersion.version == version))
    if item is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return item


def _node_detail(db, node_id: str):
    return db.scalar(select(KnowledgeNode).options(joinedload(KnowledgeNode.source)).where(KnowledgeNode.id == node_id))


@router.post("/knowledge/nodes/{node_id}/publish", response_model=KnowledgeNodeDetail)
def publish_node(node_id: str, db: DbSession, admin: AdminUser, payload: KnowledgeNodeAction | None = None) -> KnowledgeNode:
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    source = db.get(KnowledgeSource, node.source_id)
    if source is None or source.authorization_status not in ("authorized", "self_owned", "self_owned_demo"):
        raise HTTPException(status_code=409, detail="未确认授权的来源不能发布到正式库")
    node.review_status = "approved"
    node.is_active = True
    node.version += 1
    _add_version(db, node, admin.id, status="published", change_note=(payload.change_note if payload else None) or "发布版本")
    db.add(AuditLog(actor_user_id=admin.id, action="knowledge_node.published", target_type="knowledge_node", target_id=node.id, details={"version": node.version}))
    db.commit()
    return _node_detail(db, node.id)


@router.post("/knowledge/nodes/{node_id}/withdraw", response_model=KnowledgeNodeDetail)
def withdraw_node(node_id: str, db: DbSession, admin: AdminUser, payload: KnowledgeNodeAction | None = None) -> KnowledgeNode:
    node = db.get(KnowledgeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    node.review_status = "archived"
    node.is_active = False
    node.version += 1
    _add_version(db, node, admin.id, status="withdrawn", change_note=(payload.change_note if payload else None) or "下线版本")
    db.add(AuditLog(actor_user_id=admin.id, action="knowledge_node.withdrawn", target_type="knowledge_node", target_id=node.id, details={"version": node.version}))
    db.commit()
    return _node_detail(db, node.id)


@router.post("/knowledge/nodes/{node_id}/restore", response_model=KnowledgeNodeDetail)
def restore_node(node_id: str, payload: KnowledgeNodeRestore, db: DbSession, admin: AdminUser) -> KnowledgeNode:
    node = db.get(KnowledgeNode, node_id)
    version = db.scalar(select(KnowledgeNodeVersion).where(KnowledgeNodeVersion.node_id == node_id, KnowledgeNodeVersion.version == payload.version))
    if node is None or version is None:
        raise HTTPException(status_code=404, detail="知识点或版本不存在")
    snapshot = version.snapshot
    source = db.get(KnowledgeSource, snapshot.get("source_id"))
    if source is None:
        raise HTTPException(status_code=404, detail="版本来源不存在")
    if payload.publish and source.authorization_status not in ("authorized", "self_owned", "self_owned_demo"):
        raise HTTPException(status_code=409, detail="未确认授权的来源不能发布到正式库")
    for key in ("name", "subject", "grade", "textbook_version", "chapter", "definition", "explanation", "common_errors", "question_types", "source_id", "source_excerpt"):
        if key in snapshot:
            setattr(node, key, snapshot[key])
    node.version += 1
    node.review_status = "draft"
    node.is_active = False
    if payload.publish:
        node.review_status = "approved"
        node.is_active = True
    _add_version(db, node, admin.id, status="published" if payload.publish else "draft", change_note=payload.change_note or f"恢复版本 {payload.version}")
    db.add(AuditLog(actor_user_id=admin.id, action="knowledge_node.restored", target_type="knowledge_node", target_id=node.id, details={"from_version": payload.version, "version": node.version, "published": payload.publish}))
    db.commit()
    return _node_detail(db, node.id)


@router.get("/feedback", response_model=list[AdminFeedbackResponse])
def list_feedback(
    db: DbSession,
    _admin: AdminUser,
    status_filter: str = Query(default="pending", alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[FeedbackSubmission]:
    statement = select(FeedbackSubmission).order_by(FeedbackSubmission.created_at.asc()).limit(limit)
    if status_filter != "all":
        statement = statement.where(FeedbackSubmission.status == status_filter)
    return list(db.scalars(statement))


@router.patch("/feedback/{feedback_id}", response_model=AdminFeedbackResponse)
def review_feedback(
    feedback_id: str,
    payload: FeedbackReview,
    db: DbSession,
    admin: AdminUser,
) -> FeedbackSubmission:
    feedback = db.get(FeedbackSubmission, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    feedback.status = payload.status
    feedback.review_note = payload.review_note
    feedback.reviewed_by = admin.id
    feedback.reviewed_at = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="feedback.reviewed",
            target_type="feedback",
            target_id=feedback.id,
            details={"status": payload.status, "review_note": payload.review_note},
        )
    )
    db.commit()
    db.refresh(feedback)
    return feedback


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def audit_logs(
    db: DbSession,
    _admin: AdminUser,
    actor_user_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditLog]:
    statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if actor_user_id:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    if action:
        statement = statement.where(AuditLog.action == action)
    if target_type:
        statement = statement.where(AuditLog.target_type == target_type)
    return list(db.scalars(statement))


@router.get("/model-costs", response_model=list[ModelCostResponse])
def model_costs(db: DbSession, _admin: AdminUser, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    message_rows = db.execute(
        select(Message.provider, Message.model, func.count(Message.id), func.coalesce(func.sum(Message.input_tokens), 0), func.coalesce(func.sum(Message.output_tokens), 0))
        .where(Message.role == "assistant")
        .group_by(Message.provider, Message.model)
    ).all()
    credit_rows = db.execute(
        select(CreditLedger.provider, CreditLedger.model, func.coalesce(func.sum(CreditLedger.amount), 0))
        .where(CreditLedger.entry_type == "qa_charge")
        .group_by(CreditLedger.provider, CreditLedger.model)
    ).all()
    credits = {(provider, model): abs(int(amount or 0)) for provider, model, amount in credit_rows}
    aggregates = {
        (provider, model): {"provider": provider, "model": model, "calls": int(calls), "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0), "credits": 0}
        for provider, model, calls, input_tokens, output_tokens in message_rows
    }
    for key, amount in credits.items():
        aggregates.setdefault(key, {"provider": key[0], "model": key[1], "calls": 0, "input_tokens": 0, "output_tokens": 0, "credits": 0})["credits"] = amount
    result = list(aggregates.values())
    return result[:limit]
