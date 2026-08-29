"""Alibaba Cloud OSS migration helpers.

The database remains the source of truth. OSS stores immutable source files,
the vector sidecar, and a manifest so the migration is resumable and auditable.
Credentials are read from Settings and are never written to manifests.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import KnowledgeDocument


def _client():
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - exercised in incomplete deployments
        raise RuntimeError("OSS support requires the oss2 package") from exc
    if not settings.oss_bucket or not settings.oss_access_key_id or not settings.oss_access_key_secret:
        raise RuntimeError("OSS_BUCKET, OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET must be configured")
    auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
    endpoint = settings.oss_endpoint.rstrip("/")
    return oss2.Bucket(auth, endpoint, settings.oss_bucket)


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _local_path(source_uri: str) -> Path | None:
    if not source_uri.startswith("local:///"):
        return None
    return Path(urllib.parse.unquote(source_uri.removeprefix("local:///"))).resolve()


def _object_exists(bucket: Any, key: str, checksum: str, size: int) -> bool:
    try:
        meta = bucket.head_object(key)
    except Exception:
        return False
    headers = getattr(meta, "headers", {}) or {}
    remote_checksum = headers.get("x-oss-meta-sha256")
    remote_size = int(getattr(meta, "content_length", headers.get("content-length", -1)))
    return (remote_checksum == checksum and remote_size == size) or (remote_checksum is None and remote_size == size)


def _upload_one(bucket: Any, item: dict[str, Any]) -> dict[str, Any]:
    path = Path(item["local_path"])
    checksum, size = _sha256(path)
    key = item["key"]
    if _object_exists(bucket, key, checksum, size):
        return {**item, "sha256": checksum, "size_bytes": size, "status": "skipped"}
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Content-Type": content_type, "x-oss-meta-sha256": checksum}
    for attempt in range(3):
        try:
            bucket.put_object_from_file(key, str(path), headers=headers)
            return {**item, "sha256": checksum, "size_bytes": size, "status": "uploaded"}
        except Exception:
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


def _source_items(db) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for document in db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.id)):
        path = _local_path(document.source_uri)
        if path is None:
            continue
        if not path.is_file():
            raise RuntimeError(f"Referenced source file does not exist: {path}")
        safe_name = path.name.replace("/", "_").replace("\\", "_")
        items.append({
            "document_id": document.id,
            "local_path": str(path),
            "key": f"{settings.oss_source_prefix.strip('/')}/{document.id}/{safe_name}",
            "source_uri": document.source_uri,
        })
    return items


def migrate_knowledge_to_oss(*, workers: int = 4, dry_run: bool = False, update_source_uri: bool = True) -> None:
    """Upload referenced local documents and the vector index, resumably."""
    bucket = None if dry_run else _client()
    index_path = Path(settings.retrieval_vector_index_path).expanduser().resolve()
    if not index_path.is_file():
        raise RuntimeError(f"Vector index does not exist: {index_path}")
    with SessionLocal() as db:
        items = _source_items(db)
    total_bytes = sum(Path(item["local_path"]).stat().st_size for item in items) + index_path.stat().st_size
    print(f"OSS migration: documents={len(items)}, total_bytes={total_bytes}, dry_run={dry_run}", flush=True)
    if dry_run:
        for item in items[:10]: print(f"PLAN {item['local_path']} -> {item['key']}", flush=True)
        return

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="oss-upload") as executor:
        futures = [executor.submit(_upload_one, bucket, item) for item in items]
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            print(f"SOURCE {index}/{len(futures)} {result['status']} {Path(result['local_path']).name}", flush=True)

    vector_checksum, vector_size = _sha256(index_path)
    vector_item = {"local_path": str(index_path), "key": settings.oss_vector_object_key}
    vector_result = _upload_one(bucket, vector_item)
    print(f"VECTOR {vector_result['status']} {index_path.name}", flush=True)

    manifest = {
        "schema": "qingkui-oss-migration-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bucket": settings.oss_bucket,
        "endpoint": settings.oss_endpoint,
        "vector": {"key": settings.oss_vector_object_key, "sha256": vector_checksum, "size_bytes": vector_size},
        "documents": [
            {k: result[k] for k in ("document_id", "key", "source_uri", "sha256", "size_bytes", "status")}
            for result in sorted(results, key=lambda value: value["document_id"])
        ],
    }
    manifest_key = f"knowledge/manifests/migration-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    bucket.put_object(manifest_key, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), headers={"Content-Type": "application/json"})
    if update_source_uri:
        with SessionLocal() as db:
            for result in results:
                document = db.get(KnowledgeDocument, result["document_id"])
                if document is None:
                    continue
                metadata = dict(document.document_metadata or {})
                metadata.setdefault("original_source_uri", result["source_uri"])
                metadata["oss_object_key"] = result["key"]
                metadata["oss_bucket"] = settings.oss_bucket
                document.document_metadata = metadata
                document.source_uri = f"oss://{settings.oss_bucket}/{result['key']}"
            db.commit()
    print(f"OSS migration complete: manifest=oss://{settings.oss_bucket}/{manifest_key}", flush=True)
