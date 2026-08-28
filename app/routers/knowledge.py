from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from app.deps import CurrentUser, DbSession
from app.models import KnowledgeEdge, KnowledgeNode, KnowledgeStatus, UserKnowledgeState
from app.schemas import KnowledgeNodeDetail, KnowledgeNodeSummary, NeighborNode, NeighborResponse
from app.services.knowledge import search_nodes, status_for


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
        if node is None or not node.is_active:
            continue
        values = _summary(db, user.id, node).model_dump()
        items.append(NeighborNode(**values, edge_type=edge.edge_type, edge_explanation=edge.explanation))
    return NeighborResponse(center=_summary(db, user.id, center), nodes=items)
