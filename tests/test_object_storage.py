import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import object_storage


class FakeObject:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None):
        self.payload = payload
        self.headers = headers or {}
        self.content_length = len(payload)

    def read(self) -> bytes:
        return self.payload


class FakeBucket:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.metadata: dict[str, dict[str, str]] = {}
        self.downloads = 0

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def get_object(self, key: str) -> FakeObject:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return FakeObject(self.objects[key], self.metadata.get(key))

    def get_object_to_file(self, key: str, path: str) -> None:
        self.downloads += 1
        Path(path).write_bytes(self.objects[key])

    def head_object(self, key: str):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return SimpleNamespace(
            headers=self.metadata.get(key, {}),
            content_length=len(self.objects[key]),
        )

    def put_object(self, key: str, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self.objects[key] = payload
        self.metadata[key] = headers or {}


def descriptor(key: str, payload: bytes) -> dict[str, object]:
    return {
        "key": key,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def pointer(current: dict[str, object], previous: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        {
            "schema": object_storage.POINTER_SCHEMA,
            "updated_at": "2026-08-30T00:00:00+00:00",
            "current": current,
            "previous": previous,
        }
    ).encode()


def test_sync_vector_index_is_verified_and_atomic(tmp_path, monkeypatch):
    old_payload = b"old-index"
    new_payload = b"new-index"
    index_path = tmp_path / "qingkui-vectors.npz"
    index_path.write_bytes(old_payload)
    expected = descriptor("knowledge/index/versions/new.npz", new_payload)
    bucket = FakeBucket(
        {
            object_storage.settings.oss_vector_pointer_key: pointer(expected),
            str(expected["key"]): new_payload,
        }
    )
    monkeypatch.setattr(object_storage.settings, "retrieval_vector_index_path", str(index_path))
    monkeypatch.setattr(object_storage, "_client", lambda: bucket)

    result = object_storage.sync_vector_index_from_oss()

    assert result["status"] == "downloaded"
    assert index_path.read_bytes() == new_payload
    assert (tmp_path / "qingkui-vectors.previous.npz").read_bytes() == old_payload
    sidecar = json.loads((tmp_path / "qingkui-vectors.npz.metadata.json").read_text(encoding="utf-8"))
    assert sidecar["sha256"] == expected["sha256"]

    second = object_storage.sync_vector_index_from_oss()
    assert second["status"] == "skipped"
    assert bucket.downloads == 1


def test_sync_rejects_corrupt_download_without_replacing_local_index(tmp_path, monkeypatch):
    old_payload = b"known-good"
    advertised_payload = b"expected"
    index_path = tmp_path / "qingkui-vectors.npz"
    index_path.write_bytes(old_payload)
    expected = descriptor("knowledge/index/versions/current.npz", advertised_payload)
    bucket = FakeBucket(
        {
            object_storage.settings.oss_vector_pointer_key: pointer(expected),
            str(expected["key"]): b"corrupt",
        }
    )
    monkeypatch.setattr(object_storage.settings, "retrieval_vector_index_path", str(index_path))
    monkeypatch.setattr(object_storage, "_client", lambda: bucket)

    with pytest.raises(RuntimeError, match="checksum"):
        object_storage.sync_vector_index_from_oss()

    assert index_path.read_bytes() == old_payload


def test_sync_can_retain_local_index_during_oss_outage(tmp_path, monkeypatch):
    index_path = tmp_path / "qingkui-vectors.npz"
    index_path.write_bytes(b"cached")
    monkeypatch.setattr(object_storage.settings, "retrieval_vector_index_path", str(index_path))
    monkeypatch.setattr(object_storage, "_client", lambda: (_ for _ in ()).throw(ConnectionError("offline")))

    result = object_storage.sync_vector_index_from_oss(allow_stale=True)

    assert result["status"] == "stale"
    assert index_path.read_bytes() == b"cached"


def test_vector_pointer_retains_one_previous_version(monkeypatch):
    first = descriptor("knowledge/index/versions/first.npz", b"first")
    second = descriptor("knowledge/index/versions/second.npz", b"second")
    bucket = FakeBucket()
    monkeypatch.setattr(object_storage.settings, "oss_vector_pointer_key", "knowledge/index/current.json")

    initial = object_storage._publish_vector_pointer(bucket, first)
    published = object_storage._publish_vector_pointer(bucket, second)

    assert initial["current"] == first
    assert initial["previous"] == first
    assert published["current"] == second
    assert published["previous"] == first
