"""第三阶段知识候选校验与最终化测试。

已覆盖的行为：

1. 第二阶段真实输出可以直接进入第三阶段，形成端到端处理链路。
2. Candidate 的相同 statement 会精确去重，并合并多个 Evidence。
3. `assistant_only` 根据 Evidence 引用消息的真实角色自动派生。
4. Conversation result 根据 Evidence 自动关联稳定的 candidate IDs。
5. 不存在或不属于指定对话的 message ID 会被拒绝。
6. completed 但没有候选的对话必须提供原因或审核说明。
7. 校验失败时不创建输出目录；成功时生成候选和提取 manifest。
8. 已存在输出目录时拒绝覆盖，manifest 不包含消息正文。

本文件验证结构、来源引用和确定性处理，不验证候选事实是否正确，
也不评价 Codex 是否准确理解了对话语义。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPT_PATH = SKILL_DIR / "scripts" / "knowledge_candidates.py"
EXPORT_SCRIPT_PATH = SKILL_DIR / "scripts" / "chatgpt_export.py"
FIXTURE_PATH = SKILL_DIR / "tests" / "fixtures" / "synthetic_export.json"

SPEC = importlib.util.spec_from_file_location("knowledge_candidates", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
knowledge_candidates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = knowledge_candidates
SPEC.loader.exec_module(knowledge_candidates)

EXPORT_SPEC = importlib.util.spec_from_file_location(
    "candidate_chatgpt_export", EXPORT_SCRIPT_PATH
)
assert EXPORT_SPEC is not None and EXPORT_SPEC.loader is not None
candidate_chatgpt_export = importlib.util.module_from_spec(EXPORT_SPEC)
sys.modules[EXPORT_SPEC.name] = candidate_chatgpt_export
EXPORT_SPEC.loader.exec_module(candidate_chatgpt_export)

SOURCE_A = "a" * 64
SOURCE_B = "b" * 64


def write_json(path: Path, value: object) -> None:
    """写入测试 JSON。"""

    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def normalized_document(
    source_sha256: str,
    conversation_id: str,
    messages: list[dict[str, object]],
) -> dict[str, object]:
    """构造最小 normalized.v3 测试文档。"""

    return {
        "format": "conversation-knowledge-curator.normalized.v3",
        "source_sha256": source_sha256,
        "conversations": [
            {
                "source_index": 0,
                "conversation_id": conversation_id,
                "title": "Synthetic conversation",
                "create_time": None,
                "update_time": None,
                "messages": messages,
            }
        ],
    }


def build_normalization_directory(root: Path) -> Path:
    """创建包含两个独立来源的合成标准化输出目录。"""

    normalization_dir = root / "normalized"
    normalization_dir.mkdir()
    first_name = "first.normalized.json"
    second_name = "second.normalized.json"
    write_json(
        normalization_dir / first_name,
        normalized_document(
            SOURCE_A,
            "conv-a",
            [
                {
                    "message_id": "user-a",
                    "role": "user",
                    "create_time": None,
                    "text": "User evidence",
                },
                {
                    "message_id": "assistant-a",
                    "role": "assistant",
                    "create_time": None,
                    "text": "Assistant context",
                },
            ],
        ),
    )
    write_json(
        normalization_dir / second_name,
        normalized_document(
            SOURCE_B,
            "conv-b",
            [
                {
                    "message_id": "assistant-b",
                    "role": "assistant",
                    "create_time": None,
                    "text": "Assistant-only evidence",
                }
            ],
        ),
    )
    write_json(
        normalization_dir / "manifest.json",
        {
            "format": "conversation-knowledge-curator.manifest.v1",
            "status": "ok",
            "entries": [
                {
                    "status": "normalized",
                    "source": {"sha256": SOURCE_A},
                    "output": first_name,
                },
                {
                    "status": "normalized",
                    "source": {"sha256": SOURCE_B},
                    "output": second_name,
                },
            ],
        },
    )
    return normalization_dir


def complete_draft() -> dict[str, object]:
    """构造覆盖多来源证据、精确重复和角色派生的草稿。"""

    return {
        "format": "conversation-knowledge-curator.extraction-draft.v1",
        "candidates": [
            {
                "statement": "项目优先兼容 JetBrains 系列 IDE。",
                "evidence": [
                    {
                        "source_sha256": SOURCE_A,
                        "conversation_id": "conv-a",
                        "message_ids": ["user-a"],
                    }
                ],
                "flags": [],
            },
            {
                "statement": "项目优先兼容 JetBrains 系列 IDE。",
                "evidence": [
                    {
                        "source_sha256": SOURCE_B,
                        "conversation_id": "conv-b",
                        "message_ids": ["assistant-b"],
                    }
                ],
                "flags": ["uncertain"],
            },
            {
                "statement": "某项技术解释需要进一步核验。",
                "evidence": [
                    {
                        "source_sha256": SOURCE_B,
                        "conversation_id": "conv-b",
                        "message_ids": ["assistant-b"],
                    }
                ],
                "flags": [],
            },
        ],
        "conversation_results": [
            {
                "source_sha256": SOURCE_A,
                "conversation_id": "conv-a",
                "processing_status": "completed",
            },
            {
                "source_sha256": SOURCE_B,
                "conversation_id": "conv-b",
                "processing_status": "completed",
            },
        ],
    }


class KnowledgeCandidateTests(unittest.TestCase):
    def test_second_stage_output_can_be_finalized_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            normalization_dir = root / "normalized"
            candidate_chatgpt_export.normalize_input(
                FIXTURE_PATH, normalization_dir
            )
            normalized_path = next(
                normalization_dir.glob("*.normalized.json")
            )
            normalized = json.loads(
                normalized_path.read_text(encoding="utf-8")
            )
            source_sha256 = normalized["source_sha256"]
            conversation = normalized["conversations"][0]
            message_id = conversation["messages"][0]["message_id"]
            draft = {
                "format": (
                    "conversation-knowledge-curator.extraction-draft.v1"
                ),
                "candidates": [
                    {
                        "statement": "Synthetic knowledge candidate.",
                        "evidence": [
                            {
                                "source_sha256": source_sha256,
                                "conversation_id": conversation[
                                    "conversation_id"
                                ],
                                "message_ids": [message_id],
                            }
                        ],
                        "flags": [],
                    }
                ],
                "conversation_results": [
                    {
                        "source_sha256": source_sha256,
                        "conversation_id": conversation[
                            "conversation_id"
                        ],
                        "processing_status": "completed",
                    }
                ],
            }
            draft_path = root / "draft.json"
            output_dir = root / "extracted"
            write_json(draft_path, draft)

            manifest = knowledge_candidates.finalize_to_directory(
                normalization_dir, draft_path, output_dir
            )

            self.assertEqual(manifest["status"], "ok")
            self.assertEqual(manifest["summary"]["candidate_count"], 1)

    def test_finalize_merges_evidence_and_derives_assistant_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            normalization_dir = build_normalization_directory(Path(directory))
            bundle = knowledge_candidates.load_normalized_bundle(
                normalization_dir
            )

            candidates_document, manifest = knowledge_candidates.build_outputs(
                bundle, complete_draft()
            )

            self.assertEqual(
                candidates_document["format"],
                "conversation-knowledge-curator.knowledge-candidates.v1",
            )
            candidates = candidates_document["candidates"]
            self.assertEqual(len(candidates), 2)
            merged = next(
                candidate
                for candidate in candidates
                if candidate["statement"].startswith("项目优先")
            )
            self.assertEqual(len(merged["evidence"]), 2)
            self.assertEqual(merged["flags"], ["uncertain"])
            assistant_only = next(
                candidate
                for candidate in candidates
                if candidate["statement"].startswith("某项技术")
            )
            self.assertEqual(assistant_only["flags"], ["assistant_only"])
            self.assertEqual(manifest["summary"]["candidate_count"], 2)
            self.assertNotIn(
                "decision", json.dumps(manifest, ensure_ascii=False)
            )

    def test_conversation_results_link_candidates_by_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            normalization_dir = build_normalization_directory(Path(directory))
            bundle = knowledge_candidates.load_normalized_bundle(
                normalization_dir
            )

            _, manifest = knowledge_candidates.build_outputs(
                bundle, complete_draft()
            )

            results = manifest["conversation_results"]
            first_ids = results[0]["candidate_ids"]
            second_ids = results[1]["candidate_ids"]
            self.assertEqual(len(first_ids), 1)
            self.assertEqual(len(second_ids), 2)
            self.assertTrue(set(first_ids).issubset(second_ids))

    def test_invalid_message_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            normalization_dir = build_normalization_directory(root)
            draft = complete_draft()
            draft["candidates"][0]["evidence"][0]["message_ids"] = [
                "missing"
            ]
            draft_path = root / "draft.json"
            output_dir = root / "extracted"
            write_json(draft_path, draft)

            with self.assertRaises(knowledge_candidates.CandidateError):
                knowledge_candidates.finalize_to_directory(
                    normalization_dir, draft_path, output_dir
                )
            self.assertFalse(output_dir.exists())

    def test_completed_conversation_without_candidate_requires_reason(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            normalization_dir = build_normalization_directory(Path(directory))
            bundle = knowledge_candidates.load_normalized_bundle(
                normalization_dir
            )
            draft = {
                "format": (
                    "conversation-knowledge-curator.extraction-draft.v1"
                ),
                "candidates": [],
                "conversation_results": [
                    {
                        "source_sha256": SOURCE_A,
                        "conversation_id": "conv-a",
                        "processing_status": "completed",
                        "no_candidate_reason": "ordinary_qa",
                    },
                    {
                        "source_sha256": SOURCE_B,
                        "conversation_id": "conv-b",
                        "processing_status": "completed",
                    },
                ],
            }

            with self.assertRaises(knowledge_candidates.CandidateError):
                knowledge_candidates.build_outputs(bundle, draft)

    def test_finalize_writes_new_directory_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            normalization_dir = build_normalization_directory(root)
            draft_path = root / "draft.json"
            output_dir = root / "extracted"
            write_json(draft_path, complete_draft())

            manifest = knowledge_candidates.finalize_to_directory(
                normalization_dir, draft_path, output_dir
            )

            self.assertTrue(
                (output_dir / "knowledge-candidates.json").is_file()
            )
            self.assertTrue(
                (output_dir / "extraction-manifest.json").is_file()
            )
            self.assertEqual(manifest["status"], "ok")
            serialized_manifest = json.dumps(manifest, ensure_ascii=False)
            self.assertNotIn("User evidence", serialized_manifest)
            self.assertNotIn("Assistant-only evidence", serialized_manifest)
            with self.assertRaises(knowledge_candidates.CandidateError):
                knowledge_candidates.finalize_to_directory(
                    normalization_dir, draft_path, output_dir
                )


if __name__ == "__main__":
    unittest.main()
