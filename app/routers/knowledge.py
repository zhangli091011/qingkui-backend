from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from app.deps import CurrentUser, DbSession
from app.models import KnowledgeEdge, KnowledgeNode, KnowledgeStatus, UserKnowledgeState
from app.schemas import (
    KnowledgeCatalogItem,
    KnowledgeNodeDetail,
    KnowledgeNodeSummary,
    KnowledgeTreeChapter,
    KnowledgeTreeNode,
    KnowledgeTreeResponse,
    KnowledgeTreeSection,
    NeighborNode,
    NeighborResponse,
    SubjectClassificationResponse,
)
from app.services.knowledge import search_nodes, status_for
from app.subjects import SUBJECTS, classify_subject_semantic, infer_subject


router = APIRouter(prefix="/knowledge", tags=["知识图谱"])


def _status(db: DbSession, user_id: str, node_id: str) -> KnowledgeStatus:
    state = status_for(db, user_id, node_id)
    return state.status if state else KnowledgeStatus.unexplored


def _summary(db: DbSession, user_id: str, node: KnowledgeNode) -> KnowledgeNodeSummary:
    return KnowledgeNodeSummary.model_validate(node).model_copy(update={"status": _status(db, user_id, node.id)})


@router.get("/search", response_model=list[KnowledgeNodeSummary])
def search(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[KnowledgeNodeSummary]:
    return [_summary(db, user.id, node) for node in search_nodes(db, q, limit)]


@router.get("/subjects", response_model=list[str])
def subjects(user: CurrentUser) -> list[str]:
    """Return the stable subject catalog for clients and import tooling."""
    return list(SUBJECTS)


@router.get("/catalog", response_model=list[KnowledgeCatalogItem])
def catalog(db: DbSession, user: CurrentUser) -> list[KnowledgeCatalogItem]:
    rows = db.execute(
        select(
            KnowledgeNode.subject,
            KnowledgeNode.grade,
            KnowledgeNode.textbook_version,
            func.count(KnowledgeNode.id),
        )
        .where(KnowledgeNode.is_active.is_(True), KnowledgeNode.review_status == "approved")
        .group_by(KnowledgeNode.subject, KnowledgeNode.grade, KnowledgeNode.textbook_version)
        .order_by(KnowledgeNode.subject, KnowledgeNode.grade, KnowledgeNode.textbook_version)
    ).all()
    return [KnowledgeCatalogItem(subject=s, grade=g, textbook_version=v, node_count=count) for s, g, v, count in rows]


@router.get("/tree", response_model=KnowledgeTreeResponse)
def knowledge_tree(
    db: DbSession,
    user: CurrentUser,
    subject: str = Query(min_length=1, max_length=40),
    grade: str = Query(min_length=1, max_length=40),
    textbook_version: str = Query(min_length=1, max_length=80),
) -> KnowledgeTreeResponse:
    nodes = list(
        db.scalars(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.subject == subject,
                KnowledgeNode.grade == grade,
                KnowledgeNode.textbook_version == textbook_version,
                KnowledgeNode.is_active.is_(True),
                KnowledgeNode.review_status == "approved",
            )
            .order_by(KnowledgeNode.chapter, KnowledgeNode.section, KnowledgeNode.name)
        )
    )
    grouped: dict[str, dict[str, list[KnowledgeTreeNode]]] = {}
    for node in nodes:
        grouped.setdefault(node.chapter, {}).setdefault(node.section or "本章知识点", []).append(
            KnowledgeTreeNode(id=node.id, name=node.name, status=_status(db, user.id, node.id))
        )
    chapters = [
        KnowledgeTreeChapter(
            name=chapter,
            sections=[KnowledgeTreeSection(name=section, nodes=values) for section, values in sections.items()],
        )
        for chapter, sections in grouped.items()
    ]
    return KnowledgeTreeResponse(subject=subject, grade=grade, textbook_version=textbook_version, chapters=chapters)


@router.get("/classify-subject", response_model=SubjectClassificationResponse)
def classify_subject(user: CurrentUser, q: str = Query(min_length=1, max_length=200)) -> SubjectClassificationResponse:
    """Return semantic subject scores for UI previews and manual correction."""
    result = classify_subject_semantic(q)
    if result.source == "unavailable":
        result = result.__class__(infer_subject(q), 0.0, 0.0, "keyword_fallback", {})
    return SubjectClassificationResponse(
        subject=result.subject,
        confidence=result.confidence,
        margin=result.margin,
        source=result.source,
        scores=result.scores,
    )


@router.get("/nodes/{node_id}", response_model=KnowledgeNodeDetail)
def node_detail(node_id: str, db: DbSession, user: CurrentUser) -> KnowledgeNodeDetail:
    node = db.scalar(
        select(KnowledgeNode)
        .options(joinedload(KnowledgeNode.source))
        .where(KnowledgeNode.id == node_id, KnowledgeNode.is_active.is_(True), KnowledgeNode.review_status == "approved")
    )
    if node is None:
        raise HTTPException(status_code=404, detail="知识点不存在")
    return KnowledgeNodeDetail.model_validate(node).model_copy(update={"status": _status(db, user.id, node.id)})


@router.get("/nodes/{node_id}/neighbors", response_model=NeighborResponse)
def neighbors(
    node_id: str,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=100),
) -> NeighborResponse:
    center = db.get(KnowledgeNode, node_id)
    if center is None or not center.is_active or center.review_status != "approved":
        raise HTTPException(status_code=404, detail="知识点不存在")
    edges = list(
        db.scalars(
            select(KnowledgeEdge)
            .where(or_(KnowledgeEdge.source_node_id == node_id, KnowledgeEdge.target_node_id == node_id))
            .limit(limit)
        )
    )
    items: list[NeighborNode] = []
    for edge in edges:
        other_id = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
        node = db.get(KnowledgeNode, other_id)
        # Auto-extracted graph nodes remain hidden from learners until review.
        if node is None or not node.is_active or node.review_status != "approved":
            continue
        values = _summary(db, user.id, node).model_dump()
        items.append(NeighborNode(**values, edge_type=edge.edge_type, edge_explanation=edge.explanation))
    return NeighborResponse(center=_summary(db, user.id, center), nodes=items)
