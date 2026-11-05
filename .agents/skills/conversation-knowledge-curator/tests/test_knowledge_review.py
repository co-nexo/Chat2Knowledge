"""第四阶段审核包生成测试。

已覆盖的输入校验行为：

1. 重新校验 proposals、retrieval、inventory 和 candidates 的哈希与引用关系。
2. 拒绝被修改的 proposals 和知识库变化后的过期快照。
3. 已有审核目录不会被覆盖，失败时不留下正式审核目录。
4. 知识库内只允许固定的 `.knowledge-curator/runs/<run_id>/review` 审核位置。

已覆盖的审核产物行为：

1. 为可写提案生成完整 after、稳定 unified diff 和哈希绑定的 manifest。
2. 非写入提案进入 manifest，但不生成 after 或 diff。
3. replace 只替换直属章节，insert 位于完整章节子树之后。
4. 现有 Markdown 的 UTF-8 BOM 和 CRLF 会保留，新建文档从 /dev/null 生成 diff。
5. 新建文档必须通过严格标题编号检查，现有文档必须保持基本标题结构。
6. 审核包生成过程不会修改正式知识库。

已覆盖的 CLI 行为：

1. 成功时发布完整审核目录并输出结构化汇总。
2. 输入失败时返回退出码 2 且不创建审核目录。

已覆盖的审核决定行为：

1. 只记录草稿中明确选择的 Proposal，缺少决定不会默认批准。
2. proposal 和 diff 哈希从已验证 manifest 派生，支持写入与非写入提案。
3. 拒绝未知、重复或非法决定，以及被修改的审核文件。
4. decisions.json 独占创建，CLI 输出批准和拒绝数量。

已覆盖的对话审核报告行为：

1. 报告直接汇总 Proposal、候选 Evidence、章节级变化和风险关注项。
2. 报告重新绑定 candidates 与当前知识文件，拒绝篡改或过期输入。
3. report CLI 只输出结构化结果，不创建额外报告文件。
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
SCRIPTS_DIR = SKILL_DIR / "scripts"
FIXTURE_ROOT = SKILL_DIR / "tests" / "fixtures" / "knowledge-base"


def load_script(name: str, path: Path) -> object:
    """按真实导入名加载相互依赖的 Skill 脚本。"""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


knowledge_inventory = load_script(
    "knowledge_inventory",
    SCRIPTS_DIR / "knowledge_inventory.py",
)
knowledge_retrieval = load_script(
    "knowledge_retrieval",
    SCRIPTS_DIR / "knowledge_retrieval.py",
)
knowledge_document_quality = load_script(
    "knowledge_document_quality",
    SCRIPTS_DIR / "knowledge_document_quality.py",
)
knowledge_proposals = load_script(
    "knowledge_proposals",
    SCRIPTS_DIR / "knowledge_proposals.py",
)
knowledge_review = load_script(
    "knowledge_review",
    SCRIPTS_DIR / "knowledge_review.py",
)

SOURCE_SHA256 = "a" * 64
CANDIDATE_IDE = "kc_" + "1" * 20
CANDIDATE_PYTHON = "kc_" + "2" * 20
CANDIDATE_DEFER = "kc_" + "3" * 20


def json_bytes(value: object) -> bytes:
    """按正式阶段产物格式序列化 JSON。"""

    return knowledge_inventory.serialize_json(value).encode("utf-8")


def json_sha256(value: object) -> str:
    """计算正式 JSON 字节的 SHA-256。"""

    return hashlib.sha256(json_bytes(value)).hexdigest()


def write_json(path: Path, value: object) -> None:
    """使用正式写入函数创建 LF 结尾的测试 JSON。"""

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
    """构造包含写入、无变更和暂缓结果的候选。"""

    return {
        "format": "conversation-knowledge-curator.knowledge-candidates.v1",
        "candidates": [
            candidate(CANDIDATE_IDE, "项目应兼容 JetBrains IDE。", []),
            candidate(CANDIDATE_PYTHON, "Python 已有独立主题。", []),
            candidate(CANDIDATE_DEFER, "未经确认的临时偏好。", ["uncertain"]),
        ],
    }


def section_target(
    inventory: dict[str, object],
    path: str,
    heading: str,
) -> dict[str, object]:
    """从 inventory 中取得首个匹配标题的真实 Target。"""

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


def build_stage_documents(
    knowledge_root: Path = FIXTURE_ROOT,
    ide_action: str = "merge",
    ide_operation: dict[str, str] | None = None,
    ide_target: dict[str, object] | None = None,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    """构造相互绑定的四个第四阶段输入文件。"""

    candidates = candidates_document()
    inventory = knowledge_inventory.build_inventory(knowledge_root)
    retrieval = knowledge_retrieval.build_retrieval(
        candidates,
        json_sha256(candidates),
        inventory,
        json_sha256(inventory),
        knowledge_root,
        20,
    )
    target = ide_target or section_target(
        inventory,
        "preferences/development-tools.md",
        "JetBrains",
    )
    operation = ide_operation or {
        "type": "append_to_section",
        "markdown": "\n项目说明默认兼容 JetBrains 系列 IDE。\n",
    }
    draft = {
        "format": "conversation-knowledge-curator.proposal-draft.v1",
        "inputs": {
            "knowledge_candidates_sha256": json_sha256(candidates),
            "knowledge_inventory_sha256": json_sha256(inventory),
            "knowledge_retrieval_sha256": json_sha256(retrieval),
        },
        "proposals": [
            {
                "candidate_ids": [CANDIDATE_IDE],
                "action": ide_action,
                "target": target,
                "rationale": "现有文档包含同一主题。",
                "operation": operation,
                "flags": [],
            },
            {
                "candidate_ids": [CANDIDATE_PYTHON],
                "action": "no_change",
                "target": section_target(
                    inventory,
                    "technologies/python.md",
                    "Python",
                ),
                "rationale": "现有文档已经覆盖该主题。",
                "operation": None,
                "flags": [],
            },
            {
                "candidate_ids": [CANDIDATE_DEFER],
                "action": "defer",
                "target": None,
                "rationale": "等待更多证据。",
                "operation": None,
                "flags": ["uncertain"],
            },
        ],
    }
    proposals = knowledge_proposals.build_proposals(
        candidates,
        json_sha256(candidates),
        inventory,
        json_sha256(inventory),
        retrieval,
        json_sha256(retrieval),
        draft,
        knowledge_root,
    )
    return candidates, inventory, retrieval, proposals


def build_review(
    candidates: dict[str, object],
    inventory: dict[str, object],
    retrieval: dict[str, object],
    proposals: dict[str, object],
    knowledge_root: Path = FIXTURE_ROOT,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """调用审核包核心逻辑并使用真实文件字节哈希。"""

    return knowledge_review.build_review_package(
        candidates,
        json_sha256(candidates),
        inventory,
        json_sha256(inventory),
        retrieval,
        json_sha256(retrieval),
        proposals,
        json_sha256(proposals),
        knowledge_root,
    )


def create_review_fixture(root: Path) -> tuple[Path, dict[str, object]]:
    """在测试临时目录中创建完整、可继续审核的 review package。"""

    candidates, inventory, retrieval, proposals = build_stage_documents()
    inventory_path = root / "inventory.json"
    candidates_path = root / "candidates.json"
    retrieval_path = root / "retrieval.json"
    proposals_path = root / "proposals.json"
    review_path = root / "review"
    write_json(inventory_path, inventory)
    write_json(candidates_path, candidates)
    write_json(retrieval_path, retrieval)
    write_json(proposals_path, proposals)
    manifest = knowledge_review.create_review_package(
        FIXTURE_ROOT,
        inventory_path,
        candidates_path,
        retrieval_path,
        proposals_path,
        review_path,
    )
    return review_path, manifest


class ReviewValidationTests(unittest.TestCase):
    def test_review_builds_after_diff_manifest_without_changing_knowledge(self) -> None:
        candidates, inventory, retrieval, proposals = build_stage_documents()
        source_path = FIXTURE_ROOT / "preferences" / "development-tools.md"
        before_source = source_path.read_bytes()

        manifest, artifacts = build_review(
            candidates,
            inventory,
            retrieval,
            proposals,
        )

        self.assertEqual(source_path.read_bytes(), before_source)
        self.assertEqual(
            manifest["format"],
            "conversation-knowledge-curator.review-manifest.v1",
        )
        self.assertTrue(str(manifest["review_id"]).startswith("kr_"))
        writable = next(
            entry for entry in manifest["entries"] if entry["action"] == "merge"
        )
        after = artifacts[writable["after_path"]].decode("utf-8")
        diff = artifacts[writable["diff_path"]].decode("utf-8")
        self.assertIn("项目说明默认兼容 JetBrains 系列 IDE", after)
        self.assertIn("--- a/preferences/development-tools.md", diff)
        self.assertIn("+++ b/preferences/development-tools.md", diff)
        self.assertEqual(
            writable["after_sha256"],
            hashlib.sha256(artifacts[writable["after_path"]]).hexdigest(),
        )
        non_writable = [
            entry for entry in manifest["entries"] if entry["action"] != "merge"
        ]
        self.assertTrue(all(entry["after_path"] is None for entry in non_writable))

    def test_tampered_proposals_are_rejected(self) -> None:
        candidates, inventory, retrieval, proposals = build_stage_documents()
        proposals["proposals"][0]["rationale"] = "被修改的说明"

        with self.assertRaises(knowledge_review.ReviewError):
            build_review(candidates, inventory, retrieval, proposals)

    def test_stale_knowledge_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            knowledge_root = Path(directory) / "knowledge"
            shutil.copytree(FIXTURE_ROOT, knowledge_root)
            candidates, inventory, retrieval, proposals = build_stage_documents(
                knowledge_root
            )
            target = knowledge_root / "preferences" / "development-tools.md"
            target.write_text("# 已变化\n", encoding="utf-8")

            with self.assertRaises(knowledge_review.ReviewError):
                build_review(
                    candidates,
                    inventory,
                    retrieval,
                    proposals,
                    knowledge_root,
                )

    def test_replace_preserves_child_section(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        target = section_target(
            inventory,
            "preferences/development-tools.md",
            "默认 IDE",
        )
        documents = build_stage_documents(
            ide_operation={
                "type": "replace_section",
                "markdown": "## 默认 IDE\n\n替换后的直属正文。\n",
            },
            ide_target=target,
        )

        manifest, artifacts = build_review(*documents)

        writable = next(
            entry for entry in manifest["entries"] if entry["action"] == "merge"
        )
        after = artifacts[writable["after_path"]].decode("utf-8")
        self.assertIn("替换后的直属正文", after)
        self.assertIn("### JetBrains", after)

    def test_insert_occurs_after_target_subtree(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        target = section_target(
            inventory,
            "preferences/development-tools.md",
            "默认 IDE",
        )
        documents = build_stage_documents(
            ide_action="add",
            ide_operation={
                "type": "insert_section",
                "markdown": "## 新增章节\n\n新增内容。\n",
            },
            ide_target=target,
        )

        manifest, artifacts = build_review(*documents)

        writable = next(
            entry for entry in manifest["entries"] if entry["action"] == "add"
        )
        after = artifacts[writable["after_path"]].decode("utf-8")
        self.assertLess(after.index("### JetBrains"), after.index("## 新增章节"))
        self.assertLess(
            after.index("## 新增章节"),
            after.rindex("## 默认 IDE"),
        )

    def test_broken_after_document_is_rejected_before_review(self) -> None:
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        target = section_target(
            inventory,
            "preferences/development-tools.md",
            "开发工具偏好",
        )
        documents = build_stage_documents(
            ide_operation={
                "type": "replace_section",
                "markdown": "### 错误层级\n\n正文。\n",
            },
            ide_target=target,
        )

        with self.assertRaisesRegex(
            knowledge_review.ReviewError,
            "质量检查失败",
        ):
            build_review(*documents)


class ReviewEncodingTests(unittest.TestCase):
    def test_existing_file_preserves_bom_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            knowledge_root = Path(directory) / "knowledge"
            knowledge_root.mkdir()
            target_path = knowledge_root / "alpha.md"
            target_path.write_bytes(
                b"\xef\xbb\xbf# Alpha\r\n\r\nOld text.\r\n"
            )
            candidates = {
                "format": "conversation-knowledge-curator.knowledge-candidates.v1",
                "candidates": [candidate(CANDIDATE_IDE, "Alpha 新内容。", [])],
            }
            inventory = knowledge_inventory.build_inventory(knowledge_root)
            retrieval = knowledge_retrieval.build_retrieval(
                candidates,
                json_sha256(candidates),
                inventory,
                json_sha256(inventory),
                knowledge_root,
                20,
            )
            target = section_target(inventory, "alpha.md", "Alpha")
            draft = {
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
                        "target": target,
                        "rationale": "更新同一主题。",
                        "operation": {
                            "type": "replace_section",
                            "markdown": "# Alpha\n\nNew text.\n",
                        },
                        "flags": [],
                    }
                ],
            }
            proposals = knowledge_proposals.build_proposals(
                candidates,
                json_sha256(candidates),
                inventory,
                json_sha256(inventory),
                retrieval,
                json_sha256(retrieval),
                draft,
                knowledge_root,
            )

            manifest, artifacts = build_review(
                candidates,
                inventory,
                retrieval,
                proposals,
                knowledge_root,
            )

            entry = manifest["entries"][0]
            after = artifacts[entry["after_path"]]
            self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", after)
            self.assertNotIn(b"\n", after.replace(b"\r\n", b""))

    def test_create_document_diff_starts_from_dev_null(self) -> None:
        candidates, inventory, retrieval, proposals = build_stage_documents()
        draft_target = {
            "path": "new/jetbrains.md",
            "file_sha256": None,
            "section_id": None,
            "section_sha256": None,
        }
        inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
        retrieval = knowledge_retrieval.build_retrieval(
            candidates,
            json_sha256(candidates),
            inventory,
            json_sha256(inventory),
            FIXTURE_ROOT,
            20,
        )
        draft = {
            "format": "conversation-knowledge-curator.proposal-draft.v1",
            "inputs": {
                "knowledge_candidates_sha256": json_sha256(candidates),
                "knowledge_inventory_sha256": json_sha256(inventory),
                "knowledge_retrieval_sha256": json_sha256(retrieval),
            },
            "proposals": [
                {
                    "candidate_ids": [CANDIDATE_IDE],
                    "action": "add",
                    "target": draft_target,
                    "rationale": "创建独立主题。",
                    "operation": {
                        "type": "create_document",
                        "markdown": (
                            "# JetBrains\n\n"
                            "本文记录合成的 IDE 知识。\n\n"
                            "## 1. 适用范围\n\n知识内容。\n"
                        ),
                    },
                    "flags": [],
                },
                {
                    "candidate_ids": [CANDIDATE_PYTHON],
                    "action": "no_change",
                    "target": section_target(
                        inventory,
                        "technologies/python.md",
                        "Python",
                    ),
                    "rationale": "已有主题。",
                    "operation": None,
                    "flags": [],
                },
                {
                    "candidate_ids": [CANDIDATE_DEFER],
                    "action": "defer",
                    "target": None,
                    "rationale": "等待证据。",
                    "operation": None,
                    "flags": ["uncertain"],
                },
            ],
        }
        proposals = knowledge_proposals.build_proposals(
            candidates,
            json_sha256(candidates),
            inventory,
            json_sha256(inventory),
            retrieval,
            json_sha256(retrieval),
            draft,
            FIXTURE_ROOT,
        )

        manifest, artifacts = build_review(
            candidates,
            inventory,
            retrieval,
            proposals,
        )

        entry = next(item for item in manifest["entries"] if item["action"] == "add")
        diff = artifacts[entry["diff_path"]].decode("utf-8")
        self.assertTrue(diff.startswith("--- /dev/null\n+++ b/new/jetbrains.md\n"))
        self.assertIsNone(entry["base_file_sha256"])


class ReviewReportTests(unittest.TestCase):
    def test_report_contains_section_changes_evidence_and_attention(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, manifest = create_review_fixture(root)

            report = knowledge_review.build_review_report(
                FIXTURE_ROOT,
                root / "candidates.json",
                review_path,
            )

            self.assertEqual(
                report["format"],
                "conversation-knowledge-curator.review-report.v1",
            )
            self.assertEqual(report["review_id"], manifest["review_id"])
            self.assertEqual(report["status"], "needs_review")
            self.assertEqual(report["summary"]["proposal_count"], 3)
            self.assertEqual(report["summary"]["writable_count"], 1)
            writable = next(
                entry for entry in report["entries"] if entry["action"] == "merge"
            )
            self.assertEqual(
                writable["candidates"][0]["statement"],
                "项目应兼容 JetBrains IDE。",
            )
            self.assertEqual(
                writable["candidates"][0]["evidence"][0]["message_ids"],
                ["message-1"],
            )
            self.assertIn(
                "JetBrains",
                {
                    change["heading"]
                    for change in writable["section_changes"]["modified"]
                },
            )
            self.assertEqual(report["summary"]["attention_count"], 1)
            self.assertEqual(report["attention"][0]["action"], "defer")

    def test_report_rejects_tampered_candidates(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, _ = create_review_fixture(root)
            candidates_path = root / "candidates.json"
            candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
            candidates["candidates"][0]["statement"] = "被修改"
            candidates_path.write_text(
                json.dumps(candidates, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                knowledge_review.ReviewError,
                "绑定哈希不一致",
            ):
                knowledge_review.build_review_report(
                    FIXTURE_ROOT,
                    candidates_path,
                    review_path,
                )

    def test_report_cli_outputs_json_without_new_report_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, _ = create_review_fixture(root)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_review.main(
                    [
                        "report",
                        str(FIXTURE_ROOT),
                        str(root / "candidates.json"),
                        str(review_path),
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue())["format"],
                "conversation-knowledge-curator.review-report.v1",
            )
            self.assertFalse((review_path / "report.json").exists())
            self.assertFalse((review_path / "report.md").exists())


class ReviewDecisionTests(unittest.TestCase):
    def test_partial_decisions_bind_hashes_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, manifest = create_review_fixture(root)
            writable = next(
                entry for entry in manifest["entries"] if entry["action"] == "merge"
            )
            no_change = next(
                entry
                for entry in manifest["entries"]
                if entry["action"] == "no_change"
            )
            draft_path = root / "decision-draft.json"
            write_json(
                draft_path,
                {
                    "format": (
                        "conversation-knowledge-curator."
                        "review-decision-draft.v1"
                    ),
                    "review_id": manifest["review_id"],
                    "decisions": [
                        {
                            "proposal_id": writable["proposal_id"],
                            "decision": "approved",
                            "note": "已检查 diff。",
                        },
                        {
                            "proposal_id": no_change["proposal_id"],
                            "decision": "rejected",
                            "note": None,
                        },
                    ],
                },
            )

            result = knowledge_review.create_decisions_file(review_path, draft_path)

            self.assertEqual(
                result["format"],
                "conversation-knowledge-curator.review-decisions.v1",
            )
            self.assertEqual(len(result["decisions"]), 2)
            by_id = {item["proposal_id"]: item for item in result["decisions"]}
            self.assertEqual(
                by_id[writable["proposal_id"]]["proposal_sha256"],
                writable["proposal_sha256"],
            )
            self.assertEqual(
                by_id[writable["proposal_id"]]["diff_sha256"],
                writable["diff_sha256"],
            )
            self.assertIsNone(by_id[no_change["proposal_id"]]["diff_sha256"])
            self.assertEqual(
                {item["proposal_id"] for item in result["decisions"]},
                {writable["proposal_id"], no_change["proposal_id"]},
            )

    def test_unknown_duplicate_and_invalid_decisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            _, manifest = create_review_fixture(root)
            proposal_id = manifest["entries"][0]["proposal_id"]
            base = {
                "format": (
                    "conversation-knowledge-curator.review-decision-draft.v1"
                ),
                "review_id": manifest["review_id"],
                "decisions": [],
            }
            unknown = {**base, "decisions": [
                {
                    "proposal_id": "kp_" + "f" * 20,
                    "decision": "approved",
                    "note": None,
                }
            ]}
            duplicate_entry = {
                "proposal_id": proposal_id,
                "decision": "approved",
                "note": None,
            }
            duplicate = {**base, "decisions": [duplicate_entry, duplicate_entry]}
            invalid = {**base, "decisions": [
                {
                    "proposal_id": proposal_id,
                    "decision": "maybe",
                    "note": None,
                }
            ]}

            for draft in (unknown, duplicate, invalid):
                with self.subTest(draft=draft), self.assertRaises(
                    knowledge_review.ReviewError
                ):
                    knowledge_review.build_review_decisions(manifest, draft)

    def test_tampered_diff_prevents_decision_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, manifest = create_review_fixture(root)
            writable = next(
                entry for entry in manifest["entries"] if entry["diff_path"] is not None
            )
            diff_path = review_path.joinpath(*Path(writable["diff_path"]).parts)
            diff_path.write_text("tampered\n", encoding="utf-8")
            draft_path = root / "decision-draft.json"
            write_json(
                draft_path,
                {
                    "format": (
                        "conversation-knowledge-curator."
                        "review-decision-draft.v1"
                    ),
                    "review_id": manifest["review_id"],
                    "decisions": [
                        {
                            "proposal_id": writable["proposal_id"],
                            "decision": "approved",
                            "note": None,
                        }
                    ],
                },
            )

            with self.assertRaises(knowledge_review.ReviewError):
                knowledge_review.create_decisions_file(review_path, draft_path)

            self.assertFalse((review_path / "decisions.json").exists())

    def test_existing_decisions_file_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, _ = create_review_fixture(root)
            decisions_path = review_path / "decisions.json"
            decisions_path.write_text("keep", encoding="utf-8")

            with self.assertRaises(knowledge_review.ReviewError):
                knowledge_review.create_decisions_file(
                    review_path,
                    root / "missing-draft.json",
                )

            self.assertEqual(decisions_path.read_text(encoding="utf-8"), "keep")

    def test_decide_cli_reports_approved_and_rejected_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            review_path, manifest = create_review_fixture(root)
            first, second = manifest["entries"][:2]
            draft_path = root / "decision-draft.json"
            write_json(
                draft_path,
                {
                    "format": (
                        "conversation-knowledge-curator."
                        "review-decision-draft.v1"
                    ),
                    "review_id": manifest["review_id"],
                    "decisions": [
                        {
                            "proposal_id": first["proposal_id"],
                            "decision": "approved",
                            "note": None,
                        },
                        {
                            "proposal_id": second["proposal_id"],
                            "decision": "rejected",
                            "note": "暂不采用。",
                        },
                    ],
                },
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_review.main(
                    ["decide", str(review_path), str(draft_path)]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["approved_count"], 1)
            self.assertEqual(summary["rejected_count"], 1)
            self.assertTrue((review_path / "decisions.json").is_file())


class ReviewPackageCliTests(unittest.TestCase):
    def test_cli_publishes_complete_review_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            candidates, inventory, retrieval, proposals = build_stage_documents()
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            retrieval_path = root / "retrieval.json"
            proposals_path = root / "proposals.json"
            output_path = root / "review"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates)
            write_json(retrieval_path, retrieval)
            write_json(proposals_path, proposals)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_review.main(
                    [
                        "prepare",
                        str(FIXTURE_ROOT),
                        str(inventory_path),
                        str(candidates_path),
                        str(retrieval_path),
                        str(proposals_path),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            summary = json.loads(stdout.getvalue())
            manifest = json.loads(
                (output_path / "review-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["writable_count"], 1)
            self.assertEqual(summary["review_id"], manifest["review_id"])
            self.assertTrue((output_path / "proposals.json").is_file())
            self.assertFalse((output_path / "decisions.json").exists())
            writable = next(
                entry for entry in manifest["entries"] if entry["after_path"] is not None
            )
            self.assertTrue((output_path / writable["after_path"]).is_file())
            self.assertTrue((output_path / writable["diff_path"]).is_file())

    def test_existing_directory_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            output_path = root / "review"
            output_path.mkdir()
            marker = output_path / "keep.txt"
            marker.write_text("keep", encoding="utf-8")

            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = knowledge_review.main(
                    [
                        "prepare",
                        str(FIXTURE_ROOT),
                        str(root / "missing-inventory.json"),
                        str(root / "missing-candidates.json"),
                        str(root / "missing-retrieval.json"),
                        str(root / "missing-proposals.json"),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertTrue(stderr.getvalue().strip())
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_missing_inputs_do_not_leave_review_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            output_path = root / "review"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = knowledge_review.main(
                    [
                        "prepare",
                        str(FIXTURE_ROOT),
                        str(root / "missing-inventory.json"),
                        str(root / "missing-candidates.json"),
                        str(root / "missing-retrieval.json"),
                        str(root / "missing-proposals.json"),
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertTrue(stderr.getvalue().strip())
            self.assertFalse(output_path.exists())

    def test_quality_failure_does_not_leave_review_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            inventory = knowledge_inventory.build_inventory(FIXTURE_ROOT)
            target = section_target(
                inventory,
                "preferences/development-tools.md",
                "开发工具偏好",
            )
            candidates, inventory, retrieval, proposals = build_stage_documents(
                ide_operation={
                    "type": "replace_section",
                    "markdown": "### 错误层级\n\n正文。\n",
                },
                ide_target=target,
            )
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            retrieval_path = root / "retrieval.json"
            proposals_path = root / "proposals.json"
            output_path = root / "review"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates)
            write_json(retrieval_path, retrieval)
            write_json(proposals_path, proposals)

            with self.assertRaisesRegex(
                knowledge_review.ReviewError,
                "质量检查失败",
            ):
                knowledge_review.create_review_package(
                    FIXTURE_ROOT,
                    inventory_path,
                    candidates_path,
                    retrieval_path,
                    proposals_path,
                    output_path,
                )

            self.assertFalse(output_path.exists())

    def test_review_directory_cannot_be_inside_knowledge_root(self) -> None:
        output_path = FIXTURE_ROOT / ".review-test-output"
        self.assertFalse(output_path.exists())

        with self.assertRaises(knowledge_review.ReviewError):
            knowledge_review.create_review_package(
                FIXTURE_ROOT,
                Path("missing-inventory.json"),
                Path("missing-candidates.json"),
                Path("missing-retrieval.json"),
                Path("missing-proposals.json"),
                output_path,
            )

        self.assertFalse(output_path.exists())

    def test_review_directory_can_use_reserved_run_location(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root = root / "knowledge"
            shutil.copytree(FIXTURE_ROOT, knowledge_root)
            candidates, inventory, retrieval, proposals = build_stage_documents(
                knowledge_root
            )
            inventory_path = root / "inventory.json"
            candidates_path = root / "candidates.json"
            retrieval_path = root / "retrieval.json"
            proposals_path = root / "proposals.json"
            write_json(inventory_path, inventory)
            write_json(candidates_path, candidates)
            write_json(retrieval_path, retrieval)
            write_json(proposals_path, proposals)
            run_root = (
                knowledge_root
                / ".knowledge-curator"
                / "runs"
                / "reserved-run"
            )
            run_root.mkdir(parents=True)
            output_path = run_root / "review"

            manifest = knowledge_review.create_review_package(
                knowledge_root,
                inventory_path,
                candidates_path,
                retrieval_path,
                proposals_path,
                output_path,
            )

            self.assertTrue((output_path / "review-manifest.json").is_file())
            self.assertEqual(
                manifest["format"],
                "conversation-knowledge-curator.review-manifest.v1",
            )


if __name__ == "__main__":
    unittest.main()
