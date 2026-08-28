import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import KnowledgeNode, UserKnowledgeState


def search_nodes(db: Session, query: str, limit: int = 20) -> list[KnowledgeNode]:
    term = f"%{query.strip()}%"
    return list(
        db.scalars(
            select(KnowledgeNode)
            .options(joinedload(KnowledgeNode.source))
            .where(
                KnowledgeNode.is_active.is_(True),
                KnowledgeNode.review_status == "approved",
                or_(
                    KnowledgeNode.name.ilike(term),
                    KnowledgeNode.chapter.ilike(term),
                    KnowledgeNode.definition.ilike(term),
                ),
            )
            .limit(limit)
        )
    )


def retrieve_nodes(db: Session, question: str, pinned_node_id: str | None, limit: int = 5) -> list[KnowledgeNode]:
    nodes = list(
        db.scalars(
            select(KnowledgeNode)
            .options(joinedload(KnowledgeNode.source))
            .where(KnowledgeNode.is_active.is_(True), KnowledgeNode.review_status == "approved")
        )
    )
    tokens = set(re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z0-9]+", question.lower()))

    def score(node: KnowledgeNode) -> int:
        haystack = f"{node.name} {node.chapter} {node.definition} {node.explanation}".lower()
        value = 100 if node.id == pinned_node_id else 0
        if node.name.lower() in question.lower():
            value += 50
        value += sum(3 for token in tokens if token in haystack)
        return value

    ranked = sorted(nodes, key=score, reverse=True)
    relevant = [node for node in ranked if score(node) > 0]
    if pinned_node_id and not any(node.id == pinned_node_id for node in relevant):
        pinned = next((node for node in nodes if node.id == pinned_node_id), None)
        if pinned:
            relevant.insert(0, pinned)
    return (relevant or ranked[:1])[:limit]


def status_for(db: Session, user_id: str, node_id: str):
    return db.scalar(
        select(UserKnowledgeState).where(
            UserKnowledgeState.user_id == user_id,
            UserKnowledgeState.node_id == node_id,
        )
    )
