"""Local knowledge metadata backfill and conservative graph extraction."""

from __future__ import annotations

import re
import hashlib
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

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
from app.subjects import infer_subject, normalize_subject


DOCUMENT_ROLES = (
    "original", "solution", "answer", "answer_sheet", "teacher_guide",
    "student_edition", "textbook", "notes", "ocr", "other",
)

# Conservative, curriculum-level concepts used to turn imported documents into
# reviewable graph nodes.  A concept must occur in at least two documents of
# the same subject before it is materialized.
GRAPH_CONCEPTS: dict[str, tuple[str, ...]] = {
    "语文": (
        "现代文阅读", "文言文", "古诗词", "作文", "语言文字运用", "小说", "散文", "诗歌",
        "论述类文本", "实用类文本", "信息类文本", "病句", "成语", "修辞", "断句", "翻译",
        "名著", "阅读理解", "主题思想", "表达技巧", "人物形象", "论证结构",
    ),
    "数学": (
        "函数概念", "一次函数", "二次函数", "指数函数", "对数函数", "三角函数", "数列", "不等式",
        "集合", "方程", "导数", "积分", "概率", "统计", "随机变量", "向量", "立体几何",
        "解析几何", "抛物线", "复数", "排列组合", "极限", "数学归纳法", "空间向量",
    ),
    "英语": (
        "词汇", "语法", "时态", "语态", "从句", "非谓语动词", "阅读理解", "完形填空",
        "英语写作", "听力", "口语", "七选五", "语篇填空", "长难句", "翻译",
    ),
    "物理": (
        "运动学", "牛顿运动定律", "曲线运动", "万有引力", "机械能", "动量", "电场", "电势",
        "电路", "磁场", "电磁感应", "交变电流", "光学", "热学", "原子物理", "实验探究",
    ),
    "化学": (
        "物质结构", "元素周期律", "化学键", "化学反应速率", "化学平衡", "电解质", "酸碱盐",
        "氧化还原反应", "原电池", "电解池", "有机化学", "烃", "官能团", "化学实验", "溶液",
        "摩尔", "气体", "无机非金属", "金属及其化合物", "化学方程式", "同分异构体",
    ),
    "生物": (
        "细胞结构", "细胞代谢", "光合作用", "呼吸作用", "遗传规律", "基因工程", "DNA",
        "生物进化", "生态系统", "种群", "群落", "稳态与调节", "免疫调节", "激素调节", "实验设计",
    ),
    "政治": (
        "马克思主义哲学", "唯物论", "辩证法", "认识论", "历史唯物主义", "经济生活", "市场经济",
        "政治生活", "国家治理", "依法治国", "文化生活", "价值观", "中国特色社会主义", "国际关系",
    ),
    "历史": (
        "中国古代史", "选官制度", "世官制", "察举制", "九品中正制", "科举制", "唐律疏议",
        "鸦片战争", "洋务运动", "甲午战争", "辛亥革命", "新文化运动", "五四运动", "关陇集团",
        "新航路开辟", "工业革命", "英国革命", "法国大革命", "半殖民地半封建", "乡约制度",
        "丝绸之路", "四大发明", "民族工业", "抗日战争", "改革开放", "史料实证",
    ),
    "地理": (
        "地球运动", "地图", "经纬度", "大气运动", "气候", "水循环", "地形", "河流",
        "自然地理", "人文地理", "人口", "城市化", "农业", "工业", "交通运输", "区域地理",
        "可持续发展", "地理信息技术", "板块构造", "自然灾害",
    ),
}

GRAPH_SOURCE_ID = "source-local-knowledge-base-v1"
GRAPH_SUBJECT_CODES = {"语文": "chinese", "数学": "math", "英语": "english", "物理": "physics", "化学": "chemistry", "生物": "biology", "政治": "politics", "历史": "history", "地理": "geography"}


def _haystack(document: KnowledgeDocument) -> str:
    metadata = document.document_metadata or {}
    values = [
        document.title,
        document.source_uri,
        str(metadata.get("filename", "")),
        str(metadata.get("relative_path", "")),
        str(metadata.get("original_source_uri", "")),
    ]
    return unquote(" ".join(value for value in values if value)).lower()


def infer_document_role(text: str, source_type: str | None = None) -> str:
    value = text.lower()
    if source_type and "ocr" in source_type.lower():
        return "ocr"
    patterns = (
        ("answer_sheet", ("答题卡", "answer sheet", "答题纸")),
        ("teacher_guide", ("教师用书", "教师版", "teacher", "教参", "教学参考")),
        ("solution", ("解析", "详解", "solution", "讲解", "解法")),
        ("answer", ("答案", "answer", "参考答案")),
        ("original", ("原卷", "试卷", "真题", "模拟题", "卷面", "original")),
        ("student_edition", ("学生版", "学生用书", "student")),
        ("textbook", ("教材", "课本", "textbook", "教科书")),
        ("notes", ("笔记", "知识清单", "讲义", "notes", "思维导图")),
    )
    for role, keywords in patterns:
        if any(keyword in value for keyword in keywords):
            return role
    return "other"


def infer_grade(text: str) -> str | None:
    value = text.lower()
    patterns = (
        (r"选择性必修(?:第一|第二|第三)册", "高二"),
        (r"空间向量|直线和圆的方程|圆锥曲线|椭圆|双曲线|抛物线|数列|等差数列|等比数列|导数", "高二"),
        (r"计数原理|排列组合|随机变量|二项分布|超几何分布|正态分布|成对数据", "高二"),
        (r"高中数学必修第一册|(?<!选择性)必修第一册", "高一"),
        (r"高中数学必修第二册|(?<!选择性)必修第二册", "高一"),
        (r"高中一年级|高\s*[一1]", "高一"),
        (r"高中二年级|高\s*[二2]", "高二"),
        (r"高中三年级|高\s*[三3]", "高三"),
        (r"七年级|初\s*[一1]", "初一"),
        (r"八年级|初\s*[二2]", "初二"),
        (r"九年级|初\s*[三3]", "初三"),
    )
    for pattern, result in patterns:
        if re.search(pattern, value):
            return result
    return None


def infer_textbook_version(text: str) -> str | None:
    value = text.lower()
    patterns = (
        r"(人教(?:a|b)?版)", r"(苏教版)", r"(北师大?版)", r"(沪教版)", r"(浙教版)",
        r"((?:20\d{2}|19\d{2})版)", r"(新课标(?:i{1,2})?)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            result = match.group(1)
            lowered = result.lower()
            if lowered == "人教a版":
                return "人教A版"
            if lowered == "人教b版":
                return "人教B版"
            if lowered == "人教版":
                return "人教版"
            return result
    return None


def infer_chapter(text: str, metadata: dict | None = None) -> str | None:
    metadata = metadata or {}
    topic = metadata.get("topic")
    if isinstance(topic, str) and topic.strip():
        return topic.strip()[:120]

    # The caller may retry with a richer path after trying the display title.
    # Always inspect that explicit text first instead of letting filename mask it.
    candidates = [
        text,
        metadata.get("filename"),
        metadata.get("relative_path"),
        metadata.get("original_source_uri"),
    ]
    seen: set[str] = set()
    topic_tokens = (
        "函数", "方程", "运动", "战争", "概率", "统计", "几何", "三角", "数列", "导数",
        "期望", "随机变量", "向量", "不等式", "集合", "复数", "排列组合", "二面角",
        "直线与平面", "空间距离", "中点弦",
    )
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, str) or not raw_candidate.strip():
            continue
        decoded = unquote(urlparse(raw_candidate).path if "://" in raw_candidate else raw_candidate)
        if decoded in seen:
            continue
        seen.add(decoded)
        segments = [segment.strip() for segment in re.split(r"[\\/|]", decoded) if segment.strip()]
        for segment in reversed(segments or [decoded]):
            title = re.sub(r"\.(docx|pdf|txt|md|pptx)$", "", segment, flags=re.IGNORECASE).strip()
            if re.search(
                r"全册.*公式|公式.*全册|公式(?:汇总|大全|手册)|(?:选择性)?必修第?[一二三四五六0-9]*册.*公式\s*ocr",
                title,
                re.IGNORECASE,
            ):
                return "全册公式索引"
            title = re.sub(r"^第\s*\d+\s*讲\s*", "", title)
            title = re.sub(r"^拓展\s*[一二三四五六七八九十0-9]+\s*[:：]?\s*", "", title)
            section = re.match(r"(\d+(?:\.\d+){1,2})\s*([^（(]{1,60})", title)
            if section:
                return f"{section.group(1)} {section.group(2).strip()}"[:120]
            special = re.match(r"专题\s*(\d+(?:\.\d+)*)\s*[:：]?\s*([^（(]{2,80})", title)
            if special:
                return f"{special.group(1)} {special.group(2).strip()}"[:120]
            patterns = (
                r"第\s*[一二三四五六七八九十百0-9]+\s*章\s*[:：]?\s*([^()（）|｜\\/]{2,80})",
                r"chapter\s*[0-9ivx]+\s*[-:：]?\s*([A-Za-z][^\\/|]{2,80})",
            )
            for pattern in patterns:
                match = re.search(pattern, title, re.IGNORECASE)
                if match:
                    return re.sub(r"\s+", " ", match.group(1)).strip(" -_，,。.")[:120]
            if any(token in title for token in topic_tokens):
                cleaned = re.split(r"[（(](?:教师版|学生版|知识清单|解析|答案)", title, maxsplit=1)[0]
                return re.sub(r"\s+", " ", cleaned).strip(" -_，,。.：:")[:120]
    return None


def document_family_key(document: KnowledgeDocument) -> str:
    value = unicodedata.normalize("NFKC", document.title or "").lower()
    value = re.sub(r"\.(docx|pdf|txt|md|pptx)$", "", value)
    value = re.sub(r"(?:解析|详解|答案|答题卡|原卷|试卷|教师版|学生版|参考答案|solution|answer|original)", "", value)
    value = re.sub(r"[\s\[\]()（）【】_\-]+", "", value)
    return value[:180] or document.id


def resolve_document_grade(
    current_grade: str | None,
    *,
    title: str,
    haystack: str,
    metadata_source: str | None,
) -> str | None:
    """Resolve grade while allowing strong curriculum evidence to repair stale metadata."""
    title_grade = infer_grade(title)
    inferred_grade = infer_grade(haystack)
    if title_grade:
        return title_grade
    if current_grade == "高一" and inferred_grade == "高二":
        return "高二"
    if metadata_source == "rule_backfill" and inferred_grade:
        return inferred_grade
    return current_grade or inferred_grade


def backfill_document_metadata(
    db: Session,
    *,
    limit: int | None = None,
    commit: bool = True,
) -> dict[str, int]:
    statement = select(KnowledgeDocument).order_by(KnowledgeDocument.created_at)
    if limit:
        statement = statement.limit(limit)
    documents = list(db.scalars(statement))
    changed = 0
    for document in documents:
        metadata = dict(document.document_metadata or {})
        haystack = _haystack(document)
        subject = normalize_subject(document.subject) or normalize_subject(metadata.get("subject")) or infer_subject(haystack)
        grade_aliases = {"高中一": "高一", "高中二": "高二", "高中三": "高三", "初中一": "初一", "初中二": "初二", "初中三": "初三"}
        current_grade = grade_aliases.get(document.grade or "", document.grade) or grade_aliases.get(str(metadata.get("grade") or ""), metadata.get("grade"))
        grade = resolve_document_grade(
            current_grade,
            title=document.title,
            haystack=haystack,
            metadata_source=metadata.get("metadata_source"),
        )
        if not grade and document.source_type == "wikibooks_api":
            grade = "高一" if str(metadata.get("topic") or "") in {"函数", "不等式", "三角函数"} else "高二"
        textbook_aliases = {"人教a版": "人教A版", "人教b版": "人教B版"}
        textbook = textbook_aliases.get(document.textbook_version or "", document.textbook_version) or textbook_aliases.get(str(metadata.get("textbook_version") or ""), metadata.get("textbook_version")) or infer_textbook_version(haystack)
        if not textbook and document.source_type == "wikibooks_api":
            textbook = "通用课程"
        current_chapter = document.chapter or metadata.get("chapter")
        if metadata.get("metadata_source") == "rule_backfill" and current_chapter and (
            len(str(current_chapter)) >= 110 or str(current_chapter).lower().count(".docx") > 1
        ):
            current_chapter = None
        chapter = current_chapter or infer_chapter(document.title, metadata) or infer_chapter(haystack, metadata)
        role = document.document_role or metadata.get("document_role") or infer_document_role(haystack, document.source_type)
        family = metadata.get("document_family") or document_family_key(document)
        updates = {"subject": subject, "grade": grade, "textbook_version": textbook, "chapter": chapter, "document_role": role}
        for key, value in updates.items():
            if value and getattr(document, key) != value:
                setattr(document, key, value)
                changed += 1
        for key, value in updates.items():
            if value:
                metadata[key] = value
        metadata["document_family"] = family
        metadata.setdefault("metadata_source", "rule_backfill")
        document.document_metadata = metadata
    required_fields = ("subject", "grade", "textbook_version", "chapter", "document_role")
    missing_by_field = {
        f"missing_{field}": sum(not getattr(document, field) for document in documents)
        for field in required_fields
    }
    incomplete_documents = sum(
        not all(getattr(document, field) for field in required_fields)
        for document in documents
    )
    summary = {
        "documents": len(documents),
        "fields_changed": changed,
        "incomplete_documents": incomplete_documents,
        **missing_by_field,
    }
    if commit:
        db.commit()
    else:
        db.rollback()
    return summary


def _graph_node_id(subject: str, name: str) -> str:
    code = GRAPH_SUBJECT_CODES.get(subject, "subject")
    digest = hashlib.sha1(f"{subject}:{name}".encode("utf-8")).hexdigest()[:16]
    return f"auto_{code}_{digest}"


def _graph_source(db: Session) -> KnowledgeSource:
    source = db.get(KnowledgeSource, GRAPH_SOURCE_ID)
    if source is None:
        source = KnowledgeSource(
            id=GRAPH_SOURCE_ID,
            title="青葵本地知识库自动抽取",
            publisher="青葵计划",
            edition="自动候选 V1",
            location="本地导入文档",
            authorization_status="self_owned",
        )
        db.add(source)
        db.flush()
    return source


def materialize_graph_nodes(
    db: Session,
    *,
    limit: int | None = None,
    min_document_support: int = 2,
    include_taxonomy: bool = False,
) -> int:
    """Create draft nodes for recurring topics and optional curriculum seeds."""
    if min_document_support < 1:
        raise ValueError("min_document_support must be positive")
    statement = select(KnowledgeDocument).where(KnowledgeDocument.subject.in_(tuple(GRAPH_CONCEPTS))).order_by(KnowledgeDocument.created_at)
    if limit:
        statement = statement.limit(limit)
    documents = list(db.scalars(statement))
    support: dict[tuple[str, str], set[str]] = {}
    excerpts: dict[tuple[str, str], str] = {}
    for document in documents:
        subject = document.subject
        if subject not in GRAPH_CONCEPTS:
            continue
        chunks = db.scalars(
            select(KnowledgeChunk.content)
            .where(KnowledgeChunk.document_id == document.id)
            .order_by(KnowledgeChunk.sequence)
        )
        content = "\n".join(chunks)
        haystack = f"{document.title}\n{document.chapter or ''}\n{content}".lower()
        for concept in GRAPH_CONCEPTS[subject]:
            if concept.lower() in haystack:
                key = (subject, concept)
                support.setdefault(key, set()).add(document.id)
                excerpts.setdefault(key, document.title[:220])

    if include_taxonomy:
        for subject, concepts in GRAPH_CONCEPTS.items():
            for concept in concepts:
                support.setdefault((subject, concept), set())
                excerpts.setdefault((subject, concept), "课程主题词表（待导入文档）")

    existing = {(node.subject, node.name): node for node in db.scalars(select(KnowledgeNode))}
    source = _graph_source(db)
    created = 0
    for (subject, concept), document_ids in sorted(support.items()):
        has_document_evidence = len(document_ids) >= min_document_support
        if (not has_document_evidence and not include_taxonomy) or (subject, concept) in existing:
            continue
        evidence_label = (
            f"该主题在本地知识库的 {len(document_ids)} 份文档中出现，系统已生成候选节点，需管理员核验内容和层级。"
            if has_document_evidence
            else "该节点来自课程主题词表，当前学科尚未导入足够文档，需补充来源并由管理员审核。"
        )
        node = KnowledgeNode(
            id=_graph_node_id(subject, concept),
            name=concept,
            subject=subject,
            grade="中学",
            textbook_version="综合资料",
            chapter=f"{subject}自动主题",
            definition=f"文档或课程主题词表中的{subject}主题“{concept}”，正式定义待审核。",
            explanation=evidence_label,
            common_errors=[],
            question_types=["概念辨析", "综合应用"],
            source_id=source.id,
            source_excerpt=f"自动抽取：{excerpts[(subject, concept)]}",
            review_status="draft",
            is_active=True,
        )
        db.add(node)
        db.flush()
        db.add(
            KnowledgeNodeVersion(
                node_id=node.id,
                version=node.version,
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
                    "question_types": list(node.question_types or []),
                    "source_id": node.source_id,
                    "source_excerpt": node.source_excerpt,
                    "review_status": node.review_status,
                    "is_active": node.is_active,
                },
                status="draft",
                change_note="从本地文档自动生成候选节点",
            )
        )
        existing[(subject, concept)] = node
        created += 1
    db.commit()
    return created


@dataclass(frozen=True)
class GraphExtractionSummary:
    documents: int
    candidates: int
    edges_created: int
    nodes_created: int = 0


def extract_graph_relations(
    db: Session,
    *,
    limit: int | None = None,
    include_taxonomy: bool = False,
) -> GraphExtractionSummary:
    """Create conservative, deduplicated edges from co-occurrence and cue phrases."""
    nodes_created = materialize_graph_nodes(db, limit=limit, include_taxonomy=include_taxonomy)
    nodes = list(db.scalars(select(KnowledgeNode).where(KnowledgeNode.is_active.is_(True))))
    if not nodes:
        return GraphExtractionSummary(0, 0, 0, nodes_created)
    docs_stmt = select(KnowledgeDocument).order_by(KnowledgeDocument.created_at)
    if limit:
        docs_stmt = docs_stmt.limit(limit)
    docs = list(db.scalars(docs_stmt))
    existing = {(edge.source_node_id, edge.target_node_id, edge.edge_type.value if hasattr(edge.edge_type, "value") else edge.edge_type) for edge in db.scalars(select(KnowledgeEdge))}
    candidates = 0
    created = 0
    for document in docs:
        chunks = list(db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)))
        content = "\n".join(chunk.content for chunk in chunks)
        matched = [node for node in nodes if node.subject == document.subject and (node.name in content or (document.chapter and node.name in document.chapter))]
        if len(matched) < 2:
            continue
        # Preserve deterministic order and cap pair generation for long exam papers.
        matched.sort(key=lambda node: content.find(node.name) if node.name in content else len(content))
        matched = matched[:20]
        lower = content.lower()
        if any(cue in lower for cue in ("前置", "基础", "需要掌握", "先学习")):
            edge_type = EdgeType.prerequisite
        elif any(cue in lower for cue in ("易混", "区别", "辨析", "易错")):
            edge_type = EdgeType.confused_with
        elif any(cue in lower for cue in ("题型", "常见题", "考法")):
            edge_type = EdgeType.question_type
        else:
            edge_type = EdgeType.related
        if edge_type == EdgeType.related:
            pairs = zip(matched, matched[1:])
        else:
            pairs = ((source, target) for index, source in enumerate(matched) for target in matched[index + 1 :])
        for source, target in pairs:
            if source.id == target.id:
                continue
            candidates += 1
            key = (source.id, target.id, edge_type.value)
            if key in existing:
                continue
            db.add(KnowledgeEdge(source_node_id=source.id, target_node_id=target.id, edge_type=edge_type, explanation=f"由文档《{document.title[:80]}》自动抽取，待审核"))
            existing.add(key)
            created += 1
    if include_taxonomy:
        # Adjacent terms in the same curriculum list are only marked related;
        # this avoids asserting an unverified prerequisite direction.
        by_key = {(node.subject, node.name): node for node in nodes}
        for subject, concepts in GRAPH_CONCEPTS.items():
            for left_name, right_name in zip(concepts, concepts[1:]):
                left = by_key.get((subject, left_name))
                right = by_key.get((subject, right_name))
                if left is None or right is None:
                    continue
                candidates += 1
                key = (left.id, right.id, EdgeType.related.value)
                if key in existing:
                    continue
                db.add(
                    KnowledgeEdge(
                        source_node_id=left.id,
                        target_node_id=right.id,
                        edge_type=EdgeType.related,
                        explanation="课程主题词表相邻节点自动关联，待导入文档和管理员审核",
                    )
                )
                existing.add(key)
                created += 1
    db.commit()
    return GraphExtractionSummary(len(docs), candidates, created, nodes_created)
