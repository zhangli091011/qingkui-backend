from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from app.deps import AdminUser, DbSession, SuperAdminUser
from app.models import (
    AuditLog,
    CreditAccount,
    CreditLedger,
    FeedbackSubmission,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeVersion,
    KnowledgeSource,
    Message,
    MistakeAsset,
    MistakeProblem,
    OcrTask,
    RefreshSession,
    User,
    UserRole,
)
from app.schemas import (
    AdminFeedbackResponse,
    AdminMistakeResponse,
    AdminMistakeReview,
    AdminOcrTaskDetail,
    AdminOcrTaskResponse,
    AdminUserResponse,
    AdminUserRoleUpdate,
    AdminUserStatusUpdate,
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
    ContentGovernanceReport,
    FormulaReviewItem,
    FormulaReviewQueueResponse,
    FormulaReviewUpdate,
    KnowledgeReviewQueueResponse,
    AuditLogResponse,
    ModelCostResponse,
    KnowledgeSourceCreate,
    SourceResponse,
)
from app.services.content_governance import node_publication_blockers, governance_report
from app.services.mistakes import enqueue_ocr_task


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


@router.get("/knowledge/nodes/{node_id}", response_model=KnowledgeNodeDetail)
def get_node(node_id: str, db: DbSession, _admin: AdminUser) -> KnowledgeNode:
    node = db.scalar(
        select(KnowledgeNode)
        .options(joinedload(KnowledgeNode.source))
        .where(KnowledgeNode.id == node_id)
    )
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    return node


@router.get("/knowledge/chapters", response_model=list[str])
def list_chapters(db: DbSession, _admin: AdminUser, subject: str | None = None) -> list[str]:
    statement = select(KnowledgeNode.chapter).distinct().order_by(KnowledgeNode.chapter)
    if subject:
        statement = statement.where(KnowledgeNode.subject == subject)
    return list(db.scalars(statement))


@router.get("/knowledge/governance", response_model=ContentGovernanceReport)
def get_content_governance_report(
    db: DbSession,
    _admin: AdminUser,
    subject: str | None = None,
    grade: str | None = None,
    textbook_version: str | None = None,
) -> dict:
    return governance_report(db, subject=subject, grade=grade, textbook_version=textbook_version)


@router.get("/knowledge/review-queue", response_model=KnowledgeReviewQueueResponse)
def knowledge_review_queue(
    db: DbSession,
    _admin: AdminUser,
    subject: str | None = None,
    grade: str | None = None,
    textbook_version: str | None = None,
    chapter: str | None = None,
    review_status: str = "draft",
    q: str | None = None,
    blocker: str | None = None,
    candidate_only: bool = True,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    statement = select(KnowledgeNode).options(joinedload(KnowledgeNode.source)).order_by(
        KnowledgeNode.chapter, KnowledgeNode.name
    )
    if candidate_only:
        statement = statement.where(KnowledgeNode.id.like("candidate_%"))
    if subject:
        statement = statement.where(KnowledgeNode.subject == subject)
    if grade:
        statement = statement.where(KnowledgeNode.grade == grade)
    if textbook_version:
        statement = statement.where(KnowledgeNode.textbook_version == textbook_version)
    if chapter:
        statement = statement.where(KnowledgeNode.chapter == chapter)
    if review_status != "all":
        statement = statement.where(KnowledgeNode.review_status == review_status)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(or_(KnowledgeNode.name.ilike(pattern), KnowledgeNode.chapter.ilike(pattern)))
    rows = list(db.scalars(statement))
    if blocker:
        rows_with_blockers = [
            (node, node_publication_blockers(db, node, enforce_scope=True)) for node in rows
        ]
        rows_with_blockers = [item for item in rows_with_blockers if blocker in item[1]]
        total = len(rows_with_blockers)
        page = rows_with_blockers[offset : offset + limit]
    else:
        total = len(rows)
        page = [
            (node, node_publication_blockers(db, node, enforce_scope=True))
            for node in rows[offset : offset + limit]
        ]
    items = []
    for node, blockers in page:
        items.append(
            {
                "id": node.id,
                "name": node.name,
                "subject": node.subject,
                "grade": node.grade,
                "textbook_version": node.textbook_version,
                "chapter": node.chapter,
                "review_status": node.review_status,
                "is_active": node.is_active,
                "source_title": node.source.title,
                "source_excerpt": node.source_excerpt,
                "blockers": blockers,
                "updated_at": node.updated_at,
            }
        )
    return {"total": total, "offset": offset, "limit": limit, "items": items}


def _formula_item(chunk: KnowledgeChunk) -> dict:
    return {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "document_title": chunk.document.title,
        "subject": chunk.document.subject,
        "chapter": chunk.document.chapter,
        "sequence": chunk.sequence,
        "formula_latex": chunk.formula_latex,
        "formula_source": chunk.formula_source,
        "ocr_confidence": chunk.ocr_confidence,
        "review_status": chunk.formula_review_status,
        "review_note": chunk.formula_review_note,
    }


@router.get("/knowledge/formulas", response_model=FormulaReviewQueueResponse)
def formula_review_queue(
    db: DbSession,
    _admin: AdminUser,
    review_status: str = "pending",
    subject: str | None = None,
    document_id: str | None = None,
    confidence_max: float | None = Query(default=None, ge=0, le=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    filters = [KnowledgeChunk.content_type == "formula"]
    if review_status != "all":
        filters.append(KnowledgeChunk.formula_review_status == review_status)
    if subject:
        filters.append(KnowledgeDocument.subject == subject)
    if document_id:
        filters.append(KnowledgeChunk.document_id == document_id)
    if confidence_max is not None:
        filters.append(KnowledgeChunk.ocr_confidence <= confidence_max)
    total = db.scalar(
        select(func.count(KnowledgeChunk.id)).join(KnowledgeDocument).where(*filters)
    ) or 0
    chunks = list(
        db.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeDocument)
            .options(joinedload(KnowledgeChunk.document))
            .where(*filters)
            .order_by(KnowledgeChunk.ocr_confidence.asc().nulls_last(), KnowledgeChunk.created_at)
            .offset(offset)
            .limit(limit)
        )
    )
    return {"total": total, "offset": offset, "limit": limit, "items": [_formula_item(chunk) for chunk in chunks]}


@router.patch("/knowledge/formulas/{chunk_id}", response_model=FormulaReviewItem)
def review_formula(
    chunk_id: str,
    payload: FormulaReviewUpdate,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    chunk = db.scalar(
        select(KnowledgeChunk)
        .options(joinedload(KnowledgeChunk.document))
        .where(KnowledgeChunk.id == chunk_id)
    )
    if chunk is None or chunk.content_type != "formula":
        raise HTTPException(status_code=404, detail="公式分块不存在")
    previous_status = chunk.formula_review_status
    if payload.formula_latex is not None:
        chunk.formula_latex = payload.formula_latex
    chunk.formula_review_status = payload.review_status
    chunk.formula_review_note = payload.review_note
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="knowledge_formula.reviewed",
            target_type="knowledge_chunk",
            target_id=chunk.id,
            details={
                "previous_status": previous_status,
                "review_status": payload.review_status,
                "formula_updated": payload.formula_latex is not None,
                "review_note": payload.review_note,
            },
        )
    )
    db.commit()
    db.refresh(chunk)
    return _formula_item(chunk)


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
    if payload.review_status == "approved" and source.authorization_status not in ("authorized", "self_owned", "public_domain"):
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
    if review_status == "approved" and source.authorization_status not in ("authorized", "self_owned", "public_domain"):
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
    blockers = node_publication_blockers(db, node)
    if blockers:
        raise HTTPException(status_code=409, detail={"message": "知识点未通过发布门", "blockers": blockers})
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
    if payload.publish and source.authorization_status not in ("authorized", "self_owned", "public_domain"):
        raise HTTPException(status_code=409, detail="未确认授权的来源不能发布到正式库")
    for key in ("name", "subject", "grade", "textbook_version", "chapter", "definition", "explanation", "common_errors", "question_types", "source_id", "source_excerpt"):
        if key in snapshot:
            setattr(node, key, snapshot[key])
    node.version += 1
    node.review_status = "draft"
    node.is_active = False
    if payload.publish:
        blockers = node_publication_blockers(db, node)
        if blockers:
            raise HTTPException(status_code=409, detail={"message": "知识点未通过发布门", "blockers": blockers})
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


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(
    db: DbSession,
    _admin: SuperAdminUser,
    q: str | None = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
    include_deleted: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    statement = (
        select(User, CreditAccount.balance)
        .outerjoin(CreditAccount, CreditAccount.user_id == User.id)
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if q:
        needle = f"%{q.strip()}%"
        statement = statement.where(
            or_(User.username.ilike(needle), User.nickname.ilike(needle), User.email.ilike(needle))
        )
    if role is not None:
        statement = statement.where(User.role == role)
    if is_active is not None:
        statement = statement.where(User.is_active.is_(is_active))
    if not include_deleted:
        statement = statement.where(User.deleted_at.is_(None))
    return [
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "nickname": user.nickname,
            "role": user.role,
            "tenant_id": user.tenant_id,
            "is_active": user.is_active,
            "balance": balance,
            "created_at": user.created_at,
            "deleted_at": user.deleted_at,
        }
        for user, balance in db.execute(statement)
    ]


def _ensure_admin_change_is_safe(db: DbSession, actor: User, target: User, *, removing_admin: bool) -> None:
    if actor.id == target.id:
        raise HTTPException(status_code=409, detail="不能在当前会话中修改自己的角色或冻结自己")
    if target.deleted_at is not None:
        raise HTTPException(status_code=409, detail="已注销账户不能恢复或变更角色")
    if removing_admin and target.role == UserRole.admin:
        active_admins = db.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.admin,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        if int(active_admins or 0) <= 1:
            raise HTTPException(status_code=409, detail="必须保留至少一个可用系统管理员")


@router.patch("/users/{user_id}/status", response_model=AdminUserResponse)
def update_user_status(
    user_id: str,
    payload: AdminUserStatusUpdate,
    db: DbSession,
    admin: SuperAdminUser,
) -> dict:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _ensure_admin_change_is_safe(db, admin, target, removing_admin=not payload.is_active)
    target.is_active = payload.is_active
    if not payload.is_active:
        db.execute(
            update(RefreshSession)
            .where(RefreshSession.user_id == target.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="user.unfrozen" if payload.is_active else "user.frozen",
            target_type="user",
            target_id=target.id,
            details={"is_active": payload.is_active},
        )
    )
    db.commit()
    balance = db.scalar(select(CreditAccount.balance).where(CreditAccount.user_id == target.id))
    return {
        "id": target.id,
        "username": target.username,
        "email": target.email,
        "nickname": target.nickname,
        "role": target.role,
        "tenant_id": target.tenant_id,
        "is_active": target.is_active,
        "balance": balance,
        "created_at": target.created_at,
        "deleted_at": target.deleted_at,
    }


@router.patch("/users/{user_id}/role", response_model=AdminUserResponse)
def update_user_role(
    user_id: str,
    payload: AdminUserRoleUpdate,
    db: DbSession,
    admin: SuperAdminUser,
) -> dict:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _ensure_admin_change_is_safe(
        db,
        admin,
        target,
        removing_admin=target.role == UserRole.admin and payload.role != UserRole.admin,
    )
    old_role = target.role.value
    target.role = payload.role
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="user.role_changed",
            target_type="user",
            target_id=target.id,
            details={"from": old_role, "to": payload.role.value},
        )
    )
    db.commit()
    balance = db.scalar(select(CreditAccount.balance).where(CreditAccount.user_id == target.id))
    return {
        "id": target.id,
        "username": target.username,
        "email": target.email,
        "nickname": target.nickname,
        "role": target.role,
        "tenant_id": target.tenant_id,
        "is_active": target.is_active,
        "balance": balance,
        "created_at": target.created_at,
        "deleted_at": target.deleted_at,
    }


@router.get("/ocr-tasks", response_model=list[AdminOcrTaskResponse])
def list_ocr_tasks(
    db: DbSession,
    _admin: SuperAdminUser,
    status_filter: str | None = Query(default=None, alias="status"),
    requires_review: bool | None = None,
    user_id: str | None = None,
    q: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[OcrTask]:
    statement = (
        select(OcrTask)
        .join(User, User.id == OcrTask.user_id)
        .join(MistakeProblem, MistakeProblem.id == OcrTask.mistake_id)
        .order_by(OcrTask.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if status_filter:
        statement = statement.where(OcrTask.status == status_filter)
    if requires_review is not None:
        statement = statement.where(OcrTask.requires_review.is_(requires_review))
    if user_id:
        statement = statement.where(OcrTask.user_id == user_id)
    if q:
        needle = f"%{q.strip()}%"
        statement = statement.where(
            or_(User.username.ilike(needle), MistakeProblem.question_text.ilike(needle))
        )
    return list(db.scalars(statement))


def _admin_ocr_detail(db: DbSession, task_id: str) -> AdminOcrTaskDetail:
    row = db.execute(
        select(OcrTask, MistakeProblem, MistakeAsset)
        .join(MistakeProblem, MistakeProblem.id == OcrTask.mistake_id)
        .join(MistakeAsset, MistakeAsset.id == OcrTask.asset_id)
        .where(OcrTask.id == task_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="OCR 任务不存在")
    task, mistake, asset = row
    values = AdminOcrTaskResponse.model_validate(task).model_dump()
    return AdminOcrTaskDetail(
        **values,
        subject=mistake.subject,
        question_text=mistake.question_text,
        corrected_text=mistake.corrected_text,
        student_work=mistake.student_work,
        question_goal=mistake.question_goal,
        asset_mime_type=asset.mime_type,
        asset_size_bytes=asset.size_bytes,
    )


@router.get("/ocr-tasks/{task_id}", response_model=AdminOcrTaskDetail)
def get_admin_ocr_task(task_id: str, db: DbSession, _admin: SuperAdminUser) -> AdminOcrTaskDetail:
    return _admin_ocr_detail(db, task_id)


@router.post("/ocr-tasks/{task_id}/cancel", response_model=AdminOcrTaskDetail)
def cancel_admin_ocr_task(task_id: str, db: DbSession, admin: SuperAdminUser) -> AdminOcrTaskDetail:
    task = db.get(OcrTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="OCR 任务不存在")
    if task.status in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="当前任务状态不能取消")
    task.cancel_requested = True
    if task.status == "queued":
        task.status = "cancelled"
        task.completed_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor_user_id=admin.id, action="ocr.cancelled", target_type="ocr_task", target_id=task.id))
    db.commit()
    return _admin_ocr_detail(db, task_id)


@router.post("/ocr-tasks/{task_id}/retry", response_model=AdminOcrTaskDetail, status_code=202)
def retry_admin_ocr_task(task_id: str, db: DbSession, admin: SuperAdminUser) -> AdminOcrTaskDetail:
    task = db.get(OcrTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="OCR 任务不存在")
    if task.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有失败或已取消的任务可以重试")
    task.status = "queued"
    task.attempts = 0
    task.cancel_requested = False
    task.error_code = None
    task.error_message = None
    task.completed_at = None
    task.queued_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor_user_id=admin.id, action="ocr.retried", target_type="ocr_task", target_id=task.id))
    db.commit()
    try:
        enqueue_ocr_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error_code = "queue_unavailable"
        task.error_message = "OCR 队列暂时不可用"
        task.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(status_code=503, detail="OCR 队列暂时不可用") from exc
    return _admin_ocr_detail(db, task_id)


@router.get("/mistakes", response_model=list[AdminMistakeResponse])
def list_admin_mistakes(
    db: DbSession,
    _admin: SuperAdminUser,
    review_status: str | None = None,
    link_status: str | None = None,
    user_id: str | None = None,
    q: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MistakeProblem]:
    statement = (
        select(MistakeProblem)
        .options(
            selectinload(MistakeProblem.assets),
            selectinload(MistakeProblem.ocr_tasks),
            selectinload(MistakeProblem.practices),
        )
        .order_by(MistakeProblem.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if review_status:
        statement = statement.where(MistakeProblem.review_status == review_status)
    if link_status:
        statement = statement.where(MistakeProblem.link_status == link_status)
    if user_id:
        statement = statement.where(MistakeProblem.user_id == user_id)
    if q:
        needle = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                MistakeProblem.question_text.ilike(needle),
                MistakeProblem.corrected_text.ilike(needle),
                MistakeProblem.student_work.ilike(needle),
            )
        )
    return list(db.scalars(statement))


@router.get("/mistakes/{mistake_id}", response_model=AdminMistakeResponse)
def get_admin_mistake(mistake_id: str, db: DbSession, _admin: SuperAdminUser) -> MistakeProblem:
    mistake = db.scalar(
        select(MistakeProblem)
        .where(MistakeProblem.id == mistake_id)
        .options(
            selectinload(MistakeProblem.assets),
            selectinload(MistakeProblem.ocr_tasks),
            selectinload(MistakeProblem.practices),
        )
    )
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    return mistake


@router.patch("/mistakes/{mistake_id}", response_model=AdminMistakeResponse)
def review_admin_mistake(
    mistake_id: str,
    payload: AdminMistakeReview,
    db: DbSession,
    admin: SuperAdminUser,
) -> MistakeProblem:
    mistake = db.get(MistakeProblem, mistake_id)
    if mistake is None:
        raise HTTPException(status_code=404, detail="错题不存在")
    values = payload.model_dump(exclude_unset=True)
    if "knowledge_node_id" in values and values["knowledge_node_id"] is not None:
        node = db.get(KnowledgeNode, values["knowledge_node_id"])
        if node is None:
            raise HTTPException(status_code=404, detail="知识点不存在")
        values.setdefault("link_status", "confirmed")
    for key, value in values.items():
        setattr(mistake, key, value)
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="mistake.reviewed",
            target_type="mistake",
            target_id=mistake.id,
            details=values,
        )
    )
    db.commit()
    return get_admin_mistake(mistake.id, db, admin)
