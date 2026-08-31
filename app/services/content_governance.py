from collections import Counter

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EdgeType, KnowledgeChunk, KnowledgeDocument, KnowledgeEdge, KnowledgeNode, KnowledgeSource


PUBLISHABLE_AUTHORIZATION = {"authorized", "self_owned", "public_domain"}
PLACEHOLDER_MARKERS = ("待审核", "待补充", "正式定义待审核", "演示范围", "课程主题词表")


def node_publication_blockers(db: Session, node: KnowledgeNode, *, enforce_scope: bool | None = None) -> list[str]:
    blockers: list[str] = []
    source = db.get(KnowledgeSource, node.source_id)
    if source is None:
        blockers.append("来源不存在")
    elif source.authorization_status not in PUBLISHABLE_AUTHORIZATION:
        blockers.append("来源未取得正式发布授权")

    should_enforce_scope = settings.content_enforce_launch_scope if enforce_scope is None else enforce_scope
    if should_enforce_scope:
        if node.subject != settings.content_launch_subject:
            blockers.append(f"首发仅允许学科：{settings.content_launch_subject}")
        if node.grade != settings.content_launch_grade:
            blockers.append(f"首发仅允许年级：{settings.content_launch_grade}")
        if node.textbook_version != settings.content_launch_textbook_version:
            blockers.append(f"首发仅允许教材版本：{settings.content_launch_textbook_version}")

    if len(node.definition.strip()) < 10:
        blockers.append("定义过短")
    if len(node.explanation.strip()) < 30:
        blockers.append("解释过短")
    combined = f"{node.definition}\n{node.explanation}\n{node.source_excerpt}"
    if any(marker in combined for marker in PLACEHOLDER_MARKERS):
        blockers.append("内容仍包含待审核或演示占位语")
    if not node.common_errors:
        blockers.append("缺少常见错误")
    if not node.question_types:
        blockers.append("缺少典型题型")
    if len(node.source_excerpt.strip()) < 6:
        blockers.append("来源定位不足")

    edge_count = db.scalar(
        select(func.count(KnowledgeEdge.id)).where(
            or_(KnowledgeEdge.source_node_id == node.id, KnowledgeEdge.target_node_id == node.id)
        )
    ) or 0
    if edge_count == 0:
        blockers.append("缺少知识关系")
    return blockers


def governance_report(
    db: Session,
    *,
    subject: str | None = None,
    grade: str | None = None,
    textbook_version: str | None = None,
) -> dict:
    subject = subject or settings.content_launch_subject
    grade = grade or settings.content_launch_grade
    textbook_version = textbook_version or settings.content_launch_textbook_version

    nodes = list(
        db.scalars(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.subject == subject,
                KnowledgeNode.grade == grade,
                KnowledgeNode.textbook_version == textbook_version,
            )
            .order_by(KnowledgeNode.chapter, KnowledgeNode.name)
        )
    )
    candidates: list[dict] = []
    blocker_counts: Counter[str] = Counter()
    for node in nodes:
        blockers = node_publication_blockers(db, node, enforce_scope=False)
        blocker_counts.update(blockers)
        candidates.append(
            {
                "id": node.id,
                "name": node.name,
                "chapter": node.chapter,
                "review_status": node.review_status,
                "publishable": not blockers,
                "blockers": blockers,
            }
        )

    subject_documents = list(
        db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.subject == subject))
    )
    # A document explicitly assigned to another grade or edition is outside the
    # launch gate. Null values remain candidates so incomplete launch metadata
    # cannot disappear from the report merely because it is incomplete.
    documents = [
        document
        for document in subject_documents
        if document.grade in {None, grade}
        and document.textbook_version in {None, textbook_version}
    ]
    missing_metadata = sum(
        1
        for document in documents
        if not all((document.grade, document.textbook_version, document.chapter, document.document_role))
    )
    unpublishable_documents = sum(
        1 for document in documents if document.authorization_status not in PUBLISHABLE_AUTHORIZATION
    )
    pending_formulas = db.scalar(
        select(func.count(KnowledgeChunk.id))
        .join(KnowledgeChunk.document)
        .where(
            KnowledgeDocument.subject == subject,
            or_(KnowledgeDocument.grade.is_(None), KnowledgeDocument.grade == grade),
            or_(
                KnowledgeDocument.textbook_version.is_(None),
                KnowledgeDocument.textbook_version == textbook_version,
            ),
            KnowledgeChunk.content_type == "formula",
            KnowledgeChunk.formula_review_status == "pending",
        )
    ) or 0

    relation_counts = {
        str(edge_type.value if hasattr(edge_type, "value") else edge_type): count
        for edge_type, count in db.execute(
            select(KnowledgeEdge.edge_type, func.count(KnowledgeEdge.id))
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeEdge.source_node_id)
            .where(KnowledgeNode.subject == subject)
            .group_by(KnowledgeEdge.edge_type)
        )
    }
    return {
        "scope": {"subject": subject, "grade": grade, "textbook_version": textbook_version},
        "nodes": {
            "total": len(nodes),
            "approved": sum(node.review_status == "approved" and node.is_active for node in nodes),
            "publishable": sum(item["publishable"] for item in candidates),
            "blocked": sum(not item["publishable"] for item in candidates),
        },
        "documents": {
            "total_for_subject": len(subject_documents),
            "total_in_launch_scope": len(documents),
            "missing_metadata": missing_metadata,
            "unpublishable_authorization": unpublishable_documents,
            "pending_formula_review": pending_formulas,
        },
        "relations": relation_counts,
        "blocker_counts": dict(blocker_counts.most_common()),
        "candidates": candidates,
    }


def graph_integrity_report(db: Session, *, subject: str | None = None) -> dict:
    """Return deterministic graph problems that require content-admin review."""
    all_nodes = list(db.scalars(select(KnowledgeNode).order_by(KnowledgeNode.id)))
    node_by_id = {node.id: node for node in all_nodes}
    scoped_nodes = [node for node in all_nodes if subject is None or node.subject == subject]
    scoped_ids = {node.id for node in scoped_nodes}
    edges = list(db.scalars(select(KnowledgeEdge).order_by(KnowledgeEdge.id)))
    scoped_edges = [
        edge
        for edge in edges
        if subject is None or edge.source_node_id in scoped_ids or edge.target_node_id in scoped_ids
    ]

    connected_ids = {
        node_id
        for edge in scoped_edges
        for node_id in (edge.source_node_id, edge.target_node_id)
    }
    orphaned = sorted(node.id for node in scoped_nodes if node.id not in connected_ids)
    inactive_edges = sorted(
        edge.id
        for edge in scoped_edges
        if (
            node_by_id.get(edge.source_node_id) is None
            or node_by_id.get(edge.target_node_id) is None
            or not node_by_id[edge.source_node_id].is_active
            or not node_by_id[edge.target_node_id].is_active
        )
    )
    cross_subject = sorted(
        edge.id
        for edge in scoped_edges
        if (
            node_by_id.get(edge.source_node_id) is not None
            and node_by_id.get(edge.target_node_id) is not None
            and node_by_id[edge.source_node_id].subject != node_by_id[edge.target_node_id].subject
        )
    )
    self_referential = sorted(
        edge.id for edge in scoped_edges if edge.source_node_id == edge.target_node_id
    )

    identities: dict[tuple[str, str, str, str, str], list[str]] = {}
    for node in scoped_nodes:
        identity = (
            node.subject,
            node.grade,
            node.textbook_version,
            node.chapter,
            node.name.strip().casefold(),
        )
        identities.setdefault(identity, []).append(node.id)
    duplicate_groups = sorted(
        (sorted(node_ids) for node_ids in identities.values() if len(node_ids) > 1),
        key=lambda group: group[0],
    )

    adjacency = {node.id: [] for node in scoped_nodes}
    for edge in scoped_edges:
        edge_type = edge.edge_type.value if hasattr(edge.edge_type, "value") else str(edge.edge_type)
        if (
            edge_type == EdgeType.prerequisite.value
            and edge.source_node_id in scoped_ids
            and edge.target_node_id in scoped_ids
        ):
            adjacency[edge.source_node_id].append(edge.target_node_id)
    for targets in adjacency.values():
        targets.sort()

    state = {node_id: 0 for node_id in scoped_ids}
    stack: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def visit(node_id: str) -> None:
        state[node_id] = 1
        stack.append(node_id)
        for target_id in adjacency[node_id]:
            if state[target_id] == 0:
                visit(target_id)
            elif state[target_id] == 1:
                start = stack.index(target_id)
                cycle = stack[start:]
                rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
                cycles.add(min(rotations))
        stack.pop()
        state[node_id] = 2

    for node_id in sorted(scoped_ids):
        if state[node_id] == 0:
            visit(node_id)

    return {
        "node_count": len(scoped_nodes),
        "edge_count": len(scoped_edges),
        "orphaned_node_ids": orphaned,
        "inactive_edge_ids": inactive_edges,
        "cross_subject_edge_ids": cross_subject,
        "self_referential_edge_ids": self_referential,
        "duplicate_node_groups": [list(group) for group in duplicate_groups],
        "prerequisite_cycles": [list(cycle) + [cycle[0]] for cycle in sorted(cycles)],
    }
