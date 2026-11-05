"""单流程 run.v2 的行为与安全边界测试。

已覆盖的初始化与读取行为：

1. 初始化只创建 run.json 和 proposed，不持久化 normalized、候选或检索结果。
2. run.json 使用 v2，保存输入、解析批次和知识库快照，不复制对话正文。
3. read-input 每次从原始 JSON 动态重建可见消息，并拒绝变化后的输入。
4. 空知识库的 context 明确跳过检索。
5. 非空知识库的 context 返回有限相关原文，但不保存 inventory 或 retrieval 文件。
6. 运行入口接受单个 JSON/ZIP，并拒绝目录成为第二种输入流程。

已覆盖的审核与应用行为：

1. prepare-review 校验来源元数据；新建文档默认不重复输出正文，完整 diff 可按需请求。
2. 审核绑定目标、基线和 proposed 哈希；内容变化后旧批准失效。
3. apply 必须明确批准全部目标，且不会执行任何 Git 操作。
4. 知识库基线变化会在任何写入前停止，多文件中断可由 recover 恢复。
5. recover 遇到中断后外部修改时拒绝覆盖，并保留恢复状态。
6. 每个输出精确登记真实 message ID；缺失或伪造来源时不更新 run。
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
import zipfile
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = SKILL_DIR.parents[2]
SCRIPTS_DIR = SKILL_DIR / "scripts"
FIXTURE = SKILL_DIR / "tests" / "fixtures" / "synthetic_export.json"


def load_script(name: str) -> object:
    """按正式模块名加载脚本及其依赖。"""

    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_script("chatgpt_export")
load_script("knowledge_inventory")
load_script("knowledge_retrieval")
load_script("knowledge_document_quality")
runtime = load_script("knowledge_runtime")


def create_inputs(root: Path) -> tuple[Path, Path]:
    """创建空知识库和脱敏导出副本。"""

    knowledge_root = root / "knowledge"
    knowledge_root.mkdir()
    input_path = root / "export.json"
    shutil.copyfile(FIXTURE, input_path)
    return knowledge_root, input_path


def review_metadata(
    run_root: Path,
    target_paths: list[str],
    issues: list[dict[str, object]] | None = None,
) -> str:
    """根据真实可见消息构造测试用审核元数据。"""

    _, source, document = runtime.load_run(run_root)
    _, metadata = runtime.current_view(source, document)
    conversation = metadata["conversations"][0]
    source_ref = {
        "source_sha256": conversation["source_sha256"],
        "conversation_id": conversation["conversation_id"],
        "message_ids": [
            message["message_id"] for message in conversation["messages"]
        ],
    }
    return json.dumps(
        {
            "format": "conversation-knowledge-curator.review-input.v1",
            "outputs": [
                {"target_path": path, "sources": [source_ref]}
                for path in target_paths
            ],
            "issues": issues or [],
        },
        ensure_ascii=False,
    )


class RuntimeV2Tests(unittest.TestCase):
    def run_cli(
        self,
        argv: list[str],
        stdin_text: str = "",
    ) -> tuple[int, dict[str, object], str]:
        """运行 CLI 并解析结构化标准输出。"""

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            mock.patch.object(runtime.sys, "stdin", io.StringIO(stdin_text)),
        ):
            exit_code = runtime.main(argv)
        value = json.loads(stdout.getvalue()) if stdout.getvalue() else {}
        return exit_code, value, stderr.getvalue()

    def test_init_and_read_input_do_not_persist_intermediate_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)

            code, result, stderr = self.run_cli(
                ["init", str(knowledge_root), str(input_path), "runtime-v2"]
            )

            self.assertEqual(code, 0, stderr)
            run_root = Path(str(result["run_directory"]))
            self.assertEqual(
                sorted(path.name for path in run_root.iterdir()),
                ["proposed", "run.json"],
            )
            run = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["format"], "conversation-knowledge-curator.run.v2")
            self.assertNotIn("conversations", run["parsing"])
            self.assertTrue(run["knowledge_base"]["empty"])

            read_code, view, read_stderr = self.run_cli(
                ["read-input", str(run_root)]
            )
            self.assertEqual(read_code, 0, read_stderr)
            self.assertEqual(
                view["format"], "conversation-knowledge-curator.visible-input.v1"
            )
            self.assertTrue(view["sources"][0]["conversations"])
            self.assertFalse(any(run_root.rglob("*normalized*")))

            input_path.write_text("[]\n", encoding="utf-8")
            changed_code, changed, changed_stderr = self.run_cli(
                ["read-input", str(run_root)]
            )
            self.assertEqual(changed_code, 2)
            self.assertEqual(changed, {})
            self.assertIn("变化", changed_stderr)

    def test_init_accepts_zip_and_rejects_directory_input(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root = root / "knowledge"
            knowledge_root.mkdir()
            archive = root / "export.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.write(FIXTURE, "conversations.json")

            zip_code, zip_result, zip_stderr = self.run_cli(
                ["init", str(knowledge_root), str(archive), "zip-input"]
            )
            self.assertEqual(zip_code, 0, zip_stderr)
            self.assertTrue(Path(str(zip_result["run_directory"])).is_dir())

            second_root = root / "knowledge-two"
            second_root.mkdir()
            input_directory = root / "inputs"
            input_directory.mkdir()
            shutil.copyfile(FIXTURE, input_directory / "export.json")
            directory_code, _, directory_stderr = self.run_cli(
                [
                    "init", str(second_root), str(input_directory),
                    "directory-input",
                ]
            )
            self.assertEqual(directory_code, 2)
            self.assertIn("只接受单个", directory_stderr)
            self.assertFalse((second_root / ".knowledge-curator").exists())

    def test_empty_context_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "empty-context"
            )

            code, result, stderr = self.run_cli(
                ["context", str(run_root), "--query", "Python"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(result["status"], "skipped_empty_knowledge_base")
            self.assertEqual(result["matches"], [])

    def test_nonempty_context_returns_markdown_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            (knowledge_root / "python.md").write_text(
                "# Python\n\n## 1. 虚拟环境\n\n使用 venv 管理环境。\n",
                encoding="utf-8",
            )
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "nonempty-context"
            )

            code, result, stderr = self.run_cli(
                ["context", str(run_root), "--query", "Python venv", "--limit", "3"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["matches"])
            self.assertTrue(
                any("venv" in match["markdown"] for match in result["matches"])
            )
            self.assertFalse(any(run_root.rglob("*inventory*")))
            self.assertFalse(any(run_root.rglob("*retrieval*")))

    def test_review_and_apply_bind_current_hashes(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "review-apply"
            )
            proposed = run_root / "proposed" / "topics" / "example.md"
            proposed.parent.mkdir()
            proposed.write_text(
                "# 示例知识\n\n## 1. 结论\n\n这是整理后的结论。\n",
                encoding="utf-8",
                newline="\n",
            )

            metadata_input = review_metadata(
                run_root,
                ["topics/example.md"],
                [
                    {
                        "kind": "time_sensitive",
                        "message": "需要复核版本范围",
                        "target_path": "topics/example.md",
                    }
                ],
            )
            review_code, review, review_stderr = self.run_cli(
                ["prepare-review", str(run_root)], metadata_input
            )

            self.assertEqual(review_code, 0, review_stderr)
            self.assertFalse(review["outputs"][0]["diff_included"])
            self.assertNotIn("diff", review["outputs"][0])
            self.assertEqual(review["outputs"][0]["outline"]["title"], "示例知识")
            self.assertFalse(any(run_root.rglob("*.diff")))
            run = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["outputs"][0]["target_path"], "topics/example.md")
            self.assertEqual(run["issues"][0]["kind"], "time_sensitive")
            self.assertIsNone(run["approval"])

            full_code, full_review, full_stderr = self.run_cli(
                ["prepare-review", str(run_root), "--full-diff"],
                metadata_input,
            )
            self.assertEqual(full_code, 0, full_stderr)
            self.assertIn(
                "+++ b/topics/example.md", full_review["outputs"][0]["diff"]
            )

            missing_code, _, missing_stderr = self.run_cli(
                ["apply", str(run_root), "--approve", "other.md"]
            )
            self.assertEqual(missing_code, 2)
            self.assertIn("全部目标", missing_stderr)
            self.assertFalse((knowledge_root / "topics" / "example.md").exists())

            apply_code, applied, apply_stderr = self.run_cli(
                ["apply", str(run_root), "--approve", "topics/example.md"]
            )
            self.assertEqual(apply_code, 0, apply_stderr)
            self.assertEqual(applied["applied_file_count"], 1)
            self.assertEqual(
                (knowledge_root / "topics" / "example.md").read_bytes(),
                proposed.read_bytes(),
            )
            self.assertFalse((run_root / "apply-state.json").exists())

    def test_review_metadata_precisely_maps_and_validates_sources(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "source-mapping"
            )
            (run_root / "proposed" / "one.md").write_text(
                "# 一\n\n## 1. 内容\n\n内容。\n", encoding="utf-8"
            )
            (run_root / "proposed" / "two.md").write_text(
                "# 二\n\n## 1. 内容\n\n内容。\n", encoding="utf-8"
            )
            _, source, document = runtime.load_run(run_root)
            _, metadata = runtime.current_view(source, document)
            conversation = metadata["conversations"][0]
            message_ids = [
                message["message_id"] for message in conversation["messages"]
            ]
            self.assertGreaterEqual(len(message_ids), 2)

            missing_code, _, missing_stderr = self.run_cli(
                ["prepare-review", str(run_root)]
            )
            self.assertEqual(missing_code, 2)
            self.assertIn("标准输入", missing_stderr)

            invalid = {
                "format": "conversation-knowledge-curator.review-input.v1",
                "outputs": [
                    {
                        "target_path": path,
                        "sources": [
                            {
                                "source_sha256": conversation["source_sha256"],
                                "conversation_id": conversation["conversation_id"],
                                "message_ids": ["unknown-message"],
                            }
                        ],
                    }
                    for path in ("one.md", "two.md")
                ],
                "issues": [],
            }
            invalid_code, _, invalid_stderr = self.run_cli(
                ["prepare-review", str(run_root)],
                json.dumps(invalid, ensure_ascii=False),
            )
            self.assertEqual(invalid_code, 2)
            self.assertIn("未知可见消息", invalid_stderr)
            unchanged = json.loads(
                (run_root / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(unchanged["outputs"], [])

            valid = {
                "format": "conversation-knowledge-curator.review-input.v1",
                "outputs": [
                    {
                        "target_path": path,
                        "sources": [
                            {
                                "source_sha256": conversation["source_sha256"],
                                "conversation_id": conversation["conversation_id"],
                                "message_ids": [message_id],
                            }
                        ],
                    }
                    for path, message_id in zip(
                        ("one.md", "two.md"), message_ids[:2], strict=True
                    )
                ],
                "issues": [],
            }
            valid_code, _, valid_stderr = self.run_cli(
                ["prepare-review", str(run_root)],
                json.dumps(valid, ensure_ascii=False),
            )
            self.assertEqual(valid_code, 0, valid_stderr)
            updated = json.loads(
                (run_root / "run.json").read_text(encoding="utf-8")
            )
            sources_by_target = {
                output["target_path"]: output["sources"][0]["message_ids"]
                for output in updated["outputs"]
            }
            self.assertEqual(sources_by_target["one.md"], [message_ids[0]])
            self.assertEqual(sources_by_target["two.md"], [message_ids[1]])

    def test_modified_proposed_invalidates_review_binding(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "tampered-proposed"
            )
            proposed = run_root / "proposed" / "example.md"
            proposed.write_text(
                "# 示例\n\n## 1. 初始内容\n\n内容。\n", encoding="utf-8"
            )
            code, _, stderr = self.run_cli(
                ["prepare-review", str(run_root)],
                review_metadata(run_root, ["example.md"]),
            )
            self.assertEqual(code, 0, stderr)
            proposed.write_text(
                "# 示例\n\n## 1. 被修改内容\n\n内容。\n", encoding="utf-8"
            )

            apply_code, _, apply_stderr = self.run_cli(
                ["apply", str(run_root), "--approve", "example.md"]
            )

            self.assertEqual(apply_code, 2)
            self.assertIn("批准失效", apply_stderr)
            self.assertFalse((knowledge_root / "example.md").exists())

    def test_stale_target_stops_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            existing = knowledge_root / "existing.md"
            existing.write_text(
                "# 已有\n\n## 1. 原内容\n\n原内容。\n", encoding="utf-8"
            )
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "stale-target"
            )
            (run_root / "proposed" / "existing.md").write_text(
                "# 已有\n\n## 1. 新内容\n\n新内容。\n", encoding="utf-8"
            )
            (run_root / "proposed" / "new.md").write_text(
                "# 新文档\n\n## 1. 内容\n\n内容。\n", encoding="utf-8"
            )
            code, review, stderr = self.run_cli(
                ["prepare-review", str(run_root)],
                review_metadata(run_root, ["existing.md", "new.md"]),
            )
            self.assertEqual(code, 0, stderr)
            update_item = next(
                item for item in review["outputs"]
                if item["target_path"] == "existing.md"
            )
            create_item = next(
                item for item in review["outputs"]
                if item["target_path"] == "new.md"
            )
            self.assertTrue(update_item["diff_included"])
            self.assertIn("diff", update_item)
            self.assertFalse(create_item["diff_included"])
            self.assertNotIn("diff", create_item)
            existing.write_text(
                "# 已有\n\n## 1. 外部修改\n\n外部修改。\n", encoding="utf-8"
            )

            apply_code, _, apply_stderr = self.run_cli(
                [
                    "apply", str(run_root),
                    "--approve", "existing.md",
                    "--approve", "new.md",
                ]
            )

            self.assertEqual(apply_code, 2)
            self.assertIn("批准失效", apply_stderr)
            self.assertFalse((knowledge_root / "new.md").exists())
            self.assertIn("外部修改", existing.read_text(encoding="utf-8"))

    def test_interrupted_multi_file_apply_can_recover(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            original = knowledge_root / "one.md"
            original.write_text(
                "# 一\n\n## 1. 原内容\n\n原内容。\n", encoding="utf-8"
            )
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "recover-apply"
            )
            (run_root / "proposed" / "one.md").write_text(
                "# 一\n\n## 1. 新内容\n\n新内容。\n", encoding="utf-8"
            )
            (run_root / "proposed" / "two.md").write_text(
                "# 二\n\n## 1. 内容\n\n内容。\n", encoding="utf-8"
            )
            code, _, stderr = self.run_cli(
                ["prepare-review", str(run_root)],
                review_metadata(run_root, ["one.md", "two.md"]),
            )
            self.assertEqual(code, 0, stderr)
            root_value, _, document = runtime.load_run(run_root)
            real_atomic_write = runtime.atomic_write
            target_writes = 0

            def fail_second_target(path: Path, content: bytes) -> None:
                """在第二个正式目标写入前模拟进程级失败。"""

                nonlocal target_writes
                if path.is_relative_to(knowledge_root) and not path.is_relative_to(
                    knowledge_root / ".knowledge-curator"
                ):
                    target_writes += 1
                    if target_writes == 2:
                        raise OSError("synthetic interruption")
                real_atomic_write(path, content)

            with mock.patch.object(runtime, "atomic_write", side_effect=fail_second_target):
                with self.assertRaises(runtime.RunError):
                    runtime.apply_outputs(
                        root_value,
                        run_root,
                        document,
                        ["one.md", "two.md"],
                    )
            self.assertTrue((run_root / "apply-state.json").is_file())
            self.assertIn("新内容", original.read_text(encoding="utf-8"))

            _, _, recovery_document = runtime.load_run(run_root)
            result = runtime.recover_apply(
                knowledge_root, run_root, recovery_document
            )

            self.assertEqual(result["restored_file_count"], 1)
            self.assertIn("原内容", original.read_text(encoding="utf-8"))
            self.assertFalse((knowledge_root / "two.md").exists())
            self.assertFalse((run_root / "apply-state.json").exists())

    def test_recover_rejects_external_change_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory(dir=WORKSPACE_ROOT) as directory:
            root = Path(directory)
            knowledge_root, input_path = create_inputs(root)
            target = knowledge_root / "example.md"
            target.write_text(
                "# 示例\n\n## 1. 原内容\n\n原内容。\n", encoding="utf-8"
            )
            run_root, _ = runtime.initialize_run(
                knowledge_root, input_path, "recover-conflict"
            )
            (run_root / "proposed" / "example.md").write_text(
                "# 示例\n\n## 1. 新内容\n\n新内容。\n", encoding="utf-8"
            )
            code, _, stderr = self.run_cli(
                ["prepare-review", str(run_root)],
                review_metadata(run_root, ["example.md"]),
            )
            self.assertEqual(code, 0, stderr)
            root_value, _, document = runtime.load_run(run_root)
            runtime.prepare_apply_state(
                root_value, run_root, document["outputs"]
            )
            target.write_text(
                "# 示例\n\n## 1. 外部修改\n\n外部修改。\n", encoding="utf-8"
            )

            with self.assertRaises(runtime.RunError):
                runtime.recover_apply(root_value, run_root, document)

            self.assertIn("外部修改", target.read_text(encoding="utf-8"))
            self.assertTrue((run_root / "apply-state.json").is_file())


if __name__ == "__main__":
    unittest.main()
