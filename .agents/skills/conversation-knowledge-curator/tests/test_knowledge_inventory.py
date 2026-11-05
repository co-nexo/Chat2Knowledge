"""第四阶段只读知识目录测试。

已覆盖的 Markdown 解析行为：

1. 忽略代码围栏中的伪标题，并识别围栏后的真实 ATX 标题。
2. 章节哈希只包含当前标题和直属正文，不包含子章节。
3. 为标题前正文建立根级章节，并用 ordinal 区分重复标题路径。

已覆盖的 inventory 构建行为：

1. 相同知识库生成稳定结果，顶层字段符合契约且不复制正文。
2. 稳定记录相对路径、文档标题、标题路径和重复标题序号。
3. 文件哈希基于原始字节；章节哈希保留 CRLF 但排除 UTF-8 BOM。
4. 忽略非 Markdown、`.git` 和 `.knowledge-curator` 内容。
5. 被识别的链接会跳过并形成结构化警告，不进入扫描结果。
6. 在系统允许时使用真实目录链接验证不会跟随到知识库外部。

"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPT_PATH = SKILL_DIR / "scripts" / "knowledge_inventory.py"
FIXTURE_ROOT = SKILL_DIR / "tests" / "fixtures" / "knowledge-base"

SPEC = importlib.util.spec_from_file_location("knowledge_inventory", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
knowledge_inventory = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = knowledge_inventory
SPEC.loader.exec_module(knowledge_inventory)


def create_directory_symlink_or_skip(
    test_case: unittest.TestCase,
    link: Path,
    target: Path,
) -> None:
    """创建真实目录链接；当前系统不允许时将相关测试标记为跳过。"""

    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        test_case.skipTest("当前系统无法创建目录符号链接")


class MarkdownSectionTests(unittest.TestCase):
    def test_headings_in_fences_are_ignored(self) -> None:
        text = (
            "# 文档\n\n"
            "```python\n"
            "## 代码中的伪标题\n"
            "```\n\n"
            "## 真实标题 ##\n"
        )

        headings = knowledge_inventory.parse_headings(text)

        self.assertEqual(
            [(heading.level, heading.text) for heading in headings],
            [(1, "文档"), (2, "真实标题")],
        )

    def test_sections_exclude_child_sections(self) -> None:
        text = "# 父级\n\n父级正文。\n\n## 子级\n\n子级正文。\n"

        sections = knowledge_inventory.parse_sections(text, "topic.md")

        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["heading_path"], ["父级"])
        self.assertEqual(
            sections[0]["sha256"],
            hashlib.sha256("# 父级\n\n父级正文。\n\n".encode()).hexdigest(),
        )
        self.assertEqual(sections[1]["heading_path"], ["父级", "子级"])

    def test_root_content_and_duplicate_headings_are_distinguished(self) -> None:
        text = "前言。\n\n# 文档\n\n## 重复\n\nA\n\n## 重复\n\nB\n"

        sections = knowledge_inventory.parse_sections(text, "topic.md")

        self.assertEqual(sections[0]["heading_path"], [])
        duplicates = [
            section for section in sections if section["heading"] == "重复"
        ]
        self.assertEqual(
            [section["ordinal"] for section in duplicates],
            [0, 1],
        )
        self.assertNotEqual(
            duplicates[0]["section_id"],
            duplicates[1]["section_id"],
        )


class InventoryBuildTests(unittest.TestCase):
    def test_fixture_inventory_is_stable_and_does_not_copy_body(self) -> None:
        first = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        second = knowledge_inventory.build_inventory(FIXTURE_ROOT)

        self.assertEqual(first, second)
        self.assertEqual(
            first["format"],
            "conversation-knowledge-curator.knowledge-inventory.v1",
        )
        self.assertEqual(first["status"], "ok")
        self.assertEqual(
            set(first),
            {"format", "status", "files", "warnings"},
        )
        self.assertEqual(len(first["files"]), 3)
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("Python 开发优先使用 PyCharm", serialized)
        self.assertNotIn("print(\"synthetic\")", serialized)

    def test_fixture_records_titles_paths_and_duplicate_ordinals(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        entries = {entry["path"]: entry for entry in inventory["files"]}

        self.assertEqual(
            list(entries),
            [
                "preferences/development-tools.md",
                "projects/knowledge-curator.md",
                "technologies/python.md",
            ],
        )
        self.assertEqual(
            entries["preferences/development-tools.md"]["title"],
            "开发工具偏好",
        )
        self.assertEqual(
            entries["projects/knowledge-curator.md"]["title"],
            "knowledge-curator",
        )
        duplicate_sections = [
            section
            for section in entries["preferences/development-tools.md"][
                "sections"
            ]
            if section["heading"] == "默认 IDE"
        ]
        self.assertEqual(
            [section["ordinal"] for section in duplicate_sections],
            [0, 1],
        )

    def test_file_hash_uses_original_bytes(self) -> None:
        path = FIXTURE_ROOT / "technologies" / "python.md"
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        entry = next(
            item
            for item in inventory["files"]
            if item["path"] == "technologies/python.md"
        )

        self.assertEqual(
            entry["sha256"],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_bom_and_crlf_follow_file_and_section_hash_contracts(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            raw_content = (
                b"\xef\xbb\xbf# Windows Markdown\r\n\r\nBody.\r\n"
            )
            path = root / "windows.md"
            path.write_bytes(raw_content)

            inventory = knowledge_inventory.build_inventory(root)

            entry = inventory["files"][0]
            self.assertEqual(entry["title"], "Windows Markdown")
            self.assertEqual(
                entry["sha256"],
                hashlib.sha256(raw_content).hexdigest(),
            )
            self.assertEqual(
                entry["sections"][0]["sha256"],
                hashlib.sha256(
                    b"# Windows Markdown\r\n\r\nBody.\r\n"
                ).hexdigest(),
            )

    def test_excluded_directories_and_non_markdown_files_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".knowledge-curator").mkdir()
            (root / ".git" / "hidden.md").write_text(
                "# Hidden\n", encoding="utf-8"
            )
            (root / ".knowledge-curator" / "run.md").write_text(
                "# Run\n", encoding="utf-8"
            )
            (root / "notes.txt").write_text("not markdown", encoding="utf-8")
            (root / "VISIBLE.MD").write_text("# Visible\n", encoding="utf-8")

            inventory = knowledge_inventory.build_inventory(root)

            self.assertEqual(
                [entry["path"] for entry in inventory["files"]],
                ["VISIBLE.MD"],
            )

    def test_detected_link_is_skipped_with_structured_warning(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            linked = root / "linked.md"
            linked.write_text("# Linked\n", encoding="utf-8")
            regular = root / "regular.md"
            regular.write_text("# Regular\n", encoding="utf-8")
            original = knowledge_inventory.is_link_like

            def simulated_detection(path: Path) -> bool:
                return path.name == "linked.md" or original(path)

            with mock.patch.object(
                knowledge_inventory,
                "is_link_like",
                side_effect=simulated_detection,
            ):
                inventory = knowledge_inventory.build_inventory(root)

            self.assertEqual(inventory["status"], "completed_with_issues")
            self.assertEqual(
                [entry["path"] for entry in inventory["files"]],
                ["regular.md"],
            )
            self.assertEqual(inventory["warnings"][0]["code"], "link_skipped")
            self.assertEqual(inventory["warnings"][0]["path"], "linked.md")

    def test_directory_symlink_is_reported_and_not_followed(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root = root / "knowledge"
            knowledge_root.mkdir()
            regular = knowledge_root / "regular.md"
            regular.write_text("# Regular\n", encoding="utf-8")
            external = root / "external"
            external.mkdir()
            (external / "private.md").write_text(
                "# Private\n", encoding="utf-8"
            )
            create_directory_symlink_or_skip(
                self,
                knowledge_root / "external-link",
                external,
            )

            inventory = knowledge_inventory.build_inventory(knowledge_root)

            self.assertEqual(inventory["status"], "completed_with_issues")
            self.assertEqual(
                [entry["path"] for entry in inventory["files"]],
                ["regular.md"],
            )
            self.assertEqual(len(inventory["warnings"]), 1)
            self.assertEqual(inventory["warnings"][0]["code"], "link_skipped")
            self.assertEqual(
                inventory["warnings"][0]["path"],
                "external-link",
            )


if __name__ == "__main__":
    unittest.main()
