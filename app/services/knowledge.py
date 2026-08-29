import re
from dataclasses import dataclass
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, defer, joinedload

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument, KnowledgeNode, UserKnowledgeState
from app.services.bailian import BailianClient
from app.subjects import normalize_subject, resolve_subject

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only in incomplete deployments
    np = None


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: KnowledgeChunk
    score: float


@dataclass(frozen=True)
class VectorIndex:
    chunk_ids: tuple[str, ...]
    subjects: tuple[str, ...]
    matrix: object
    model: str


_vector_index_cache: tuple[Path, int, VectorIndex] | None = None
_vector_index_lock = RLock()
_query_embedding_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_query_embedding_lock = RLock()


_CASUAL_MESSAGES = {"你好", "您好", "嗨", "哈喽", "hello", "hi", "hey", "谢谢", "谢谢你", "再见"}


def is_casual_message(value: str) -> bool:
    normalized = re.sub(r"[\s，。！？!?、~～]+", "", value.strip().lower())
    return normalized in _CASUAL_MESSAGES or bool(re.fullmatch(r"(?:你好|您好|嗨|哈喽)(?:呀|啊)*", normalized))


def search_nodes(db: Session, query: str, limit: int = 20, subject: str | None = None) -> list[KnowledgeNode]:
    term = f"%{query.strip()}%"
    resolved_subject = normalize_subject(subject) or resolve_subject(query)
    if resolved_subject is None:
        return []
    filters = [KnowledgeNode.is_active.is_(True), KnowledgeNode.review_status == "approved"]
    if resolved_subject:
        filters.append(KnowledgeNode.subject == resolved_subject)
    return list(
        db.scalars(
            select(KnowledgeNode)
            .options(joinedload(KnowledgeNode.source))
            .where(
                *filters,
                or_(
                    KnowledgeNode.name.ilike(term),
                    KnowledgeNode.chapter.ilike(term),
                    KnowledgeNode.definition.ilike(term),
                ),
            )
            .limit(limit)
        )
    )


def retrieve_nodes(db: Session, question: str, pinned_node_id: str | None, limit: int = 5, subject: str | None = None) -> list[KnowledgeNode]:
    if is_casual_message(question):
        return []
    resolved_subject = normalize_subject(subject) or resolve_subject(question)
    pinned = db.get(KnowledgeNode, pinned_node_id) if pinned_node_id else None
    resolved_subject = resolved_subject or (pinned.subject if pinned else None)
    if resolved_subject is None:
        return []
    nodes = list(
        db.scalars(
            select(KnowledgeNode)
            .options(joinedload(KnowledgeNode.source))
            .where(KnowledgeNode.is_active.is_(True), KnowledgeNode.review_status == "approved", KnowledgeNode.subject == resolved_subject)
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
        if pinned and pinned.subject == resolved_subject:
            relevant.insert(0, pinned)
    return relevant[:limit]


def _tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z0-9]+", value.lower()))
    for span in re.findall(r"[\u4e00-\u9fff]+", value):
        tokens.add(span)
        for width in range(2, min(4, len(span)) + 1):
            tokens.update(span[index:index + width] for index in range(len(span) - width + 1))
    return tokens


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = sqrt(sum(value * value for value in left)) * sqrt(sum(value * value for value in right))
    return dot / denominator if denominator else 0.0


def _vector_index_path() -> Path:
    return Path(settings.retrieval_vector_index_path).expanduser().resolve()


def build_vector_index(db: Session) -> int:
    """Export indexed JSON embeddings into a compact normalized matrix for fast retrieval."""
    if np is None:
        raise RuntimeError("NumPy is required to build the vector index")
    rows = db.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.embedding, KnowledgeChunk.embedding_model, KnowledgeDocument.subject)
        .join(KnowledgeChunk.document)
        .where(
            KnowledgeDocument.authorization_status.in_(("authorized", "self_owned", "public_domain")),
            KnowledgeDocument.status == "indexed",
            KnowledgeChunk.embedding.is_not(None),
            KnowledgeChunk.embedding_model == settings.dashscope_embedding_model,
        )
        .order_by(KnowledgeChunk.id)
    ).all()
    if not rows:
        raise RuntimeError("No indexed embeddings found")
    valid_rows = [(chunk_id, embedding, subject or "") for chunk_id, embedding, _, subject in rows if embedding]
    if not valid_rows:
        raise RuntimeError("No valid indexed embeddings found")
    dimension = len(valid_rows[0][1])
    if any(len(embedding) != dimension for _, embedding, _ in valid_rows):
        raise RuntimeError("Indexed embeddings have inconsistent dimensions")
    matrix = np.asarray([embedding for _, embedding, _ in valid_rows], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.maximum(norms, 1e-12)
    ids = np.asarray([chunk_id for chunk_id, _, _ in valid_rows], dtype="U36")
    subjects = np.asarray([subject for _, _, subject in valid_rows], dtype="U40")
    path = _vector_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    np.savez_compressed(temporary_path, chunk_ids=ids, subjects=subjects, matrix=matrix, model=settings.dashscope_embedding_model)
    generated_path = Path(f"{temporary_path}.npz") if not temporary_path.exists() else temporary_path
    generated_path.replace(path)
    clear_vector_index_cache()
    return len(valid_rows)


def clear_vector_index_cache() -> None:
    global _vector_index_cache
    with _vector_index_lock:
        _vector_index_cache = None


def _load_vector_index() -> VectorIndex | None:
    if np is None:
        return None
    path = _vector_index_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    global _vector_index_cache
    with _vector_index_lock:
        if _vector_index_cache and _vector_index_cache[:2] == (path, mtime_ns):
            return _vector_index_cache[2]
        try:
            with np.load(path, allow_pickle=False) as archive:
                ids = tuple(str(value) for value in archive["chunk_ids"].tolist())
                if "subjects" not in archive:
                    # Old sidecars cannot be safely partitioned by subject.
                    return None
                subjects = tuple(str(value) for value in archive["subjects"].tolist())
                matrix = np.asarray(archive["matrix"], dtype=np.float32)
                model = str(archive["model"].item())
        except (OSError, KeyError, ValueError):
            return None
        if len(subjects) != len(ids):
            return None
        index = VectorIndex(ids, subjects, matrix, model)
        _vector_index_cache = (path, mtime_ns, index)
        return index


def _cached_query_embedding(client: BailianClient, question: str) -> list[float]:
    key = (settings.dashscope_embedding_model, question.strip())
    with _query_embedding_lock:
        cached = _query_embedding_cache.get(key)
        if cached is not None:
            _query_embedding_cache.move_to_end(key)
            return cached
    embedding = client.embed([question])[0]
    with _query_embedding_lock:
        _query_embedding_cache[key] = embedding
        _query_embedding_cache.move_to_end(key)
        while len(_query_embedding_cache) > max(1, settings.retrieval_query_cache_size):
            _query_embedding_cache.popitem(last=False)
    return embedding


def _fetch_chunks_by_ids(db: Session, chunk_ids: list[str]) -> dict[str, KnowledgeChunk]:
    if not chunk_ids:
        return {}
    chunks = db.scalars(
        select(KnowledgeChunk)
        .options(joinedload(KnowledgeChunk.document), defer(KnowledgeChunk.embedding))
        .where(KnowledgeChunk.id.in_(chunk_ids))
    )
    return {chunk.id: chunk for chunk in chunks}


def _lexical_retrieve(db: Session, question: str, result_limit: int, subject: str | None = None) -> list[RetrievedChunk]:
    tokens = sorted(_tokens(question), key=len, reverse=True)[:20]
    if not tokens:
        return []
    filters = [KnowledgeChunk.content.ilike(f"%{token}%") for token in tokens if len(token) >= 2]
    if not filters:
        return []
    query_filters = [
        KnowledgeDocument.authorization_status.in_(("authorized", "self_owned", "public_domain")),
        KnowledgeDocument.status.in_(("text_ready", "indexed")),
        or_(*filters),
    ]
    if subject:
        query_filters.append(KnowledgeDocument.subject == subject)
    chunks = list(
        db.scalars(
            select(KnowledgeChunk)
            .options(joinedload(KnowledgeChunk.document), defer(KnowledgeChunk.embedding))
            .join(KnowledgeChunk.document)
            .where(*query_filters)
            .limit(settings.retrieval_candidate_limit * 10)
        )
    )
    question_tokens = _tokens(question)
    ranked = sorted(
        chunks,
        key=lambda chunk: sum(1 for token in question_tokens if token in chunk.content.lower()),
        reverse=True,
    )
    return _dedupe_retrieved([
        RetrievedChunk(chunk, float(sum(1 for token in question_tokens if token in chunk.content.lower())))
        for chunk in ranked[:settings.retrieval_candidate_limit]
    ], question, result_limit)


def _dedupe_retrieved(candidates: list[RetrievedChunk], question: str, limit: int) -> list[RetrievedChunk]:
    """Keep one representative chunk per document family when copies overlap."""
    preferred = ("solution", "answer", "answer_sheet") if any(token in question for token in ("答案", "解析", "解法", "怎么做", "过程")) else ("textbook", "notes", "teacher_guide", "original")
    seen: set[str] = set()
    deferred: list[RetrievedChunk] = []
    result: list[RetrievedChunk] = []
    for item in candidates:
        document = item.chunk.document
        metadata = document.document_metadata or {}
        family = str(metadata.get("document_family") or document.id)
        role = document.document_role or metadata.get("document_role") or "other"
        if family in seen:
            deferred.append(item)
        elif role in preferred or len(result) >= max(1, limit // 2):
            result.append(item)
            seen.add(family)
        else:
            deferred.append(item)
        if len(result) >= limit:
            return result[:limit]
    for item in deferred:
        if len(result) >= limit:
            break
        document = item.chunk.document
        family = str((document.document_metadata or {}).get("document_family") or document.id)
        if family not in seen:
            result.append(item)
            seen.add(family)
    return result[:limit]


def retrieve_chunks(db: Session, question: str, limit: int | None = None, subject: str | None = None) -> list[RetrievedChunk]:
    if is_casual_message(question):
        return []
    result_limit = limit or settings.retrieval_result_limit
    resolved_subject = normalize_subject(subject) or resolve_subject(question)
    if resolved_subject is None:
        return []
    if settings.retrieval_provider != "bailian" or not settings.dashscope_api_key:
        return _lexical_retrieve(db, question, result_limit, resolved_subject)
    try:
        client = BailianClient(timeout_seconds=settings.dashscope_query_timeout_seconds)
        index = _load_vector_index()
        if index is None or index.model != settings.dashscope_embedding_model:
            return _lexical_retrieve(db, question, result_limit, resolved_subject)
        if resolved_subject not in index.subjects:
            return _lexical_retrieve(db, question, result_limit, resolved_subject)
        query_embedding = np.asarray(_cached_query_embedding(client, question), dtype=np.float32)
        if query_embedding.shape[0] != index.matrix.shape[1]:
            return _lexical_retrieve(db, question, result_limit, resolved_subject)
        query_embedding /= max(float(np.linalg.norm(query_embedding)), 1e-12)
        scores = index.matrix @ query_embedding
        if index.subjects:
            subject_mask = np.asarray([value == resolved_subject for value in index.subjects], dtype=bool)
            scores = np.where(subject_mask, scores, -1.0)
        candidate_count = min(settings.retrieval_candidate_limit, len(scores))
        if candidate_count <= 0:
            return []
        candidate_indices = np.argpartition(-scores, candidate_count - 1)[:candidate_count]
        candidate_indices = candidate_indices[np.argsort(-scores[candidate_indices])]
        ranked_pairs = [
            (int(index_value), index.chunk_ids[int(index_value)], float(scores[int(index_value)]))
            for index_value in candidate_indices
            if scores[int(index_value)] > 0
        ]
        ranked_ids = [chunk_id for _, chunk_id, _ in ranked_pairs]
        chunks_by_id = _fetch_chunks_by_ids(db, ranked_ids)
        candidates = [
            RetrievedChunk(chunks_by_id[chunk_id], score)
            for _, chunk_id, score in ranked_pairs
            if chunk_id in chunks_by_id
        ]
        candidates = candidates[:settings.retrieval_candidate_limit]
        if not candidates:
            # A newly imported subject may still be text_ready while older
            # subjects already have vectors. Keep that subject searchable.
            return _lexical_retrieve(db, question, result_limit, resolved_subject)
        if settings.dashscope_rerank_enabled:
            try:
                reranked = client.rerank(question, [item.chunk.content for item in candidates], result_limit)
                results = [RetrievedChunk(candidates[index], score) for index, score in reranked if 0 <= index < len(candidates)]
                if results:
                    return _dedupe_retrieved(results, question, result_limit)
            except RuntimeError:
                pass
        return _dedupe_retrieved(candidates, question, result_limit)
    except (RuntimeError, ValueError, TypeError):
        return _lexical_retrieve(db, question, result_limit, resolved_subject)


def status_for(db: Session, user_id: str, node_id: str):
    return db.scalar(
        select(UserKnowledgeState).where(
            UserKnowledgeState.user_id == user_id,
            UserKnowledgeState.node_id == node_id,
        )
    )
