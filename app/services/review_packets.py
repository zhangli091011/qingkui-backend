"""Traceable content-review packets for launch-scope knowledge candidates."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeNode,
    KnowledgeNodeVersion,
    KnowledgeSource,
    User,
    UserRole,
)
from app.services.content_governance import PUBLISHABLE_AUTHORIZATION, node_publication_blockers


PACKET_SCHEMA = "qingkui-content-review-packet-v1"


@dataclass
class ReviewApplySummary:
    completed: int = 0
    approved: int = 0
    rejected: int = 0
    changes_requested: int = 0
    published: int = 0
    blocked: dict[str, list[str]] = field(default_factory=dict)
    skipped_pending: int = 0
    stale: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reviewed_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("reviewed_at is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reviewed_at must include a timezone")
    if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("reviewed_at cannot be in the future")
    return parsed.astimezone(timezone.utc)


def _review_strings(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not 2 <= len(item.strip()) <= 500:
            raise ValueError(f"{field_name} contains an invalid item")
        text = item.strip()
        if text not in normalized:
            normalized.append(text)
    return normalized[:30]


def _review_text(value: object, field_name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"{field_name} must be {minimum}-{maximum} characters")
    return value.strip()


def _snapshot(node: KnowledgeNode) -> dict[str, Any]:
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


def apply_content_review_packet(
    db: Session,
    packet: dict[str, Any],
    *,
    reviewer: User,
    publish: bool = False,
) -> ReviewApplySummary:
    if not reviewer.is_active or reviewer.role not in (UserRole.admin, UserRole.content_admin):
        raise ValueError("reviewer must be an active admin or content_admin")
    if packet.get("schema") != PACKET_SCHEMA:
        raise ValueError("unsupported content review packet schema")
    scope = packet.get("scope")
    items = packet.get("items")
    if not isinstance(scope, dict) or not isinstance(items, list):
        raise ValueError("invalid content review packet")

    summary = ReviewApplySummary()
    node_id_counts = Counter(
        node_data.get("id")
        for item in items
        if isinstance(item, dict)
        and isinstance((node_data := item.get("node")), dict)
        and isinstance(node_data.get("id"), str)
        and node_data.get("id")
    )
    duplicate_node_ids = {node_id for node_id, count in node_id_counts.items() if count > 1}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            summary.errors[f"item-{len(summary.errors) + 1}"] = "item must be an object"
            continue
        node_data = raw_item.get("node")
        review = raw_item.get("review")
        node_id = node_data.get("id") if isinstance(node_data, dict) else None
        error_key = str(node_id or f"item-{len(summary.errors) + 1}")
        if not isinstance(node_data, dict) or not isinstance(review, dict):
            summary.errors[error_key] = "node and review are required"
            continue
        if not isinstance(node_id, str) or not node_id:
            summary.errors[error_key] = "node id is required"
            continue
        if node_id in duplicate_node_ids:
            summary.errors[error_key] = "node appears more than once in the packet"
            continue
        if review.get("status") != "completed":
            summary.skipped_pending += 1
            continue
        try:
            if review.get("reviewer") != reviewer.username:
                raise ValueError("reviewer must match the authenticated operator")
            reviewed_at = _reviewed_at(review.get("reviewed_at"))
            decision = review.get("decision")
            if decision not in {"approved", "rejected", "changes_requested"}:
                raise ValueError("decision must be approved, rejected, or changes_requested")
            notes = _review_text(review.get("notes"), "review notes", 2, 2000)
            node = db.scalar(
                select(KnowledgeNode).where(KnowledgeNode.id == node_id).with_for_update()
            )
            if node is None:
                raise ValueError("knowledge node no longer exists")
            packet_version = node_data.get("version")
            if not isinstance(packet_version, int) or packet_version != node.version:
                summary.stale.append(node.id)
                continue
            if any(
                node_value != scope.get(scope_key)
                for node_value, scope_key in (
                    (node.subject, "subject"),
                    (node.grade, "grade"),
                    (node.textbook_version, "textbook_version"),
                )
            ):
                raise ValueError("node is outside the packet scope")
            if scope.get("chapter") is not None and node.chapter != scope["chapter"]:
                raise ValueError("node is outside the packet chapter")
            if node.review_status != "draft" or node.is_active:
                raise ValueError("only inactive draft nodes can be reviewed from a packet")

            reviewed_definition = reviewed_explanation = None
            reviewed_common_errors = reviewed_question_types = None
            if decision == "approved":
                reviewed_definition = _review_text(node_data.get("definition"), "definition", 10, 4000)
                reviewed_explanation = _review_text(node_data.get("explanation"), "explanation", 30, 8000)
                reviewed_common_errors = _review_strings(node_data.get("common_errors"), "common_errors")
                reviewed_question_types = _review_strings(node_data.get("question_types"), "question_types")
                source = db.get(KnowledgeSource, node.source_id)
                if source is None or source.authorization_status not in PUBLISHABLE_AUTHORIZATION:
                    raise ValueError("source is not authorized for reviewed content")

            summary.completed += 1
            node.version += 1
            if decision == "changes_requested":
                node.review_status = "draft"
                node.is_active = False
                version_status = "changes_requested"
                summary.changes_requested += 1
            elif decision == "rejected":
                node.review_status = "archived"
                node.is_active = False
                version_status = "withdrawn"
                summary.rejected += 1
            else:
                assert reviewed_definition is not None and reviewed_explanation is not None
                assert reviewed_common_errors is not None and reviewed_question_types is not None
                node.definition = reviewed_definition
                node.explanation = reviewed_explanation
                node.common_errors = reviewed_common_errors
                node.question_types = reviewed_question_types
                blockers = node_publication_blockers(db, node)
                node.review_status = "draft"
                node.is_active = False
                version_status = "draft"
                summary.approved += 1
                if publish and not blockers:
                    node.review_status = "approved"
                    node.is_active = True
                    version_status = "published"
                    summary.published += 1
                elif publish:
                    summary.blocked[node.id] = blockers

            db.add(
                KnowledgeNodeVersion(
                    node_id=node.id,
                    version=node.version,
                    snapshot=_snapshot(node),
                    change_note=f"人工审核包：{notes}"[:255],
                    status=version_status,
                    created_by=reviewer.id,
                    published_at=reviewed_at if version_status == "published" else None,
                    withdrawn_at=reviewed_at if version_status == "withdrawn" else None,
                )
            )
            db.add(
                AuditLog(
                    actor_user_id=reviewer.id,
                    action=f"knowledge_node.review_{decision}",
                    target_type="knowledge_node",
                    target_id=node.id,
                    details={
                        "reviewed_at": reviewed_at.isoformat(),
                        "notes": notes,
                        "version": node.version,
                        "published": version_status == "published",
                        "blockers": summary.blocked.get(node.id, []),
                    },
                )
            )
        except (TypeError, ValueError) as exc:
            summary.errors[error_key] = str(exc)
    db.flush()
    return summary
ERROR_MARKERS = ("易错", "误区", "常见错误", "错误", "注意", "警示", "混淆", "陷阱")
QUESTION_PATTERN = re.compile(
    r"(?:题型|例题|考法|考点|典例|求(?:值|解|证|范围|最值|参数)|判断|证明|应用)"
)
SKIP_LINE_PATTERN = re.compile(r"^(?:答案|解析|详解|参考答案|本题答案)\s*[:：]?")
SEQUENCE_PATTERN = re.compile(r"片段\s*(\d+)")


def _clean_line(value: str) -> str:
    value = re.sub(r"^[\s#>*•·\-—]+", "", value)
    value = re.sub(r"^\d+(?:\.\d+)*[、.)）]\s*", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _candidate_lines(chunks: Iterable[KnowledgeChunk]) -> list[tuple[KnowledgeChunk, str]]:
    values: list[tuple[KnowledgeChunk, str]] = []
    for chunk in chunks:
        for raw_line in chunk.content.splitlines():
            line = _clean_line(raw_line)
            if 8 <= len(line) <= 220 and not SKIP_LINE_PATTERN.match(line):
                values.append((chunk, line))
    return values


def _suggestions(
    chunks: list[KnowledgeChunk],
    *,
    kind: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk, line in _candidate_lines(chunks):
        matches = any(marker in line for marker in ERROR_MARKERS) if kind == "common_error" else bool(
            QUESTION_PATTERN.search(line)
        )
        if not matches:
            continue
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", line).casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(
            {
                "text": line,
                "chunk_id": chunk.id,
                "sequence": chunk.sequence,
                "evidence": chunk.content[:500],
            }
        )
        if len(result) >= limit:
            break
    return result


def _select_evidence(node: KnowledgeNode, chunks: list[KnowledgeChunk], limit: int = 3) -> list[KnowledgeChunk]:
    text_chunks = [chunk for chunk in chunks if chunk.content_type == "text"]
    if not text_chunks:
        return []
    exact = [chunk for chunk in text_chunks if node.name in chunk.content]
    if exact:
        return exact[:limit]
    sequence_match = SEQUENCE_PATTERN.search(node.source_excerpt or "")
    if sequence_match:
        sequence = int(sequence_match.group(1))
        return sorted(text_chunks, key=lambda chunk: (abs(chunk.sequence - sequence), chunk.sequence))[:limit]
    chapter_terms = [term for term in re.split(r"[\s：:（）()、/]+", node.chapter) if len(term) >= 2]
    ranked = sorted(
        text_chunks,
        key=lambda chunk: (
            -sum(term in chunk.content for term in chapter_terms),
            chunk.sequence,
        ),
    )
    return ranked[:limit]


def _document_for_source(
    source: KnowledgeSource | None,
    by_uri: dict[str, KnowledgeDocument],
    by_title: dict[str, list[KnowledgeDocument]],
) -> KnowledgeDocument | None:
    if source is None:
        return None
    document = by_uri.get(source.location)
    if document is not None:
        return document
    matching_titles = by_title.get(source.title, [])
    return matching_titles[0] if len(matching_titles) == 1 else None


def build_content_review_packet(
    db: Session,
    *,
    subject: str,
    grade: str,
    textbook_version: str,
    chapter: str | None = None,
    limit: int = 600,
) -> dict[str, Any]:
    node_query = (
        select(KnowledgeNode)
        .where(
            KnowledgeNode.subject == subject,
            KnowledgeNode.grade == grade,
            KnowledgeNode.textbook_version == textbook_version,
            KnowledgeNode.review_status == "draft",
            KnowledgeNode.is_active.is_(False),
        )
        .order_by(KnowledgeNode.chapter, KnowledgeNode.name)
        .limit(max(1, min(limit, 600)))
    )
    if chapter:
        node_query = node_query.where(KnowledgeNode.chapter == chapter)
    nodes = list(db.scalars(node_query))
    source_ids = {node.source_id for node in nodes}
    sources = {
        source.id: source
        for source in db.scalars(select(KnowledgeSource).where(KnowledgeSource.id.in_(source_ids)))
    } if source_ids else {}

    documents = list(
        db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.subject == subject,
                KnowledgeDocument.grade == grade,
                KnowledgeDocument.textbook_version == textbook_version,
            )
        )
    )
    by_uri = {document.source_uri[:255]: document for document in documents}
    by_title: dict[str, list[KnowledgeDocument]] = defaultdict(list)
    for document in documents:
        by_title[document.title].append(document)
    document_ids = {
        document.id
        for source in sources.values()
        if (document := _document_for_source(source, by_uri, by_title)) is not None
    }
    chunks_by_document: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    if document_ids:
        for chunk_item in db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id.in_(document_ids))
            .order_by(KnowledgeChunk.document_id, KnowledgeChunk.sequence)
        ):
            chunks_by_document[chunk_item.document_id].append(chunk_item)

    items: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()
    evidence_count = common_error_count = question_type_count = 0
    pending_formula_ids: set[str] = set()
    for node in nodes:
        source = sources.get(node.source_id)
        document = _document_for_source(source, by_uri, by_title)
        document_chunks = chunks_by_document.get(document.id, []) if document else []
        evidence_chunks = _select_evidence(node, document_chunks)
        common_error_suggestions = _suggestions(evidence_chunks, kind="common_error")
        question_type_suggestions = _suggestions(evidence_chunks, kind="question_type")
        pending_formulas = [
            {
                "chunk_id": item.id,
                "sequence": item.sequence,
                "latex": item.formula_latex or item.content,
                "confidence": item.ocr_confidence,
                "review_status": item.formula_review_status,
            }
            for item in document_chunks
            if item.content_type == "formula" and item.formula_review_status == "pending"
        ][:20]
        blockers = node_publication_blockers(db, node, enforce_scope=False)
        blocker_counts.update(blockers)
        evidence_count += bool(evidence_chunks)
        common_error_count += len(common_error_suggestions)
        question_type_count += len(question_type_suggestions)
        pending_formula_ids.update(item["chunk_id"] for item in pending_formulas)
        items.append(
            {
                "node": {
                    "id": node.id,
                    "name": node.name,
                    "chapter": node.chapter,
                    "definition": node.definition,
                    "explanation": node.explanation,
                    "common_errors": list(node.common_errors or []),
                    "question_types": list(node.question_types or []),
                    "source_excerpt": node.source_excerpt,
                    "version": node.version,
                },
                "source": {
                    "id": source.id if source else None,
                    "title": source.title if source else None,
                    "location": source.location if source else None,
                    "authorization_status": source.authorization_status if source else None,
                    "document_id": document.id if document else None,
                    "document_role": document.document_role if document else None,
                },
                "blockers": blockers,
                "evidence": [
                    {
                        "chunk_id": item.id,
                        "sequence": item.sequence,
                        "content": item.content[:1200],
                    }
                    for item in evidence_chunks
                ],
                "suggested_common_errors": common_error_suggestions,
                "suggested_question_types": question_type_suggestions,
                "pending_formulas": pending_formulas,
                "review": {
                    "status": "pending",
                    "reviewer": None,
                    "reviewed_at": None,
                    "decision": None,
                    "notes": None,
                },
            }
        )
    return {
        "schema": PACKET_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "subject": subject,
            "grade": grade,
            "textbook_version": textbook_version,
            "chapter": chapter,
        },
        "warning": "所有建议均来自原文启发式抽取，仅供人工审核；不得自动写回或发布节点。",
        "summary": {
            "nodes": len(items),
            "nodes_with_evidence": evidence_count,
            "common_error_suggestions": common_error_count,
            "question_type_suggestions": question_type_count,
            "pending_formulas_included": len(pending_formula_ids),
            "blocker_counts": dict(blocker_counts.most_common()),
        },
        "items": items,
    }


def render_content_review_markdown(packet: dict[str, Any]) -> str:
    scope = packet["scope"]
    summary = packet["summary"]
    lines = [
        "# 知识节点人工审核包",
        "",
        f"范围：{scope['subject']} / {scope['grade']} / {scope['textbook_version']}",
        f"生成时间：{packet['generated_at']}",
        "",
        f"> {packet['warning']}",
        "",
        "## 汇总",
        "",
        f"- 节点：{summary['nodes']}",
        f"- 有原文证据：{summary['nodes_with_evidence']}",
        f"- 易错点建议：{summary['common_error_suggestions']}",
        f"- 题型建议：{summary['question_type_suggestions']}",
        f"- 待审核公式：{summary['pending_formulas_included']}",
        "",
    ]
    for item in packet["items"]:
        node = item["node"]
        source = item["source"]
        lines.extend(
            [
                f"## {node['chapter']} · {node['name']}",
                "",
                f"节点 ID：`{node['id']}`",
                f"来源：{source['title'] or '未匹配'} · `{source['location'] or 'unknown'}`",
                f"阻断：{'；'.join(item['blockers']) or '无'}",
                "",
                "### 当前内容",
                "",
                node["definition"],
                "",
                node["explanation"],
                "",
                "### 原文证据",
                "",
            ]
        )
        if item["evidence"]:
            for evidence in item["evidence"]:
                lines.append(
                    f"- 分块 `{evidence['chunk_id']}` / {evidence['sequence']}：{evidence['content']}"
                )
        else:
            lines.append("- 未匹配到原文分块，必须人工定位来源。")
        lines.extend(["", "### 待确认建议", ""])
        errors = item["suggested_common_errors"]
        questions = item["suggested_question_types"]
        lines.append("易错点：" + ("；".join(value["text"] for value in errors) if errors else "未抽取到"))
        lines.append("题型：" + ("；".join(value["text"] for value in questions) if questions else "未抽取到"))
        lines.append(f"待审核公式：{len(item['pending_formulas'])}")
        lines.extend(["", "审核结论：`pending`", "", "---", ""])
    return "\n".join(lines).rstrip() + "\n"
