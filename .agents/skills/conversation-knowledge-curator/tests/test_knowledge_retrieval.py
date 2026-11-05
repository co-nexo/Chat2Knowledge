"""内存关键词召回核心测试。

已覆盖的可观察行为：

1. 从英文技术词和连续中文生成稳定查询词。
2. 英文技术词按完整词匹配，避免 SQL 命中 NoSQL。
3. 路径、标题、标题层级和正文使用公开权重累计分数。
4. 无命中章节返回零分和空命中词。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"

SPEC = importlib.util.spec_from_file_location(
    "knowledge_retrieval",
    SCRIPTS_DIR / "knowledge_retrieval.py",
)
assert SPEC is not None and SPEC.loader is not None
knowledge_retrieval = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = knowledge_retrieval
SPEC.loader.exec_module(knowledge_retrieval)


class RetrievalCoreTests(unittest.TestCase):
    def test_query_terms_include_technical_words_and_chinese_bigrams(self) -> None:
        terms = knowledge_retrieval.query_terms(
            "SQL、NoSQL、C++、C#、.NET 与数据库事务"
        )

        self.assertIn("sql", terms)
        self.assertIn("nosql", terms)
        self.assertIn("c++", terms)
        self.assertIn("c#", terms)
        self.assertIn(".net", terms)
        self.assertIn("数据", terms)
        self.assertIn("事务", terms)

    def test_latin_terms_require_exact_token_matches(self) -> None:
        section = knowledge_retrieval.SearchSection(
            path="数据库/nosql.md",
            file_sha256="a" * 64,
            title="NoSQL",
            section_id="section-1",
            section_sha256="b" * 64,
            heading_path=("NoSQL",),
            order=0,
            markdown="面向非关系数据模型。",
        )

        score, matched = knowledge_retrieval.score_section(
            section,
            knowledge_retrieval.query_terms("SQL"),
        )

        self.assertEqual(score, 0)
        self.assertEqual(matched, [])

    def test_score_accumulates_public_field_weights(self) -> None:
        section = knowledge_retrieval.SearchSection(
            path="软件工程/开发方法/TDD.md",
            file_sha256="a" * 64,
            title="TDD",
            section_id="section-1",
            section_sha256="b" * 64,
            heading_path=("测试驱动开发", "红绿重构"),
            order=0,
            markdown="TDD 使用红、绿、重构循环。",
        )
        terms = knowledge_retrieval.query_terms("TDD 红绿重构")

        score, matched = knowledge_retrieval.score_section(section, terms)

        self.assertGreater(score, 0)
        self.assertIn("tdd", matched)
        self.assertIn("红绿", matched)

    def test_unmatched_section_returns_zero(self) -> None:
        section = knowledge_retrieval.SearchSection(
            path="数据库/事务.md",
            file_sha256="a" * 64,
            title="数据库事务",
            section_id="section-1",
            section_sha256="b" * 64,
            heading_path=("隔离级别",),
            order=0,
            markdown="说明事务隔离级别。",
        )

        score, matched = knowledge_retrieval.score_section(
            section,
            knowledge_retrieval.query_terms("TDD"),
        )

        self.assertEqual(score, 0)
        self.assertEqual(matched, [])


if __name__ == "__main__":
    unittest.main()
