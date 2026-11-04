#!/usr/bin/env python3
"""应用已明确批准的 Markdown 变更，并提供可检测、可恢复的写入状态。"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import knowledge_inventory
import knowledge_proposals
import knowledge_retrieval
import knowledge_review

FORMAT_VERSION = "conversation-knowledge-curator.apply-manifest.v1"
STATE_FORMAT = "conversation-knowledge-curator.apply-state.v1"
STATE_FILE = "apply-state.json"
MANIFEST_FILE = "apply-manifest.json"
DECISIONS_FILE = "decisions.json"
ROLLBACK_DIRECTORY = "rollback"


class ApplyError(Exception):
    """表示应用预检、写入或恢复过程无法安全继续。"""


@dataclass(frozen=True)
class ApplyPlan:
    """保存一个已通过全部预检的文件替换计划。"""

    proposal_id: str
    candidate_ids: tuple[str, ...]
    target_path: str
    operation: str
    target: Path
    before_bytes: bytes | None
    after_bytes: bytes
    before_sha256: str | None
    after_sha256: str
    backup_path: str | None
    temporary_name: str


def sha256_bytes(value: bytes) -> str:
    """返回字节内容的小写 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def knowledge_root_identity(knowledge_root: Path) -> str:
    """计算不暴露绝对路径的知识库根目录身份。"""

    normalized = os.path.normcase(str(knowledge_root.resolve(strict=True)))
    return sha256_bytes(normalized.encode("utf-8"))


def load_json(path: Path, context: str) -> tuple[dict[str, object], bytes]:
    """读取阶段 JSON，并统一转换底层解析错误。"""

    try:
        return knowledge_retrieval.load_json_document(path, context)
    except knowledge_retrieval.RetrievalError as exc:
        raise ApplyError(str(exc)) from exc


def require_exact_fields(
    value: dict[str, object],
    expected: set[str],
    context: str,
) -> None:
    """拒绝缺失或契约未声明的字段。"""

    try:
        knowledge_review.require_exact_fields(value, expected, context)
    except knowledge_review.ReviewError as exc:
        raise ApplyError(str(exc)) from exc


def require_string(value: object, context: str) -> str:
    """返回去除首尾空白后的非空字符串。"""

    try:
        return knowledge_review.require_string(value, context)
    except knowledge_review.ReviewError as exc:
        raise ApplyError(str(exc)) from exc


def require_sha256(value: object, context: str) -> str:
    """校验小写 SHA-256 字符串。"""

    try:
        return knowledge_review.require_sha256(value, context)
    except knowledge_review.ReviewError as exc:
        raise ApplyError(str(exc)) from exc


def resolve_knowledge_root(knowledge_root: Path) -> Path:
    """解析真实、非链接的知识库根目录。"""

    if knowledge_inventory.is_link_like(knowledge_root):
        raise ApplyError("知识库根目录不得是链接")
    try:
        resolved = knowledge_root.resolve(strict=True)
    except OSError as exc:
        raise ApplyError(f"知识库根目录不存在：{knowledge_root}") from exc
    if not resolved.is_dir():
        raise ApplyError(f"知识库根路径不是目录：{knowledge_root}")
    return resolved


def new_target_path(knowledge_root: Path, target_path: str) -> Path:
    """校验新建 Markdown 路径并返回知识库内绝对位置。"""

    try:
        relative = knowledge_retrieval.safe_inventory_path(target_path)
        knowledge_proposals.validate_new_target_path(knowledge_root, relative)
    except (knowledge_retrieval.RetrievalError, knowledge_proposals.ProposalError) as exc:
        raise ApplyError(str(exc)) from exc
    target = knowledge_root.joinpath(*relative.parts)
    if not knowledge_inventory.is_within(target.resolve(strict=False), knowledge_root):
        raise ApplyError(f"新建目标超出知识库：{target_path}")
    return target


def existing_target_path(knowledge_root: Path, target_path: str) -> Path:
    """解析现有 Markdown 路径并拒绝链接或目录逃逸。"""

    try:
        relative = knowledge_retrieval.safe_inventory_path(target_path)
        return knowledge_retrieval.resolve_markdown_path(knowledge_root, relative)
    except knowledge_retrieval.RetrievalError as exc:
        raise ApplyError(str(exc)) from exc


def missing_parent_directories(
    knowledge_root: Path,
    target: Path,
) -> list[str]:
    """返回创建目标文件前需要新建的相对目录，按从浅到深排序。"""

    missing: list[Path] = []
    current = target.parent
    while current != knowledge_root and not current.exists():
        missing.append(current)
        current = current.parent
    if current != knowledge_root:
        if knowledge_inventory.is_link_like(current) or not current.is_dir():
            raise ApplyError(f"目标父路径不是安全目录：{target}")
        if not knowledge_inventory.is_within(current.resolve(strict=True), knowledge_root):
            raise ApplyError(f"目标父路径超出知识库：{target}")
    return [path.relative_to(knowledge_root).as_posix() for path in reversed(missing)]


def preflight_apply(
    knowledge_root: Path,
    review_directory: Path,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[ApplyPlan],
    list[str],
    bytes,
    bytes,
]:
    """在任何知识库写入前完成审核包、批准和目标快照校验。"""

    try:
        review_manifest, proposals = knowledge_review.validate_review_package(
            review_directory
        )
    except knowledge_review.ReviewError as exc:
        raise ApplyError(str(exc)) from exc
    decisions_document, decisions_raw = load_json(
        review_directory / DECISIONS_FILE,
        "review decisions",
    )
    try:
        decisions = knowledge_review.validate_review_decisions(
            review_manifest,
            decisions_document,
        )
    except knowledge_review.ReviewError as exc:
        raise ApplyError(str(exc)) from exc
    _, review_manifest_raw = load_json(
        review_directory / "review-manifest.json",
        "review manifest",
    )
    raw_entries = review_manifest["entries"]
    assert isinstance(raw_entries, list)
    manifest_entries = {str(entry["proposal_id"]): entry for entry in raw_entries}
    approved_ids = {
        str(item["proposal_id"])
        for item in decisions
        if item["decision"] == "approved"
    }

    plans: list[ApplyPlan] = []
    created_directories: set[str] = set()
    seen_targets: set[str] = set()
    for proposal_id in sorted(approved_ids):
        proposal = proposals[proposal_id]
        action = str(proposal["action"])
        if action not in knowledge_review.WRITABLE_ACTIONS:
            continue
        entry = manifest_entries[proposal_id]
        target_path = str(entry["target_path"])
        if target_path in seen_targets:
            raise ApplyError(f"多个获批提案竞争同一目标：{target_path}")
        seen_targets.add(target_path)
        operation_value = proposal["operation"]
        if not isinstance(operation_value, dict):
            raise ApplyError(f"获批 Proposal 缺少 operation：{proposal_id}")
        operation = str(operation_value["type"])
        if operation == "create_document":
            target = new_target_path(knowledge_root, target_path)
            before_bytes = None
            before_sha256 = None
            created_directories.update(
                missing_parent_directories(knowledge_root, target)
            )
        else:
            target = existing_target_path(knowledge_root, target_path)
            try:
                before_bytes = target.read_bytes()
            except OSError as exc:
                raise ApplyError(f"无法读取应用目标：{target_path}") from exc
            before_sha256 = sha256_bytes(before_bytes)
            if before_sha256 != entry["base_file_sha256"]:
                raise ApplyError(f"知识库文件已不同于审核版本：{target_path}")

        try:
            expected_after, expected_diff, expected_base = (
                knowledge_review.prepare_write_artifacts(
                    proposal,
                    knowledge_root,
                )
            )
            after_bytes = knowledge_review.read_review_artifact(
                review_directory.resolve(strict=True),
                str(entry["after_path"]),
                "after 文件",
            )
            diff_bytes = knowledge_review.read_review_artifact(
                review_directory.resolve(strict=True),
                str(entry["diff_path"]),
                "diff 文件",
            )
        except (knowledge_review.ReviewError, OSError) as exc:
            raise ApplyError(str(exc)) from exc
        if expected_base != before_sha256:
            raise ApplyError(f"应用目标基础哈希不一致：{target_path}")
        if expected_after != after_bytes or expected_diff != diff_bytes:
            raise ApplyError(f"after 或 diff 不再对应获批操作：{proposal_id}")
        after_sha256 = sha256_bytes(after_bytes)
        if after_sha256 != entry["after_sha256"]:
            raise ApplyError(f"after 文件哈希不一致：{proposal_id}")
        backup_path = (
            None
            if before_bytes is None
            else f"{ROLLBACK_DIRECTORY}/{proposal_id}.md"
        )
        plans.append(
            ApplyPlan(
                proposal_id=proposal_id,
                candidate_ids=tuple(str(item) for item in proposal["candidate_ids"]),
                target_path=target_path,
                operation=operation,
                target=target,
                before_bytes=before_bytes,
                after_bytes=after_bytes,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                backup_path=backup_path,
                temporary_name=f".knowledge-curator-{proposal_id}.tmp",
            )
        )
    created = sorted(created_directories, key=lambda value: (value.count("/"), value))
    return (
        review_manifest,
        decisions,
        plans,
        created,
        review_manifest_raw,
        decisions_raw,
    )


def state_document(
    knowledge_root: Path,
    review_id: str,
    plans: list[ApplyPlan],
    created_directories: list[str],
) -> dict[str, object]:
    """构造应用前写入的可恢复状态。"""

    return {
        "format": STATE_FORMAT,
        "review_id": review_id,
        "knowledge_root_sha256": knowledge_root_identity(knowledge_root),
        "status": "prepared",
        "created_directories": created_directories,
        "entries": [
            {
                "proposal_id": plan.proposal_id,
                "target_path": plan.target_path,
                "before_sha256": plan.before_sha256,
                "after_sha256": plan.after_sha256,
                "backup_path": plan.backup_path,
                "temporary_name": plan.temporary_name,
                "result": "pending",
            }
            for plan in plans
        ],
    }


def write_state(path: Path, state: dict[str, object], create: bool = False) -> None:
    """独占创建或通过同目录临时文件安全更新应用状态。"""

    serialized = knowledge_inventory.serialize_json(state)
    if create:
        try:
            knowledge_inventory.write_new_file(path, serialized)
        except knowledge_inventory.InventoryError as exc:
            raise ApplyError(str(exc)) from exc
        return
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".apply-state-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ApplyError("无法安全更新 apply-state.json") from exc


def write_bytes_new(path: Path, content: bytes) -> None:
    """独占写入并同步一个回滚或暂存文件。"""

    try:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise ApplyError(f"无法创建应用临时文件：{path}") from exc


def replace_target(temporary_path: Path, target: Path) -> None:
    """以单文件原子替换方式发布已暂存的 after 内容。"""

    os.replace(temporary_path, target)


def validate_state(
    state: dict[str, object],
) -> tuple[str, str, str, list[str], list[dict[str, object]]]:
    """校验内部 apply state，供中断恢复使用。"""

    require_exact_fields(
        state,
        {
            "format",
            "review_id",
            "knowledge_root_sha256",
            "status",
            "created_directories",
            "entries",
        },
        "apply state 顶层",
    )
    if state["format"] != STATE_FORMAT:
        raise ApplyError("不支持的 apply state 格式")
    review_id = require_string(state["review_id"], "apply state.review_id")
    knowledge_root_sha256 = require_sha256(
        state["knowledge_root_sha256"],
        "apply state.knowledge_root_sha256",
    )
    status = require_string(state["status"], "apply state.status")
    if status not in {"prepared", "applying", "applied", "recovery_required"}:
        raise ApplyError("apply state.status 非法")
    raw_directories = state["created_directories"]
    if not isinstance(raw_directories, list) or any(
        not isinstance(value, str) for value in raw_directories
    ):
        raise ApplyError("apply state.created_directories 必须是字符串数组")
    directories: list[str] = []
    for value in raw_directories:
        if "\\" in value or ":" in value:
            raise ApplyError("apply state.created_directories 包含非法路径")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or any(
            part in {"", ".", "..", ".git", ".knowledge-curator"}
            for part in path.parts
        ):
            raise ApplyError("apply state.created_directories 包含非法路径")
        directories.append(value)
    raw_entries = state["entries"]
    if not isinstance(raw_entries, list):
        raise ApplyError("apply state.entries 必须是数组")
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw_entries):
        context = f"apply state.entries[{index}]"
        if not isinstance(entry, dict):
            raise ApplyError(f"{context} 必须是对象")
        require_exact_fields(
            entry,
            {
                "proposal_id",
                "target_path",
                "before_sha256",
                "after_sha256",
                "backup_path",
                "temporary_name",
                "result",
            },
            context,
        )
        proposal_id = require_string(entry["proposal_id"], f"{context}.proposal_id")
        if proposal_id in seen_ids:
            raise ApplyError(f"apply state proposal_id 重复：{proposal_id}")
        seen_ids.add(proposal_id)
        require_string(entry["target_path"], f"{context}.target_path")
        if entry["before_sha256"] is not None:
            require_sha256(entry["before_sha256"], f"{context}.before_sha256")
        require_sha256(entry["after_sha256"], f"{context}.after_sha256")
        backup_path = entry["backup_path"]
        if backup_path is not None and backup_path != (
            f"{ROLLBACK_DIRECTORY}/{proposal_id}.md"
        ):
            raise ApplyError(f"{context}.backup_path 非法")
        if entry["temporary_name"] != f".knowledge-curator-{proposal_id}.tmp":
            raise ApplyError(f"{context}.temporary_name 非法")
        if entry["result"] not in {"pending", "applied"}:
            raise ApplyError(f"{context}.result 非法")
        entries.append(entry)
    return review_id, knowledge_root_sha256, status, directories, entries


def recovery_actions(
    knowledge_root: Path,
    review_directory: Path,
    entries: list[dict[str, object]],
) -> list[tuple[str, Path, bytes | None, Path]]:
    """在恢复写入前验证所有当前文件和回滚副本，避免部分恢复。"""

    actions: list[tuple[str, Path, bytes | None, Path]] = []
    review_root = review_directory.resolve(strict=True)
    for entry in reversed(entries):
        target_path = str(entry["target_path"])
        try:
            relative = knowledge_retrieval.safe_inventory_path(target_path)
        except knowledge_retrieval.RetrievalError as exc:
            raise ApplyError(str(exc)) from exc
        target = knowledge_root.joinpath(*relative.parts)
        if not knowledge_inventory.is_within(target.resolve(strict=False), knowledge_root):
            raise ApplyError(f"恢复目标超出知识库：{target_path}")
        temporary_path = target.parent / str(entry["temporary_name"])
        before_sha256 = entry["before_sha256"]
        after_sha256 = str(entry["after_sha256"])
        if before_sha256 is None:
            if target.exists():
                if knowledge_inventory.is_link_like(target) or not target.is_file():
                    raise ApplyError(f"新建目标状态无法安全恢复：{target_path}")
                current = target.read_bytes()
                if sha256_bytes(current) != after_sha256:
                    raise ApplyError(f"新建目标已被额外修改，拒绝删除：{target_path}")
                actions.append(("delete", target, None, temporary_path))
            else:
                actions.append(("none", target, None, temporary_path))
        else:
            backup_path = entry["backup_path"]
            assert isinstance(backup_path, str)
            backup = knowledge_review.read_review_artifact(
                review_root,
                backup_path,
                "回滚文件",
            )
            if sha256_bytes(backup) != before_sha256:
                raise ApplyError(f"回滚文件哈希不匹配：{target_path}")
            if not target.is_file() or knowledge_inventory.is_link_like(target):
                raise ApplyError(f"原有目标状态无法安全恢复：{target_path}")
            current_sha256 = sha256_bytes(target.read_bytes())
            if current_sha256 == after_sha256:
                actions.append(("restore", target, backup, temporary_path))
            elif current_sha256 == before_sha256:
                actions.append(("none", target, None, temporary_path))
            else:
                raise ApplyError(f"目标已被额外修改，拒绝覆盖恢复：{target_path}")
    return actions


def recover_interrupted_apply(
    knowledge_root: Path,
    review_directory: Path,
) -> dict[str, object]:
    """按哈希恢复中断应用，操作可重复执行。"""

    if (review_directory / MANIFEST_FILE).exists():
        raise ApplyError("apply manifest 已存在，不能把已完成应用当作中断恢复")
    state_path = review_directory / STATE_FILE
    state, _ = load_json(state_path, "apply state")
    (
        _,
        state_root_sha256,
        status,
        created_directories,
        entries,
    ) = validate_state(state)
    resolved_root = resolve_knowledge_root(knowledge_root)
    if state_root_sha256 != knowledge_root_identity(resolved_root):
        raise ApplyError("apply state 不属于当前知识库根目录")
    actions = (
        []
        if status == "prepared"
        else recovery_actions(
            resolved_root,
            review_directory,
            entries,
        )
    )
    restored_count = 0
    for action, target, backup, temporary_path in actions:
        if action == "restore":
            assert backup is not None
            recovery_temp = target.parent / f"{temporary_path.name}.recover"
            if recovery_temp.exists():
                recovery_temp.unlink()
            write_bytes_new(recovery_temp, backup)
            replace_target(recovery_temp, target)
            restored_count += 1
        elif action == "delete":
            target.unlink()
            restored_count += 1
        if temporary_path.exists():
            temporary_path.unlink()
    if status == "prepared":
        for entry in entries:
            target_path = str(entry["target_path"])
            try:
                relative = knowledge_retrieval.safe_inventory_path(target_path)
            except knowledge_retrieval.RetrievalError as exc:
                raise ApplyError(str(exc)) from exc
            target = resolved_root.joinpath(*relative.parts)
            temporary_path = target.parent / str(entry["temporary_name"])
            if temporary_path.exists():
                temporary_path.unlink()
    for relative_directory in sorted(
        created_directories,
        key=lambda value: (value.count("/"), value),
        reverse=True,
    ):
        directory = resolved_root.joinpath(*PurePosixPath(relative_directory).parts)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    rollback_path = review_directory / ROLLBACK_DIRECTORY
    if rollback_path.exists():
        if knowledge_inventory.is_link_like(rollback_path):
            raise ApplyError("rollback 目录不得是链接")
        shutil.rmtree(rollback_path)
    state_path.unlink()
    return {
        "status": "rolled_back",
        "restored_count": restored_count,
    }


def build_apply_manifest(
    review_manifest: dict[str, object],
    plans: list[ApplyPlan],
    review_manifest_raw: bytes,
    decisions_raw: bytes,
) -> dict[str, object]:
    """生成不复制正文和 diff 的最小应用审计清单。"""

    return {
        "format": FORMAT_VERSION,
        "review_id": review_manifest["review_id"],
        "review_manifest_sha256": sha256_bytes(review_manifest_raw),
        "review_decisions_sha256": sha256_bytes(decisions_raw),
        "status": "applied",
        "entries": [
            {
                "proposal_id": plan.proposal_id,
                "candidate_ids": list(plan.candidate_ids),
                "target_path": plan.target_path,
                "operation": plan.operation,
                "before_sha256": plan.before_sha256,
                "after_sha256": plan.after_sha256,
                "result": "applied",
            }
            for plan in plans
        ],
        "warnings": [],
    }


def apply_approved_changes(
    knowledge_root: Path,
    review_directory: Path,
) -> dict[str, object]:
    """预检全部获批操作，分文件原子写入并生成 apply manifest。"""

    manifest_path = review_directory / MANIFEST_FILE
    state_path = review_directory / STATE_FILE
    rollback_path = review_directory / ROLLBACK_DIRECTORY
    if manifest_path.exists():
        raise ApplyError(f"apply manifest 已存在：{manifest_path}")
    if state_path.exists():
        raise ApplyError("检测到未清理的 apply state；请先运行 recover")
    if rollback_path.exists():
        raise ApplyError("检测到孤立 rollback 目录，拒绝开始新的应用")
    resolved_root = resolve_knowledge_root(knowledge_root)
    (
        review_manifest,
        _,
        plans,
        created_directories,
        review_manifest_raw,
        decisions_raw,
    ) = preflight_apply(resolved_root, review_directory)
    apply_manifest = build_apply_manifest(
        review_manifest,
        plans,
        review_manifest_raw,
        decisions_raw,
    )
    if not plans:
        try:
            knowledge_inventory.write_new_file(
                manifest_path,
                knowledge_inventory.serialize_json(apply_manifest),
            )
        except knowledge_inventory.InventoryError as exc:
            raise ApplyError(str(exc)) from exc
        return apply_manifest

    state = state_document(
        resolved_root,
        str(review_manifest["review_id"]),
        plans,
        created_directories,
    )
    write_state(state_path, state, create=True)
    try:
        rollback_path.mkdir()
        for plan in plans:
            if plan.before_bytes is not None and plan.backup_path is not None:
                backup = review_directory.joinpath(
                    *PurePosixPath(plan.backup_path).parts
                )
                write_bytes_new(backup, plan.before_bytes)
        for relative_directory in created_directories:
            resolved_root.joinpath(
                *PurePosixPath(relative_directory).parts
            ).mkdir()
        for plan in plans:
            write_bytes_new(
                plan.target.parent / plan.temporary_name,
                plan.after_bytes,
            )
        state["status"] = "applying"
        write_state(state_path, state)
        state_entries = state["entries"]
        assert isinstance(state_entries, list)
        state_by_id = {
            str(entry["proposal_id"]): entry for entry in state_entries
        }
        for plan in plans:
            replace_target(
                plan.target.parent / plan.temporary_name,
                plan.target,
            )
            state_by_id[plan.proposal_id]["result"] = "applied"
            write_state(state_path, state)
        state["status"] = "applied"
        write_state(state_path, state)
        knowledge_inventory.write_new_file(
            manifest_path,
            knowledge_inventory.serialize_json(apply_manifest),
        )
    except (OSError, ApplyError, knowledge_inventory.InventoryError) as exc:
        try:
            recover_interrupted_apply(resolved_root, review_directory)
        except (OSError, ApplyError) as recovery_exc:
            state["status"] = "recovery_required"
            try:
                write_state(state_path, state)
            except ApplyError:
                pass
            raise ApplyError(
                "应用失败且自动回滚未完成；保留 apply state 供 recover 使用"
            ) from recovery_exc
        raise ApplyError("应用失败，已自动恢复到写入前状态") from exc
    return apply_manifest


def create_parser() -> argparse.ArgumentParser:
    """创建非交互式安全应用 CLI。"""

    parser = argparse.ArgumentParser(
        description="应用已批准的 Markdown after 文件或恢复中断应用。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser(
        "apply",
        help="应用 approved 写入提案并生成 apply-manifest.v1",
    )
    apply_parser.add_argument("knowledge_root", type=Path)
    apply_parser.add_argument("review_directory", type=Path)
    recover_parser = subparsers.add_parser(
        "recover",
        help="按 apply-state.json 回滚中断应用",
    )
    recover_parser.add_argument("knowledge_root", type=Path)
    recover_parser.add_argument("review_directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行安全应用或恢复并输出结构化汇总。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "apply":
            manifest = apply_approved_changes(
                args.knowledge_root,
                args.review_directory,
            )
            summary = {
                "status": manifest["status"],
                "review_id": manifest["review_id"],
                "applied_count": len(manifest["entries"]),
                "output": str((args.review_directory / MANIFEST_FILE).resolve()),
            }
        elif args.command == "recover":
            result = recover_interrupted_apply(
                args.knowledge_root,
                args.review_directory,
            )
            summary = result
        else:
            parser.error("未知命令")
    except (
        ApplyError,
        knowledge_review.ReviewError,
        knowledge_retrieval.RetrievalError,
        knowledge_inventory.InventoryError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(knowledge_inventory.serialize_json(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
