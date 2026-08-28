from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.deps import AdminUser, DbSession
from app.models import AuditLog, FeedbackSubmission, KnowledgeEdge, KnowledgeNode, KnowledgeSource
from app.schemas import (
    AdminFeedbackResponse,
    FeedbackReview,
    KnowledgeEdgeCreate,
    KnowledgeNodeCreate,
    KnowledgeNodeDetail,
    KnowledgeNodeUpdate,
    KnowledgeSourceCreate,
    SourceResponse,
)


router = APIRouter(prefix="/admin", tags=["管理后台"])


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


@router.post("/knowledge/edges", status_code=201)
def create_edge(payload: KnowledgeEdgeCreate, db: DbSession, admin: AdminUser) -> dict:
    if payload.source_node_id == payload.target_node_id:
        raise HTTPException(status_code=409, detail="知识关系不能指向自身")
    if db.get(KnowledgeNode, payload.source_node_id) is None or db.get(KnowledgeNode, payload.target_node_id) is None:
        raise HTTPException(status_code=404, detail="关系节点不存在")
    edge = KnowledgeEdge(**payload.model_dump())
    db.add(edge)
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="knowledge_edge.created",
            target_type="knowledge_edge",
            target_id=edge.id,
        )
    )
    db.commit()
    return {"id": edge.id, **payload.model_dump(mode="json")}


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
