"""Subject catalog and fast, deterministic subject routing for knowledge retrieval."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock


SUBJECTS: tuple[str, ...] = ("语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理")

# These terms are intentionally conservative. Routing is used to narrow a database
# query, so a weak guess is worse than returning no knowledge context.
SUBJECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "数学": ("函数", "导数", "微分", "积分", "方程", "不等式", "概率", "统计", "随机变量", "期望", "方差", "正态分布", "二项分布", "向量", "几何", "三角", "诱导公式", "数列", "集合", "极限", "复数", "排列", "排列数", "组合", "组合数", "抛物线", "指数", "对数", "斜率", "二次", "面积", "体积", "坐标", "直线", "立体几何", "极值", "数学", "math", "calculus", "algebra", "geometry"),
    "语文": ("语文", "文言文", "古诗", "古文", "作文", "阅读理解", "成语", "修辞", "名著", "拼音", "病句", "文学", "诗歌", "散文"),
    "英语": ("英语", "英文", "单词", "词汇", "听力", "口语", "英语作文", "英译", "时态", "语法", "english", "grammar", "vocabulary", "reading comprehension"),
    "物理": ("物理", "牛顿", "力学", "速度", "加速度", "电场", "磁场", "电路", "电流", "电压", "动量", "机械能", "光学", "热力学", "波动", "物理学", "physics", "newton"),
    "化学": ("化学", "元素", "化合物", "原子", "分子", "离子", "酸碱", "氧化还原", "有机", "无机", "化学方程式", "摩尔", "电解质", "化学键", "chemistry", "molecule"),
    "生物": ("生物", "细胞", "dna", "rna", "基因", "遗传", "生态", "光合作用", "呼吸作用", "蛋白质", "酶", "免疫", "进化", "染色体", "biology", "photosynthesis"),
    "政治": ("政治", "思想政治", "哲学", "经济生活", "法律", "政治生活", "中国特色社会主义", "唯物", "价值观", "社会", "马克思主义", "politics"),
    "历史": ("历史", "朝代", "秦汉", "唐宋", "近代史", "选官制度", "世官制", "察举制", "九品中正制", "科举", "唐律疏议", "十二铜表法", "兰亭序", "王羲之", "关陇集团", "乡约", "吕氏乡约", "基层治理", "鸦片战争", "洋务运动", "戊戌变法", "辛亥革命", "五四运动", "太平天国", "抗日战争", "改革开放", "世界史", "资本主义", "革命史", "史料", "历史学", "history"),
    "地理": ("地理", "地球", "地图", "气候", "地形", "河流", "人口", "城市", "农业", "工业", "板块", "经纬度", "自然地理", "人文地理", "geography"),
}

# Short descriptions make the semantic boundary explicit without requiring a
# separately trained classifier. They are embedded once per process and reused.
SUBJECT_PROTOTYPES: dict[str, str] = {
    "语文": "中学语文：现代文阅读、文言文、古诗词、作文、文学常识、语言文字运用",
    "数学": "中学数学：代数、函数、方程、不等式、概率统计、随机变量、几何、导数与积分",
    "英语": "中学英语：单词词汇、语法、时态、阅读理解、听力、口语和英语写作",
    "物理": "中学物理：力学、运动、电磁学、电路、光学、热学、能量和动量",
    "化学": "中学化学：元素、原子分子、化学反应、酸碱盐、氧化还原、有机化学",
    "生物": "中学生物：细胞、遗传、基因、生态、进化、光合作用、人体生理",
    "政治": "中学政治：哲学、经济、法律、政治制度、思想政治和中国特色社会主义",
    "历史": "中学历史：中国史、世界史、朝代、战争、革命、改革、制度和历史材料分析",
    "地理": "中学地理：地球、地图、气候、地形、人口、城市、农业、工业和区域地理",
}


@dataclass(frozen=True)
class SubjectClassification:
    subject: str | None
    confidence: float
    margin: float
    source: str
    scores: dict[str, float]


_semantic_prototype_cache: dict[str, tuple[str, list[list[float]]]] = {}
_semantic_query_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_semantic_cache_lock = RLock()


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def classify_subject_semantic(text: str | None, client=None) -> SubjectClassification:
    """Classify by embedding similarity, returning scores for observability."""
    if not text or not text.strip():
        return SubjectClassification(None, 0.0, 0.0, "empty", {})
    try:
        from app.config import settings
        if settings.retrieval_provider != "bailian" or not settings.dashscope_api_key:
            raise RuntimeError("semantic classifier is not configured")
        if client is None:
            from app.services.bailian import BailianClient
            client = BailianClient(timeout_seconds=settings.dashscope_query_timeout_seconds)
        model = settings.dashscope_embedding_model
        with _semantic_cache_lock:
            prototype_entry = _semantic_prototype_cache.get(model)
        if prototype_entry is None:
            labels = list(SUBJECTS)
            vectors = client.embed([SUBJECT_PROTOTYPES[label] for label in labels])
            with _semantic_cache_lock:
                _semantic_prototype_cache[model] = ("|".join(labels), vectors)
        else:
            labels = prototype_entry[0].split("|")
            vectors = prototype_entry[1]
        key = (model, text.strip())
        with _semantic_cache_lock:
            query_vector = _semantic_query_cache.get(key)
        if query_vector is None:
            query_vector = client.embed([text.strip()])[0]
            with _semantic_cache_lock:
                _semantic_query_cache[key] = query_vector
                while len(_semantic_query_cache) > 256:
                    _semantic_query_cache.popitem(last=False)
        scores = {label: _cosine(query_vector, vector) for label, vector in zip(labels, vectors, strict=True)}
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_label, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best_score - second_score
        # A low margin is intentionally treated as ambiguous to prevent bad
        # subject filters from hiding useful context.
        subject = best_label if best_score >= 0.30 and margin >= 0.02 else None
        return SubjectClassification(subject, round(best_score, 6), round(margin, 6), "semantic", scores)
    except Exception:
        return SubjectClassification(None, 0.0, 0.0, "unavailable", {})


def resolve_subject(text: str | None, *, client=None) -> str | None:
    """Prefer semantic routing when configured, then use deterministic fallback."""
    semantic = classify_subject_semantic(text, client=client)
    if semantic.subject:
        return semantic.subject
    return infer_subject(text)


def normalize_subject(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    aliases = {"语文": "语文", "国语": "语文", "中文": "语文", "英语": "英语", "英文": "英语", "思政": "政治"}
    return aliases.get(value, value) if value in SUBJECTS or value in aliases else None


def infer_subject(text: str | None) -> str | None:
    """Return one high-confidence subject, or None when routing is ambiguous."""
    if not text:
        return None
    normalized = re.sub(r"\s+", "", text.lower())
    scores = {
        subject: sum((len(keyword) if len(keyword) > 1 else 1) for keyword in keywords if keyword in normalized)
        for subject, keywords in SUBJECT_KEYWORDS.items()
    }
    if any(symbol in normalized for symbol in ("∫", "∑", "√", "≤", "≥", "²", "³")):
        scores["数学"] += 5
    best = max(scores.values(), default=0)
    if best < 2:
        return None
    winners = [subject for subject, score in scores.items() if score == best]
    return winners[0] if len(winners) == 1 else None
