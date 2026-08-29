from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from app.db import SessionLocal
from app.models import HelpLevel, QaMode
from app.services.ai import AiStreamState, stream_answer_text
from app.services.knowledge import RetrievedChunk, retrieve_chunks, retrieve_nodes
from app.subjects import SUBJECTS, classify_subject_semantic, infer_subject, normalize_subject


MODE_NAMES = {mode.value: mode for mode in QaMode}
HELP_NAMES = {level.value: level for level in HelpLevel}


@dataclass
class ConsoleState:
    mode: QaMode
    help_level: HelpLevel
    limit: int
    show_sources: bool = True
    subject: str | None = None
    last_answer: str = ""


def _configure_console_encoding() -> None:
    """Keep Windows terminals from aborting on formulas or non-GBK output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _print_help() -> None:
    print(
        "命令：\n"
        "  /mode [knowledge|problem|error|review|explore|verify]  切换问答场景\n"
        "  /help [keyword|next_step|approach|full|conclusion]   切换帮助级别\n"
        "  /sources                                                  显示最近一轮引用\n"
        "  /subject [学科|auto]                                     设置或清除学科覆盖\n"
        "  /clear                                                    清除当前屏幕\n"
        "  /config                                                   显示当前配置\n"
        "  /quit                                                     退出\n"
        "直接输入问题即可开始流式回答。"
    )


def _print_config(state: ConsoleState) -> None:
    from app.config import settings

    print(
        f"mode={state.mode.value}, help_level={state.help_level.value}, "
        f"retrieval_limit={state.limit}, subject={state.subject or 'auto'}, "
        f"ai={settings.ai_provider}/{settings.deepseek_model if settings.ai_provider == 'deepseek' else 'grounded-stub'}, "
        f"retrieval={settings.retrieval_provider}/{settings.dashscope_embedding_model}"
    )


def _print_sources(nodes, chunks: list[RetrievedChunk]) -> None:
    if nodes:
        print("知识图谱节点：")
        for node in nodes:
            print(f"  - {node.name} [{node.id}]")
    if chunks:
        print("文档片段：")
        for item in chunks:
            chunk = item.chunk
            print(f"  - {chunk.document.title} · 第 {chunk.sequence + 1} 片段 · score={item.score:.4f}")
            print(f"    {chunk.content[:180].replace(chr(10), ' ')}")
    if not nodes and not chunks:
        print("本轮没有检索到知识库引用。")


def _run_question(question: str, state: ConsoleState) -> tuple[list, list[RetrievedChunk]]:
    _configure_console_encoding()
    started = time.perf_counter()
    state.last_answer = ""
    classification = classify_subject_semantic(question)
    subject = normalize_subject(state.subject) or classification.subject or infer_subject(question)
    if classification.source == "semantic":
        print(
            f"\n[学科] {subject or '未确定'} · semantic "
            f"confidence={classification.confidence:.3f}, margin={classification.margin:.3f}"
        )
    else:
        fallback_source = "keyword_fallback" if subject else classification.source
        print(f"\n[学科] {subject or '未确定'} · {fallback_source}")
    with SessionLocal() as db:
        nodes = retrieve_nodes(db, question, pinned_node_id=None, limit=5, subject=subject)
        chunks = retrieve_chunks(db, question, limit=state.limit, subject=subject)
        print(f"\n[检索] nodes={len(nodes)}, chunks={len(chunks)}")
        print("[回答] ", end="", flush=True)
        stream_state = AiStreamState()
        try:
            for delta in stream_answer_text(question, state.mode, state.help_level, nodes, chunks, stream_state):
                print(delta, end="", flush=True)
                state.last_answer += delta
        except KeyboardInterrupt:
            print("\n[中断] 已停止本轮输出。")
            return nodes, chunks
        except RuntimeError as exc:
            print(f"\n[错误] {exc}")
            return nodes, chunks
        print(f"\n\n[完成] provider={stream_state.provider}, model={stream_state.model}, elapsed={time.perf_counter() - started:.1f}s")
        if stream_state.input_tokens is not None or stream_state.output_tokens is not None:
            print(f"[用量] input_tokens={stream_state.input_tokens}, output_tokens={stream_state.output_tokens}")
        return nodes, chunks


def run_console(state: ConsoleState) -> int:
    _configure_console_encoding()
    last_sources: tuple[list, list[RetrievedChunk]] = ([], [])
    print("青葵 AI 问答流式测试器。输入 /help 查看命令，输入 /quit 退出。")
    _print_config(state)
    while True:
        try:
            question = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0
        if not question:
            continue
        if question in {"/quit", "/exit", ":q"}:
            print("已退出。")
            return 0
        if question in {"/help", ":help"}:
            _print_help()
            continue
        if question == "/config":
            _print_config(state)
            continue
        if question == "/sources":
            _print_sources(*last_sources)
            continue
        if question == "/clear":
            print("\033[2J\033[H", end="")
            continue
        if question.startswith("/subject"):
            value = question.partition(" ")[2].strip()
            if value.lower() in {"", "auto", "自动"}:
                state.subject = None
                print("已恢复自动语义学科分类。")
            else:
                normalized = normalize_subject(value)
                if normalized:
                    state.subject = normalized
                    print(f"已固定 subject={normalized}，输入 /subject auto 恢复自动分类。")
                else:
                    print(f"可选学科：{', '.join(SUBJECTS)}")
            continue
        if question.startswith("/mode"):
            value = question.partition(" ")[2].strip()
            if value in MODE_NAMES:
                state.mode = MODE_NAMES[value]
                print(f"已切换 mode={state.mode.value}")
            else:
                print(f"可选 mode：{', '.join(MODE_NAMES)}")
            continue
        if question.startswith("/help "):
            value = question.partition(" ")[2].strip()
            if value in HELP_NAMES:
                state.help_level = HELP_NAMES[value]
                print(f"已切换 help_level={state.help_level.value}")
            else:
                print(f"可选 help_level：{', '.join(HELP_NAMES)}")
            continue
        last_sources = _run_question(question, state)


def main() -> int:
    _configure_console_encoding()
    parser = argparse.ArgumentParser(description="Interactive streaming AI QA console")
    parser.add_argument("--mode", choices=tuple(MODE_NAMES), default=QaMode.knowledge.value)
    parser.add_argument("--help-level", choices=tuple(HELP_NAMES), default=HelpLevel.approach.value)
    parser.add_argument("--limit", type=int, default=5, choices=range(1, 31))
    parser.add_argument("--subject", choices=SUBJECTS, help="固定检索学科；不传则使用语义分类")
    parser.add_argument("--question", help="执行一个问题后退出，便于脚本化体验")
    args = parser.parse_args()
    state = ConsoleState(MODE_NAMES[args.mode], HELP_NAMES[args.help_level], args.limit, subject=args.subject)
    if args.question:
        _run_question(args.question, state)
        return 0
    return run_console(state)


if __name__ == "__main__":
    sys.exit(main())
