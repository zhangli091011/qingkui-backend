"""Alibaba Cloud OSS migration and vector-index synchronization helpers.

PostgreSQL remains the source of truth. OSS stores immutable source files and
content-addressed vector-index versions. API queries always read a verified
local index; they never fetch vectors from OSS on the request path.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import KnowledgeDocument


POINTER_SCHEMA = "qingkui-vector-pointer-v1"
MIGRATION_SCHEMA = "qingkui-oss-migration-v2"


def _client():
    try:
        import oss2
    except ImportError as exc:  # pragma: no cover - incomplete deployments
        raise RuntimeError("OSS support requires the oss2 package") from exc
    if not settings.oss_bucket or not settings.oss_access_key_id or not settings.oss_access_key_secret:
        raise RuntimeError("OSS_BUCKET, OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET must be configured")
    auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
    return oss2.Bucket(auth, settings.oss_endpoint.rstrip("/"), settings.oss_bucket)


def put_private_bytes(key: str, payload: bytes, *, content_type: str, checksum_sha256: str) -> None:
    """Store a private user object and verify its checksum and size."""
    bucket = _client()
    headers = {
        "Content-Type": content_type,
        "x-oss-meta-sha256": checksum_sha256,
        "Cache-Control": "private, no-store",
    }
    bucket.put_object(key, payload, headers=headers)
    if not _object_exists(bucket, key, checksum_sha256, len(payload)):
        try:
            bucket.delete_object(key)
        finally:
            raise RuntimeError("OSS upload verification failed")


def get_private_bytes(key: str) -> bytes:
    return _client().get_object(key).read()


def delete_private_object(key: str) -> None:
    _client().delete_object(key)


def sign_private_download(key: str, *, expires_seconds: int | None = None) -> str:
    """Create a short-lived URL for trusted server-side integrations."""
    lifetime = expires_seconds or settings.oss_signed_url_seconds
    return _client().sign_url("GET", key, max(30, min(lifetime, 3600)), slash_safe=True)


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


def _content_addressed_source_key(path: Path, checksum: str) -> str:
    prefix = settings.oss_source_prefix.strip("/")
    safe_name = path.name.replace("/", "_").replace("\\", "_")
    return f"{prefix}/sha256/{checksum[:2]}/{checksum}/{safe_name}"


def _versioned_vector_key(checksum: str) -> str:
    configured = PurePosixPath(settings.oss_vector_object_key)
    return str(configured.parent / "versions" / f"{checksum}.npz")


def _object_exists(bucket: Any, key: str, checksum: str, size: int) -> bool:
    try:
        meta = bucket.head_object(key)
    except Exception:
        return False
    headers = getattr(meta, "headers", {}) or {}
    remote_checksum = headers.get("x-oss-meta-sha256")
    remote_size = int(getattr(meta, "content_length", headers.get("content-length", -1)))
    return remote_checksum == checksum and remote_size == size


def _upload_one(bucket: Any, item: dict[str, Any]) -> dict[str, Any]:
    path = Path(item["local_path"])
    checksum = item.get("sha256")
    size = item.get("size_bytes")
    if not isinstance(checksum, str) or not isinstance(size, int):
        checksum, size = _sha256(path)
    key = item["key"]
    if _object_exists(bucket, key, checksum, size):
        return {**item, "sha256": checksum, "size_bytes": size, "status": "skipped"}
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Content-Type": content_type, "x-oss-meta-sha256": checksum}
    for attempt in range(3):
        try:
            bucket.put_object_from_file(key, str(path), headers=headers)
            if not _object_exists(bucket, key, checksum, size):
                raise RuntimeError(f"OSS verification failed after upload: {key}")
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
        checksum, size = _sha256(path)
        if document.checksum_sha256 and document.checksum_sha256 != checksum:
            raise RuntimeError(f"Source checksum differs from database: {path}")
        items.append(
            {
                "document_id": document.id,
                "local_path": str(path),
                "key": _content_addressed_source_key(path, checksum),
                "source_uri": document.source_uri,
                "sha256": checksum,
                "size_bytes": size,
            }
        )
    return items


def _read_json(bucket: Any, key: str) -> dict[str, Any]:
    payload = bucket.get_object(key).read()
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"OSS JSON object is not a mapping: {key}")
    return value


def _read_json_if_exists(bucket: Any, key: str) -> dict[str, Any] | None:
    if not bucket.object_exists(key):
        return None
    return _read_json(bucket, key)


def _put_json(bucket: Any, key: str, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    bucket.put_object(key, payload, headers={"Content-Type": "application/json"})


def _descriptor(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Vector pointer has no {label} descriptor")
    key = value.get("key")
    checksum = value.get("sha256")
    size = value.get("size_bytes")
    if not isinstance(key, str) or not key or not isinstance(checksum, str) or len(checksum) != 64:
        raise RuntimeError(f"Vector pointer has an invalid {label} descriptor")
    if not isinstance(size, int) or size <= 0:
        raise RuntimeError(f"Vector pointer has an invalid {label} size")
    return {"key": key, "sha256": checksum, "size_bytes": size}


def _publish_vector_pointer(bucket: Any, current: dict[str, Any]) -> dict[str, Any]:
    old = _read_json_if_exists(bucket, settings.oss_vector_pointer_key)
    # The first published index is also the rollback baseline until a distinct
    # second version exists. This keeps every production pointer recoverable.
    previous = current
    if old is not None:
        if old.get("schema") != POINTER_SCHEMA:
            raise RuntimeError("Refusing to replace an unknown vector pointer schema")
        old_current = _descriptor(old.get("current"), label="current")
        if old_current["sha256"] != current["sha256"]:
            previous = old_current
        elif old.get("previous") is not None:
            previous = _descriptor(old.get("previous"), label="previous")
        else:
            previous = old_current
    pointer = {
        "schema": POINTER_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "current": current,
        "previous": previous,
    }
    _put_json(bucket, settings.oss_vector_pointer_key, pointer)
    return pointer


def migrate_knowledge_to_oss(*, workers: int = 4, dry_run: bool = False, update_source_uri: bool = True) -> None:
    """Upload local source documents and publish a versioned vector index."""
    bucket = None if dry_run else _client()
    index_path = Path(settings.retrieval_vector_index_path).expanduser().resolve()
    if not index_path.is_file():
        raise RuntimeError(f"Vector index does not exist: {index_path}")
    with SessionLocal() as db:
        items = _source_items(db)
    total_bytes = sum(item["size_bytes"] for item in items) + index_path.stat().st_size
    print(f"OSS migration: documents={len(items)}, total_bytes={total_bytes}, dry_run={dry_run}", flush=True)
    if dry_run:
        for item in items[:10]:
            print(f"PLAN {item['local_path']} -> {item['key']}", flush=True)
        checksum, size = _sha256(index_path)
        print(f"PLAN {index_path} -> {_versioned_vector_key(checksum)} ({size} bytes)", flush=True)
        return

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="oss-upload") as executor:
        futures = [executor.submit(_upload_one, bucket, item) for item in items]
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            print(f"SOURCE {index}/{len(futures)} {result['status']} {Path(result['local_path']).name}", flush=True)

    vector_checksum, vector_size = _sha256(index_path)
    vector_item = {
        "local_path": str(index_path),
        "key": _versioned_vector_key(vector_checksum),
        "sha256": vector_checksum,
        "size_bytes": vector_size,
    }
    vector_result = _upload_one(bucket, vector_item)
    vector_descriptor = {k: vector_result[k] for k in ("key", "sha256", "size_bytes")}
    print(f"VECTOR {vector_result['status']} {index_path.name}", flush=True)

    created_at = datetime.now(timezone.utc)
    manifest = {
        "schema": MIGRATION_SCHEMA,
        "created_at": created_at.isoformat(),
        "bucket": settings.oss_bucket,
        "endpoint": settings.oss_endpoint,
        "vector": vector_descriptor,
        "documents": [
            {k: result[k] for k in ("document_id", "key", "source_uri", "sha256", "size_bytes", "status")}
            for result in sorted(results, key=lambda value: value["document_id"])
        ],
    }
    manifest_key = f"knowledge/manifests/migration-{created_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    _put_json(bucket, manifest_key, manifest)
    _publish_vector_pointer(bucket, vector_descriptor)

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
                metadata["oss_sha256"] = result["sha256"]
                document.document_metadata = metadata
                document.source_uri = f"oss://{settings.oss_bucket}/{result['key']}"
            db.commit()
    print(f"OSS migration complete: manifest=oss://{settings.oss_bucket}/{manifest_key}", flush=True)


def _sidecar_path(index_path: Path) -> Path:
    return index_path.with_name(f"{index_path.name}.metadata.json")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _install_index_atomically(downloaded: Path, target: Path) -> None:
    if target.is_file():
        previous = target.with_name(f"{target.stem}.previous{target.suffix}")
        staged_previous = previous.with_name(f".{previous.name}.{os.getpid()}.tmp")
        staged_previous.unlink(missing_ok=True)
        try:
            os.link(target, staged_previous)
        except OSError:
            shutil.copy2(target, staged_previous)
        os.replace(staged_previous, previous)
    os.replace(downloaded, target)


def sync_vector_index_from_oss(
    *, version: Literal["current", "previous"] = "current", allow_stale: bool = False
) -> dict[str, Any]:
    """Download, verify and atomically install a vector index from OSS."""
    index_path = Path(settings.retrieval_vector_index_path).expanduser().resolve()
    try:
        bucket = _client()
        pointer = _read_json(bucket, settings.oss_vector_pointer_key)
        if pointer.get("schema") != POINTER_SCHEMA:
            raise RuntimeError("Unsupported vector pointer schema")
        expected = _descriptor(pointer.get(version), label=version)
        if index_path.is_file():
            checksum, size = _sha256(index_path)
            if checksum == expected["sha256"] and size == expected["size_bytes"]:
                _write_json_atomic(_sidecar_path(index_path), {**expected, "version": version})
                print(f"Vector index already current: sha256={checksum}", flush=True)
                return {**expected, "status": "skipped", "version": version}

        index_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{index_path.name}.", suffix=".download", dir=index_path.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            bucket.get_object_to_file(expected["key"], str(temporary))
            checksum, size = _sha256(temporary)
            if checksum != expected["sha256"] or size != expected["size_bytes"]:
                raise RuntimeError("Downloaded vector index does not match the published checksum")
            _install_index_atomically(temporary, index_path)
            _write_json_atomic(_sidecar_path(index_path), {**expected, "version": version})
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Vector index installed: version={version}, sha256={expected['sha256']}", flush=True)
        return {**expected, "status": "downloaded", "version": version}
    except Exception as exc:
        if allow_stale and index_path.is_file():
            print(f"WARNING: OSS sync failed; retaining local vector index: {type(exc).__name__}", flush=True)
            return {"status": "stale", "version": version}
        raise


def _parse_oss_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith("oss://"):
        return None
    bucket_and_key = uri.removeprefix("oss://").split("/", 1)
    if len(bucket_and_key) != 2 or not all(bucket_and_key):
        return None
    return bucket_and_key[0], bucket_and_key[1]


def verify_knowledge_oss(*, workers: int = 8) -> dict[str, int]:
    """HEAD-check all OSS-backed documents and the published vector index."""
    bucket = _client()
    with SessionLocal() as db:
        documents = list(db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.id)))

    checks: list[tuple[str, str, str]] = []
    malformed = 0
    foreign_bucket = 0
    for document in documents:
        parsed = _parse_oss_uri(document.source_uri)
        if parsed is None:
            malformed += document.source_uri.startswith("oss://")
            continue
        bucket_name, key = parsed
        if bucket_name != settings.oss_bucket:
            foreign_bucket += 1
            continue
        checks.append((document.id, key, document.checksum_sha256))

    def check(item: tuple[str, str, str]) -> tuple[str, str | None]:
        document_id, key, expected_checksum = item
        try:
            result = bucket.head_object(key)
            headers = getattr(result, "headers", {}) or {}
            remote_checksum = headers.get("x-oss-meta-sha256")
            if remote_checksum and expected_checksum and remote_checksum != expected_checksum:
                return document_id, "checksum"
            return document_id, None
        except Exception:
            return document_id, "missing"

    missing = 0
    checksum_mismatch = 0
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="oss-audit") as executor:
        for _, problem in executor.map(check, checks):
            missing += problem == "missing"
            checksum_mismatch += problem == "checksum"

    pointer = _read_json(bucket, settings.oss_vector_pointer_key)
    if pointer.get("schema") != POINTER_SCHEMA:
        raise RuntimeError("Unsupported vector pointer schema")
    current = _descriptor(pointer.get("current"), label="current")
    vector_ok = _object_exists(bucket, current["key"], current["sha256"], current["size_bytes"])
    result = {
        "documents_checked": len(checks),
        "documents_missing": missing,
        "documents_checksum_mismatch": checksum_mismatch,
        "documents_foreign_bucket": foreign_bucket,
        "documents_malformed": malformed,
        "vector_ok": int(vector_ok),
    }
    print("OSS verification: " + ", ".join(f"{key}={value}" for key, value in result.items()), flush=True)
    if missing or checksum_mismatch or foreign_bucket or malformed or not vector_ok:
        raise RuntimeError("OSS verification failed")
    return result
