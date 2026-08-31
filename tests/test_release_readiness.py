import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.db import SessionLocal
from app.services import release_readiness


SHA_A = "a" * 64
SHA_B = "b" * 64


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_evidence(root: Path, now: datetime) -> None:
    dataset = {
        "schema": "qingkui-human-eval-dataset-v1",
        "id": "math-gaoyi-rjb-a-v1",
        "version": 1,
        "review_status": "approved",
        "subject": "数学",
        "grade": "高一",
        "textbook_version": "人教A版",
    }
    _write(root / "human-eval-dataset.json", dataset)
    run_dataset = {
        "id": dataset["id"],
        "version": dataset["version"],
        "source_sha256": _sha256(root / "human-eval-dataset.json"),
        "subject": dataset["subject"],
        "grade": dataset["grade"],
        "textbook_version": dataset["textbook_version"],
        "review_status": dataset["review_status"],
        "thresholds": {},
    }
    _write(
        root / "human-eval-reviewed-run.json",
        {"schema": "qingkui-human-eval-run-v1", "dataset": run_dataset, "results": []},
    )
    _write(
        root / "human-eval-score.json",
        {
            "schema": "qingkui-human-eval-score-v1",
            "release_gate_passed": True,
            "reviewed_run_sha256": _sha256(root / "human-eval-reviewed-run.json"),
            "reviewers": ["math-reviewer-01"],
            "dataset": run_dataset,
        },
    )
    _write(
        root / "recovery-drill.json",
        {
            "schema": "qingkui-recovery-drill-v1",
            "status": "passed",
            "completed_at": (now - timedelta(days=1)).isoformat(),
            "production_data_modified": False,
            "database": {
                "isolated_restore": True,
                "metrics": {"alembic_version": "20260901_0022"},
            },
            "vector_indexes": {"current_sha256": SHA_A, "previous_sha256": SHA_B},
            "object_storage": {"status": "full_audit_verified"},
        },
    )
    _write(
        root / "android-release-manifest.json",
        {
            "schema": "qingkui-android-release-v1",
            "status": "approved",
            "created_at": now.isoformat(),
            "environment": "pilot",
            "android_commit": "1" * 40,
            "backend_commit": "2" * 40,
            "version_code": 2,
            "version_name": "0.1.1",
            "certificate_sha256": SHA_B,
            "apk_sha256": SHA_A,
            "vector_index_sha256": SHA_A,
            "api_rollback_image": "registry.example/qingkui-api@sha256:" + SHA_B,
            "approved_by": "release-owner-01",
            "approved_at": now.isoformat(),
        },
    )
    for check_id in sorted(release_readiness.REQUIRED_C9_CHECKS):
        evidence_path = root / "c9" / f"{check_id}.txt"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(f"verified evidence for {check_id}\n", encoding="utf-8")
    _write(
        root / "c9-evidence.json",
        {
            "schema": "qingkui-c9-evidence-v1",
            "status": "passed",
            "completed_at": now.isoformat(),
            "device": {
                "model": "Huawei Qingyun C9",
                "api_level": 34,
                "firmware": "test-firmware",
                "mdm_version": "test-mdm",
            },
            "network": "controlled-test-network",
            "tester": "device-tester-01",
            "apk_sha256": SHA_A,
            "checks": [
                {
                    "id": check_id,
                    "status": "passed",
                    "notes": "evidence recorded",
                    "evidence": [
                        {
                            "path": f"c9/{check_id}.txt",
                            "sha256": _sha256(root / "c9" / f"{check_id}.txt"),
                        }
                    ],
                }
                for check_id in sorted(release_readiness.REQUIRED_C9_CHECKS)
            ],
        },
    )


def _test_settings(evidence_dir: Path) -> Settings:
    return Settings(
        app_env="test",
        release_evidence_dir=str(evidence_dir),
        release_min_approved_nodes=0,
        release_recovery_max_age_days=30,
    )


def _clean_governance(*_args, **_kwargs) -> dict:
    return {
        "scope": {"subject": "数学", "grade": "高一", "textbook_version": "人教A版"},
        "nodes": {"total": 0, "approved": 0, "publishable": 0, "blocked": 0},
        "documents": {
            "total_for_subject": 0,
            "missing_metadata": 0,
            "unpublishable_authorization": 0,
            "pending_formula_review": 0,
        },
        "relations": {},
        "blocker_counts": {},
        "candidates": [],
    }


def test_evidence_templates_are_non_passing_and_do_not_overwrite(tmp_path: Path) -> None:
    written = release_readiness.write_evidence_templates(tmp_path)
    assert len(written) == 4
    assert json.loads((tmp_path / "c9-evidence.json").read_text(encoding="utf-8"))["status"] == "pending"
    assert json.loads((tmp_path / "android-release-manifest.json").read_text(encoding="utf-8"))["status"] == "draft"
    assert release_readiness.write_evidence_templates(tmp_path) == []


def test_release_readiness_passes_only_with_consistent_complete_evidence(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    _valid_evidence(tmp_path, now)
    monkeypatch.setattr(release_readiness, "_expected_migration_head", lambda: "20260901_0022")
    monkeypatch.setattr(release_readiness, "_database_migration_revision", lambda _db: "20260901_0022")
    monkeypatch.setattr(release_readiness, "governance_report", _clean_governance)

    with SessionLocal() as db:
        report = release_readiness.build_release_readiness_report(
            db,
            evidence_dir=tmp_path,
            app_settings=_test_settings(tmp_path),
            now=now,
        )

    assert report["ready"] is True, report
    assert report["blockers"] == []
    assert all(check["passed"] for check in report["checks"])


def test_release_readiness_rejects_stale_recovery_and_apk_mismatch(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    _valid_evidence(tmp_path, now)
    recovery = json.loads((tmp_path / "recovery-drill.json").read_text(encoding="utf-8"))
    recovery["completed_at"] = (now - timedelta(days=31)).isoformat()
    _write(tmp_path / "recovery-drill.json", recovery)
    c9 = json.loads((tmp_path / "c9-evidence.json").read_text(encoding="utf-8"))
    c9["apk_sha256"] = SHA_B
    _write(tmp_path / "c9-evidence.json", c9)
    monkeypatch.setattr(release_readiness, "_expected_migration_head", lambda: "20260901_0022")
    monkeypatch.setattr(release_readiness, "_database_migration_revision", lambda _db: "20260901_0022")
    monkeypatch.setattr(release_readiness, "governance_report", _clean_governance)

    with SessionLocal() as db:
        report = release_readiness.build_release_readiness_report(
            db,
            evidence_dir=tmp_path,
            app_settings=_test_settings(tmp_path),
            now=now,
        )

    assert report["ready"] is False
    assert "recovery_drill" in report["blockers"]
    assert "c9_compatibility" in report["blockers"]
    assert "artifact_consistency" in report["blockers"]
