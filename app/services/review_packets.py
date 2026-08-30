"""Traceable content-review packets for launch-scope knowledge candidates."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeNode, KnowledgeSource
from app.services.content_governance import node_publication_blockers


PACKET_SCHEMA = "qingkui-content-review-packet-v1"
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
