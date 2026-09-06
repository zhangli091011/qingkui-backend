from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import case, func, or_, select
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


def _statuses(db: DbSession, user_id: str, node_ids: list[str]) -> dict[str, KnowledgeStatus]:
    if not node_ids:
        return {}
    return dict(
        db.execute(
            select(UserKnowledgeState.node_id, UserKnowledgeState.status).where(
                UserKnowledgeState.user_id == user_id,
                UserKnowledgeState.node_id.in_(node_ids),
            )
        ).all()
    )


def _states(db: DbSession, user_id: str, node_ids: list[str]) -> dict[str, UserKnowledgeState]:
    if not node_ids:
        return {}
    return {
        state.node_id: state
        for state in db.scalars(
            select(UserKnowledgeState).where(
                UserKnowledgeState.user_id == user_id,
                UserKnowledgeState.node_id.in_(node_ids),
            )
        )
    }


def _summaries(db: DbSession, user_id: str, nodes: list[KnowledgeNode]) -> list[KnowledgeNodeSummary]:
    states = _states(db, user_id, [node.id for node in nodes])
    return [
        KnowledgeNodeSummary.model_validate(node).model_copy(
            update={
                "status": states[node.id].status if node.id in states else KnowledgeStatus.unexplored,
                "is_favorite": states[node.id].is_favorite if node.id in states else False,
            }
        )
        for node in nodes
    ]


@router.get("/search", response_model=list[KnowledgeNodeSummary])
def search(
    db: DbSession,
    user: CurrentUser,
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[KnowledgeNodeSummary]:
    return _summaries(db, user.id, search_nodes(db, q, limit))


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
    statuses = _statuses(db, user.id, [node.id for node in nodes])
    grouped: dict[str, dict[str, list[KnowledgeTreeNode]]] = {}
    for node in nodes:
        grouped.setdefault(node.chapter, {}).setdefault(node.section or "本章知识点", []).append(
            KnowledgeTreeNode(id=node.id, name=node.name, status=statuses.get(node.id, KnowledgeStatus.unexplored))
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
    state = status_for(db, user.id, node.id)
    return KnowledgeNodeDetail.model_validate(node).model_copy(
        update={
            "status": state.status if state else KnowledgeStatus.unexplored,
            "is_favorite": state.is_favorite if state else False,
            "note": state.note if state else None,
        }
    )


@router.get("/nodes/{node_id}/neighbors", response_model=NeighborResponse)
def neighbors(
    node_id: str,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(default=30, ge=1, le=100),
    direction: str = Query(default="all", pattern="^(all|outgoing|incoming)$"),
) -> NeighborResponse:
    center = db.get(KnowledgeNode, node_id)
    if center is None or not center.is_active or center.review_status != "approved":
        raise HTTPException(status_code=404, detail="知识点不存在")
    other_node_id = case(
        (KnowledgeEdge.source_node_id == node_id, KnowledgeEdge.target_node_id),
        else_=KnowledgeEdge.source_node_id,
    )
    edge_filter = {
        "all": or_(KnowledgeEdge.source_node_id == node_id, KnowledgeEdge.target_node_id == node_id),
        "outgoing": KnowledgeEdge.source_node_id == node_id,
        "incoming": KnowledgeEdge.target_node_id == node_id,
    }[direction]
    rows = db.execute(
        select(KnowledgeEdge, KnowledgeNode)
        .join(KnowledgeNode, KnowledgeNode.id == other_node_id)
        .where(
            edge_filter,
            KnowledgeNode.is_active.is_(True),
            KnowledgeNode.review_status == "approved",
        )
        .order_by(KnowledgeEdge.id)
        .limit(limit)
    ).all()
    nodes = [node for _, node in rows]
    states = _states(db, user.id, [center.id, *[node.id for node in nodes]])
    items: list[NeighborNode] = []
    for edge, node in rows:
        values = KnowledgeNodeSummary.model_validate(node).model_copy(
            update={
                "status": states[node.id].status if node.id in states else KnowledgeStatus.unexplored,
                "is_favorite": states[node.id].is_favorite if node.id in states else False,
            }
        ).model_dump()
        items.append(
            NeighborNode(
                **values,
                edge_type=edge.edge_type,
                edge_explanation=edge.explanation,
                edge_outgoing=edge.source_node_id == node_id,
            )
        )
    center_summary = KnowledgeNodeSummary.model_validate(center).model_copy(
        update={
            "status": states[center.id].status if center.id in states else KnowledgeStatus.unexplored,
            "is_favorite": states[center.id].is_favorite if center.id in states else False,
        }
    )
    return NeighborResponse(center=center_summary, nodes=items)
