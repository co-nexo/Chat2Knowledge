"""第四阶段关键词召回测试。

已覆盖的输入校验行为：

1. 校验 knowledge candidates 和 inventory 的格式、字段与唯一标识。
2. 拒绝不安全相对路径、过期文件哈希和变化后的章节快照。
3. 失败时不创建输出，已存在输出不会被覆盖。

已覆盖的召回行为：

1. 从英文技术词和连续中文生成稳定查询词。
2. 英文和技术标识按完整词匹配，避免常见子串误召回。
3. 按路径、文档标题、标题路径和章节正文的公开权重排序。
4. 任一查询词命中即可召回，每个候选独立限制匹配数量。
5. 没有命中时保留空结果，默认最多保留 20 个匹配章节。
6. 输出保留定位、分数和命中词，但不复制候选陈述或 Markdown 正文。
7. 输入文件哈希和 inventory 警告会进入召回结果。

已覆盖的 CLI 行为：

1. 成功时写入召回文件并输出结构化汇总。
2. 继承 inventory 警告时返回退出码 1。
3. 输入失败时返回退出码 2 且不留下正式输出。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
INVENTORY_SCRIPT = SKILL_DIR / "scripts" / "knowledge_inventory.py"
RETRIEVAL_SCRIPT = SKILL_DIR / "scripts" / "knowledge_retrieval.py"
FIXTURE_ROOT = SKILL_DIR / "tests" / "fixtures" / "knowledge-base"

INVENTORY_SPEC = importlib.util.spec_from_file_location(
    "knowledge_inventory",
    INVENTORY_SCRIPT,
)
assert INVENTORY_SPEC is not None and INVENTORY_SPEC.loader is not None
knowledge_inventory = importlib.util.module_from_spec(INVENTORY_SPEC)
sys.modules[INVENTORY_SPEC.name] = knowledge_inventory
INVENTORY_SPEC.loader.exec_module(knowledge_inventory)

RETRIEVAL_SPEC = importlib.util.spec_from_file_location(
    "knowledge_retrieval",
    RETRIEVAL_SCRIPT,
)
assert RETRIEVAL_SPEC is not None and RETRIEVAL_SPEC.loader is not None
knowledge_retrieval = importlib.util.module_from_spec(RETRIEVAL_SPEC)
sys.modules[RETRIEVAL_SPEC.name] = knowledge_retrieval
RETRIEVAL_SPEC.loader.exec_module(knowledge_retrieval)

SOURCE_SHA256 = "a" * 64
CANDIDATE_JETBRAINS = "kc_" + "1" * 20
CANDIDATE_PYTHON = "kc_" + "2" * 20
CANDIDATE_MISSING = "kc_" + "3" * 20


def write_json(path: Path, value: object) -> None:
    """写入稳定、可读且保留中文的测试 JSON。"""

    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def candidate(
    candidate_id: str,
    statement: str,
    flags: list[str] | None = None,
) -> dict[str, object]:
    """构造满足第三阶段公开契约的最小知识候选。"""

    return {
        "candidate_id": candidate_id,
        "statement": statement,
        "evidence": [
            {
                "source_sha256": SOURCE_SHA256,
                "conversation_id": "conversation-1",
                "message_ids": ["message-1"],
            }
        ],
        "flags": flags or [],
    }


def candidates_document() -> dict[str, object]:
    """构造包含有命中和无命中情况的候选文档。"""

    return {
        "format": "conversation-knowledge-curator.knowledge-candidates.v1",
        "candidates": [
            candidate(
                CANDIDATE_JETBRAINS,
                "项目说明应兼容 JetBrains 系列 IDE。",
            ),
            candidate(CANDIDATE_PYTHON, "Python 开发知识。"),
            candidate(CANDIDATE_MISSING, "量子香蕉协议。"),
        ],
    }


class RetrievalInputTests(unittest.TestCase):
    def test_invalid_candidate_format_is_rejected(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        candidates = candidates_document()
        candidates["format"] = "unsupported"

        with self.assertRaises(knowledge_retrieval.RetrievalError):
            knowledge_retrieval.build_retrieval(
                candidates,
                "b" * 64,
                inventory,
                "c" * 64,
                FIXTURE_ROOT,
                5,
            )

    def test_malformed_or_duplicate_candidate_ids_are_rejected(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        malformed = candidates_document()
        malformed["candidates"][0]["candidate_id"] = "invalid"
        duplicate = candidates_document()
        duplicate["candidates"][1]["candidate_id"] = CANDIDATE_JETBRAINS

        for document in (malformed, duplicate):
            with self.subTest(document=document), self.assertRaises(
                knowledge_retrieval.RetrievalError
            ):
                knowledge_retrieval.build_retrieval(
                    document,
                    "b" * 64,
                    inventory,
                    "c" * 64,
                    FIXTURE_ROOT,
                    5,
                )

    def test_unsafe_inventory_path_is_rejected(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        inventory["files"][0]["path"] = "../outside.md"

        with self.assertRaises(knowledge_retrieval.RetrievalError):
            knowledge_retrieval.build_retrieval(
                candidates_document(),
                "b" * 64,
                inventory,
                "c" * 64,
                FIXTURE_ROOT,
                5,
            )

    def test_unsafe_inventory_warning_path_is_rejected(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        inventory["status"] = "completed_with_issues"
        inventory["warnings"] = [
            {
                "code": "link_skipped",
                "path": "../private",
                "message": "synthetic warning",
            }
        ]

        with self.assertRaises(knowledge_retrieval.RetrievalError):
            knowledge_retrieval.build_retrieval(
                candidates_document(),
                "b" * 64,
                inventory,
                "c" * 64,
                FIXTURE_ROOT,
                5,
            )

    def test_changed_markdown_rejects_stale_inventory_without_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root = root / "knowledge"
            shutil.copytree(FIXTURE_ROOT, knowledge_root)
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            output_path = root / "retrieval.json"
            write_json(
                inventory_path,
                knowledge_inventory.build_inventory(knowledge_root),
            )
            write_json(candidates_path, candidates_document())
            target = knowledge_root / "technologies" / "python.md"
            target.write_text("# Changed\n", encoding="utf-8")

            with self.assertRaises(knowledge_retrieval.RetrievalError):
                knowledge_retrieval.create_retrieval_file(
                    knowledge_root,
                    inventory_path,
                    candidates_path,
                    output_path,
                    5,
                )

            self.assertFalse(output_path.exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            output_path = root / "retrieval.json"
            write_json(
                inventory_path,
                knowledge_inventory.build_inventory(FIXTURE_ROOT),
            )
            write_json(candidates_path, candidates_document())
            output_path.write_text("keep", encoding="utf-8")

            with self.assertRaises(knowledge_retrieval.RetrievalError):
                knowledge_retrieval.create_retrieval_file(
                    FIXTURE_ROOT,
                    inventory_path,
                    candidates_path,
                    output_path,
                    5,
                )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep")


class RetrievalRankingTests(unittest.TestCase):
    def test_query_terms_include_latin_words_and_chinese_bigrams(self) -> None:
        terms = knowledge_retrieval.query_terms(
            "兼容 JetBrains 系列 IDE、C++、C#、.NET 和 B+Tree"
        )

        self.assertIn("jetbrains", terms)
        self.assertIn("ide", terms)
        self.assertIn("c++", terms)
        self.assertIn("c#", terms)
        self.assertIn(".net", terms)
        self.assertIn("b+tree", terms)
        self.assertIn("兼容", terms)
        self.assertIn("系列", terms)
        self.assertEqual(terms, sorted(set(terms)))

    def test_latin_terms_require_exact_token_matches(self) -> None:
        section = knowledge_retrieval.SearchSection(
            path="notes/nosql.md",
            file_sha256="a" * 64,
            title="NoSQL and MongoDB",
            section_id="ks_" + "1" * 20,
            section_sha256="b" * 64,
            heading_path=("Indexing",),
            order=0,
            markdown="NoSQL MongoDB indexing",
        )

        score, matched = knowledge_retrieval.score_section(
            section,
            ["sql", "go", "index"],
        )

        self.assertEqual(score, 0)
        self.assertEqual(matched, [])

    def test_retrieval_ranks_relevant_sections_and_keeps_empty_results(self) -> None:
        result = knowledge_retrieval.build_retrieval(
            candidates_document(),
            "b" * 64,
            knowledge_inventory.build_inventory(FIXTURE_ROOT),
            "c" * 64,
            FIXTURE_ROOT,
            5,
        )

        by_id = {item["candidate_id"]: item for item in result["results"]}
        jetbrains_match = by_id[CANDIDATE_JETBRAINS]["matches"][0]
        self.assertEqual(
            jetbrains_match["path"],
            "preferences/development-tools.md",
        )
        self.assertIn("JetBrains", jetbrains_match["heading_path"])
        python_match = by_id[CANDIDATE_PYTHON]["matches"][0]
        self.assertEqual(python_match["path"], "technologies/python.md")
        self.assertEqual(by_id[CANDIDATE_MISSING]["matches"], [])

    def test_limit_is_applied_per_candidate_and_output_is_stable(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)

        first = knowledge_retrieval.build_retrieval(
            candidates_document(),
            "b" * 64,
            inventory,
            "c" * 64,
            FIXTURE_ROOT,
            1,
        )
        second = knowledge_retrieval.build_retrieval(
            candidates_document(),
            "b" * 64,
            inventory,
            "c" * 64,
            FIXTURE_ROOT,
            1,
        )

        self.assertEqual(first, second)
        self.assertTrue(
            all(len(item["matches"]) <= 1 for item in first["results"])
        )

    def test_output_does_not_copy_statements_or_markdown_body(self) -> None:
        result = knowledge_retrieval.build_retrieval(
            candidates_document(),
            "b" * 64,
            knowledge_inventory.build_inventory(FIXTURE_ROOT),
            "c" * 64,
            FIXTURE_ROOT,
            5,
        )

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("项目说明应兼容 JetBrains 系列 IDE", serialized)
        self.assertNotIn("Python 开发优先使用 PyCharm", serialized)
        self.assertIn("matched_terms", serialized)

    def test_inventory_warnings_are_preserved(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        inventory["status"] = "completed_with_issues"
        inventory["warnings"] = [
            {
                "code": "link_skipped",
                "path": "linked",
                "message": "synthetic warning",
            }
        ]

        result = knowledge_retrieval.build_retrieval(
            candidates_document(),
            "b" * 64,
            inventory,
            "c" * 64,
            FIXTURE_ROOT,
            5,
        )

        self.assertEqual(result["status"], "completed_with_issues")
        self.assertEqual(result["warnings"], inventory["warnings"])


class RetrievalCliTests(unittest.TestCase):
    def test_cli_writes_output_and_reports_input_hashes(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            output_path = root / "retrieval.json"
            write_json(
                inventory_path,
                knowledge_inventory.build_inventory(FIXTURE_ROOT),
            )
            write_json(candidates_path, candidates_document())
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_retrieval.main(
                    [
                        "retrieve",
                        str(FIXTURE_ROOT),
                        str(inventory_path),
                        str(candidates_path),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            summary = json.loads(stdout.getvalue())
            output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["candidate_count"], 3)
            self.assertEqual(
                output["format"],
                "conversation-knowledge-curator.knowledge-retrieval.v2",
            )
            self.assertEqual(output["parameters"], {"limit": 20})
            self.assertEqual(
                output["inputs"]["knowledge_candidates_sha256"],
                hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                output["inputs"]["knowledge_inventory_sha256"],
                hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
            )

    def test_cli_returns_one_when_inventory_contains_warnings(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
            inventory["status"] = "completed_with_issues"
            inventory["warnings"] = [
                {
                    "code": "link_skipped",
                    "path": "linked",
                    "message": "synthetic warning",
                }
            ]
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            output_path = root / "retrieval.json"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates_document())
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = knowledge_retrieval.main(
                    [
                        "retrieve",
                        str(FIXTURE_ROOT),
                        str(inventory_path),
                        str(candidates_path),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(
                json.loads(stdout.getvalue())["status"],
                "completed_with_issues",
            )

    def test_cli_failure_returns_two_without_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            output_path = root / "retrieval.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_retrieval.main(
                    [
                        "retrieve",
                        str(FIXTURE_ROOT),
                        str(root / "missing-inventory.json"),
                        str(root / "missing-candidates.json"),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue().strip())
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
