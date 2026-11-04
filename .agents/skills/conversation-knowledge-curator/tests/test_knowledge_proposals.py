"""第四阶段知识提案最终化测试。

已覆盖的输入绑定行为：

1. 校验 candidates、inventory、retrieval 和 draft 的格式及文件哈希。
2. 确定性重算 retrieval，拒绝被修改或已过期的阶段产物。
3. 失败时不创建输出，已有输出不会被覆盖。

已覆盖的提案行为：

1. 每个候选必须且只能被一个提案覆盖，多个候选可以组成同一提案。
2. flags 必须完整继承关联候选，最终 proposal ID 和顺序保持稳定。
3. 校验五种 action 与有限 operation 的组合及 Markdown 基本形状。
4. 新建目标必须安全且不存在，现有目标必须匹配文件和章节哈希。
5. 同一目标文件不能存在多个相互竞争的可写提案。

已覆盖的 CLI 行为：

1. 成功时生成 proposals 文件并只输出结构化汇总。
2. 输入失败时返回退出码 2，且不留下正式输出。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
INVENTORY_SCRIPT = SKILL_DIR / "scripts" / "knowledge_inventory.py"
RETRIEVAL_SCRIPT = SKILL_DIR / "scripts" / "knowledge_retrieval.py"
PROPOSALS_SCRIPT = SKILL_DIR / "scripts" / "knowledge_proposals.py"
FIXTURE_ROOT = SKILL_DIR / "tests" / "fixtures" / "knowledge-base"


def load_script(name: str, path: Path) -> object:
    """按脚本真实导入名加载模块，支持脚本间的同目录依赖。"""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


knowledge_inventory = load_script("knowledge_inventory", INVENTORY_SCRIPT)
knowledge_retrieval = load_script("knowledge_retrieval", RETRIEVAL_SCRIPT)
knowledge_proposals = load_script("knowledge_proposals", PROPOSALS_SCRIPT)

SOURCE_SHA256 = "a" * 64
CANDIDATE_IDE = "kc_" + "1" * 20
CANDIDATE_PYTHON = "kc_" + "2" * 20
CANDIDATE_DEFER = "kc_" + "3" * 20


def write_json(path: Path, value: object) -> None:
    """写入稳定、保留中文且以换行结束的测试 JSON。"""

    knowledge_inventory.write_new_file(
        path,
        knowledge_inventory.serialize_json(value),
    )


def candidate(
    candidate_id: str,
    statement: str,
    flags: list[str],
) -> dict[str, object]:
    """构造满足第三阶段契约的最小候选。"""

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
        "flags": flags,
    }


def candidates_document() -> dict[str, object]:
    """构造覆盖合并、无变更和暂缓分支的候选文档。"""

    return {
        "format": "conversation-knowledge-curator.knowledge-candidates.v1",
        "candidates": [
            candidate(
                CANDIDATE_IDE,
                "项目说明应兼容 JetBrains 系列 IDE。",
                ["time_sensitive"],
            ),
            candidate(CANDIDATE_PYTHON, "Python 是当前知识库已有主题。", []),
            candidate(CANDIDATE_DEFER, "未经确认的临时偏好。", ["uncertain"]),
        ],
    }


def json_sha256(value: object) -> str:
    """按正式 JSON 序列化规则计算测试文档哈希。"""

    payload = knowledge_inventory.serialize_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def section_target(
    inventory: dict[str, object],
    path: str,
    heading: str,
) -> dict[str, object]:
    """从合成 inventory 中构造真实章节 Target。"""

    for file_entry in inventory["files"]:
        if file_entry["path"] != path:
            continue
        for section in file_entry["sections"]:
            if section["heading"] == heading:
                return {
                    "path": path,
                    "file_sha256": file_entry["sha256"],
                    "section_id": section["section_id"],
                    "section_sha256": section["sha256"],
                }
    raise AssertionError(f"测试章节不存在：{path}#{heading}")


def build_stage_documents() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    """构造相互绑定的 candidates、inventory 和 retrieval。"""

    candidates = candidates_document()
    inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
    retrieval = knowledge_retrieval.build_retrieval(
        candidates,
        json_sha256(candidates),
        inventory,
        json_sha256(inventory),
        FIXTURE_ROOT,
        20,
    )
    return candidates, inventory, retrieval


def valid_draft(
    candidates: dict[str, object],
    inventory: dict[str, object],
    retrieval: dict[str, object],
) -> dict[str, object]:
    """构造覆盖全部候选的合法 proposal draft。"""

    return {
        "format": "conversation-knowledge-curator.proposal-draft.v1",
        "inputs": {
            "knowledge_candidates_sha256": json_sha256(candidates),
            "knowledge_inventory_sha256": json_sha256(inventory),
            "knowledge_retrieval_sha256": json_sha256(retrieval),
        },
        "proposals": [
            {
                "candidate_ids": [CANDIDATE_IDE],
                "action": "merge",
                "target": section_target(
                    inventory,
                    "preferences/development-tools.md",
                    "JetBrains",
                ),
                "rationale": "现有章节属于同一主题，应补充而不是新建文档。",
                "operation": {
                    "type": "append_to_section",
                    "markdown": "\n项目说明默认兼容 JetBrains 系列 IDE。\n",
                },
                "flags": ["time_sensitive"],
            },
            {
                "candidate_ids": [CANDIDATE_PYTHON],
                "action": "no_change",
                "target": section_target(
                    inventory,
                    "technologies/python.md",
                    "Python",
                ),
                "rationale": "现有文档已经覆盖 Python 主题。",
                "operation": None,
                "flags": [],
            },
            {
                "candidate_ids": [CANDIDATE_DEFER],
                "action": "defer",
                "target": None,
                "rationale": "候选带有不确定标记，等待更多证据。",
                "operation": None,
                "flags": ["uncertain"],
            },
        ],
    }


def build_result(
    candidates: dict[str, object],
    inventory: dict[str, object],
    retrieval: dict[str, object],
    draft: dict[str, object],
) -> dict[str, object]:
    """使用与文件模式相同的哈希调用核心最终化逻辑。"""

    return knowledge_proposals.build_proposals(
        candidates,
        json_sha256(candidates),
        inventory,
        json_sha256(inventory),
        retrieval,
        json_sha256(retrieval),
        draft,
        FIXTURE_ROOT,
    )


class ProposalInputTests(unittest.TestCase):
    def test_tampered_retrieval_is_rejected(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        retrieval["results"][0]["matches"] = []
        draft["inputs"]["knowledge_retrieval_sha256"] = json_sha256(retrieval)

        with self.assertRaises(knowledge_proposals.ProposalError):
            build_result(candidates, inventory, retrieval, draft)

    def test_draft_input_hash_mismatch_is_rejected(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        draft["inputs"]["knowledge_candidates_sha256"] = "f" * 64

        with self.assertRaises(knowledge_proposals.ProposalError):
            build_result(candidates, inventory, retrieval, draft)

    def test_existing_output_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            output_path = root / "proposals.json"
            output_path.write_text("keep", encoding="utf-8")

            with self.assertRaises(knowledge_proposals.ProposalError):
                knowledge_proposals.create_proposals_file(
                    FIXTURE_ROOT,
                    root / "inventory.json",
                    root / "candidates.json",
                    root / "retrieval.json",
                    root / "draft.json",
                    output_path,
                )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "keep")


class ProposalValidationTests(unittest.TestCase):
    def test_valid_draft_generates_stable_ids_and_preserves_flags(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)

        first = build_result(candidates, inventory, retrieval, draft)
        second = build_result(candidates, inventory, retrieval, draft)

        self.assertEqual(first, second)
        self.assertEqual(
            first["format"],
            "conversation-knowledge-curator.knowledge-proposals.v1",
        )
        proposals = first["proposals"]
        self.assertEqual(
            [item["proposal_id"] for item in proposals],
            sorted(item["proposal_id"] for item in proposals),
        )
        self.assertTrue(
            all(re.fullmatch(r"kp_[0-9a-f]{20}", item["proposal_id"]) for item in proposals)
        )
        by_candidate = {
            item["candidate_ids"][0]: item
            for item in proposals
        }
        self.assertEqual(by_candidate[CANDIDATE_IDE]["flags"], ["time_sensitive"])
        self.assertEqual(by_candidate[CANDIDATE_DEFER]["flags"], ["uncertain"])

    def test_missing_or_duplicate_candidate_is_rejected(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        missing = valid_draft(candidates, inventory, retrieval)
        missing["proposals"].pop()
        duplicate = valid_draft(candidates, inventory, retrieval)
        duplicate["proposals"][1]["candidate_ids"] = [CANDIDATE_IDE]

        for draft in (missing, duplicate):
            with self.subTest(draft=draft), self.assertRaises(
                knowledge_proposals.ProposalError
            ):
                build_result(candidates, inventory, retrieval, draft)

    def test_multiple_candidates_can_share_one_proposal(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        draft["proposals"][0]["candidate_ids"] = [
            CANDIDATE_PYTHON,
            CANDIDATE_IDE,
        ]
        draft["proposals"].pop(1)

        result = build_result(candidates, inventory, retrieval, draft)

        grouped = next(
            item
            for item in result["proposals"]
            if CANDIDATE_IDE in item["candidate_ids"]
        )
        self.assertEqual(
            grouped["candidate_ids"],
            sorted([CANDIDATE_IDE, CANDIDATE_PYTHON]),
        )

    def test_candidate_flags_cannot_be_omitted(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        draft["proposals"][0]["flags"] = []

        with self.assertRaises(knowledge_proposals.ProposalError):
            build_result(candidates, inventory, retrieval, draft)

    def test_action_and_operation_must_be_compatible(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        draft["proposals"][0]["operation"]["type"] = "create_document"

        with self.assertRaises(knowledge_proposals.ProposalError):
            build_result(candidates, inventory, retrieval, draft)

    def test_operation_markdown_shape_is_validated(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        no_newline = valid_draft(candidates, inventory, retrieval)
        no_newline["proposals"][0]["operation"]["markdown"] = "正文"
        no_heading = valid_draft(candidates, inventory, retrieval)
        no_heading["proposals"][0]["operation"] = {
            "type": "replace_section",
            "markdown": "没有标题的正文。\n",
        }

        for draft in (no_newline, no_heading):
            with self.subTest(draft=draft), self.assertRaises(
                knowledge_proposals.ProposalError
            ):
                build_result(candidates, inventory, retrieval, draft)

    def test_create_document_requires_safe_new_path_and_complete_document(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        proposal = draft["proposals"][0]
        proposal["action"] = "add"
        proposal["target"] = {
            "path": "new/jetbrains.md",
            "file_sha256": None,
            "section_id": None,
            "section_sha256": None,
        }
        proposal["operation"] = {
            "type": "create_document",
            "markdown": "# JetBrains\n\n项目说明应兼容 JetBrains 系列 IDE。\n",
        }

        result = build_result(candidates, inventory, retrieval, draft)

        by_candidate = {
            item["candidate_ids"][0]: item
            for item in result["proposals"]
        }
        self.assertEqual(
            by_candidate[CANDIDATE_IDE]["target"]["path"],
            "new/jetbrains.md",
        )

    def test_create_document_rejects_existing_or_unsafe_path(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        for path in (
            "technologies/python.md",
            "technologies/python.md/child.md",
            "../outside.md",
        ):
            draft = valid_draft(candidates, inventory, retrieval)
            proposal = draft["proposals"][0]
            proposal["action"] = "add"
            proposal["target"] = {
                "path": path,
                "file_sha256": None,
                "section_id": None,
                "section_sha256": None,
            }
            proposal["operation"] = {
                "type": "create_document",
                "markdown": "# New\n",
            }

            with self.subTest(path=path), self.assertRaises(
                knowledge_proposals.ProposalError
            ):
                build_result(candidates, inventory, retrieval, draft)

    def test_existing_target_hash_must_match_inventory(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        draft["proposals"][0]["target"]["section_sha256"] = "f" * 64

        with self.assertRaises(knowledge_proposals.ProposalError):
            build_result(candidates, inventory, retrieval, draft)

    def test_two_writable_proposals_cannot_target_same_file(self) -> None:
        candidates, inventory, retrieval = build_stage_documents()
        draft = valid_draft(candidates, inventory, retrieval)
        second = draft["proposals"][1]
        second["action"] = "merge"
        second["target"] = section_target(
            inventory,
            "preferences/development-tools.md",
            "默认 IDE",
        )
        second["operation"] = {
            "type": "append_to_section",
            "markdown": "\n补充 Python 开发知识。\n",
        }

        with self.assertRaises(knowledge_proposals.ProposalError):
            build_result(candidates, inventory, retrieval, draft)


class ProposalCliTests(unittest.TestCase):
    def test_cli_writes_output_and_reports_summary(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            candidates, inventory, retrieval = build_stage_documents()
            draft = valid_draft(candidates, inventory, retrieval)
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            retrieval_path = root / "retrieval.json"
            draft_path = root / "draft.json"
            output_path = root / "proposals.json"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates)
            write_json(retrieval_path, retrieval)
            write_json(draft_path, draft)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_proposals.main(
                    [
                        "finalize",
                        str(FIXTURE_ROOT),
                        str(inventory_path),
                        str(candidates_path),
                        str(retrieval_path),
                        str(draft_path),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            summary = json.loads(stdout.getvalue())
            output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["proposal_count"], 3)
            self.assertEqual(summary["action_counts"]["merge"], 1)
            self.assertEqual(
                output["format"],
                "conversation-knowledge-curator.knowledge-proposals.v1",
            )

    def test_cli_failure_returns_two_without_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            output_path = root / "proposals.json"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_proposals.main(
                    [
                        "finalize",
                        str(FIXTURE_ROOT),
                        str(root / "missing-inventory.json"),
                        str(root / "missing-candidates.json"),
                        str(root / "missing-retrieval.json"),
                        str(root / "missing-draft.json"),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(stderr.getvalue().strip())
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
