from collections import Counter

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeEdge, KnowledgeNode, KnowledgeSource


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

    documents = list(db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.subject == subject)))
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
            "total_for_subject": len(documents),
            "missing_metadata": missing_metadata,
            "unpublishable_authorization": unpublishable_documents,
            "pending_formula_review": pending_formulas,
        },
        "relations": relation_counts,
        "blocker_counts": dict(blocker_counts.most_common()),
        "candidates": candidates,
    }
