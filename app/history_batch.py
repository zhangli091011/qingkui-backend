"""Concurrent history QA batch runner. This module writes answers only; it does not score them."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from app.db import SessionLocal
from app.models import HelpLevel, QaMode
from app.services.ai import answer_question, render_answer
from app.services.knowledge import retrieve_chunks, retrieve_nodes
from app.subjects import resolve_subject


QUESTIONS: tuple[str, ...] = (
    "请写出中国古代选官制度演变的完整脉络（从世官制到科举制），并标注每个主要阶段对应的朝代。",
    "简述《唐律疏议》的历史地位及其与罗马《十二铜表法》在性质上的根本区别。",
    "中国书法史上被誉为“天下第一行书”的作品是什么？其作者生活在哪个时期？",
    "在历史地理中，“关陇集团”主要活跃于哪个时期？其核心地域大致位于今天的哪个省份？",
    "鸦片战争爆发的根本原因是什么？直接原因（导火索）又是什么？",
    "“半殖民地半封建社会”中，“半殖民地”和“半封建”分别侧重指什么？",
    "北宋初年赵匡胤实行“强干弱枝”、“更戍法”的直接目的是什么？这一政策带来了什么长远负面影响？",
    "关于辛亥革命的成败，标准观点认为它“既成功又失败”，请简述其“成功”和“失败”的具体史实依据。",
    "阅读材料“（秦）焚之（诗书）而所谓经术者，非焚而绝之，特焚而掩之耳。”（清·皮锡瑞）。请问这里的“焚”指什么事件？作者认为儒家经典是否真的被断绝了？请结合所学说明理由。",
    "请描述北宋时期科举取士人数（进士）与唐代的对比变化趋势，并分析这一变化背后反映的社会阶层流动历史意义。",
    "有学者认为“李鸿章是近代中国外交的裱糊匠”，请结合甲午战争前后的史实，评价这一观点的合理性。",
    "17世纪的英国革命与18世纪末的法国大革命相比，两者在革命任务和对君主制的处理方式上有何显著不同？",
    "简述新航路开辟对中国明清时期经济（如白银流入、农作物引进）产生的具体影响。",
    "新文化运动时期，胡适等人提倡的“实验主义”深受西方哪一思想流派影响？中国知识界引入这一思想的初衷是什么？",
    "阅读材料“如果中国在14世纪复兴，那么停滞的欧洲将向崛起的中国学习；但中国在近代确实落后了。”请结合明清时期（14世纪-19世纪）的政治、经济、对外政策史实，评析这一观点中的“落后”原因。要求观点明确，逻辑清晰，至少列举三个维度。",
    "给出1920-1949年中国近代民族工业发展曲线图（波峰波谷），请分析哪两个时间段发展最快，并分别说明其内外驱动因素。",
    "中国古代乡村治理中，“乡约制度”起源于北宋哪位思想家？其核心内容通常围绕哪六个字（即“德业相劝”等）展开？",
    "“丝绸之路：长安—天山廊道的路网”被列入世界遗产。请说出这条路上两个重要的中国境内遗址（如城镇、石窟等）及其主要历史功能。",
    "中国古代四大发明中，哪一项在欧洲大航海时代发挥了最直接的决定性作用？它是通过什么路线传入欧洲的？",
)


@dataclass
class HistoryAnswer:
    number: int
    question: str
    subject: str | None
    elapsed_seconds: float
    nodes: int
    chunks: int
    provider: str | None
    model: str | None
    answer: str
    error: str | None = None


def _run_one(number: int, question: str) -> HistoryAnswer:
    started = time.perf_counter()
    try:
        subject = resolve_subject(question)
        with SessionLocal() as db:
            nodes = retrieve_nodes(db, question, pinned_node_id=None, limit=5, subject=subject)
            chunks = retrieve_chunks(db, question, limit=5, subject=subject)
        result = answer_question(question, QaMode.knowledge, HelpLevel.full, nodes, chunks)
        return HistoryAnswer(number, question, subject, round(time.perf_counter() - started, 3), len(nodes), len(chunks), result.provider, result.model, render_answer(result.answer))
    except Exception as exc:  # Keep the batch output complete if one request fails.
        return HistoryAnswer(number, question, None, round(time.perf_counter() - started, 3), 0, 0, None, None, "", f"{type(exc).__name__}: {exc}")


def run_history_batch(output_path: Path, *, workers: int = 8, markdown_path: Path | None = None) -> dict:
    started = time.perf_counter()
    workers = max(1, min(int(workers), len(QUESTIONS)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="history-qa") as executor:
        futures = [executor.submit(_run_one, number, question) for number, question in enumerate(QUESTIONS, start=1)]
        results = [future.result() for future in as_completed(futures)]
    results.sort(key=lambda item: item.number)
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "workers": workers,
        "question_count": len(results),
        "results": [asdict(result) for result in results],
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if markdown_path:
        lines = ["# 历史知识库并发问答结果", "", f"- 并发数：{workers}", f"- 题目数：{len(results)}", f"- 总耗时：{report['elapsed_seconds']} 秒", ""]
        for result in results:
            lines.extend([f"## {result.number}. {result.question}", "", result.answer or f"错误：{result.error}", ""])
        markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return report
