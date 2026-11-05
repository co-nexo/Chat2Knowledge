"""ChatGPT 导出检查与标准化测试。

已覆盖的检查行为：

1. inspect 报告结构统计，但不泄露消息正文和隐藏内容。
2. 识别单个 JSON、目录和 ZIP，并在目录中继续处理其他有效来源。
3. 在内存中检查 ZIP，拒绝不安全成员，记录损坏压缩包问题。
4. 目录发现不跟随符号链接，避免读取来源根目录之外的数据。
5. 拒绝不存在的路径、不支持的文件类型和不支持的 JSON 容器。

已覆盖的标准化行为：

1. 默认只保留活动分支，也支持显式遍历全部可见分支。
2. 排除 hidden reasoning、工具载荷和其他非用户可见内容。
3. 活动分支不可确认时跳过对话，并在诊断信息中记录原因。
4. 为缺失或重复的 conversation/message ID 生成稳定唯一替代值。
5. 只标准化 inspect 确认支持的来源，并保持结果在内存中。

"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPT_PATH = SKILL_DIR / "scripts" / "chatgpt_export.py"
FIXTURE_PATH = SKILL_DIR / "tests" / "fixtures" / "synthetic_export.json"

SPEC = importlib.util.spec_from_file_location("chatgpt_export", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
chatgpt_export = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = chatgpt_export
SPEC.loader.exec_module(chatgpt_export)


class InspectExportTests(unittest.TestCase):
    def test_inspection_reports_structure_without_message_bodies(self) -> None:
        report = chatgpt_export.inspect_export(FIXTURE_PATH)

        self.assertEqual(report["conversation_count"], 1)
        self.assertEqual(report["message_count"], 5)
        self.assertEqual(report["roles"], {"assistant": 3, "tool": 1, "user": 1})
        self.assertEqual(report["visibility"]["included"], 3)
        serialized = json.dumps(report)
        self.assertNotIn("Visible question", serialized)
        self.assertNotIn("Hidden reasoning", serialized)

    def test_input_inspection_accepts_single_json_file(self) -> None:
        report = chatgpt_export.inspect_input(FIXTURE_PATH)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["input"]["type"], "json_file")
        self.assertEqual(report["summary"]["supported_count"], 1)
        self.assertEqual(report["discovery"]["candidate_json_count"], 1)
        serialized = json.dumps(report)
        self.assertNotIn("Visible question", serialized)
        self.assertNotIn("Hidden reasoning", serialized)

    def test_directory_inspection_continues_after_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            input_dir = Path(directory) / "imports"
            nested_dir = input_dir / "nested"
            nested_dir.mkdir(parents=True)
            (input_dir / "valid.json").write_bytes(FIXTURE_PATH.read_bytes())
            (input_dir / "invalid.json").write_text("{", encoding="utf-8")
            (input_dir / "notes.txt").write_text("ignored", encoding="utf-8")
            archive_path = nested_dir / "export.zip"
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("inside/conversations.json", FIXTURE_PATH.read_bytes())
                archive.writestr("inside/readme.txt", "ignored")

            report = chatgpt_export.inspect_input(input_dir)

            self.assertEqual(report["input"]["type"], "directory")
            self.assertEqual(report["status"], "issues_found")
            self.assertEqual(report["discovery"]["json_file_count"], 2)
            self.assertEqual(report["discovery"]["zip_file_count"], 1)
            self.assertEqual(report["discovery"]["other_file_count"], 1)
            self.assertEqual(report["summary"]["supported_count"], 2)
            self.assertEqual(report["summary"]["invalid_json_count"], 1)

    def test_directory_inspection_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            input_dir = root / "imports"
            outside = root / "outside"
            input_dir.mkdir()
            outside.mkdir()
            (input_dir / "local.json").write_bytes(FIXTURE_PATH.read_bytes())
            (outside / "external.json").write_bytes(FIXTURE_PATH.read_bytes())
            link = input_dir / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("当前系统无法创建目录符号链接")

            resolved = chatgpt_export.resolve_input(input_dir)

            self.assertEqual(len(resolved.sources), 1)
            self.assertEqual(resolved.sources[0].path.name, "local.json")
            self.assertTrue(any("跳过链接目录" in item for item in resolved.warnings))

    def test_zip_is_inspected_in_memory_and_unsafe_member_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            archive_path = Path(directory) / "export.zip"
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("conversations.json", FIXTURE_PATH.read_bytes())
                archive.writestr("../escape.json", FIXTURE_PATH.read_bytes())

            report = chatgpt_export.inspect_input(archive_path)

            self.assertEqual(report["input"]["type"], "zip_archive")
            self.assertEqual(report["summary"]["supported_count"], 1)
            self.assertEqual(report["archives"][0]["skipped_json_count"], 1)
            self.assertTrue(report["warnings"])
            self.assertFalse((Path(directory).parent / "escape.json").exists())

    def test_invalid_zip_becomes_archive_issue(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            archive_path = Path(directory) / "broken.zip"
            archive_path.write_bytes(b"not a zip archive")

            report = chatgpt_export.inspect_input(archive_path)

            self.assertEqual(report["status"], "issues_found")
            self.assertEqual(report["summary"]["candidate_count"], 0)
            self.assertEqual(report["summary"]["archive_issue_count"], 1)
            self.assertEqual(report["archives"][0]["status"], "invalid")

    def test_unsupported_file_type_is_rejected_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            path = Path(directory) / "notes.txt"
            path.write_text("not json", encoding="utf-8")

            with self.assertRaises(chatgpt_export.ExportError):
                chatgpt_export.inspect_input(path)

    def test_unsupported_json_container_becomes_report_item(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            path = Path(directory) / "unknown.json"
            path.write_text('{"unknown": true}', encoding="utf-8")

            report = chatgpt_export.inspect_input(path)

            self.assertEqual(report["status"], "issues_found")
            self.assertEqual(report["summary"]["unsupported_count"], 1)
            self.assertEqual(
                report["items"][0]["json"]["top_level_keys"], ["unknown"]
            )

    def test_missing_input_path_is_reported_by_inspector(self) -> None:
        missing = WORKSPACE_ROOT / "missing-inspection-input.json"

        with self.assertRaises(chatgpt_export.ExportError):
            chatgpt_export.inspect_input(missing)


class NormalizeExportTests(unittest.TestCase):
    def test_active_branch_keeps_only_visible_active_messages(self) -> None:
        result = chatgpt_export.normalize_export(FIXTURE_PATH)
        messages = result["conversations"][0]["messages"]

        self.assertEqual(
            result["format"], "conversation-knowledge-curator.normalized.v3"
        )
        self.assertEqual(
            set(result), {"format", "source_sha256", "conversations"}
        )
        self.assertEqual(result["conversations"][0]["source_index"], 0)
        self.assertEqual(
            [message["text"] for message in messages],
            ["Visible question", "Visible answer"],
        )
        self.assertEqual(
            set(messages[0]), {"message_id", "role", "create_time", "text"}
        )
        serialized = json.dumps(result)
        self.assertNotIn("Inactive branch answer", serialized)
        self.assertNotIn("Hidden reasoning", serialized)
        self.assertNotIn("Hidden tool payload", serialized)
        self.assertNotIn("Structured tool text", serialized)

    def test_all_branches_includes_visible_inactive_branch(self) -> None:
        result = chatgpt_export.normalize_export(FIXTURE_PATH, branch="all")
        texts = [
            message["text"]
            for message in result["conversations"][0]["messages"]
        ]

        self.assertEqual(
            texts, ["Visible question", "Inactive branch answer", "Visible answer"]
        )

    def test_missing_current_node_skips_active_conversation(self) -> None:
        data = chatgpt_export.load_json(FIXTURE_PATH)
        del data[0]["current_node"]
        conversations, _ = chatgpt_export.conversation_objects(data)

        normalized, warnings, reason = chatgpt_export.normalize_conversation(
            conversations[0], "active"
        )

        self.assertIsNone(normalized)
        self.assertTrue(warnings)
        self.assertEqual(reason, "active_branch_unavailable")

    def test_missing_current_node_is_recorded_in_normalized_output(self) -> None:
        data = chatgpt_export.load_json(FIXTURE_PATH)
        del data[0]["current_node"]
        source = {
            "kind": "json_file",
            "path": str(FIXTURE_PATH),
            "size_bytes": FIXTURE_PATH.stat().st_size,
            "sha256": chatgpt_export.sha256_file(FIXTURE_PATH),
        }

        result = chatgpt_export.normalize_loaded_export(data, source, "active")

        self.assertEqual(result.document["conversations"], [])
        self.assertNotIn("skipped_conversations", result.document)
        self.assertEqual(result.diagnostics["skipped_conversation_count"], 1)
        self.assertEqual(
            result.diagnostics["skipped_conversations"][0]["reason_code"],
            "active_branch_unavailable",
        )

    def test_missing_conversation_id_gets_stable_synthetic_id(self) -> None:
        data = chatgpt_export.load_json(FIXTURE_PATH)
        data[0].pop("id", None)
        data[0].pop("conversation_id", None)
        source = {
            "kind": "json_file",
            "path": str(FIXTURE_PATH),
            "size_bytes": FIXTURE_PATH.stat().st_size,
            "sha256": chatgpt_export.sha256_file(FIXTURE_PATH),
        }

        first = chatgpt_export.normalize_loaded_export(data, source, "active")
        second = chatgpt_export.normalize_loaded_export(data, source, "active")
        first_id = first.document["conversations"][0]["conversation_id"]
        second_id = second.document["conversations"][0]["conversation_id"]

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("synthetic-conversation-"))

    def test_duplicate_ids_get_stable_unique_replacements(self) -> None:
        data = chatgpt_export.load_json(FIXTURE_PATH)
        duplicate = copy.deepcopy(data[0])
        data.append(duplicate)
        visible_nodes = ["user-1", "assistant-final"]
        for node_id in visible_nodes:
            data[0]["mapping"][node_id]["message"]["id"] = "duplicate"
        source = {
            "kind": "json_file",
            "path": str(FIXTURE_PATH),
            "size_bytes": FIXTURE_PATH.stat().st_size,
            "sha256": chatgpt_export.sha256_file(FIXTURE_PATH),
        }

        result = chatgpt_export.normalize_loaded_export(data, source, "active")
        conversations = result.document["conversations"]
        conversation_ids = [item["conversation_id"] for item in conversations]
        message_ids = [
            message["message_id"] for message in conversations[0]["messages"]
        ]

        self.assertEqual(len(conversation_ids), len(set(conversation_ids)))
        self.assertEqual(len(message_ids), len(set(message_ids)))
        self.assertTrue(result.diagnostics["warnings"])

    def test_normalize_source_rejects_unsupported_inspection(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            path = Path(directory) / "unknown.json"
            path.write_text('{"unknown": true}', encoding="utf-8")
            resolved = chatgpt_export.resolve_input(path)
            source = resolved.sources[0]
            inspection = chatgpt_export.inspect_source(source)

            with self.assertRaises(chatgpt_export.ExportError):
                chatgpt_export.normalize_source(
                    source, inspection=inspection
                )


if __name__ == "__main__":
    unittest.main()

