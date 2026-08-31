from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from csv import DictReader
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDocument
from app.services.bailian import BailianClient, is_math_formula as is_latex_math_formula
from app.services.knowledge import build_vector_index
from app.subjects import SUBJECTS, infer_subject, normalize_subject


AUTHORIZED_STATUSES = {"authorized", "self_owned", "public_domain"}
SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".pptx"}


@dataclass(frozen=True)
class ImportResult:
    document: KnowledgeDocument
    created: bool
    chunk_count: int


@dataclass(frozen=True)
class ClassifiedPart:
    start: int
    end: int
    content: str
    content_type: str
    formula_latex: str | None = None
    formula_source: str | None = None


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    text: str
    confidence: float | None
    formulas: tuple[tuple[str, str], ...] = ()
    review_warnings: tuple[str, ...] = ()


# Supports Markdown/LaTeX delimiters emitted by PDF-to-text and authored notes.
FORMULA_PATTERN = re.compile(
    r"(?P<display>\$\$.*?\$\$|\\\[.*?\\\])|"
    r"(?P<inline>\$[^$\n]+?\$|\\\([^\n]+?\\\))",
    re.DOTALL,
)
FORMULA_HINT = re.compile(
    r"(?=.*(?:[=<>≤≥±√∑∫])|(?=.*\\(?:frac|sqrt|sum|int|lim|alpha|beta)))"
    r"[A-Za-z0-9xytzα-ωΑ-Ω²³⁰¹⁻⁺()\[\]{}^_./+\-*\s]{3,}",
)


def is_math_formula(latex: str) -> bool:
    """Reject prose wrapped as LaTeX text while retaining real mathematical expressions."""
    return is_latex_math_formula(latex)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported file type: {suffix or '(none)'}")
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig")
    if suffix == ".pdf":
        # PyMuPDF extracts pages incrementally and stays bounded on very large
        # textbook PDFs; retain pypdf as a compatibility fallback.
        try:
            import pymupdf

            with pymupdf.open(path) as pdf:
                return "\n\n".join(page.get_text("text", sort=True) or "" for page in pdf)
        except (ImportError, OSError, RuntimeError):
            return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix == ".pptx":
        return _extract_pptx_text(path)
    document = DocxDocument(path)
    blocks = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        blocks.extend("\t".join(cell.text for cell in row.cells) for row in table.rows)
    return "\n".join(blocks)


def _extract_pptx_text(path: Path) -> str:
    """Extract visible text from PowerPoint slides without requiring Office or COM."""
    slide_names: list[str]
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            name for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        )
        slides: list[str] = []
        for name in slide_names:
            root = ElementTree.fromstring(archive.read(name))
            paragraphs: list[str] = []
            for paragraph in root.iter():
                if paragraph.tag.rsplit("}", 1)[-1] != "p":
                    continue
                runs = [
                    node.text or ""
                    for node in paragraph.iter()
                    if node.tag.rsplit("}", 1)[-1] == "t"
                ]
                text = "".join(runs).strip()
                if text:
                    paragraphs.append(text)
            if paragraphs:
                slides.append("\n".join(paragraphs))
    return "\n\n".join(slides)


def _file_checksum(path: Path) -> tuple[str, int]:
    """Hash a file without retaining the entire source in memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _resolve_tesseract(binary: str | None) -> str:
    candidate = binary or shutil.which("tesseract")
    if not candidate:
        raise RuntimeError("Tesseract was not found. Install it or pass --tesseract-binary.")
    return candidate


def _resolve_tessdata_directory(binary: str, language: str) -> Path | None:
    executable = Path(binary).resolve()
    candidates = [Path(__file__).resolve().parents[2] / ".local" / "tessdata"]
    candidates.extend(
        Path(value) for value in (os.environ.get("TESSDATA_PREFIX"),) if value
    )
    candidates.extend((executable.parent / "tessdata", executable.parent.parent / "share" / "tessdata"))
    for candidate in candidates:
        if (candidate / f"{language}.traineddata").is_file():
            return candidate
    return None


def _ocr_tsv(
    image_path: Path,
    binary: str,
    language: str,
    page_segmentation_mode: int,
    tessdata_directory: Path | None,
) -> tuple[str, float | None]:
    command = [binary, str(image_path), "stdout"]
    if tessdata_directory:
        command.extend(("--tessdata-dir", str(tessdata_directory)))
    command.extend(("-l", language, "--psm", str(page_segmentation_mode)))
    # Some bundled tessdata directories contain the language model but omit
    # the tsv config. Use an installed config explicitly when available so
    # page-level confidence can be persisted for selective review.
    tsv_config = (tessdata_directory / "configs" / "tsv") if tessdata_directory else None
    if not tsv_config or not tsv_config.is_file():
        for candidate in (
            Path(binary).resolve().parent / "tessdata" / "configs" / "tsv",
            Path(binary).resolve().parent.parent / "share" / "tessdata" / "configs" / "tsv",
        ):
            if candidate.is_file():
                tsv_config = candidate
                break
    command.append(str(tsv_config) if tsv_config and tsv_config.is_file() else "tsv")
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if completed.returncode:
        raise RuntimeError(f"Tesseract failed: {completed.stderr.strip() or 'unknown error'}")

    # Some Windows Tesseract distributions do not include the tsv config file.
    # They return plain OCR text successfully, so preserve it rather than dropping a whole page.
    if not completed.stdout.startswith("level\tpage_num\tblock_num"):
        return completed.stdout, None

    lines: dict[tuple[str, str, str], list[str]] = {}
    confidences: list[float] = []
    for row in DictReader(StringIO(completed.stdout), delimiter="\t"):
        word = (row.get("text") or "").strip()
        if not word or row.get("level") != "5":
            continue
        key = (row.get("block_num") or "0", row.get("par_num") or "0", row.get("line_num") or "0")
        lines.setdefault(key, []).append(word)
        try:
            confidence = float(row.get("conf", "-1"))
        except ValueError:
            continue
        if confidence >= 0:
            confidences.append(confidence)
    text = "\n".join(" ".join(words) for words in lines.values())
    average_confidence = round(sum(confidences) / len(confidences), 2) if confidences else None
    return text, average_confidence


def classify_ocr_text(text: str, formulas: tuple[tuple[str, str], ...] = ()) -> list[ClassifiedPart]:
    """Combine plain OCR prose with formula OCR results as typed chunks."""
    parts = classify_parts(text)
    structured_keys = {
        re.sub(r"\s+", "", latex).strip("$") for _, latex in formulas if latex.strip()
    }
    embedded_keys: set[str] = set()
    for index, part in enumerate(parts):
        if part.content_type != "formula" or not part.formula_latex:
            continue
        key = re.sub(r"\s+", "", part.formula_latex).strip("$")
        if key not in structured_keys:
            continue
        embedded_keys.add(key)
        parts[index] = ClassifiedPart(
            part.start,
            part.end,
            part.content,
            "formula",
            part.formula_latex,
            "bailian_vision_ocr",
        )
    cursor = len(text)
    for raw, latex in formulas:
        key = re.sub(r"\s+", "", latex).strip("$")
        if not key or key in embedded_keys:
            continue
        parts.append(ClassifiedPart(cursor, cursor + len(raw), raw, "formula", latex, "bailian_vision_ocr"))
        embedded_keys.add(key)
        cursor += len(raw) + 1
    return parts


def ocr_scanned_pdf(
    path: Path,
    *,
    start_page: int = 1,
    end_page: int | None = None,
    dpi: int = 200,
    language: str = "chi_sim",
    tesseract_binary: str | None = None,
    engine: str = "bailian",
    bailian_client: BailianClient | None = None,
) -> list[OcrPage]:
    """Render a scanned PDF page by page and recognize it with local Tesseract."""
    if path.suffix.lower() != ".pdf":
        raise ValueError("OCR import only supports PDF files")
    if start_page < 1 or dpi < 100:
        raise ValueError("start_page must be positive and dpi must be at least 100")
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required for scanned-PDF OCR. Install project dependencies first.") from exc

    if engine not in {"bailian", "local"}:
        raise ValueError("OCR engine must be bailian or local")
    binary = _resolve_tesseract(tesseract_binary) if engine == "local" else ""
    tessdata_directory = _resolve_tessdata_directory(binary, language) if binary else None
    vision_client = bailian_client or BailianClient() if engine == "bailian" else None
    pages: list[OcrPage] = []
    with pymupdf.open(path) as pdf:
        final_page = min(end_page or pdf.page_count, pdf.page_count)
        if final_page < start_page:
            raise ValueError(f"Page range is outside this {pdf.page_count}-page PDF")
        with tempfile.TemporaryDirectory(prefix="qingkui-ocr-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            for page_number in range(start_page, final_page + 1):
                page = pdf.load_page(page_number - 1)
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                if vision_client:
                    recognized = vision_client.recognize_math_page(pixmap.tobytes("png"))
                    if recognized.text.strip() or recognized.formulas:
                        pages.append(
                            OcrPage(
                                page_number,
                                normalize_text(recognized.text),
                                recognized.confidence,
                                recognized.formulas,
                                recognized.review_warnings,
                            )
                        )
                    continue
                image_path = temporary_root / f"page-{page_number}.png"
                pixmap.save(image_path)
                text, confidence = _ocr_tsv(
                    image_path,
                    binary,
                    language,
                    page_segmentation_mode=6,
                    tessdata_directory=tessdata_directory,
                )
                if text.strip():
                    pages.append(OcrPage(page_number, normalize_text(text), confidence))
    return pages


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_formula_delimiters(value: str) -> tuple[str, str]:
    value = value.strip()
    if value.startswith("$$") and value.endswith("$$"):
        return value[2:-2].strip(), "dollar_display"
    if value.startswith("\\[") and value.endswith("\\]"):
        return value[2:-2].strip(), "latex_display"
    if value.startswith("$") and value.endswith("$"):
        return value[1:-1].strip(), "dollar_inline"
    if value.startswith("\\(") and value.endswith("\\)"):
        return value[2:-2].strip(), "latex_inline"
    return value, "heuristic"


def classify_parts(text: str, size: int = 800, overlap: int = 120) -> list[ClassifiedPart]:
    """Split explicit LaTeX formulas from prose, then chunk prose normally."""
    parts: list[ClassifiedPart] = []
    cursor = 0
    for match in FORMULA_PATTERN.finditer(text):
        if match.start() > cursor:
            for start, end, content in chunk_text(text[cursor:match.start()], size=size, overlap=overlap):
                parts.append(ClassifiedPart(cursor + start, cursor + end, content, "text"))
        raw = match.group(0)
        latex, source = _strip_formula_delimiters(raw)
        if is_math_formula(latex):
            parts.append(ClassifiedPart(match.start(), match.end(), latex, "formula", latex, source))
        else:
            parts.append(ClassifiedPart(match.start(), match.end(), raw, "text"))
        cursor = match.end()
    if cursor < len(text):
        for start, end, content in chunk_text(text[cursor:], size=size, overlap=overlap):
            parts.append(ClassifiedPart(cursor + start, cursor + end, content, "text"))
    if not parts and text:
        for start, end, content in chunk_text(text, size=size, overlap=overlap):
            parts.append(ClassifiedPart(start, end, content, "text"))
    return parts


def chunk_text(text: str, size: int = 800, overlap: int = 120) -> list[tuple[int, int, str]]:
    if size < 200 or overlap < 0 or overlap >= size:
        raise ValueError("Invalid chunk size or overlap")
    result: list[tuple[int, int, str]] = []
    start = 0
    length = len(text)
    while start < length:
        hard_end = min(start + size, length)
        end = hard_end
        if hard_end < length:
            boundary = max(
                text.rfind("\n", start + size // 2, hard_end),
                text.rfind("。", start + size // 2, hard_end),
                text.rfind("；", start + size // 2, hard_end),
            )
            if boundary > start:
                end = boundary + 1
        content = text[start:end].strip()
        if content:
            result.append((start, end, content))
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return result


def _embed_chunks(chunks: list[KnowledgeChunk], client: BailianClient) -> None:
    batch_size = 10
    batches = [(offset, chunks[offset:offset + batch_size]) for offset in range(0, len(chunks), batch_size)]
    if len(batches) == 1:
        offset, batch = batches[0]
        embeddings = client.embed([chunk.content for chunk in batch])
        for chunk, embedding in zip(batch, embeddings, strict=True):
            chunk.embedding = embedding
            chunk.embedding_model = settings.dashscope_embedding_model
        return

    # The embedding endpoint accepts ten texts per request. A small bounded pool
    # keeps a large reindex practical without overwhelming the provider.
    worker_count = min(4, len(batches))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="bailian-embed") as executor:
        futures = {
            executor.submit(client.embed, [chunk.content for chunk in batch]): (offset, batch)
            for offset, batch in batches
        }
        for future in as_completed(futures):
            _offset, batch = futures[future]
            embeddings = future.result()
            for chunk, embedding in zip(batch, embeddings, strict=True):
                chunk.embedding = embedding
                chunk.embedding_model = settings.dashscope_embedding_model


def import_document(
    db: Session,
    path: Path,
    authorization_status: str,
    title: str | None = None,
    subject: str | None = None,
    grade: str | None = None,
    textbook_version: str | None = None,
    embedding_client: BailianClient | None = None,
) -> ImportResult:
    path = path.resolve()
    if authorization_status not in AUTHORIZED_STATUSES:
        raise ValueError("Document authorization must be authorized, self_owned, or public_domain")
    resolved_subject = normalize_subject(subject) or infer_subject(f"{title or ''} {path}")
    if subject and resolved_subject is None:
        raise ValueError(f"Unsupported subject: {subject}. Choose one of {', '.join(SUBJECTS)}")
    if not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    checksum, file_size = _file_checksum(path)
    existing = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.checksum_sha256 == checksum))
    if existing is not None:
        return ImportResult(existing, False, len(existing.chunks))

    text = normalize_text(extract_text(path))
    if not text:
        raise ValueError("No extractable text was found")
    parts = classify_parts(text)
    document = KnowledgeDocument(
        title=title or path.stem,
        subject=resolved_subject,
        grade=grade,
        textbook_version=textbook_version,
        source_type="local_file",
        source_uri=f"local:///{quote(str(path), safe='')}",
        authorization_status=authorization_status,
        checksum_sha256=checksum,
        mime_type=mimetypes.guess_type(path.name)[0],
        status="processing",
        document_metadata={
            "filename": path.name,
            "relative_path": path.name,
            "size_bytes": file_size,
            "source_kind": "user_supplied_local_file",
            "subject": resolved_subject,
            "grade": grade,
            "textbook_version": textbook_version,
        },
    )
    db.add(document)
    db.flush()
    chunks = [
        KnowledgeChunk(
            document_id=document.id,
            sequence=index,
            content=part.content,
            content_type=part.content_type,
            formula_latex=part.formula_latex,
            formula_source=part.formula_source,
            formula_review_status="pending" if part.content_type == "formula" and part.formula_source == "bailian_vision_ocr" else None,
            char_start=part.start,
            char_end=part.end,
        )
        for index, part in enumerate(parts)
    ]
    db.add_all(chunks)
    if settings.retrieval_provider == "bailian":
        try:
            client = embedding_client or BailianClient()
            _embed_chunks(chunks, client)
            document.status = "indexed"
        except RuntimeError as exc:
            document.status = "text_ready"
            document.error_message = str(exc)
    else:
        document.status = "text_ready"
    db.commit()
    db.refresh(document)
    return ImportResult(document, True, len(chunks))


def import_scanned_pdf_document(
    db: Session,
    path: Path,
    authorization_status: str,
    *,
    title: str | None = None,
    subject: str | None = None,
    grade: str | None = None,
    textbook_version: str | None = None,
    start_page: int = 1,
    end_page: int | None = None,
    dpi: int = 200,
    language: str = "chi_sim",
    tesseract_binary: str | None = None,
    engine: str = "bailian",
    bailian_client: BailianClient | None = None,
) -> ImportResult:
    """Import a user-supplied scanned PDF with locally generated OCR text."""
    path = path.resolve()
    if authorization_status not in AUTHORIZED_STATUSES:
        raise ValueError("Document authorization must be authorized, self_owned, or public_domain")
    resolved_subject = normalize_subject(subject) or infer_subject(f"{title or ''} {path}")
    if subject and resolved_subject is None:
        raise ValueError(f"Unsupported subject: {subject}. Choose one of {', '.join(SUBJECTS)}")
    if not path.is_file():
        raise ValueError(f"File does not exist: {path}")
    raw = path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    existing = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.checksum_sha256 == checksum))
    if existing is not None:
        return ImportResult(existing, False, len(existing.chunks))

    pages = ocr_scanned_pdf(
        path,
        start_page=start_page,
        end_page=end_page,
        dpi=dpi,
        language=language,
        tesseract_binary=tesseract_binary,
        engine=engine,
        bailian_client=bailian_client,
    )
    if not pages:
        raise ValueError("OCR did not find any readable text")
    document = KnowledgeDocument(
        title=title or path.stem,
        subject=resolved_subject,
        grade=grade,
        textbook_version=textbook_version,
        source_type="local_file",
        source_uri=f"local:///{quote(str(path), safe='')}",
        authorization_status=authorization_status,
        checksum_sha256=checksum,
        mime_type=mimetypes.guess_type(path.name)[0],
        status="text_ready",
        document_metadata={
            "filename": path.name,
            "relative_path": path.name,
            "size_bytes": len(raw),
            "source_kind": "user_supplied_local_file",
            "subject": resolved_subject,
            "grade": grade,
            "textbook_version": textbook_version,
            "ocr": {
                "engine": engine,
                "language": language,
                "dpi": dpi,
                "pages": [page.page_number for page in pages],
                "review_warnings": [
                    {"page": page.page_number, "reasons": list(page.review_warnings)}
                    for page in pages
                    if page.review_warnings
                ],
            },
        },
    )
    db.add(document)
    db.flush()
    chunks: list[KnowledgeChunk] = []
    char_offset = 0
    for page in pages:
        page_prefix = f"第 {page.page_number} 页\n"
        for part in classify_ocr_text(page.text, page.formulas):
            chunks.append(
                KnowledgeChunk(
                    document_id=document.id,
                    sequence=len(chunks),
                    content=f"{page_prefix}{part.content}",
                    content_type=part.content_type,
                    formula_latex=part.formula_latex,
                    formula_source=part.formula_source,
                    formula_review_status="pending" if part.content_type == "formula" and part.formula_source == "bailian_vision_ocr" else None,
                    ocr_confidence=page.confidence,
                    char_start=char_offset + part.start,
                    char_end=char_offset + part.end,
                )
            )
        char_offset += len(page.text) + 1
    if not chunks:
        raise ValueError("OCR did not produce importable chunks")
    db.add_all(chunks)
    db.commit()
    db.refresh(document)
    return ImportResult(document, True, len(chunks))


def reindex_documents(db: Session, embedding_client: BailianClient | None = None) -> int:
    client = embedding_client or BailianClient()
    documents = list(
        db.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.authorization_status.in_(AUTHORIZED_STATUSES),
                KnowledgeDocument.status.in_(("text_ready", "indexed")),
            )
        )
    )
    pending: dict[str, tuple[KnowledgeDocument, list[KnowledgeChunk]]] = {}
    for document in documents:
        chunks = list(
            db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document.id)
                .order_by(KnowledgeChunk.sequence)
            )
        )
        missing_chunks = [
            chunk for chunk in chunks
            if not chunk.embedding or chunk.embedding_model != settings.dashscope_embedding_model
        ]
        if not missing_chunks:
            if document.status != "indexed":
                document.status = "indexed"
                document.error_message = None
                db.commit()
            continue
        pending[document.id] = (document, missing_chunks)

    # Schedule batches across documents so small files do not serialize the
    # entire run. Database writes remain on this thread and are committed per
    # document after all remote calls for that document have completed.
    batches = [
        (document_id, offset, batch)
        for document_id, (_document, chunks) in pending.items()
        for offset in range(0, len(chunks), 10)
        for batch in (chunks[offset:offset + 10],)
    ]
    failed_documents: dict[str, str] = {}
    def embed_with_retries(batch: list[KnowledgeChunk]) -> list[list[float]]:
        last_error: RuntimeError | None = None
        for attempt in range(3):
            try:
                return client.embed([chunk.content for chunk in batch])
            except RuntimeError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise last_error or RuntimeError("Bailian embedding request failed")

    if batches:
        with ThreadPoolExecutor(max_workers=min(max(1, settings.reindex_max_workers), len(batches)), thread_name_prefix="bailian-reindex") as executor:
            futures = {
                executor.submit(embed_with_retries, batch): (document_id, batch)
                for document_id, _offset, batch in batches
            }
            for future in as_completed(futures):
                document_id, batch = futures[future]
                try:
                    embeddings = future.result()
                except RuntimeError as exc:
                    failed_documents.setdefault(document_id, str(exc))
                    continue
                for chunk, embedding in zip(batch, embeddings, strict=True):
                    chunk.embedding = embedding
                    chunk.embedding_model = settings.dashscope_embedding_model

    updated = 0
    processed_documents = 0
    for document_id, (document, chunks) in pending.items():
        if document_id in failed_documents:
            document.status = "text_ready"
            document.error_message = failed_documents[document_id]
        else:
            document.status = "indexed"
            document.error_message = None
            updated += len(chunks)
        processed_documents += 1
        if processed_documents % 25 == 0:
            print(f"Reindex progress: documents={processed_documents}/{len(pending)}, chunks={updated}", flush=True)
        db.commit()
    if updated:
        try:
            build_vector_index(db)
        except RuntimeError:
            # The database embeddings remain valid; the optimized sidecar can be rebuilt later.
            pass
    return updated
