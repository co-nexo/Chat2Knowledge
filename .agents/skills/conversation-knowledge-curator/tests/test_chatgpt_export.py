from __future__ import annotations

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
            [message["content"]["text"] for message in messages],
            ["Visible question", "Visible answer"],
        )
        serialized = json.dumps(result)
        self.assertNotIn("Inactive branch answer", serialized)
        self.assertNotIn("Hidden reasoning", serialized)
        self.assertNotIn("Hidden tool payload", serialized)
        self.assertNotIn("Structured tool text", serialized)

    def test_all_branches_includes_visible_inactive_branch(self) -> None:
        result = chatgpt_export.normalize_export(FIXTURE_PATH, branch="all")
        texts = [
            message["content"]["text"]
            for message in result["conversations"][0]["messages"]
        ]

        self.assertEqual(
            texts, ["Visible question", "Inactive branch answer", "Visible answer"]
        )

    def test_missing_current_node_warns_and_uses_all_nodes(self) -> None:
        data = chatgpt_export.load_json(FIXTURE_PATH)
        del data[0]["current_node"]
        conversations, _ = chatgpt_export.conversation_objects(data)

        normalized, warnings = chatgpt_export.normalize_conversation(
            conversations[0], "active"
        )

        self.assertTrue(warnings)
        self.assertEqual(len(normalized["messages"]), 3)

    def test_output_creation_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            output = Path(directory) / "normalized.json"
            chatgpt_export.write_new_json(output, {"first": True})

            with self.assertRaises(FileExistsError):
                chatgpt_export.write_new_json(output, {"second": True})

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"first": True})

    def test_same_input_and_output_is_rejected_by_cli(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            chatgpt_export.main(["normalize", str(FIXTURE_PATH), str(FIXTURE_PATH)])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

