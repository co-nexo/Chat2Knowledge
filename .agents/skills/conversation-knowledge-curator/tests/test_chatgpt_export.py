from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPT_PATH = SKILL_DIR / "scripts" / "chatgpt_export.py"
FIXTURE_PATH = SKILL_DIR / "tests" / "fixtures" / "synthetic_export.json"

SPEC = importlib.util.spec_from_file_location("chatgpt_export", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
chatgpt_export = importlib.util.module_from_spec(SPEC)
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

