"""Repeatable local QA benchmark for the six-level mathematics test set."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.db import SessionLocal
from app.models import HelpLevel, QaMode
from app.services.ai import answer_question, render_answer
from app.services.knowledge import retrieve_chunks, retrieve_nodes
from app.subjects import resolve_subject


@dataclass(frozen=True)
class BenchmarkCase:
    number: int
    question: str
    groups: tuple[tuple[str, ...], ...]
    contradiction: tuple[str, ...] = ()


@dataclass
class BenchmarkResult:
    number: int
    question: str
    subject: str | None
    elapsed_seconds: float
    nodes: int
    chunks: int
    score: float
    matched_groups: int
    total_groups: int
    answer: str
    error: str | None = None


CASES = (
    BenchmarkCase(1, r"请写出函数 f(x)=log_a(x) 的导数公式，并注明 a 的取值范围。", (("1/(x", "\\frac{1}{x", "x ln a"), ("a>0", "a > 0"), ("a≠1", "a != 1", "a不等于1"), ("x>0", "x > 0"))),
    BenchmarkCase(2, "请完整叙述正弦定理的表达式，包括边角关系和比例常数 2R 的含义。", (("sin", "正弦"), ("2r", "2R", "2r"), ("外接圆", "外接圆半径"))),
    BenchmarkCase(3, "请给出空间向量中两点 A(x1,y1,z1) 与 B(x2,y2,z2) 的距离公式，以及中点坐标公式。", (("sqrt", "根号"), ("x2-x1", "x_2-x_1"), ("(x1+x2)/2", "中点"))),
    BenchmarkCase(4, "什么是零向量？它与其他向量的关系是什么？它是否有方向？", (("零向量", "0"), ("没有方向", "方向不确定", "无方向"), ("加法单位", "任意向量", "平行"))),
    BenchmarkCase(5, "已知集合 A={x|ax^2-2x+1=0} 有且仅有一个元素，求实数 a。", (("a=0", "a = 0"), ("a=1", "a = 1"), ("判别式", "delta", "\\Delta"))),
    BenchmarkCase(6, "求函数 y=x+1/x 的值域，并说明是否需要限定 x>0。", (("x>0", "定义域", "x<0"), ("2", "-2"), ("(-∞,-2]", "[2,+∞)", "并集"))),
    BenchmarkCase(7, "数列前 n 项和 S_n=n^2+n+1，求通项公式 a_n，并验证 n=1。", (("a_1=3", "a1=3"), ("2n", "2*n"), ("n≥2", "n >= 2", "分段"))),
    BenchmarkCase(8, "在回归分析中，相关指数 R^2 越接近 1，模型拟合效果越好还是越差？", (("越好", "较好"), ("拟合", "解释"))),
    BenchmarkCase(9, "已知 sinα+cosα=1/5，α∈(0,π)，求 tanα，并展示如何判断象限舍去增根。", (("-4/3", "-\\frac{4}{3}"), ("第二象限", "第二象限"), ("平方", "sin cos", "增根"))),
    BenchmarkCase(10, "在正方体 ABCD-A1B1C1D1 中，求异面直线 A1B 与 B1C 所成角的余弦值。", (("1/2", "\\frac{1}{2}"), ("方向向量", "坐标系"), ("点积", "内积"))),
    BenchmarkCase(11, "求 f(x)=ln(x)/x 在 [1,e] 上的最大值和最小值。", (("1/e", "1/e"), ("0", "f(1)"), ("1-ln x", "导数", "单调"))),
    BenchmarkCase(12, "数列 a_(n+1)=2a_n+1，a_1=1。求通项并证明 1/a_1+...+1/a_n<1。", (("2^n-1", "2^n - 1"), ("a_1=1", "首项"), ("不能", "不成立", "错误", "≥1", ">=1")), contradiction=("<1", "小于1")),
    BenchmarkCase(13, "三角形中 AB·AC=3，a=√13，求面积最大值。", (("5√13/4", "5\\sqrt{13}/4", "\\frac{5\\sqrt{13}}{4}"), ("b=c", "等腰"), ("19", "b^2+c^2"))),
    BenchmarkCase(14, "点 P 在 y=e^x 上，点 Q 在 y=ln x 上，求 |PQ| 最小值，提示互为反函数对称性。", (("√2", "\\sqrt{2}"), ("对称", "y=x"), ("(0,1)", "(1,0"))),
    BenchmarkCase(15, "判断 f(x)=sin2x+√3cos2x 的说法：A周期π；B关于 x=π/6 对称；C在[-π/3,0]递增；D由 2sin2x 左移 π/6 得到。", (("a", "A"), ("c", "C"), ("d", "D"), ("b错误", "B错误", "B不正确"))),
    BenchmarkCase(16, "甲胜率2/3，比较3局2胜和5局3胜哪种赛制对甲有利，并计算概率。", (("20/27", "20 / 27"), ("64/81", "64 / 81"), ("5局", "5 局", "五局"))),
    BenchmarkCase(17, "参数方程 x=cosθ+1，y=sinθ-2 化为普通方程并说明图形。", (("(x-1)^2", "(x - 1)^2"), ("(y+2)^2", "(y + 2)^2"), ("圆", "半径1", "半径为1"))),
    BenchmarkCase(18, "复数 z=2i/(1+i) 的共轭复数的虚部是多少？", (("-1", "负1"), ("1-i", "1 - i"), ("共轭", "虚部"))),
    BenchmarkCase(19, "4个不同小球放入3个不同盒子，每盒至少一个，共有多少种放法？", (("36", "三十六"), ("容斥", "先分组", "surjection"), ("每盒", "至少一个"))),
)


_SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def _normalize_answer(value: str) -> str:
    value = value.replace("²", "^2").replace("³", "^3")
    value = unicodedata.normalize("NFKC", value).translate(_SUBSCRIPT_TRANSLATION)
    value = value.replace("−", "-").replace("–", "-")
    value = value.lower().replace("\\", "").replace(" ", "")
    return value


def _matched(answer: str, groups: tuple[tuple[str, ...], ...]) -> int:
    normalized = _normalize_answer(answer)
    return sum(1 for group in groups if any(_normalize_answer(token) in normalized for token in group))


def _run_case(case: BenchmarkCase) -> BenchmarkResult:
    item_started = time.perf_counter()
    try:
        subject = resolve_subject(case.question)
        with SessionLocal() as db:
            nodes = retrieve_nodes(db, case.question, pinned_node_id=None, limit=5, subject=subject)
            chunks = retrieve_chunks(db, case.question, limit=5, subject=subject)
        ai_result = answer_question(case.question, QaMode.knowledge, HelpLevel.approach, nodes, chunks)
        answer = render_answer(ai_result.answer)
        matched = _matched(answer, case.groups)
        return BenchmarkResult(case.number, case.question, subject, round(time.perf_counter() - item_started, 3), len(nodes), len(chunks), round(matched / len(case.groups), 3), matched, len(case.groups), answer)
    except Exception as exc:  # Keep the report complete when one provider call fails.
        return BenchmarkResult(case.number, case.question, None, round(time.perf_counter() - item_started, 3), 0, 0, 0.0, 0, len(case.groups), "", f"{type(exc).__name__}: {exc}")


def run_benchmark(output_path: Path | None = None, *, workers: int = 1) -> dict:
    results: list[BenchmarkResult] = []
    started = time.perf_counter()
    workers = max(1, min(int(workers), len(CASES)))
    if workers == 1:
        results = [_run_case(case) for case in CASES]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qa-benchmark") as executor:
            futures = [executor.submit(_run_case, case) for case in CASES]
            results = [future.result() for future in as_completed(futures)]
        results.sort(key=lambda item: item.number)
    total_groups = sum(item.total_groups for item in results)
    matched_groups = sum(item.matched_groups for item in results)
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "question_count": len(results),
        "workers": workers,
        "accuracy": round(matched_groups / total_groups, 4) if total_groups else 0.0,
        "score_100": round(100 * matched_groups / total_groups, 2) if total_groups else 0.0,
        "results": [asdict(item) for item in results],
    }
    if output_path:
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
