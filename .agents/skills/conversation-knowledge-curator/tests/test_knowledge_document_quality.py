"""知识文档确定性质量检查测试。

已覆盖的结构检查行为：

1. 严格模式接受单一 H1、连续编号的 H2 到 H4 和认知顺序明确的章节骨架。
2. 严格模式拒绝缺少 H1、标题跳级、未编号及编号不连续的文档。
3. 兼容模式允许既有文档保留未编号章节，但仍拒绝 H1 和层级结构损坏。
4. 代码围栏中的井号不会被误判为知识文档标题。

"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"


def load_script(name: str, path: Path) -> object:
    """按脚本真实导入名加载质量检查依赖。"""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_script("knowledge_inventory", SCRIPTS_DIR / "knowledge_inventory.py")
knowledge_document_quality = load_script(
    "knowledge_document_quality",
    SCRIPTS_DIR / "knowledge_document_quality.py",
)


class DocumentStructureTests(unittest.TestCase):
    def test_strict_mode_accepts_numbered_hierarchy(self) -> None:
        markdown = (
            "# 合成数据库对比\n\n"
            "本文说明两个合成系统的结构差异。\n\n"
            "## 1. 背景与比较范围\n\n正文。\n\n"
            "### 1.1 核心概念\n\n正文。\n\n"
            "#### 1.1.1 机制边界\n\n正文。\n\n"
            "## 2. 实际影响\n\n正文。\n"
        )

        report = knowledge_document_quality.analyze_document(
            markdown,
            "strict",
            "synthetic.md",
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["issues"], [])

    def test_strict_mode_rejects_missing_number_and_sequence_gap(self) -> None:
        markdown = (
            "# 合成主题\n\n"
            "## 背景\n\n正文。\n\n"
            "### 1.2 跳过首个子章节\n\n正文。\n\n"
            "## 3. 跳过第二章\n\n正文。\n"
        )

        report = knowledge_document_quality.analyze_document(
            markdown,
            "strict",
            "synthetic.md",
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            {item["code"] for item in report["issues"]},
            {"invalid_heading_number", "nonsequential_heading_number"},
        )

    def test_compatible_mode_preserves_legacy_numbering_but_checks_hierarchy(
        self,
    ) -> None:
        accepted = "说明。\n\n# 旧文档\n\n## 未编号章节\n\n正文。\n"
        rejected = "## 缺少主标题\n\n#### 跳级章节\n"

        accepted_report = knowledge_document_quality.analyze_document(
            accepted,
            "compatible",
            "legacy.md",
        )
        rejected_report = knowledge_document_quality.analyze_document(
            rejected,
            "compatible",
            "broken.md",
        )

        self.assertEqual(accepted_report["status"], "ok")
        self.assertEqual(rejected_report["status"], "failed")
        self.assertIn(
            "first_heading_not_h1",
            {item["code"] for item in rejected_report["issues"]},
        )
        self.assertIn(
            "heading_level_jump",
            {item["code"] for item in rejected_report["issues"]},
        )

    def test_fenced_code_headings_are_ignored(self) -> None:
        markdown = (
            "# 合成主题\n\n"
            "```markdown\n"
            "#### 这不是标题\n"
            "```\n\n"
            "## 1. 正式章节\n\n正文。\n"
        )

        report = knowledge_document_quality.analyze_document(
            markdown,
            "strict",
            "synthetic.md",
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["summary"]["heading_count"], 2)


if __name__ == "__main__":
    unittest.main()
