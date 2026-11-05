#!/usr/bin/env python3
"""供统一运行时执行内存关键词召回的确定性内部库。"""

from __future__ import annotations

import re
from dataclasses import dataclass

LATIN_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\.[A-Za-z][A-Za-z0-9]*|"
    r"[A-Za-z0-9]+(?:\+[A-Za-z0-9]+)+|"
    r"[A-Za-z0-9]+(?:\+\+|#)?"
    r")"
)
CJK_TERM_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class RetrievalError(Exception):
    """表示内存召回输入无法安全处理。"""


@dataclass(frozen=True)
class SearchSection:
    """保存一个已通过 inventory 哈希校验的可搜索章节。"""

    path: str
    file_sha256: str
    title: str
    section_id: str
    section_sha256: str
    heading_path: tuple[str, ...]
    order: int
    markdown: str


def query_terms(statement: str) -> list[str]:
    """从候选陈述提取稳定的英文技术词和中文相邻双字词。"""

    terms = {
        match.group(0).casefold()
        for match in LATIN_TERM_PATTERN.finditer(statement)
    }
    for match in CJK_TERM_PATTERN.finditer(statement):
        value = match.group(0)
        if len(value) >= 2:
            terms.update(value[index : index + 2] for index in range(len(value) - 1))
    return sorted(term for term in terms if len(term) >= 2)


def score_section(
    section: SearchSection,
    terms: list[str],
) -> tuple[int, list[str]]:
    """按完整词命中和公开权重计算一个章节的字面召回分数。"""

    # 查询和被检索字段使用同一套词项规则，避免 SQL 命中 NoSQL 等子串误判。
    path_terms = set(query_terms(section.path))
    title_terms = set(query_terms(section.title))
    heading_terms = set(query_terms(" ".join(section.heading_path)))
    body_terms = set(query_terms(section.markdown))
    score = 0
    matched: list[str] = []
    for term in terms:
        term_score = 0
        if term in path_terms:
            term_score += 8
        if term in title_terms:
            term_score += 6
        if term in heading_terms:
            term_score += 5
        if term in body_terms:
            term_score += 1
        if term_score:
            score += term_score
            matched.append(term)
    return score, matched
