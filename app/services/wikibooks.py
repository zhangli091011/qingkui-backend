from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk, KnowledgeDocument
from app.services.documents import chunk_text, normalize_text


API_URL = "https://zh.wikibooks.org/w/api.php"
SOURCE_NAME = "中文维基教科书"
LICENSE = "CC BY-SA 4.0"
USER_AGENT = "QingkuiKnowledgeImporter/0.1 (local educational project; contact: admin@example.invalid)"

TOPIC_SEARCHES = {
    "函数": "intitle:函数",
    "不等式": "intitle:不等式",
    "三角函数": "intitle:三角函数",
    "数列": "intitle:数列",
    "导数": "intitle:导数",
}


@dataclass(frozen=True)
class WikibooksPage:
    page_id: int
    revision_id: int
    title: str
    url: str
    content: str
    topic: str


@dataclass(frozen=True)
class WikibooksImportSummary:
    discovered: int
    imported: int
    updated: int
    skipped: int
    chunks: int


def _is_high_school_math(title: str) -> bool:
    return title.startswith("高中数学/") and "/目录" not in title and "版聊式" not in title


def _clean_wikitext(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    value = re.sub(r"<ref[^>/]*?>.*?</ref>|<ref[^>]*/>", "", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", value)
    value = re.sub(r"\[https?://[^\s\]]+\]", "", value)
    value = re.sub(r"^\s*[=*#;:].*?$", "", value, flags=re.MULTILINE)
    value = re.sub(r"'{2,}", "", value)
    value = value.replace("{{", "").replace("}}", "")
    return normalize_text(unescape(value))


class _RenderedPageText(HTMLParser):
    _BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "li", "tr", "br", "hr"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    _SKIP_CLASS_PARTS = ("mw-editsection", "reference", "mw-ref", "navbox", "metadata")
    _SKIP_TAGS = {"script", "style", "math"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        css_class = attributes.get("class") or ""
        if self.skip_depth:
            if tag not in self._VOID_TAGS:
                self.skip_depth += 1
            return
        if tag in self._SKIP_TAGS or any(part in css_class for part in self._SKIP_CLASS_PARTS):
            self.skip_depth += 1
            return
        if tag == "img":
            alt = attributes.get("alt") or ""
            if "mwe-math" in css_class and alt:
                alt = re.sub(r"^\{\\displaystyle\s*(.*)\}$", r"\1", alt)
                self.parts.append(f" {alt} ")
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            if tag not in self._VOID_TAGS:
                self.skip_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def _clean_rendered_html(value: str) -> str:
    parser = _RenderedPageText()
    parser.feed(value)
    parser.close()
    return normalize_text("".join(parser.parts))


def _get_json(client: httpx.Client, params: dict[str, str | int]) -> dict:
    response = client.get(API_URL, params={**params, "format": "json", "formatversion": "2"})
    response.raise_for_status()
    return response.json()


def discover_pages(client: httpx.Client, pages_per_topic: int) -> list[tuple[str, str]]:
    discovered: dict[str, str] = {}
    for topic, query in TOPIC_SEARCHES.items():
        data = _get_json(
            client,
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": 0,
                "srlimit": max(1, min(pages_per_topic * 3, 50)),
                "srprop": "size|wordcount",
            },
        )
        kept = 0
        for item in data.get("query", {}).get("search", []):
            title = item["title"]
            if not _is_high_school_math(title):
                continue
            discovered.setdefault(title, topic)
            kept += 1
            if kept >= pages_per_topic:
                break
        time.sleep(0.2)
    return list(discovered.items())


def fetch_page(client: httpx.Client, title: str, topic: str) -> WikibooksPage | None:
    data = _get_json(
        client,
        {
            "action": "parse",
            "page": title,
            "prop": "text|revid|displaytitle",
        },
    )
    page = data.get("parse")
    if not page:
        return None
    content = _clean_rendered_html(page.get("text", ""))
    if len(content) < 240:
        return None
    return WikibooksPage(
        page_id=int(page["pageid"]),
        revision_id=int(page["revid"]),
        title=page["title"],
        url=f"https://zh.wikibooks.org/wiki/{quote(page['title'])}",
        content=content,
        topic=topic,
    )


def _checksum(page: WikibooksPage) -> str:
    return hashlib.sha256(f"wikibooks:{page.page_id}:{page.revision_id}:{page.content}".encode("utf-8")).hexdigest()


def _source_uri(page: WikibooksPage) -> str:
    return page.url or f"https://zh.wikibooks.org/wiki/{quote(page.title)}"


def _upsert_page(db: Session, page: WikibooksPage) -> tuple[str, int]:
    source_uri = _source_uri(page)
    checksum = _checksum(page)
    document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.source_uri == source_uri))
    if document is not None and document.checksum_sha256 == checksum:
        return "skipped", 0
    parts = chunk_text(page.content)
    metadata = {
        "source_name": SOURCE_NAME,
        "license": LICENSE,
        "attribution": f"{SOURCE_NAME} contributors, {LICENSE}",
        "page_id": page.page_id,
        "revision_id": page.revision_id,
        "topic": page.topic,
        "subject": "数学",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    if document is None:
        document = KnowledgeDocument(
            title=page.title,
            subject="数学",
            source_type="wikibooks_api",
            source_uri=source_uri,
            authorization_status="authorized",
            checksum_sha256=checksum,
            mime_type="text/x-wiki",
            status="text_ready",
            document_metadata=metadata,
        )
        db.add(document)
        action = "imported"
    else:
        document.title = page.title
        document.authorization_status = "authorized"
        document.checksum_sha256 = checksum
        document.status = "text_ready"
        document.error_message = None
        document.document_metadata = metadata
        document.chunks.clear()
        db.flush()
        action = "updated"
    db.flush()
    db.add_all(
        KnowledgeChunk(
            document_id=document.id,
            sequence=index,
            content=content,
            char_start=start,
            char_end=end,
        )
        for index, (start, end, content) in enumerate(parts)
    )
    return action, len(parts)


def import_wikibooks(db: Session, pages_per_topic: int = 6) -> WikibooksImportSummary:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=True) as client:
        candidates = discover_pages(client, pages_per_topic)
        imported = updated = skipped = chunks = 0
        for title, topic in candidates:
            try:
                page = fetch_page(client, title, topic)
                if page is None:
                    skipped += 1
                    continue
                action, chunk_count = _upsert_page(db, page)
                if action == "imported":
                    imported += 1
                elif action == "updated":
                    updated += 1
                else:
                    skipped += 1
                chunks += chunk_count
                db.commit()
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                db.rollback()
                print(f"FAILED {title}: {exc}")
            time.sleep(0.25)
    return WikibooksImportSummary(len(candidates), imported, updated, skipped, chunks)
