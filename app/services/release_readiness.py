"""Machine-readable release gates backed by explicit, auditable evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.services.content_governance import governance_report


REPORT_SCHEMA = "qingkui-release-readiness-v1"
EVIDENCE_SCHEMAS = {
    "human_evaluation_dataset": ("human-eval-dataset.json", "qingkui-human-eval-dataset-v1"),
    "human_evaluation_run": ("human-eval-reviewed-run.json", "qingkui-human-eval-run-v1"),
    "human_evaluation": ("human-eval-score.json", "qingkui-human-eval-score-v1"),
    "c9_compatibility": ("c9-evidence.json", "qingkui-c9-evidence-v1"),
    "recovery_drill": ("recovery-drill.json", "qingkui-recovery-drill-v1"),
    "android_release": ("android-release-manifest.json", "qingkui-android-release-v1"),
}
REQUIRED_C9_CHECKS = {
    "mdm_install_upgrade_uninstall",
    "campus_https_api",
    "sse_ten_runs",
    "camera_permission_and_retry",
    "gallery_and_file_permissions",
    "rotation_state_restore",
    "stylus_interaction",
    "split_screen_layout",
    "system_back_navigation",
    "mdm_policy_approved",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for parser in (
        lambda: datetime.fromisoformat(raw.replace("Z", "+00:00")),
        lambda: datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
    ):
        try:
            parsed = parser()
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value.lower()))


def _check(check_id: str, passed: bool, summary: str, **details: Any) -> dict[str, Any]:
    return {
        "id": check_id,
        "required": True,
        "passed": bool(passed),
        "summary": summary,
        "details": details,
    }


def _load_evidence(evidence_dir: Path, key: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    filename, expected_schema = EVIDENCE_SCHEMAS[key]
    path = evidence_dir / filename
    metadata: dict[str, Any] = {
        "path": str(path),
        "expected_schema": expected_schema,
        "present": path.is_file(),
    }
    if not path.is_file():
        return None, metadata
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        metadata["error"] = f"invalid_json:{exc.__class__.__name__}"
        return None, metadata
    if not isinstance(payload, dict):
        metadata["error"] = "root_must_be_object"
        return None, metadata
    metadata["sha256"] = _sha256(path)
    metadata["schema"] = payload.get("schema")
    if payload.get("schema") != expected_schema:
        metadata["error"] = "schema_mismatch"
        return None, metadata
    return payload, metadata


def _evidence_file_matches(evidence_dir: Path, entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return False
    relative_path = Path(entry["path"])
    if relative_path.is_absolute() or ".." in relative_path.parts or not _is_sha256(entry.get("sha256")):
        return False
    path = (evidence_dir / relative_path).resolve()
    try:
        path.relative_to(evidence_dir.resolve())
    except ValueError:
        return False
    return path.is_file() and _sha256(path) == entry["sha256"].lower()


def _expected_migration_head() -> str | None:
    try:
        root = Path(__file__).resolve().parents[2]
        config = Config()
        config.set_main_option("script_location", str(root / "alembic"))
        heads = ScriptDirectory.from_config(config).get_heads()
        return heads[0] if len(heads) == 1 else None
    except Exception:
        return None


def _database_migration_revision(db: Session) -> str | None:
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except SQLAlchemyError:
        return None


def evidence_templates() -> dict[str, dict[str, Any]]:
    """Return deliberately non-passing templates for externally collected evidence."""
    pending_checks = [
        {"id": item, "status": "pending", "notes": None, "evidence": []}
        for item in sorted(REQUIRED_C9_CHECKS)
    ]
    return {
        "c9-evidence.json": {
            "schema": "qingkui-c9-evidence-v1",
            "status": "pending",
            "completed_at": None,
            "device": {"model": "", "api_level": None, "firmware": "", "mdm_version": ""},
            "network": "",
            "tester": "",
            "apk_sha256": "",
            "checks": pending_checks,
        },
        "android-release-manifest.json": {
            "schema": "qingkui-android-release-v1",
            "status": "draft",
            "created_at": None,
            "environment": "pilot",
            "android_commit": "",
            "backend_commit": "",
            "version_code": None,
            "version_name": "",
            "certificate_sha256": "",
            "apk_sha256": "",
            "vector_index_sha256": "",
            "api_rollback_image": "",
            "approved_by": "",
            "approved_at": None,
        },
        "human-eval-score.json": {
            "schema": "qingkui-human-eval-score-v1",
            "release_gate_passed": False,
            "reviewed_run_sha256": "",
            "reviewers": [],
            "dataset": {
                "id": "",
                "review_status": "draft",
                "subject": settings.content_launch_subject,
                "grade": settings.content_launch_grade,
                "textbook_version": settings.content_launch_textbook_version,
            },
        },
        "recovery-drill.json": {
            "schema": "qingkui-recovery-drill-v1",
            "status": "pending",
            "completed_at": None,
            "production_data_modified": False,
            "database": {"isolated_restore": False, "metrics": {"alembic_version": ""}},
            "vector_indexes": {"current_sha256": "", "previous_sha256": ""},
            "object_storage": {"status": "pending"},
        },
    }


def write_evidence_templates(output_dir: Path, *, overwrite: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, payload in evidence_templates().items():
        path = output_dir / filename
        if path.exists() and not overwrite:
            continue
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def build_release_readiness_report(
    db: Session,
    *,
    evidence_dir: Path | None = None,
    app_settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    cfg = app_settings or settings
    current_time = now or datetime.now(timezone.utc)
    root = (evidence_dir or Path(cfg.release_evidence_dir)).resolve()
    checks: list[dict[str, Any]] = []

    config_issues = cfg.release_readiness_issues
    checks.append(
        _check(
            "configuration",
            not config_issues,
            "生产配置门禁",
            issues=config_issues,
        )
    )

    expected_revision = _expected_migration_head()
    database_revision = _database_migration_revision(db)
    checks.append(
        _check(
            "database_migration",
            bool(expected_revision) and database_revision == expected_revision,
            "数据库迁移版本",
            expected=expected_revision,
            actual=database_revision,
        )
    )

    governance = governance_report(
        db,
        subject=cfg.content_launch_subject,
        grade=cfg.content_launch_grade,
        textbook_version=cfg.content_launch_textbook_version,
    )
    approved_nodes = int(governance["nodes"]["approved"])
    candidate_by_id = {item["id"]: item for item in governance["candidates"]}
    approved_blocked = [
        node_id
        for node_id, item in candidate_by_id.items()
        if item["review_status"] == "approved" and item["blockers"]
    ]
    content_passed = (
        approved_nodes >= cfg.release_min_approved_nodes
        and not approved_blocked
        and governance["documents"]["missing_metadata"] == 0
        and governance["documents"]["unpublishable_authorization"] == 0
        and governance["documents"]["pending_formula_review"] == 0
    )
    checks.append(
        _check(
            "launch_content",
            content_passed,
            "首发内容审核与公式门禁",
            scope=governance["scope"],
            minimum_approved_nodes=cfg.release_min_approved_nodes,
            approved_nodes=approved_nodes,
            approved_blocked_node_ids=approved_blocked,
            documents=governance["documents"],
            relation_counts=governance["relations"],
        )
    )

    artifacts: dict[str, dict[str, Any]] = {}
    human_dataset_file, artifacts["human_evaluation_dataset"] = _load_evidence(
        root, "human_evaluation_dataset"
    )
    human_run, artifacts["human_evaluation_run"] = _load_evidence(root, "human_evaluation_run")
    human_eval, artifacts["human_evaluation"] = _load_evidence(root, "human_evaluation")
    human_dataset = human_eval.get("dataset", {}) if human_eval else {}
    run_dataset = human_run.get("dataset", {}) if human_run else {}
    dataset_hash_matches = bool(
        human_run
        and artifacts["human_evaluation_dataset"].get("sha256")
        == run_dataset.get("source_sha256")
    )
    run_hash_matches = bool(
        human_eval
        and artifacts["human_evaluation_run"].get("sha256")
        == human_eval.get("reviewed_run_sha256")
    )
    human_passed = bool(
        human_eval
        and human_run
        and human_dataset_file
        and human_eval.get("release_gate_passed") is True
        and _is_sha256(human_eval.get("reviewed_run_sha256"))
        and isinstance(human_eval.get("reviewers"), list)
        and human_eval.get("reviewers")
        and all(isinstance(item, str) and item.strip() for item in human_eval["reviewers"])
        and dataset_hash_matches
        and run_hash_matches
        and human_dataset == run_dataset
        and human_dataset_file.get("review_status") == "approved"
        and human_dataset_file.get("id") == human_dataset.get("id")
        and human_dataset_file.get("version", 1) == human_dataset.get("version", 1)
        and human_dataset_file.get("subject") == cfg.content_launch_subject
        and human_dataset_file.get("grade") == cfg.content_launch_grade
        and human_dataset_file.get("textbook_version") == cfg.content_launch_textbook_version
    )
    checks.append(
        _check(
            "human_evaluation",
            human_passed,
            "人工问答评测",
            artifact=artifacts["human_evaluation"],
            dataset=human_dataset,
            dataset_artifact=artifacts["human_evaluation_dataset"],
            reviewed_run_artifact=artifacts["human_evaluation_run"],
            dataset_hash_matches=dataset_hash_matches,
            run_hash_matches=run_hash_matches,
            reviewers=human_eval.get("reviewers") if human_eval else None,
            reviewed_run_sha256=human_eval.get("reviewed_run_sha256") if human_eval else None,
            release_gate_passed=human_eval.get("release_gate_passed") if human_eval else None,
        )
    )

    recovery, artifacts["recovery_drill"] = _load_evidence(root, "recovery_drill")
    recovery_time = _parse_datetime(recovery.get("completed_at")) if recovery else None
    recovery_age_days = (
        (current_time - recovery_time.astimezone(timezone.utc)).total_seconds() / 86400
        if recovery_time and recovery_time <= current_time
        else None
    )
    recovery_vectors = recovery.get("vector_indexes", {}) if recovery else {}
    recovery_db = recovery.get("database", {}) if recovery else {}
    recovery_metrics = recovery_db.get("metrics", {}) if isinstance(recovery_db, dict) else {}
    recovery_passed = bool(
        recovery
        and recovery.get("status") == "passed"
        and recovery.get("production_data_modified") is False
        and recovery_db.get("isolated_restore") is True
        and recovery_metrics.get("alembic_version") == expected_revision
        and recovery_age_days is not None
        and recovery_age_days <= cfg.release_recovery_max_age_days
        and _is_sha256(recovery_vectors.get("current_sha256"))
        and _is_sha256(recovery_vectors.get("previous_sha256"))
        and recovery.get("object_storage", {}).get("status") == "full_audit_verified"
    )
    checks.append(
        _check(
            "recovery_drill",
            recovery_passed,
            "隔离恢复与 OSS 全量审计",
            artifact=artifacts["recovery_drill"],
            completed_at=recovery.get("completed_at") if recovery else None,
            age_days=round(recovery_age_days, 2) if recovery_age_days is not None else None,
            maximum_age_days=cfg.release_recovery_max_age_days,
            migration_revision=recovery_metrics.get("alembic_version"),
            object_storage_status=recovery.get("object_storage", {}).get("status") if recovery else None,
        )
    )

    android_release, artifacts["android_release"] = _load_evidence(root, "android_release")
    release_passed = bool(
        android_release
        and android_release.get("status") == "approved"
        and android_release.get("environment") in {"pilot", "production"}
        and isinstance(android_release.get("version_code"), int)
        and android_release["version_code"] > 0
        and bool(android_release.get("version_name"))
        and _is_sha256(android_release.get("certificate_sha256"))
        and _is_sha256(android_release.get("apk_sha256"))
        and _is_sha256(android_release.get("vector_index_sha256"))
        and bool(android_release.get("android_commit"))
        and bool(android_release.get("backend_commit"))
        and bool(android_release.get("api_rollback_image"))
        and bool(android_release.get("approved_by"))
        and _parse_datetime(android_release.get("approved_at")) is not None
    )
    checks.append(
        _check(
            "signed_android_release",
            release_passed,
            "正式签名 APK 发布清单",
            artifact=artifacts["android_release"],
            environment=android_release.get("environment") if android_release else None,
            version_code=android_release.get("version_code") if android_release else None,
            version_name=android_release.get("version_name") if android_release else None,
            apk_sha256=android_release.get("apk_sha256") if android_release else None,
        )
    )

    c9, artifacts["c9_compatibility"] = _load_evidence(root, "c9_compatibility")
    c9_checks = c9.get("checks", []) if c9 else []
    c9_statuses = {
        item.get("id"): item.get("status")
        for item in c9_checks
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    c9_evidence_complete = {
        item.get("id"): bool(
            isinstance(item.get("evidence"), list)
            and item["evidence"]
            and all(
                isinstance(evidence, dict)
                and isinstance(evidence.get("path"), str)
                and bool(evidence["path"].strip())
                and _is_sha256(evidence.get("sha256"))
                and _evidence_file_matches(root, evidence)
                for evidence in item["evidence"]
            )
        )
        for item in c9_checks
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    c9_passed = bool(
        c9
        and c9.get("status") == "passed"
        and REQUIRED_C9_CHECKS.issubset(c9_statuses)
        and all(c9_statuses[item] == "passed" for item in REQUIRED_C9_CHECKS)
        and all(c9_evidence_complete.get(item) is True for item in REQUIRED_C9_CHECKS)
        and bool(c9.get("tester"))
        and _parse_datetime(c9.get("completed_at")) is not None
        and _is_sha256(c9.get("apk_sha256"))
        and release_passed
        and c9.get("apk_sha256", "").lower() == android_release.get("apk_sha256", "").lower()
    )
    checks.append(
        _check(
            "c9_compatibility",
            c9_passed,
            "华为擎云 C9 真机与 MDM 验收",
            artifact=artifacts["c9_compatibility"],
            completed_checks=sum(value == "passed" for value in c9_statuses.values()),
            required_checks=len(REQUIRED_C9_CHECKS),
            missing_check_ids=sorted(REQUIRED_C9_CHECKS - set(c9_statuses)),
            failed_check_ids=sorted(
                item for item in REQUIRED_C9_CHECKS if c9_statuses.get(item) not in {None, "passed"}
            ),
            missing_evidence_check_ids=sorted(
                item for item in REQUIRED_C9_CHECKS if c9_evidence_complete.get(item) is not True
            ),
            apk_matches_release=bool(
                c9
                and android_release
                and c9.get("apk_sha256", "").lower() == android_release.get("apk_sha256", "").lower()
            ),
        )
    )

    if recovery and android_release:
        vector_matches = (
            str(recovery_vectors.get("current_sha256", "")).lower()
            == str(android_release.get("vector_index_sha256", "")).lower()
        )
    else:
        vector_matches = False
    checks.append(
        _check(
            "artifact_consistency",
            bool(release_passed and recovery_passed and vector_matches),
            "发布清单与恢复证据交叉校验",
            vector_index_sha256_matches=vector_matches,
        )
    )

    blockers = [item["id"] for item in checks if item["required"] and not item["passed"]]
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": _utc_now(),
        "environment": cfg.app_env,
        "evidence_dir": str(root),
        "ready": not blockers,
        "blockers": blockers,
        "checks": checks,
        "artifacts": artifacts,
    }
