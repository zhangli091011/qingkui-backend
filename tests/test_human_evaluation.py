import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.human_evaluation import (
    DATASET_SCHEMA,
    RUN_SCHEMA,
    load_dataset,
    score_human_evaluation,
)


def _dataset() -> dict:
    return {
        "schema": DATASET_SCHEMA,
        "id": "math-test-v1",
        "version": 1,
        "subject": "数学",
        "thresholds": {
            "minimum_review_coverage": 1,
            "minimum_execution_success_rate": 1,
            "minimum_subject_routing_accuracy": 1,
            "minimum_correctness": 0.75,
            "minimum_citation_hit_rate": 1,
            "maximum_unsupported_rate": 0,
            "minimum_correction_rate": 1,
            "minimum_helpfulness_mean": 4,
        },
        "cases": [
            {
                "id": "definition",
                "question": "什么是函数？",
                "expected_subject": "数学",
                "expected_points": ["定义"],
                "requires_citation": True,
                "correction_expected": False,
            },
            {
                "id": "correction",
                "question": "核验一个错误命题",
                "expected_subject": "数学",
                "expected_points": ["指出错误"],
                "requires_citation": False,
                "correction_expected": True,
            },
        ],
    }


def _run() -> dict:
    dataset = _dataset()
    return {
        "schema": RUN_SCHEMA,
        "dataset": {
            "id": dataset["id"],
            "version": dataset["version"],
            "subject": dataset["subject"],
            "thresholds": dataset["thresholds"],
        },
        "results": [
            {
                "case": case,
                "resolved_subject": "数学",
                "error": None,
                "review": {
                    "status": "pending",
                    "reviewer": None,
                    "reviewed_at": None,
                    "correctness": None,
                    "citation_supported": None,
                    "unsupported_claim": None,
                    "correction_success": None,
                    "helpfulness": None,
                    "notes": None,
                },
            }
            for case in dataset["cases"]
        ],
    }


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_math_seed_dataset_is_valid() -> None:
    dataset = load_dataset(Path("config/evaluation/math-v1.json"))
    assert dataset["subject"] == "数学"
    assert len(dataset["cases"]) >= 15
    assert any(case.get("correction_expected") for case in dataset["cases"])
    assert any("无依据问题" in case.get("tags", []) for case in dataset["cases"])
    assert any("跨学科误召回" in case.get("tags", []) for case in dataset["cases"])


def test_dataset_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    dataset = _dataset()
    dataset["cases"][1]["id"] = dataset["cases"][0]["id"]
    with pytest.raises(ValueError, match="Duplicate case id"):
        load_dataset(_write(tmp_path / "dataset.json", dataset))


def test_score_requires_every_human_review_by_default(tmp_path: Path) -> None:
    path = _write(tmp_path / "run.json", _run())
    with pytest.raises(ValueError, match="Pending human reviews"):
        score_human_evaluation(path)


def test_completed_human_reviews_produce_release_metrics(tmp_path: Path) -> None:
    run = deepcopy(_run())
    reviewed_at = datetime.now(timezone.utc).isoformat()
    reviews = [
        {
            "status": "completed",
            "reviewer": "math-reviewer-01",
            "reviewed_at": reviewed_at,
            "correctness": 1,
            "citation_supported": True,
            "unsupported_claim": False,
            "correction_success": None,
            "helpfulness": 5,
            "notes": "定义与来源一致",
        },
        {
            "status": "completed",
            "reviewer": "math-reviewer-01",
            "reviewed_at": reviewed_at,
            "correctness": 0.5,
            "citation_supported": True,
            "unsupported_claim": False,
            "correction_success": True,
            "helpfulness": 4,
            "notes": "结论正确，解释可补充",
        },
    ]
    for item, review in zip(run["results"], reviews, strict=True):
        item["review"] = review

    report = score_human_evaluation(
        _write(tmp_path / "reviewed.json", run),
        tmp_path / "score.json",
    )

    assert report["metrics"] == {
        "review_coverage": 1.0,
        "execution_success_rate": 1.0,
        "subject_routing_accuracy": 1.0,
        "correctness": 0.75,
        "citation_hit_rate": 1.0,
        "unsupported_rate": 0.0,
        "correction_rate": 1.0,
        "helpfulness_mean": 4.5,
    }
    assert report["release_gate_passed"] is True
    assert json.loads((tmp_path / "score.json").read_text(encoding="utf-8"))["schema"]


def test_incomplete_interim_report_never_passes_release_gate(tmp_path: Path) -> None:
    report = score_human_evaluation(
        _write(tmp_path / "run.json", _run()),
        allow_incomplete=True,
    )
    assert report["reviewed_count"] == 0
    assert report["release_gate_passed"] is False
