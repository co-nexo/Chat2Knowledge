"""第四阶段已批准知识变更应用与恢复测试。

已覆盖的应用前校验行为：

1. 只选择 decisions 中明确 approved 且具有写操作的 Proposal。
2. 拒绝过期知识库、被修改的 decisions、after、diff 和审核绑定。
3. 所有目标预检成功后才开始写入，已有 apply manifest 不会被覆盖。

已覆盖的写入行为：

1. 现有文件和新建文档使用审核过的 after 字节，并生成最小 apply manifest。
2. rejected、未决定及非写入提案不会修改知识库。
3. 多文件写入中途失败时自动恢复已经替换的文件。
4. 应用状态和回滚副本保存在私密 review 目录，不写入正式知识内容。

已覆盖的恢复行为：

1. apply-state 可以识别并恢复中断后的已替换文件。
2. apply-state 绑定原知识库根目录，拒绝在其他根目录恢复。
3. 恢复只覆盖仍等于 before 或 after 哈希的目标，拒绝额外修改。
4. 成功恢复后清理暂存文件、回滚副本和新建空目录。

已覆盖的 CLI 行为：

1. apply 和 recover 输出结构化汇总，失败返回退出码 2。
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
from unittest import mock

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
knowledge_apply = load_script(
    "knowledge_apply",
    SCRIPTS_DIR / "knowledge_apply.py",
)

SOURCE_SHA256 = "a" * 64
CANDIDATE_IDE = "kc_" + "1" * 20
CANDIDATE_PYTHON = "kc_" + "2" * 20
CANDIDATE_DEFER = "kc_" + "3" * 20


def json_bytes(value: object) -> bytes:
    """按阶段产物格式序列化 JSON。"""

    return knowledge_inventory.serialize_json(value).encode("utf-8")


def json_sha256(value: object) -> str:
    """计算正式 JSON 字节的 SHA-256。"""

    return hashlib.sha256(json_bytes(value)).hexdigest()


def write_json(path: Path, value: object) -> None:
    """独占创建使用 LF 的测试 JSON 文件。"""

    knowledge_inventory.write_new_file(
        path,
        knowledge_inventory.serialize_json(value),
    )


def candidate(candidate_id: str, statement: str, flags: list[str]) -> dict[str, object]:
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


def section_target(
    inventory: dict[str, object],
    path: str,
    heading: str,
) -> dict[str, object]:
    """从 inventory 取得首个匹配标题的真实 Target。"""

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


def create_pipeline(
    root: Path,
    decisions_by_candidate: dict[str, str],
    create_document: bool = False,
) -> tuple[Path, Path, dict[str, bytes]]:
    """创建知识库副本、审核包和指定的最终审核决定。"""

    knowledge_root = root / "knowledge"
    shutil.copytree(FIXTURE_ROOT, knowledge_root)
    before_files = {
        path.relative_to(knowledge_root).as_posix(): path.read_bytes()
        for path in knowledge_root.rglob("*.md")
    }
    candidates = {
        "format": "conversation-knowledge-curator.knowledge-candidates.v1",
        "candidates": [
            candidate(CANDIDATE_IDE, "项目应兼容 JetBrains IDE。", []),
            candidate(CANDIDATE_PYTHON, "补充 Python 使用建议。", []),
            candidate(CANDIDATE_DEFER, "未经确认的临时偏好。", ["uncertain"]),
        ],
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
    if create_document:
        ide_target = {
            "path": "new/jetbrains.md",
            "file_sha256": None,
            "section_id": None,
            "section_sha256": None,
        }
        ide_action = "add"
        ide_operation = {
            "type": "create_document",
            "markdown": (
                "# JetBrains\n\n"
                "本文记录合成的 IDE 知识。\n\n"
                "## 1. 适用范围\n\n项目应兼容 JetBrains IDE。\n"
            ),
        }
    else:
        ide_target = section_target(
            inventory,
            "preferences/development-tools.md",
            "JetBrains",
        )
        ide_action = "merge"
        ide_operation = {
            "type": "append_to_section",
            "markdown": "\n项目应继续兼容 JetBrains IDE。\n",
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
                "target": ide_target,
                "rationale": "处理 IDE 主题。",
                "operation": ide_operation,
                "flags": [],
            },
            {
                "candidate_ids": [CANDIDATE_PYTHON],
                "action": "merge",
                "target": section_target(
                    inventory,
                    "technologies/python.md",
                    "Python",
                ),
                "rationale": "融合到 Python 文档。",
                "operation": {
                    "type": "append_to_section",
                    "markdown": "\n补充 Python 使用建议。\n",
                },
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
        knowledge_root,
        inventory_path,
        candidates_path,
        retrieval_path,
        proposals_path,
        review_path,
    )
    decisions: list[dict[str, object]] = []
    for entry in manifest["entries"]:
        candidate_id = entry["candidate_ids"][0]
        decision = decisions_by_candidate.get(candidate_id)
        if decision is not None:
            decisions.append(
                {
                    "proposal_id": entry["proposal_id"],
                    "decision": decision,
                    "note": None,
                }
            )
    decision_draft_path = root / "decision-draft.json"
    write_json(
        decision_draft_path,
        {
            "format": (
                "conversation-knowledge-curator.review-decision-draft.v1"
            ),
            "review_id": manifest["review_id"],
            "decisions": decisions,
        },
    )
    knowledge_review.create_decisions_file(review_path, decision_draft_path)
    return knowledge_root, review_path, before_files


class ApplyPreflightTests(unittest.TestCase):
    def test_stale_knowledge_stops_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved", CANDIDATE_PYTHON: "approved"},
            )
            changed = knowledge_root / "technologies" / "python.md"
            changed.write_text("# 外部变化\n", encoding="utf-8")
            untouched = knowledge_root / "preferences" / "development-tools.md"
            before_untouched = untouched.read_bytes()

            with self.assertRaises(knowledge_apply.ApplyError):
                knowledge_apply.apply_approved_changes(knowledge_root, review_path)

            self.assertEqual(untouched.read_bytes(), before_untouched)
            self.assertFalse((review_path / "apply-manifest.json").exists())
            self.assertFalse((review_path / "apply-state.json").exists())

    def test_tampered_decisions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, before_files = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            decisions_path = review_path / "decisions.json"
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            decisions["decisions"][0]["proposal_sha256"] = "f" * 64
            decisions_path.write_text(
                json.dumps(decisions, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(knowledge_apply.ApplyError):
                knowledge_apply.apply_approved_changes(knowledge_root, review_path)

            for relative_path, content in before_files.items():
                self.assertEqual(
                    knowledge_root.joinpath(*Path(relative_path).parts).read_bytes(),
                    content,
                )


class ApplyWriteTests(unittest.TestCase):
    def test_only_explicitly_approved_write_is_applied(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, before_files = create_pipeline(
                root,
                {
                    CANDIDATE_IDE: "approved",
                    CANDIDATE_PYTHON: "rejected",
                    CANDIDATE_DEFER: "approved",
                },
            )

            manifest = knowledge_apply.apply_approved_changes(
                knowledge_root,
                review_path,
            )

            self.assertEqual(len(manifest["entries"]), 1)
            self.assertIn(
                "项目应继续兼容 JetBrains IDE",
                (
                    knowledge_root / "preferences" / "development-tools.md"
                ).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (knowledge_root / "technologies" / "python.md").read_bytes(),
                before_files["technologies/python.md"],
            )
            self.assertTrue((review_path / "apply-manifest.json").is_file())
            self.assertTrue((review_path / "apply-state.json").is_file())
            self.assertTrue((review_path / "rollback").is_dir())

    def test_create_document_uses_reviewed_after_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
                create_document=True,
            )

            manifest = knowledge_apply.apply_approved_changes(
                knowledge_root,
                review_path,
            )

            target = knowledge_root / "new" / "jetbrains.md"
            self.assertTrue(target.is_file())
            entry = manifest["entries"][0]
            self.assertIsNone(entry["before_sha256"])
            self.assertEqual(
                entry["after_sha256"],
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )

    def test_missing_write_decisions_create_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, before_files = create_pipeline(
                root,
                {CANDIDATE_DEFER: "approved"},
            )

            manifest = knowledge_apply.apply_approved_changes(
                knowledge_root,
                review_path,
            )

            self.assertEqual(manifest["entries"], [])
            for relative_path, content in before_files.items():
                self.assertEqual(
                    knowledge_root.joinpath(*Path(relative_path).parts).read_bytes(),
                    content,
                )
            self.assertFalse((review_path / "apply-state.json").exists())

    def test_existing_apply_manifest_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            first = knowledge_apply.apply_approved_changes(
                knowledge_root,
                review_path,
            )
            manifest_path = review_path / "apply-manifest.json"
            before_manifest = manifest_path.read_bytes()

            with self.assertRaises(knowledge_apply.ApplyError):
                knowledge_apply.apply_approved_changes(knowledge_root, review_path)

            self.assertEqual(manifest_path.read_bytes(), before_manifest)
            self.assertEqual(first["status"], "applied")

    def test_multi_file_failure_rolls_back_first_replacement(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, before_files = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved", CANDIDATE_PYTHON: "approved"},
            )
            real_replace = knowledge_apply.replace_target
            call_count = 0

            def fail_second_replace(temporary_path: Path, target: Path) -> None:
                """仅让第二次正式目标替换失败，恢复替换继续使用真实实现。"""

                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("synthetic failure")
                real_replace(temporary_path, target)

            with mock.patch.object(
                knowledge_apply,
                "replace_target",
                side_effect=fail_second_replace,
            ), self.assertRaises(knowledge_apply.ApplyError):
                knowledge_apply.apply_approved_changes(knowledge_root, review_path)

            for relative_path, content in before_files.items():
                self.assertEqual(
                    knowledge_root.joinpath(*Path(relative_path).parts).read_bytes(),
                    content,
                )
            self.assertFalse((review_path / "apply-state.json").exists())
            self.assertFalse((review_path / "rollback").exists())
            self.assertFalse((review_path / "apply-manifest.json").exists())


class ApplyRecoveryTests(unittest.TestCase):
    def test_recover_restores_interrupted_applied_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, before_files = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            (
                review_manifest,
                _,
                plans,
                created_directories,
                _,
                _,
            ) = knowledge_apply.preflight_apply(knowledge_root, review_path)
            state = knowledge_apply.state_document(
                knowledge_root,
                review_manifest["review_id"],
                plans,
                created_directories,
            )
            state["status"] = "applying"
            state["entries"][0]["result"] = "applied"
            knowledge_apply.write_state(
                review_path / "apply-state.json",
                state,
                create=True,
            )
            rollback = review_path / "rollback"
            rollback.mkdir()
            plan = plans[0]
            assert plan.before_bytes is not None and plan.backup_path is not None
            knowledge_apply.write_bytes_new(
                review_path.joinpath(*Path(plan.backup_path).parts),
                plan.before_bytes,
            )
            plan.target.write_bytes(plan.after_bytes)

            result = knowledge_apply.recover_interrupted_apply(
                knowledge_root,
                review_path,
            )

            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(
                plan.target.read_bytes(),
                before_files[plan.target_path],
            )
            self.assertFalse((review_path / "apply-state.json").exists())
            self.assertFalse((review_path / "rollback").exists())

    def test_recover_rejects_target_modified_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            review_manifest, _, plans, created_directories, _, _ = (
                knowledge_apply.preflight_apply(knowledge_root, review_path)
            )
            state = knowledge_apply.state_document(
                knowledge_root,
                review_manifest["review_id"],
                plans,
                created_directories,
            )
            state["status"] = "applying"
            knowledge_apply.write_state(
                review_path / "apply-state.json",
                state,
                create=True,
            )
            (review_path / "rollback").mkdir()
            plan = plans[0]
            assert plan.before_bytes is not None and plan.backup_path is not None
            knowledge_apply.write_bytes_new(
                review_path.joinpath(*Path(plan.backup_path).parts),
                plan.before_bytes,
            )
            plan.target.write_text("external change\n", encoding="utf-8")

            with self.assertRaises(knowledge_apply.ApplyError):
                knowledge_apply.recover_interrupted_apply(
                    knowledge_root,
                    review_path,
                )

            self.assertEqual(
                plan.target.read_text(encoding="utf-8"),
                "external change\n",
            )
            self.assertTrue((review_path / "apply-state.json").exists())

    def test_recover_rejects_different_knowledge_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            review_manifest, _, plans, created_directories, _, _ = (
                knowledge_apply.preflight_apply(knowledge_root, review_path)
            )
            state = knowledge_apply.state_document(
                knowledge_root,
                review_manifest["review_id"],
                plans,
                created_directories,
            )
            knowledge_apply.write_state(
                review_path / "apply-state.json",
                state,
                create=True,
            )
            other_root = root / "other-knowledge"
            shutil.copytree(FIXTURE_ROOT, other_root)

            with self.assertRaises(knowledge_apply.ApplyError):
                knowledge_apply.recover_interrupted_apply(
                    other_root,
                    review_path,
                )

            self.assertTrue((review_path / "apply-state.json").exists())


class ApplyCliTests(unittest.TestCase):
    def test_apply_cli_reports_structured_summary(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_apply.main(
                    ["apply", str(knowledge_root), str(review_path)]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["status"], "applied")
            self.assertEqual(summary["applied_count"], 1)

    def test_apply_cli_failure_returns_two(self) -> None:
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = knowledge_apply.main(
                ["apply", str(FIXTURE_ROOT), "missing-review"]
            )

        self.assertEqual(exit_code, 2)
        self.assertTrue(stderr.getvalue().strip())

    def test_recover_cli_reports_structured_summary(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, review_path, _ = create_pipeline(
                root,
                {CANDIDATE_IDE: "approved"},
            )
            review_manifest, _, plans, created_directories, _, _ = (
                knowledge_apply.preflight_apply(knowledge_root, review_path)
            )
            state = knowledge_apply.state_document(
                knowledge_root,
                review_manifest["review_id"],
                plans,
                created_directories,
            )
            knowledge_apply.write_state(
                review_path / "apply-state.json",
                state,
                create=True,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr
            ):
                exit_code = knowledge_apply.main(
                    ["recover", str(knowledge_root), str(review_path)]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["status"], "rolled_back")
            self.assertEqual(summary["restored_count"], 0)
            self.assertFalse((review_path / "apply-state.json").exists())


if __name__ == "__main__":
    unittest.main()
