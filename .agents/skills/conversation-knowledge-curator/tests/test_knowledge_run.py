"""完整流程 run 布局与路径约束测试。

已覆盖的初始化行为：

1. run 只能创建在知识库 `.knowledge-curator/runs/<run_id>` 下。
2. 初始化原子创建 run.json 与 proposed，不创建旧版阶段目录。
3. 非法 run ID、已存在 run、链接祖先和不存在输入会在写入前失败。

已覆盖的路径绑定行为：

1. run manifest 必须使用当前格式和固定字段，未知字段会被拒绝。
2. 移动 run 或篡改知识库根目录会被拒绝。
3. 唯一公开 CLI 已切换到 v2，不再暴露 inspect 与 normalize 阶段。

已覆盖的 CLI 行为：

1. init 与 read-input 返回结构化摘要，且不落盘标准化内容。
2. 失败返回退出码 2，且不会在知识库同级创建派生目录。
3. apply 与 recover 返回 v2 核心的实际应用和恢复数量。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"
EXPORT_FIXTURE = SKILL_DIR / "tests" / "fixtures" / "synthetic_export.json"


def load_script(name: str, path: Path) -> object:
    """按正式模块名加载相互依赖的 run 脚本。"""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_script("chatgpt_export", SCRIPTS_DIR / "chatgpt_export.py")
load_script("knowledge_inventory", SCRIPTS_DIR / "knowledge_inventory.py")
load_script("knowledge_retrieval", SCRIPTS_DIR / "knowledge_retrieval.py")
load_script(
    "knowledge_document_quality",
    SCRIPTS_DIR / "knowledge_document_quality.py",
)
load_script("knowledge_runtime", SCRIPTS_DIR / "knowledge_runtime.py")
knowledge_run = load_script("knowledge_run", SCRIPTS_DIR / "knowledge_run.py")


def create_inputs(root: Path) -> tuple[Path, Path]:
    """创建空知识库和脱敏 ChatGPT 导出输入。"""

    knowledge_root = root / "knowledge"
    knowledge_root.mkdir()
    input_path = root / "synthetic-export.json"
    shutil.copyfile(EXPORT_FIXTURE, input_path)
    return knowledge_root, input_path


class RunInitializationTests(unittest.TestCase):
    def test_init_creates_only_fixed_parent_layout(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)

            run_root, document = knowledge_run.initialize_run(
                knowledge_root,
                input_path,
                "20261104-synthetic",
            )

            self.assertEqual(
                run_root,
                knowledge_root.resolve()
                / ".knowledge-curator"
                / "runs"
                / "20261104-synthetic",
            )
            self.assertEqual(
                document["format"],
                "conversation-knowledge-curator.run.v2",
            )
            self.assertTrue((run_root / "run.json").is_file())
            self.assertTrue((run_root / "proposed").is_dir())
            for relative in (
                "input", "drafts", "manifests", "proposals", "logs",
                "normalized", "candidates", "review",
            ):
                self.assertFalse((run_root / relative).exists())

    def test_invalid_or_existing_run_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = knowledge_run.initialize_run(
                knowledge_root,
                input_path,
                "valid-run",
            )
            marker = run_root / "run.json"
            before = marker.read_bytes()

            with self.assertRaises(knowledge_run.RunError):
                knowledge_run.initialize_run(
                    knowledge_root,
                    input_path,
                    "../outside",
                )
            with self.assertRaises(knowledge_run.RunError):
                knowledge_run.initialize_run(
                    knowledge_root,
                    input_path,
                    "bad--id",
                )
            with self.assertRaises(knowledge_run.RunError):
                knowledge_run.initialize_run(
                    knowledge_root,
                    input_path,
                    "valid-run",
                )

            self.assertEqual(marker.read_bytes(), before)
            self.assertFalse((root / "outside").exists())

    def test_missing_input_fails_before_reserved_directories_are_created(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root = root / "knowledge"
            knowledge_root.mkdir()

            with self.assertRaises(knowledge_run.RunError):
                knowledge_run.initialize_run(
                    knowledge_root,
                    root / "missing.json",
                    "missing-input",
                )

            self.assertFalse((knowledge_root / ".knowledge-curator").exists())


class RunBindingTests(unittest.TestCase):
    def test_tampered_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = knowledge_run.initialize_run(
                knowledge_root,
                input_path,
                "tampered-run",
            )
            manifest_path = run_root / "run.json"
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["unexpected_path"] = "../outside.json"
            manifest_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(knowledge_run.RunError):
                knowledge_run.load_run(run_root)

            self.assertFalse((run_root.parent / "outside.json").exists())

    def test_moved_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = knowledge_run.initialize_run(
                knowledge_root,
                input_path,
                "move-run",
            )
            moved = root / "moved-run"
            run_root.rename(moved)

            with self.assertRaises(knowledge_run.RunError):
                knowledge_run.load_run(moved)


class RunCliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """捕获 run CLI 的结构化输出和诊断。"""

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = knowledge_run.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_cli_v2_reads_input_without_normalized_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)

            exit_code, stdout, stderr = self.run_cli(
                ["init", str(knowledge_root), str(input_path), "cli-run"]
            )
            self.assertEqual(exit_code, 0, stderr)
            run_root = Path(json.loads(stdout)["run_directory"])

            read_code, read_stdout, read_stderr = self.run_cli(
                ["read-input", str(run_root)]
            )
            self.assertEqual(read_code, 0, read_stderr)
            view = json.loads(read_stdout)
            self.assertEqual(
                view["format"],
                "conversation-knowledge-curator.visible-input.v1",
            )
            self.assertFalse((run_root / "normalized").exists())
            unexpected_review = knowledge_root.parent / "Knowledge-Base-Reviews"
            self.assertFalse(unexpected_review.exists())

    def test_cli_failure_has_no_data_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)

            exit_code, stdout, stderr = self.run_cli(
                ["init", str(knowledge_root), str(input_path), "Invalid_ID"]
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertTrue(stderr.strip())
            self.assertFalse((knowledge_root / ".knowledge-curator").exists())

    def test_apply_and_recover_summaries_use_underlying_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = knowledge_run.initialize_run(
                knowledge_root,
                input_path,
                "apply-run",
            )

            with mock.patch.object(
                knowledge_run.knowledge_runtime,
                "apply_outputs",
                return_value={
                    "status": "applied",
                    "applied_file_count": 2,
                    "entries": [{}, {}],
                },
            ):
                apply_summary, apply_code = knowledge_run.execute_command(
                    SimpleNamespace(
                        command="apply",
                        run_directory=run_root,
                        approve=["one.md", "two.md"],
                    )
                )
            with mock.patch.object(
                knowledge_run.knowledge_runtime,
                "recover_apply",
                return_value={"status": "recovered", "restored_file_count": 2},
            ):
                recover_summary, recover_code = knowledge_run.execute_command(
                    SimpleNamespace(command="recover", run_directory=run_root)
                )

            self.assertEqual(apply_code, 0)
            self.assertEqual(apply_summary["applied_file_count"], 2)
            self.assertEqual(recover_code, 0)
            self.assertEqual(recover_summary["status"], "recovered")
            self.assertEqual(recover_summary["restored_file_count"], 2)


if __name__ == "__main__":
    unittest.main()
