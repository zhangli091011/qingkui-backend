import argparse
import getpass
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import CreditAccount, User, UserRole
from app.security import hash_password
from app.services.documents import (
    SUPPORTED_SUFFIXES,
    import_document,
    import_scanned_pdf_document,
    ocr_scanned_pdf,
    reindex_documents,
)
from app.services.wikibooks import import_wikibooks
from app.services.knowledge import build_vector_index
from app.services.metadata import backfill_document_metadata, extract_graph_relations
from app.services.object_storage import migrate_knowledge_to_oss, sync_vector_index_from_oss, verify_knowledge_oss
from app.services.content_governance import governance_report
from app.services.launch_candidates import materialize_launch_candidates


def create_admin(username: str, email: str | None) -> None:
    password = getpass.getpass("Admin password: ")
    if len(password) < 8:
        raise SystemExit("Password must be at least 8 characters")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.username == username.lower())):
            raise SystemExit("Username already exists")
        user = User(
            username=username.lower(),
            email=email.lower() if email else None,
            nickname="青葵管理员",
            password_hash=hash_password(password),
            role=UserRole.admin,
        )
        db.add(user)
        db.flush()
        db.add(CreditAccount(user_id=user.id, balance=0))
        db.commit()
    print("Admin created")


def import_documents(path_value: str, authorization_status: str, title: str | None, subject: str | None) -> None:
    path = Path(path_value).resolve()
    if path.is_dir():
        max_size_mb = float(os.environ.get("IMPORT_MAX_FILE_MB", "0") or "0")
        files = sorted(
            item
            for item in path.rglob("*")
            if item.suffix.lower() in SUPPORTED_SUFFIXES
            and (max_size_mb <= 0 or item.stat().st_size <= max_size_mb * 1024 * 1024)
        )
        if max_size_mb > 0:
            oversized = sorted(
                item
                for item in path.rglob("*")
                if item.suffix.lower() in SUPPORTED_SUFFIXES
                and item.stat().st_size > max_size_mb * 1024 * 1024
            )
            for item in oversized:
                print(f"SKIPPED_TOO_LARGE {item.name}: {item.stat().st_size / 1024 / 1024:.1f} MB")
    else:
        files = [path]
    if title and len(files) != 1:
        raise SystemExit("--title can only be used when importing one file")
    if not files:
        raise SystemExit("No supported files found")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        for file_path in files:
            try:
                result = import_document(db, file_path, authorization_status, title=title, subject=subject)
            # A malformed or encrypted file must not abort a directory import;
            # keep the transaction isolated to this file and continue.
            except Exception as exc:
                db.rollback()
                print(f"FAILED {file_path.name}: {exc}")
                continue
            action = "IMPORTED" if result.created else "SKIPPED"
            print(f"{action} {file_path.name}: {result.chunk_count} chunks, status={result.document.status}")


def import_curated_textbooks(path_value: str) -> None:
    root = Path(path_value).resolve()
    if not root.is_dir():
        raise SystemExit(f"Directory does not exist: {root}")
    files = [
        item
        for item in root.rglob("*.docx")
        if (
            "思维导图+知识清单" in item.name
            or (
                "同步讲义-P8" in str(item.parent)
                and "教师版" in item.name
                and not item.name.endswith("1.docx")
            )
        )
    ]
    files.sort(key=lambda item: str(item))
    if not files:
        raise SystemExit("No curated DOCX files found")
    Base.metadata.create_all(bind=engine)
    imported = skipped = failed = chunks = 0
    with SessionLocal() as db:
        for file_path in files:
            try:
                result = import_document(db, file_path, "self_owned")
            except (OSError, ValueError, RuntimeError) as exc:
                db.rollback()
                failed += 1
                print(f"FAILED {file_path.name}: {exc}")
                continue
            if result.created:
                imported += 1
                chunks += result.chunk_count
            else:
                skipped += 1
    print(f"Curated textbook: files={len(files)}, imported={imported}, skipped={skipped}, failed={failed}, chunks={chunks}")


def preview_scanned_pdf_ocr(path_value: str, start_page: int, end_page: int, dpi: int, tesseract_binary: str | None, ocr_engine: str) -> None:
    pages = ocr_scanned_pdf(
        Path(path_value).resolve(),
        start_page=start_page,
        end_page=end_page,
        dpi=dpi,
        tesseract_binary=tesseract_binary,
        engine=ocr_engine,
    )
    for page in pages:
        print(f"PAGE {page.page_number} confidence={page.confidence if page.confidence is not None else 'unknown'}")
        print(page.text[:1000])
        for raw, latex in page.formulas:
            print(f"FORMULA raw={raw}\nLATEX {latex}")


def import_scanned_pdf(path_value: str, title: str | None, subject: str | None, start_page: int, end_page: int | None, dpi: int, tesseract_binary: str | None, ocr_engine: str) -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        result = import_scanned_pdf_document(
            db,
            Path(path_value).resolve(),
            "self_owned",
            title=title,
            subject=subject,
            start_page=start_page,
            end_page=end_page,
            dpi=dpi,
            tesseract_binary=tesseract_binary,
            engine=ocr_engine,
        )
    action = "IMPORTED" if result.created else "SKIPPED"
    print(f"{action} {result.document.title}: {result.chunk_count} chunks, status={result.document.status}")


def reindex_all_documents() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        count = reindex_documents(db)
    print(f"Indexed {count} chunks")


def build_vector_index_file() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        count = build_vector_index(db)
    print(f"Vector index built for {count} chunks")


def backfill_metadata_file(limit: int | None = None) -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        summary = backfill_document_metadata(db, limit=limit)
    print(f"Metadata backfill: documents={summary['documents']}, fields_changed={summary['fields_changed']}")


def extract_graph_file(limit: int | None = None) -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        summary = extract_graph_relations(db, limit=limit, include_taxonomy=True)
    print(
        f"Graph extraction: documents={summary.documents}, nodes_created={summary.nodes_created}, "
        f"candidates={summary.candidates}, edges_created={summary.edges_created}"
    )


def content_governance_report_file(output: str | None = None) -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        report = governance_report(db)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        Path(output).resolve().write_text(payload, encoding="utf-8")
    print(payload)


def materialize_launch_candidates_file(limit: int) -> None:
    from app.config import settings

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        summary = materialize_launch_candidates(
            db,
            subject=settings.content_launch_subject,
            grade=settings.content_launch_grade,
            textbook_version=settings.content_launch_textbook_version,
            limit=limit,
        )
    print(
        f"Launch candidates: documents={summary.documents}, nodes_created={summary.nodes_created}, "
        f"edges_created={summary.edges_created}, skipped_existing={summary.skipped_existing}, "
        f"nodes_removed={summary.nodes_removed}"
    )


def content_review_packet_file(
    output: str,
    markdown: str | None,
    *,
    subject: str,
    grade: str,
    textbook_version: str,
    chapter: str | None,
    limit: int,
) -> None:
    from app.services.review_packets import build_content_review_packet, render_content_review_markdown

    if not 1 <= limit <= 600:
        raise SystemExit("--limit must be between 1 and 600")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        packet = build_content_review_packet(
            db,
            subject=subject,
            grade=grade,
            textbook_version=textbook_version,
            chapter=chapter,
            limit=limit,
        )
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if markdown:
        markdown_path = Path(markdown).resolve()
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_content_review_markdown(packet), encoding="utf-8")
    summary = packet["summary"]
    print(
        f"Content review packet: nodes={summary['nodes']}, evidence={summary['nodes_with_evidence']}, "
        f"error_suggestions={summary['common_error_suggestions']}, "
        f"question_suggestions={summary['question_type_suggestions']}, output={output_path}"
    )


def apply_content_review_packet_file(
    input_path: str,
    reviewer_username: str,
    *,
    publish: bool,
    dry_run: bool,
    confirmation: str | None,
) -> None:
    from sqlalchemy import select

    from app.models import User, UserRole
    from app.services.review_packets import apply_content_review_packet

    expected_confirmation = "PUBLISH_REVIEWED_CONTENT" if publish else "APPLY_REVIEWED_CONTENT"
    if not dry_run and confirmation != expected_confirmation:
        raise SystemExit(f"--confirmation must be {expected_confirmation}")
    path = Path(input_path).resolve()
    packet = json.loads(path.read_text(encoding="utf-8"))
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        reviewer = db.scalar(select(User).where(User.username == reviewer_username.lower()))
        if reviewer is None or not reviewer.is_active or reviewer.role not in (UserRole.admin, UserRole.content_admin):
            raise SystemExit("--reviewer must name an active admin or content_admin account")
        summary = apply_content_review_packet(db, packet, reviewer=reviewer, publish=publish)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    print(json.dumps({"dry_run": dry_run, "publish": publish, **summary.as_dict()}, ensure_ascii=False, indent=2))


def import_wikibooks_content(pages_per_topic: int) -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        result = import_wikibooks(db, pages_per_topic)
    print(
        "Wikibooks: "
        f"discovered={result.discovered}, imported={result.imported}, "
        f"updated={result.updated}, skipped={result.skipped}, chunks={result.chunks}"
    )


def notify_operational_alerts_file(*, dry_run: bool) -> None:
    from app.config import settings
    from app.services.operational_alerts import (
        build_operational_alert_summary,
        notify_operational_alerts,
        webhook_payload,
    )

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        summary = build_operational_alert_summary(db)
    if dry_run:
        result = "dry_run"
    else:
        if not settings.operational_alert_webhook_url:
            raise SystemExit("OPERATIONAL_ALERT_WEBHOOK_URL is not configured")
        result = notify_operational_alerts(summary, webhook_url=settings.operational_alert_webhook_url)
    print(json.dumps({"notification": result, "payload": webhook_payload(summary)}, ensure_ascii=False, indent=2))


def release_evidence_templates_file(output_dir: str, *, overwrite: bool) -> None:
    from app.services.release_readiness import write_evidence_templates

    output = Path(output_dir).resolve()
    written = write_evidence_templates(output, overwrite=overwrite)
    print(
        json.dumps(
            {
                "output_dir": str(output),
                "written": [str(path) for path in written],
                "skipped_existing": len(list(output.glob("*.json"))) - len(written),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def release_readiness_report_file(
    output: str | None,
    *,
    evidence_dir: str | None,
    strict: bool,
) -> None:
    from app.services.release_readiness import build_release_readiness_report

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        report = build_release_readiness_report(
            db,
            evidence_dir=Path(evidence_dir).resolve() if evidence_dir else None,
        )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output_path = Path(output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if strict and not report["ready"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    admin_command = subparsers.add_parser("create-admin")
    admin_command.add_argument("--username", required=True)
    admin_command.add_argument("--email")
    import_command = subparsers.add_parser("import-documents")
    import_command.add_argument("path")
    import_command.add_argument(
        "--authorization-status",
        required=True,
        choices=("authorized", "self_owned", "public_domain"),
    )
    import_command.add_argument("--title")
    import_command.add_argument("--subject", choices=("语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"))
    curated_command = subparsers.add_parser("import-curated-textbook")
    curated_command.add_argument("path")
    ocr_preview_command = subparsers.add_parser("preview-scanned-pdf-ocr")
    ocr_preview_command.add_argument("path")
    ocr_preview_command.add_argument("--start-page", type=int, default=1)
    ocr_preview_command.add_argument("--end-page", type=int, default=3)
    ocr_preview_command.add_argument("--dpi", type=int, default=200)
    ocr_preview_command.add_argument("--tesseract-binary")
    ocr_preview_command.add_argument("--engine", choices=("bailian", "local"), default="bailian")
    ocr_import_command = subparsers.add_parser("import-scanned-pdf")
    ocr_import_command.add_argument("path")
    ocr_import_command.add_argument("--title")
    ocr_import_command.add_argument("--subject", choices=("语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"))
    ocr_import_command.add_argument("--start-page", type=int, default=1)
    ocr_import_command.add_argument("--end-page", type=int)
    ocr_import_command.add_argument("--dpi", type=int, default=200)
    ocr_import_command.add_argument("--tesseract-binary")
    ocr_import_command.add_argument("--engine", choices=("bailian", "local"), default="bailian")
    subparsers.add_parser("reindex-documents")
    subparsers.add_parser("build-vector-index", help="build the local sidecar vector index without API calls")
    oss_command = subparsers.add_parser("migrate-to-oss", help="upload source documents and vector index to Alibaba Cloud OSS")
    oss_command.add_argument("--workers", type=int, default=4, choices=range(1, 17))
    oss_command.add_argument("--dry-run", action="store_true")
    oss_command.add_argument("--keep-source-uri", action="store_true", help="do not replace local source_uri after upload")
    sync_command = subparsers.add_parser("sync-vector-index-from-oss", help="install a verified OSS vector index locally")
    sync_command.add_argument("--version", choices=("current", "previous"), default="current")
    sync_command.add_argument("--allow-stale", action="store_true", help="retain an existing local index if OSS is unavailable")
    verify_command = subparsers.add_parser("verify-oss-storage", help="verify OSS document objects and vector pointer")
    verify_command.add_argument("--workers", type=int, default=8, choices=range(1, 17))
    sqlite_import_command = subparsers.add_parser(
        "import-sqlite-knowledge",
        help="upsert static knowledge content from a SQLite snapshot into PostgreSQL",
    )
    sqlite_import_command.add_argument("snapshot")
    sqlite_import_command.add_argument("--batch-size", type=int, default=250)
    metadata_command = subparsers.add_parser("backfill-document-metadata", help="infer missing local document metadata")
    metadata_command.add_argument("--limit", type=int)
    graph_command = subparsers.add_parser("extract-knowledge-graph", help="extract conservative graph relation candidates")
    graph_command.add_argument("--limit", type=int)
    governance_command = subparsers.add_parser("content-governance-report", help="audit launch-scope content before publication")
    governance_command.add_argument("--output")
    candidate_command = subparsers.add_parser("materialize-launch-candidates", help="create review-only nodes from launch-scope documents")
    candidate_command.add_argument("--limit", type=int, default=600, choices=range(1, 601))
    review_packet_command = subparsers.add_parser(
        "content-review-packet",
        help="export traceable source evidence and suggestions for human node review",
    )
    review_packet_command.add_argument("--output", required=True)
    review_packet_command.add_argument("--markdown")
    review_packet_command.add_argument("--subject", default="数学")
    review_packet_command.add_argument("--grade", default="高一")
    review_packet_command.add_argument("--textbook-version", default="人教A版")
    review_packet_command.add_argument("--chapter")
    review_packet_command.add_argument("--limit", type=int, default=600)
    review_apply_command = subparsers.add_parser(
        "content-review-apply",
        help="apply a completed human review packet with optimistic version checks",
    )
    review_apply_command.add_argument("--input", required=True)
    review_apply_command.add_argument("--reviewer", required=True)
    review_apply_command.add_argument("--publish", action="store_true")
    review_apply_command.add_argument("--dry-run", action="store_true")
    review_apply_command.add_argument("--confirmation")
    wikibooks_command = subparsers.add_parser("import-wikibooks")
    wikibooks_command.add_argument("--pages-per-topic", type=int, default=6, choices=range(1, 11))
    alert_command = subparsers.add_parser(
        "operational-alert-notify",
        help="send active model/OCR alerts to the configured webhook",
    )
    alert_command.add_argument("--dry-run", action="store_true")
    evidence_command = subparsers.add_parser(
        "release-evidence-templates",
        help="create non-passing templates for human and device release evidence",
    )
    evidence_command.add_argument("--output-dir", default="release-evidence")
    evidence_command.add_argument("--overwrite", action="store_true")
    readiness_command = subparsers.add_parser(
        "release-readiness-report",
        help="combine config, database, content and external evidence into one release gate",
    )
    readiness_command.add_argument("--evidence-dir")
    readiness_command.add_argument("--output")
    readiness_command.add_argument("--strict", action="store_true")
    qa_command = subparsers.add_parser("qa-console", help="interactive streaming AI QA tester")
    qa_command.add_argument("--mode", choices=("knowledge", "problem", "error", "review", "explore", "verify"), default="knowledge")
    qa_command.add_argument("--help-level", choices=("keyword", "next_step", "approach", "full", "conclusion"), default="approach")
    qa_command.add_argument("--limit", type=int, default=5, choices=range(1, 31))
    qa_command.add_argument("--subject", choices=("语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"))
    qa_command.add_argument("--question", help="执行一个问题后退出，便于脚本化体验")
    benchmark_command = subparsers.add_parser("qa-benchmark", help="run the local six-level QA benchmark")
    benchmark_command.add_argument("--output", default="qa-benchmark-report.json")
    benchmark_command.add_argument("--workers", type=int, default=4, choices=range(1, 9), help="并发题目数，建议不超过4以避免模型限流")
    eval_run_command = subparsers.add_parser(
        "qa-eval-run",
        help="generate answers and sources for a human-reviewed QA evaluation",
    )
    eval_run_command.add_argument("--dataset", required=True)
    eval_run_command.add_argument("--output", required=True)
    eval_run_command.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    eval_score_command = subparsers.add_parser(
        "qa-eval-score",
        help="calculate release metrics from a completed human review file",
    )
    eval_score_command.add_argument("--run", required=True)
    eval_score_command.add_argument("--output")
    eval_score_command.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="emit interim metrics while keeping the release gate failed",
    )
    history_command = subparsers.add_parser("history-qa-batch", help="8并发生成历史题答案，不进行评分")
    history_command.add_argument("--workers", type=int, default=8, choices=range(1, 9))
    history_command.add_argument("--output", default="history-qa-answers.json")
    history_command.add_argument("--markdown", default="history-qa-answers.md")
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.username, args.email)
    elif args.command == "import-documents":
        import_documents(args.path, args.authorization_status, args.title, args.subject)
    elif args.command == "import-curated-textbook":
        import_curated_textbooks(args.path)
    elif args.command == "preview-scanned-pdf-ocr":
        preview_scanned_pdf_ocr(args.path, args.start_page, args.end_page, args.dpi, args.tesseract_binary, args.engine)
    elif args.command == "import-scanned-pdf":
        import_scanned_pdf(args.path, args.title, args.subject, args.start_page, args.end_page, args.dpi, args.tesseract_binary, args.engine)
    elif args.command == "reindex-documents":
        reindex_all_documents()
    elif args.command == "build-vector-index":
        build_vector_index_file()
    elif args.command == "migrate-to-oss":
        migrate_knowledge_to_oss(workers=args.workers, dry_run=args.dry_run, update_source_uri=not args.keep_source_uri)
    elif args.command == "sync-vector-index-from-oss":
        sync_vector_index_from_oss(version=args.version, allow_stale=args.allow_stale)
    elif args.command == "verify-oss-storage":
        verify_knowledge_oss(workers=args.workers)
    elif args.command == "import-sqlite-knowledge":
        from app.services.sqlite_import import import_sqlite_knowledge

        import_sqlite_knowledge(args.snapshot, batch_size=max(1, args.batch_size))
    elif args.command == "backfill-document-metadata":
        backfill_metadata_file(args.limit)
    elif args.command == "extract-knowledge-graph":
        extract_graph_file(args.limit)
    elif args.command == "content-governance-report":
        content_governance_report_file(args.output)
    elif args.command == "materialize-launch-candidates":
        materialize_launch_candidates_file(args.limit)
    elif args.command == "content-review-packet":
        content_review_packet_file(
            args.output,
            args.markdown,
            subject=args.subject,
            grade=args.grade,
            textbook_version=args.textbook_version,
            chapter=args.chapter,
            limit=args.limit,
        )
    elif args.command == "content-review-apply":
        apply_content_review_packet_file(
            args.input,
            args.reviewer,
            publish=args.publish,
            dry_run=args.dry_run,
            confirmation=args.confirmation,
        )
    elif args.command == "import-wikibooks":
        import_wikibooks_content(args.pages_per_topic)
    elif args.command == "operational-alert-notify":
        notify_operational_alerts_file(dry_run=args.dry_run)
    elif args.command == "release-evidence-templates":
        release_evidence_templates_file(args.output_dir, overwrite=args.overwrite)
    elif args.command == "release-readiness-report":
        release_readiness_report_file(
            args.output,
            evidence_dir=args.evidence_dir,
            strict=args.strict,
        )
    elif args.command == "qa-console":
        from app.interactive_qa import ConsoleState, HELP_NAMES, MODE_NAMES, _run_question, run_console

        state = ConsoleState(
            mode=MODE_NAMES[args.mode],
            help_level=HELP_NAMES[args.help_level],
            limit=args.limit,
            subject=args.subject,
        )
        if args.question:
            _run_question(args.question, state)
            raise SystemExit(0)
        raise SystemExit(run_console(state))
    elif args.command == "qa-benchmark":
        from app.qa_benchmark import run_benchmark

        report = run_benchmark(Path(args.output).resolve(), workers=args.workers)
        print(f"QA benchmark: questions={report['question_count']}, workers={report['workers']}, accuracy={report['accuracy']:.1%}, score={report['score_100']:.2f}/100, elapsed={report['elapsed_seconds']:.1f}s")
    elif args.command == "qa-eval-run":
        from app.human_evaluation import run_human_evaluation

        report = run_human_evaluation(
            Path(args.dataset).resolve(),
            Path(args.output).resolve(),
            workers=args.workers,
        )
        print(
            f"Human evaluation run: dataset={report['dataset']['id']}, cases={report['case_count']}, "
            f"errors={report['execution_errors']}, workers={report['workers']}, output={args.output}"
        )
    elif args.command == "qa-eval-score":
        from app.human_evaluation import score_human_evaluation

        report = score_human_evaluation(
            Path(args.run).resolve(),
            Path(args.output).resolve() if args.output else None,
            allow_incomplete=args.allow_incomplete,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "history-qa-batch":
        from app.history_batch import run_history_batch

        report = run_history_batch(Path(args.output).resolve(), workers=args.workers, markdown_path=Path(args.markdown).resolve() if args.markdown else None)
        print(f"History QA batch: questions={report['question_count']}, workers={report['workers']}, elapsed={report['elapsed_seconds']:.1f}s, output={args.output}")


if __name__ == "__main__":
    main()
