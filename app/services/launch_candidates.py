import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    EdgeType,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeVersion,
    KnowledgeSource,
)
from app.services.content_governance import PUBLISHABLE_AUTHORIZATION


HEADING_PATTERN = re.compile(r"^(知识点|题型)\s*0*\d+\s*[:：]?\s*(.{2,100})$")
STOP_PATTERN = re.compile(r"^(知识点|题型)\s*0*\d+|^【(?:即学即练|典例|变式|答案|分析|详解)")


@dataclass(frozen=True)
class CandidateSummary:
    documents: int
    nodes_created: int
    edges_created: int
    skipped_existing: int
    nodes_removed: int = 0


def _clean_title(title: str) -> str:
    value = re.sub(r"\.(docx|pdf|txt|md|pptx)$", "", title, flags=re.IGNORECASE)
    value = re.sub(r"^第\s*\d+\s*讲\s*", "", value)
    value = re.sub(r"（(?:知识清单|教师版|学生版|\d+类热点题型).*", "", value)
    value = re.sub(r"\((?:知识清单|教师版|学生版|\d+类热点题型).*", "", value)
    return re.sub(r"\s+", " ", value).strip(" -_：:")[:120]


def _node_id(subject: str, grade: str, textbook_version: str, chapter: str, name: str) -> str:
    digest = hashlib.sha1(f"{subject}|{grade}|{textbook_version}|{chapter}|{name}".encode()).hexdigest()[:24]
    return f"candidate_{digest}"


def _source_id(document_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"qingkui-document:{document_id}"))


def _source_for_document(db: Session, document: KnowledgeDocument) -> KnowledgeSource:
    source_id = _source_id(document.id)
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        source = KnowledgeSource(
            id=source_id,
            title=document.title,
            publisher="青葵知识库导入",
            edition=document.textbook_version,
            location=document.source_uri[:255],
            authorization_status=document.authorization_status,
        )
        db.add(source)
        db.flush()
    return source


def _sections(chunks: list[KnowledgeChunk]) -> list[tuple[str, str, str, int]]:
    sections: list[tuple[str, str, str, int]] = []
    for chunk in chunks:
        lines = [re.sub(r"\s+", " ", line).strip() for line in chunk.content.splitlines()]
        for index, line in enumerate(lines):
            match = HEADING_PATTERN.match(line)
            if not match:
                continue
            kind, name = match.groups()
            name = name.strip(" ：:。.")[:120]
            body: list[str] = []
            for following in lines[index + 1 :]:
                if STOP_PATTERN.match(following):
                    break
                if len(following) >= 4:
                    body.append(following)
                if sum(len(item) for item in body) >= 900:
                    break
            excerpt = " ".join(body)[:1000]
            if len(excerpt) >= 10:
                sections.append((kind, name, excerpt, chunk.sequence))
    return sections


def _add_node(
    db: Session,
    document: KnowledgeDocument,
    source: KnowledgeSource,
    *,
    name: str,
    definition: str,
    sequence: int,
    question_types: list[str] | None = None,
) -> KnowledgeNode | None:
    node_id = _node_id(
        document.subject or "",
        document.grade or "",
        document.textbook_version or "",
        document.chapter or "",
        name,
    )
    if db.get(KnowledgeNode, node_id) is not None:
        return None
    node = KnowledgeNode(
        id=node_id,
        name=name,
        subject=document.subject or "",
        grade=document.grade or "",
        textbook_version=document.textbook_version or "",
        chapter=document.chapter or name,
        definition=definition[:4000],
        explanation=f"待审核：该候选由《{document.title}》正文结构自动抽取，必须核对定义、公式、边界条件与教材定位后才能发布。",
        common_errors=[],
        question_types=question_types or [],
        source_id=source.id,
        source_excerpt=f"《{document.title}》片段 {sequence}"[:255],
        review_status="draft",
        is_active=False,
    )
    db.add(node)
    db.flush()
    db.add(
        KnowledgeNodeVersion(
            node_id=node.id,
            version=1,
            snapshot={
                "id": node.id,
                "name": node.name,
                "subject": node.subject,
                "grade": node.grade,
                "textbook_version": node.textbook_version,
                "chapter": node.chapter,
                "definition": node.definition,
                "explanation": node.explanation,
                "common_errors": [],
                "question_types": list(node.question_types),
                "source_id": node.source_id,
                "source_excerpt": node.source_excerpt,
                "review_status": "draft",
                "is_active": False,
            },
            status="draft",
            change_note="从授权文档结构生成首发内容候选",
        )
    )
    return node


def _remove_stale_scope_candidates(
    db: Session,
    *,
    subject: str,
    grade: str,
    textbook_version: str,
) -> int:
    documents = list(db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.subject == subject)))
    source_scope = {
        _source_id(document.id): (
            document.grade == grade and document.textbook_version == textbook_version
        )
        for document in documents
    }
    stale_source_ids = [source_id for source_id, in_scope in source_scope.items() if not in_scope]
    if not stale_source_ids:
        return 0
    stale_nodes = list(
        db.scalars(
            select(KnowledgeNode).where(
                KnowledgeNode.id.like("candidate_%"),
                KnowledgeNode.subject == subject,
                KnowledgeNode.grade == grade,
                KnowledgeNode.textbook_version == textbook_version,
                KnowledgeNode.source_id.in_(stale_source_ids),
                KnowledgeNode.review_status == "draft",
                KnowledgeNode.is_active.is_(False),
            )
        )
    )
    for node in stale_nodes:
        db.delete(node)
    db.flush()
    return len(stale_nodes)


def materialize_launch_candidates(
    db: Session,
    *,
    subject: str,
    grade: str,
    textbook_version: str,
    limit: int = 600,
) -> CandidateSummary:
    nodes_removed = _remove_stale_scope_candidates(
        db,
        subject=subject,
        grade=grade,
        textbook_version=textbook_version,
    )
    documents = list(
        db.scalars(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.subject == subject,
                KnowledgeDocument.grade == grade,
                KnowledgeDocument.textbook_version == textbook_version,
                KnowledgeDocument.authorization_status.in_(tuple(PUBLISHABLE_AUTHORIZATION)),
                KnowledgeDocument.status.in_(("text_ready", "indexed")),
            )
            .order_by(KnowledgeDocument.created_at)
        )
    )
    preferred: dict[str, KnowledgeDocument] = {}
    role_rank = {"teacher_guide": 0, "notes": 1, "textbook": 2, "student_edition": 3}
    for document in documents:
        family = str((document.document_metadata or {}).get("document_family") or document.id)
        current = preferred.get(family)
        if current is None or role_rank.get(document.document_role or "", 9) < role_rank.get(current.document_role or "", 9):
            preferred[family] = document

    nodes_created = edges_created = skipped_existing = 0
    for document in preferred.values():
        if nodes_created >= limit:
            break
        chunks = list(
            db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id, KnowledgeChunk.content_type == "text")
                .order_by(KnowledgeChunk.sequence)
            )
        )
        if not chunks:
            continue
        source = _source_for_document(db, document)
        root_name = _clean_title(document.title) or document.chapter or document.title[:120]
        root_definition = re.sub(r"\s+", " ", chunks[0].content).strip()[:1000]
        root = _add_node(db, document, source, name=root_name, definition=root_definition, sequence=chunks[0].sequence)
        if root is None:
            root = db.get(KnowledgeNode, _node_id(subject, grade, textbook_version, document.chapter or "", root_name))
            skipped_existing += 1
        else:
            nodes_created += 1

        seen_names = {root_name}
        for kind, name, excerpt, sequence in _sections(chunks):
            if nodes_created >= limit:
                break
            if name in seen_names:
                continue
            seen_names.add(name)
            child = _add_node(
                db,
                document,
                source,
                name=name,
                definition=excerpt,
                sequence=sequence,
                question_types=[name] if kind == "题型" else [],
            )
            if child is None:
                skipped_existing += 1
                continue
            nodes_created += 1
            if root is not None:
                edge_type = EdgeType.question_type if kind == "题型" else EdgeType.related
                db.add(
                    KnowledgeEdge(
                        source_node_id=root.id,
                        target_node_id=child.id,
                        edge_type=edge_type,
                        explanation=f"由《{document.title[:80]}》的{kind}层级自动生成，待审核",
                    )
                )
                edges_created += 1
    db.commit()
    return CandidateSummary(len(preferred), nodes_created, edges_created, skipped_existing, nodes_removed)
