from __future__ import annotations

import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import MistakeAsset, MistakeProblem, OcrTask
from app.services.bailian import BailianClient
from app.services.object_storage import delete_private_object, get_private_bytes, put_private_bytes


ALLOWED_IMAGE_FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}
_LOCAL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mistake-ocr")


@dataclass(frozen=True)
class SanitizedImage:
    payload: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    checksum_sha256: str


def sanitize_image(payload: bytes) -> SanitizedImage:
    if not payload:
        raise ValueError("图片内容为空")
    if len(payload) > settings.user_upload_max_bytes:
        raise ValueError("图片大小超过限制")
    try:
        with Image.open(io.BytesIO(payload)) as source:
            source.verify()
        with Image.open(io.BytesIO(payload)) as source:
            image_format = (source.format or "").upper()
            source = ImageOps.exif_transpose(source)
            if image_format not in ALLOWED_IMAGE_FORMATS:
                raise ValueError("仅支持 JPEG、PNG 或 WebP 图片")
            width, height = source.size
            if width < 32 or height < 32:
                raise ValueError("图片尺寸过小")
            if width * height > settings.user_upload_max_pixels:
                raise ValueError("图片像素数量超过限制")
            mime_type, extension = ALLOWED_IMAGE_FORMATS[image_format]
            output = io.BytesIO()
            if image_format == "JPEG":
                clean = source.convert("RGB")
                clean.save(output, format="JPEG", quality=90, optimize=True)
            elif image_format == "PNG":
                clean = source.convert("RGBA" if "A" in source.getbands() else "RGB")
                clean.save(output, format="PNG", optimize=True)
            else:
                clean = source.convert("RGB")
                clean.save(output, format="WEBP", quality=90, method=4)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无法安全解码") from exc
    clean_payload = output.getvalue()
    return SanitizedImage(
        payload=clean_payload,
        mime_type=mime_type,
        extension=extension,
        width=width,
        height=height,
        checksum_sha256=hashlib.sha256(clean_payload).hexdigest(),
    )


def store_mistake_image(*, user_id: str, mistake_id: str, asset_id: str, payload: bytes) -> tuple[SanitizedImage, str]:
    image = sanitize_image(payload)
    prefix = settings.oss_user_content_prefix.strip("/")
    key = f"{prefix}/{user_id}/{mistake_id}/{asset_id}.{image.extension}"
    put_private_bytes(key, image.payload, content_type=image.mime_type, checksum_sha256=image.checksum_sha256)
    return image, key


def _run_local(task_id: str) -> None:
    for attempt in range(settings.ocr_max_attempts):
        try:
            process_ocr_task(task_id)
            return
        except Exception:
            if attempt + 1 >= settings.ocr_max_attempts:
                return
            time.sleep(min(2 ** attempt, 4))


def enqueue_ocr_task(task_id: str) -> None:
    if settings.ocr_queue_provider == "redis":
        from redis import Redis
        from rq import Queue, Retry

        queue = Queue(settings.ocr_queue_name, connection=Redis.from_url(settings.redis_url))
        queue.enqueue(
            "app.services.mistakes.process_ocr_task",
            task_id,
            job_id=task_id,
            retry=Retry(max=max(settings.ocr_max_attempts - 1, 0), interval=[2, 5, 15]),
            job_timeout=180,
        )
        return
    _LOCAL_EXECUTOR.submit(_run_local, task_id)


def process_ocr_task(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(OcrTask, task_id)
        if task is None or task.status in {"succeeded", "cancelled"}:
            return
        if task.cancel_requested:
            task.status = "cancelled"
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            return
        asset = db.get(MistakeAsset, task.asset_id)
        if asset is None:
            task.status = "failed"
            task.error_code = "asset_missing"
            task.error_message = "图片记录不存在"
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            return
        task.status = "recognizing"
        task.attempts += 1
        task.started_at = datetime.now(timezone.utc)
        task.error_code = None
        task.error_message = None
        db.commit()
        object_key = asset.object_key
        mime_type = asset.mime_type

    try:
        payload = get_private_bytes(object_key)
        result = BailianClient().recognize_math_page(payload, mime_type)
        combined = result.text.strip()
        if result.formulas:
            rendered = "\n".join(f"\\[{latex}\\]" for _, latex in result.formulas)
            combined = f"{combined}\n{rendered}".strip()
        if not combined:
            raise RuntimeError("OCR 未识别出有效内容")
        confidence = result.confidence
        if confidence is None:
            confidence = 0.9 if len(combined) >= 12 else 0.65
        formulas = [{"raw": raw, "latex": latex} for raw, latex in result.formulas]
    except Exception as exc:
        with SessionLocal() as db:
            task = db.get(OcrTask, task_id)
            if task is None:
                return
            if task.cancel_requested:
                task.status = "cancelled"
            elif task.attempts >= settings.ocr_max_attempts:
                task.status = "failed"
            else:
                task.status = "queued"
            task.error_code = "ocr_failed"
            task.error_message = str(exc)[:1000]
            if task.status in {"failed", "cancelled"}:
                task.completed_at = datetime.now(timezone.utc)
            db.commit()
        raise

    with SessionLocal() as db:
        task = db.get(OcrTask, task_id)
        if task is None:
            return
        if task.cancel_requested:
            task.status = "cancelled"
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            return
        mistake = db.get(MistakeProblem, task.mistake_id)
        task.status = "succeeded"
        task.result_text = combined
        task.formulas = formulas
        task.confidence = confidence
        task.requires_review = confidence < settings.ocr_review_threshold
        task.completed_at = datetime.now(timezone.utc)
        if mistake is not None and not mistake.question_text:
            mistake.question_text = combined
            mistake.review_status = "needs_review" if task.requires_review else "recognized"
        asset = db.get(MistakeAsset, task.asset_id)
        if asset is not None:
            asset.status = "ready"
        db.commit()


def delete_assets(assets: list[MistakeAsset]) -> None:
    failures: list[str] = []
    for asset in assets:
        try:
            delete_private_object(asset.object_key)
        except Exception:
            failures.append(asset.object_key)
    if failures:
        raise RuntimeError(f"无法删除 {len(failures)} 个 OSS 对象")


def user_asset_keys(user_id: str) -> list[str]:
    with SessionLocal() as db:
        return list(db.scalars(select(MistakeAsset.object_key).where(MistakeAsset.user_id == user_id)))
