from pathlib import Path
from zipfile import ZipFile

import pytest

from app.db import SessionLocal
from app.models import KnowledgeDocument
from app.services.documents import chunk_text, classify_parts, extract_text, import_document
from app.services.knowledge import retrieve_chunks
from app.subjects import SUBJECTS, infer_subject


class FakeEmbeddingClient:
    def embed(self, texts):
        return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]


def test_subject_router_recognizes_supported_disciplines_without_ambiguous_fallback():
    assert set(SUBJECTS) == {"语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"}
    assert infer_subject("请解释导数的几何意义") == "数学"
    assert infer_subject("光合作用和细胞呼吸有什么关系") == "生物"
    assert infer_subject("什么是牛顿第二定律") == "物理"
    assert infer_subject("请帮我学习") is None


def test_chunk_text_preserves_overlap():
    text = "第一段。" * 250
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 1
    assert all(content for _, _, content in chunks)
    assert chunks[1][0] < chunks[0][1]


def test_pptx_text_extraction_preserves_slide_paragraphs(tmp_path: Path):
    source = tmp_path / "history.pptx"
    slide = """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><a:p><a:r><a:t>第一课</a:t></a:r></a:p><a:p><a:r><a:t>中华文明</a:t></a:r></a:p></p:cSld></p:sld>"""
    with ZipFile(source, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)
    assert extract_text(source) == "第一课\n中华文明"


def test_formula_classifier_keeps_text_and_latex_separate():
    parts = classify_parts(
        "二次函数根的公式是 $x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$。\n\n"
        "$$\\Delta=b^2-4ac$$\n判别式决定根的个数。"
    )
    assert [part.content_type for part in parts] == ["text", "formula", "text", "formula", "text"]
    formulas = [part for part in parts if part.content_type == "formula"]
    assert formulas[0].formula_latex == r"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}"
    assert formulas[0].formula_source == "dollar_inline"
    assert formulas[1].formula_latex == r"\Delta=b^2-4ac"
    assert formulas[1].formula_source == "dollar_display"


def test_authorized_text_import_is_idempotent_and_searchable(tmp_path: Path, client):
    source = tmp_path / "quadratic-notes.md"
    source.write_text(
        "二次函数的图像是抛物线。顶点坐标可以通过配方法求出。判别式用于判断与横轴的交点个数。",
        encoding="utf-8",
    )
    with SessionLocal() as db:
        first = import_document(db, source, "self_owned", title="二次函数自有笔记")
        second = import_document(db, source, "self_owned", title="不会重复导入")
        assert first.created is True
        assert second.created is False
        assert first.document.id == second.document.id
        assert first.document.status == "text_ready"

        results = retrieve_chunks(db, "二次函数的顶点怎么求")
        assert results
        assert results[0].chunk.document.title == "二次函数自有笔记"
        assert "配方法" in results[0].chunk.content
        from app.routers.qa import _citations

        citation = _citations([], results)[0]
        assert citation["document_id"] == first.document.id
        assert citation["chunk_id"] == results[0].chunk.id
        assert citation["source_location"].startswith("local:///")

        db.delete(db.get(KnowledgeDocument, first.document.id))
        db.commit()


def test_import_rejects_unverified_authorization(tmp_path: Path):
    source = tmp_path / "notes.txt"
    source.write_text("测试资料", encoding="utf-8")
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="authorization"):
            import_document(db, source, "pending_review")


def test_bailian_import_stores_embeddings(tmp_path: Path, client, monkeypatch):
    from app.config import settings

    source = tmp_path / "owned.txt"
    source.write_text("这是用户自有的函数学习资料。" * 80, encoding="utf-8")
    monkeypatch.setattr(settings, "retrieval_provider", "bailian")
    with SessionLocal() as db:
        result = import_document(db, source, "self_owned", embedding_client=FakeEmbeddingClient())
        assert result.document.status == "indexed"
        assert result.document.chunks[0].embedding
        assert result.document.chunks[0].embedding_model == settings.dashscope_embedding_model
        db.delete(result.document)
        db.commit()


def test_import_stores_formula_metadata(tmp_path: Path, client):
    source = tmp_path / "formula.md"
    source.write_text("公式 $a^2+b^2=c^2$ 用于直角三角形。", encoding="utf-8")
    with SessionLocal() as db:
        result = import_document(db, source, "self_owned")
        formula = next(chunk for chunk in result.document.chunks if chunk.content_type == "formula")
        assert formula.formula_latex == "a^2+b^2=c^2"
        assert formula.formula_source == "dollar_inline"
        assert formula.ocr_confidence is None
        db.delete(result.document)
        db.commit()


def test_document_subject_is_persisted_and_filters_retrieval(tmp_path: Path, client):
    source = tmp_path / "biology-notes.txt"
    source.write_text("光合作用发生在叶绿体，细胞利用光能合成有机物。", encoding="utf-8")
    with SessionLocal() as db:
        result = import_document(db, source, "self_owned", subject="生物")
        assert result.document.subject == "生物"
        assert retrieve_chunks(db, "光合作用的场所", subject="生物")
        assert retrieve_chunks(db, "光合作用的场所", subject="数学") == []
        db.delete(result.document)
        db.commit()
