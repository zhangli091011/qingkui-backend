"""Map curriculum chapters onto textbook volumes (课本分册).

The knowledge base stores subject / grade / textbook_version / chapter but has no
explicit volume column, so navigation shows a volume derived from the chapter
number. Only curricula we can verify against the source material are mapped;
everything else falls back to the textbook version label.

人教A版 (2019) 高中数学:
    必修第一册  第 1-5 章
    必修第二册  第 6-10 章

Chapters imported from source documents sometimes carry no number at all
("平面向量及其应用 章末题型大总结"), so known chapter titles are matched by
keyword as well.
"""

from __future__ import annotations

import re

_CHAPTER_NUMBER = re.compile(r"^\s*(\d+)")

_RENJIAO_A_REQUIRED: tuple[tuple[int, int, str], ...] = (
    (1, 5, "必修一"),
    (6, 10, "必修二"),
)

# Chapter titles that carry no leading number but belong to a known volume.
# 人教A版 (2019) 必修第一册 / 必修第二册 章标题.
_RENJIAO_A_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("必修第一册", "必修一"),
    ("必修一", "必修一"),
    ("集合与常用逻辑用语", "必修一"),
    ("一元二次函数", "必修一"),
    ("函数的概念与性质", "必修一"),
    ("指数函数", "必修一"),
    ("对数函数", "必修一"),
    ("三角函数", "必修一"),
    ("必修第二册", "必修二"),
    ("必修二", "必修二"),
    ("平面向量", "必修二"),
    ("三角形", "必修二"),
    ("向量", "必修二"),
    ("复数", "必修二"),
    ("立体几何", "必修二"),
    ("统计", "必修二"),
    ("概率", "必修二"),
    ("函数的应用", "必修一"),
)


def chapter_number(chapter: str | None) -> int | None:
    """Return the leading chapter number of a chapter label, e.g. "3.1.1" -> 3."""

    if not chapter:
        return None
    match = _CHAPTER_NUMBER.match(chapter)
    return int(match.group(1)) if match else None


def infer_volume(subject: str | None, textbook_version: str | None, chapter: str | None) -> str | None:
    """Return the textbook volume label, or None when the mapping is unknown."""

    if subject != "数学":
        return None
    label = (chapter or "").strip()
    version = (textbook_version or "").strip()
    if "人教A" not in version:
        return None
    for keyword, volume in _RENJIAO_A_KEYWORDS:
        if keyword in label:
            return volume
    number = chapter_number(chapter)
    if number is None:
        return None
    for low, high, volume in _RENJIAO_A_REQUIRED:
        if low <= number <= high:
            return volume
    return None


def volume_label(subject: str | None, textbook_version: str | None, chapter: str | None) -> str:
    """Return the volume label for a chapter, falling back to the textbook version."""

    return infer_volume(subject, textbook_version, chapter) or (textbook_version or "").strip()
