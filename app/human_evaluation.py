"""Human-reviewed QA evaluation runs and release-gate metrics."""

from __future__ import annotations

import json
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.models import HelpLevel, QaMode
from app.services.ai import answer_question, render_answer
from app.services.knowledge import RetrievedChunk, retrieve_chunks, retrieve_nodes
from app.subjects import SUBJECTS, resolve_subject


DATASET_SCHEMA = "qingkui-human-eval-dataset-v1"
RUN_SCHEMA = "qingkui-human-eval-run-v1"
SCORE_SCHEMA = "qingkui-human-eval-score-v1"
REVIEW_FIELDS = (
    "correctness",
    "citation_supported",
    "unsupported_claim",
    "correction_success",
    "helpfulness",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Evaluation file must contain one JSON object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dataset(path: Path) -> dict[str, Any]:
    dataset = _load_json(path)
    if dataset.get("schema") != DATASET_SCHEMA:
        raise ValueError(f"Dataset schema must be {DATASET_SCHEMA}")
    if not isinstance(dataset.get("id"), str) or not dataset["id"].strip():
        raise ValueError("Dataset id is required")
    subject = dataset.get("subject")
    if subject not in SUBJECTS:
        raise ValueError(f"Dataset subject must be one of: {', '.join(SUBJECTS)}")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset must contain at least one case")
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"Case {index} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Case {index} id is required")
        if case_id in seen:
            raise ValueError(f"Duplicate case id: {case_id}")
        seen.add(case_id)
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise ValueError(f"Case {case_id} question is required")
        try:
            QaMode(case.get("mode", "knowledge"))
            HelpLevel(case.get("help_level", "approach"))
        except ValueError as exc:
            raise ValueError(f"Case {case_id} has an invalid mode or help_level") from exc
        expected_subject = case.get("expected_subject", subject)
        if expected_subject is not None and expected_subject not in SUBJECTS:
            raise ValueError(f"Case {case_id} expected_subject is invalid")
        if not isinstance(case.get("expected_points"), list) or not case["expected_points"]:
            raise ValueError(f"Case {case_id} expected_points are required")
    return dataset


def _source_records(nodes, chunks: list[RetrievedChunk]) -> list[dict[str, Any]]:
    sources = [
        {
            "kind": "node",
            "node_id": node.id,
            "title": node.name,
            "source_title": node.source.title,
            "source_location": node.source.location,
            "excerpt": node.source_excerpt,
        }
        for node in nodes
    ]
    sources.extend(
        {
            "kind": "document_chunk",
            "document_id": item.chunk.document.id,
            "chunk_id": item.chunk.id,
            "title": item.chunk.document.title,
            "source_location": item.chunk.document.source_uri,
            "excerpt": item.chunk.content[:600],
            "score": round(item.score, 6),
        }
        for item in chunks
    )
    return sources


def _pending_review() -> dict[str, Any]:
    return {
        "status": "pending",
        "reviewer": None,
        "reviewed_at": None,
        "correctness": None,
        "citation_supported": None,
        "unsupported_claim": None,
        "correction_success": None,
        "helpfulness": None,
        "notes": None,
    }


def _execute_case(case: dict[str, Any], dataset_subject: str) -> dict[str, Any]:
    started = time.perf_counter()
    question = case["question"].strip()
    resolved_subject = resolve_subject(question)
    retrieval_subject = case.get("retrieval_subject", resolved_subject or dataset_subject)
    try:
        with SessionLocal() as db:
            nodes = retrieve_nodes(db, question, pinned_node_id=None, limit=5, subject=retrieval_subject)
            chunks = retrieve_chunks(db, question, limit=5, subject=retrieval_subject)
        result = answer_question(
            question,
            QaMode(case.get("mode", "knowledge")),
            HelpLevel(case.get("help_level", "approach")),
            nodes,
            chunks,
        )
        return {
            "case": case,
            "resolved_subject": resolved_subject,
            "retrieval_subject": retrieval_subject,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "provider": result.provider,
            "model": result.model,
            "answer": render_answer(result.answer),
            "answer_uncertain": result.answer.uncertain,
            "sources": _source_records(nodes, chunks),
            "error": None,
            "review": _pending_review(),
        }
    except Exception as exc:  # Preserve the whole run for manual triage.
        return {
            "case": case,
            "resolved_subject": resolved_subject,
            "retrieval_subject": retrieval_subject,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "provider": None,
            "model": None,
            "answer": "",
            "answer_uncertain": None,
            "sources": [],
            "error": f"{type(exc).__name__}: {exc}",
            "review": _pending_review(),
        }


def run_human_evaluation(
    dataset_path: Path,
    output_path: Path,
    *,
    workers: int = 4,
) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    started_at = _utc_now()
    started = time.perf_counter()
    cases = dataset["cases"]
    workers = max(1, min(int(workers), 8, len(cases)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="human-eval") as executor:
        futures = [executor.submit(_execute_case, case, dataset["subject"]) for case in cases]
        results = [future.result() for future in as_completed(futures)]
    order = {case["id"]: index for index, case in enumerate(cases)}
    results.sort(key=lambda item: order[item["case"]["id"]])
    run = {
        "schema": RUN_SCHEMA,
        "dataset": {
            "id": dataset["id"],
            "version": dataset.get("version", 1),
            "source_sha256": _file_sha256(dataset_path),
            "subject": dataset["subject"],
            "grade": dataset.get("grade"),
            "textbook_version": dataset.get("textbook_version"),
            "review_status": dataset.get("review_status", "draft"),
            "thresholds": dataset.get("thresholds", {}),
        },
        "started_at": started_at,
        "completed_at": _utc_now(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "workers": workers,
        "case_count": len(results),
        "execution_errors": sum(item["error"] is not None for item in results),
        "review_instructions": (
            "逐题核对答案与 sources。将 review.status 改为 completed，填写 reviewer、reviewed_at、"
            "correctness(0/0.5/1)、citation_supported、unsupported_claim、"
            "correction_success(仅纠错题)、helpfulness(1-5) 和 notes。"
        ),
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return run


def _validate_completed_review(item: dict[str, Any]) -> dict[str, Any]:
    case_id = item.get("case", {}).get("id", "unknown")
    review = item.get("review")
    if not isinstance(review, dict) or review.get("status") != "completed":
        raise ValueError(f"Case {case_id} has not been reviewed")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError(f"Case {case_id} reviewer is required")
    reviewed_at = review.get("reviewed_at")
    if not isinstance(reviewed_at, str):
        raise ValueError(f"Case {case_id} reviewed_at is required")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Case {case_id} reviewed_at must be ISO-8601") from exc
    if parsed_reviewed_at.tzinfo is None:
        raise ValueError(f"Case {case_id} reviewed_at must include a timezone")
    if review.get("correctness") not in (0, 0.5, 1):
        raise ValueError(f"Case {case_id} correctness must be 0, 0.5, or 1")
    if not isinstance(review.get("citation_supported"), bool):
        raise ValueError(f"Case {case_id} citation_supported must be boolean")
    if not isinstance(review.get("unsupported_claim"), bool):
        raise ValueError(f"Case {case_id} unsupported_claim must be boolean")
    if review.get("helpfulness") not in (1, 2, 3, 4, 5):
        raise ValueError(f"Case {case_id} helpfulness must be 1-5")
    correction_expected = bool(item.get("case", {}).get("correction_expected"))
    if correction_expected and not isinstance(review.get("correction_success"), bool):
        raise ValueError(f"Case {case_id} correction_success must be boolean")
    if not correction_expected and review.get("correction_success") is not None:
        raise ValueError(f"Case {case_id} correction_success must be null")
    return review


def _ratio(numerator: float, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def score_human_evaluation(
    run_path: Path,
    output_path: Path | None = None,
    *,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    run = _load_json(run_path)
    if run.get("schema") != RUN_SCHEMA:
        raise ValueError(f"Run schema must be {RUN_SCHEMA}")
    results = run.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("Evaluation run has no results")
    completed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending_ids: list[str] = []
    for item in results:
        if item.get("review", {}).get("status") != "completed":
            pending_ids.append(item.get("case", {}).get("id", "unknown"))
            continue
        completed.append((item, _validate_completed_review(item)))
    if pending_ids and not allow_incomplete:
        raise ValueError(f"Pending human reviews: {', '.join(pending_ids)}")

    reviewed_count = len(completed)
    total_count = len(results)
    citation_reviews = [review for item, review in completed if item["case"].get("requires_citation", True)]
    correction_reviews = [review for item, review in completed if item["case"].get("correction_expected", False)]
    subject_results = [
        item
        for item in results
        if item.get("case", {}).get("expected_subject") is not None
    ]
    metrics = {
        "review_coverage": _ratio(reviewed_count, total_count),
        "execution_success_rate": _ratio(sum(item.get("error") is None for item in results), total_count),
        "subject_routing_accuracy": _ratio(
            sum(item.get("resolved_subject") == item["case"].get("expected_subject") for item in subject_results),
            len(subject_results),
        ),
        "correctness": _ratio(sum(float(review["correctness"]) for _, review in completed), reviewed_count),
        "citation_hit_rate": _ratio(sum(review["citation_supported"] for review in citation_reviews), len(citation_reviews)),
        "unsupported_rate": _ratio(sum(review["unsupported_claim"] for _, review in completed), reviewed_count),
        "correction_rate": _ratio(sum(review["correction_success"] for review in correction_reviews), len(correction_reviews)),
        "helpfulness_mean": _ratio(sum(review["helpfulness"] for _, review in completed), reviewed_count),
    }
    thresholds = run.get("dataset", {}).get("thresholds") or {}
    comparisons = {
        "review_coverage": ("minimum_review_coverage", ">="),
        "execution_success_rate": ("minimum_execution_success_rate", ">="),
        "subject_routing_accuracy": ("minimum_subject_routing_accuracy", ">="),
        "correctness": ("minimum_correctness", ">="),
        "citation_hit_rate": ("minimum_citation_hit_rate", ">="),
        "unsupported_rate": ("maximum_unsupported_rate", "<="),
        "correction_rate": ("minimum_correction_rate", ">="),
        "helpfulness_mean": ("minimum_helpfulness_mean", ">="),
    }
    gates: dict[str, dict[str, Any]] = {}
    for metric_name, (threshold_name, operator) in comparisons.items():
        if threshold_name not in thresholds:
            continue
        value = metrics[metric_name]
        threshold = float(thresholds[threshold_name])
        passed = value is not None and (value >= threshold if operator == ">=" else value <= threshold)
        gates[metric_name] = {
            "value": value,
            "operator": operator,
            "threshold": threshold,
            "passed": passed,
        }
    report = {
        "schema": SCORE_SCHEMA,
        "scored_at": _utc_now(),
        "reviewed_run_sha256": _file_sha256(run_path),
        "reviewers": sorted({review["reviewer"].strip() for _, review in completed}),
        "dataset": run.get("dataset", {}),
        "case_count": total_count,
        "reviewed_count": reviewed_count,
        "pending_case_ids": pending_ids,
        "metrics": metrics,
        "gates": gates,
        "release_gate_passed": bool(gates) and not pending_ids and all(item["passed"] for item in gates.values()),
    }
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
