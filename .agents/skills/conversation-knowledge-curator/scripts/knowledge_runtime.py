#!/usr/bin/env python3
"""实现 conversation-knowledge-curator.run.v2 的单流程运行核心。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import chatgpt_export
import knowledge_document_quality
import knowledge_inventory
import knowledge_retrieval

FORMAT_VERSION = "conversation-knowledge-curator.run.v2"
VISIBLE_FORMAT = "conversation-knowledge-curator.visible-input.v1"
CONTEXT_FORMAT = "conversation-knowledge-curator.context.v1"
REVIEW_FORMAT = "conversation-knowledge-curator.review.v2"
REVIEW_INPUT_FORMAT = "conversation-knowledge-curator.review-input.v1"
RUN_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
RUN_RELATIVE_ROOT = PurePosixPath(".knowledge-curator/runs")
PROPOSED_DIRECTORY = "proposed"
APPLY_STATE_FILE = "apply-state.json"
DEFAULT_BATCH_CHARACTERS = 120_000
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ISSUE_KINDS = {
    "uncertain",
    "time_sensitive",
    "sensitive",
    "conflict",
    "assistant_only",
    "other",
}


class RunError(Exception):
    """表示输入、run 契约或安全状态不允许继续。"""


def serialize_json(value: object) -> str:
    """按稳定、可读格式序列化 JSON。"""

    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def sha256_bytes(content: bytes) -> str:
    """返回内容 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def require_exact_fields(
    value: dict[str, object], expected: set[str], context: str
) -> None:
    """拒绝缺失和未声明字段。"""

    actual = set(value)
    if actual != expected:
        raise RunError(
            f"{context}字段不匹配；缺少={sorted(expected - actual)}，"
            f"未知={sorted(actual - expected)}"
        )


def validate_run_id(value: object) -> str:
    """校验可安全用作目录名的 run ID。"""

    if (
        not isinstance(value, str)
        or len(value) > 64
        or not RUN_ID_PATTERN.fullmatch(value)
    ):
        raise RunError("run_id 必须由小写字母、数字和单连字符组成，长度 1 到 64")
    return value


def resolve_directory(path: Path, context: str) -> Path:
    """解析普通目录并拒绝链接。"""

    if knowledge_inventory.is_link_like(path):
        raise RunError(f"{context}不得是链接：{path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RunError(f"{context}不存在或不可访问：{path}") from exc
    if not resolved.is_dir():
        raise RunError(f"{context}不是目录：{path}")
    return resolved


def expected_run_root(knowledge_root: Path, run_id: str) -> Path:
    """返回固定 run 位置。"""

    return knowledge_root.joinpath(*RUN_RELATIVE_ROOT.parts, run_id)


def atomic_write(path: Path, content: bytes) -> None:
    """在同目录写临时文件后原子替换目标。"""

    if path.exists() and knowledge_inventory.is_link_like(path):
        raise RunError(f"拒绝写入链接：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RunError(f"无法安全写入：{path}") from exc


def inventory_snapshot(knowledge_root: Path) -> tuple[dict[str, object], str]:
    """在内存中生成知识库 inventory 和稳定快照哈希。"""

    inventory = knowledge_inventory.build_inventory(knowledge_root)
    digest = sha256_bytes(serialize_json(inventory).encode("utf-8"))
    return inventory, digest


def aggregate_source_sha256(items: list[dict[str, object]]) -> str:
    """根据来源身份和内容哈希生成输入集合哈希。"""

    identities: list[dict[str, str]] = []
    for item in items:
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        identities.append(
            {
                "kind": str(source.get("kind", "")),
                "path": str(source.get("path", "")),
                "member_path": str(source.get("member_path", "")),
                "sha256": str(source.get("sha256", "")),
            }
        )
    identities.sort(key=lambda item: serialize_json(item))
    return sha256_bytes(serialize_json(identities).encode("utf-8"))


def collect_visible_view(
    input_path: Path, branch: str
) -> tuple[dict[str, object], dict[str, object]]:
    """检查并在内存中构造用户可见消息视图，不写 normalized 文件。"""

    resolved = chatgpt_export.resolve_input(input_path)
    if resolved.input_type not in {"json_file", "zip_archive"}:
        raise RunError("当前运行入口只接受单个 ChatGPT JSON 或 ZIP 文件")
    inspections = [
        chatgpt_export.inspect_source(source) for source in resolved.sources
    ]
    supported = [item for item in inspections if item.get("status") == "supported"]
    if not supported:
        raise RunError("输入中没有可安全解析的 ChatGPT 对话 JSON")
    rejected = [item for item in inspections if item.get("status") != "supported"]
    if rejected:
        states = [str(item.get("status", "unknown")) for item in rejected]
        raise RunError(f"输入包含无法安全隔离的来源：{states}")

    sources: list[dict[str, object]] = []
    conversations: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = list(resolved.warnings)
    for source, inspection in zip(resolved.sources, inspections, strict=True):
        result = chatgpt_export.normalize_source(
            source,
            branch=branch,
            inspection=inspection,
        )
        source_info = dict(result.source)
        source_sha = str(source_info["sha256"])
        source_conversations: list[dict[str, object]] = []
        for conversation in result.document["conversations"]:
            item = dict(conversation)
            item["source_sha256"] = source_sha
            source_conversations.append(item)
            conversations.append(item)
        sources.append(
            {
                "source": source_info,
                "conversations": source_conversations,
            }
        )
        warnings.extend(result.diagnostics["warnings"])
    view = {
        "format": VISIBLE_FORMAT,
        "branch_policy": branch,
        "sources": sources,
    }
    metadata = {
        "input_type": resolved.input_type,
        "input_sha256": aggregate_source_sha256(inspections),
        "conversation_count": len(conversations),
        "message_count": sum(
            len(item.get("messages", [])) for item in conversations
        ),
        "warnings": warnings,
        "conversations": conversations,
    }
    return view, metadata


def build_batches(
    conversations: list[dict[str, object]],
    maximum_characters: int = DEFAULT_BATCH_CHARACTERS,
) -> list[dict[str, object]]:
    """按完整对话构造确定性批次元数据，不复制正文。"""

    batches: list[dict[str, object]] = []
    current_refs: list[dict[str, str]] = []
    current_characters = 0
    current_messages = 0
    for conversation in conversations:
        messages = conversation.get("messages", [])
        encoded = serialize_json(conversation)
        size = len(encoded)
        if current_refs and current_characters + size > maximum_characters:
            batches.append(
                {
                    "index": len(batches) + 1,
                    "conversation_count": len(current_refs),
                    "message_count": current_messages,
                    "character_count": current_characters,
                    "conversations": current_refs,
                }
            )
            current_refs = []
            current_characters = 0
            current_messages = 0
        current_refs.append(
            {
                "source_sha256": str(conversation["source_sha256"]),
                "conversation_id": str(conversation["conversation_id"]),
            }
        )
        current_characters += size
        current_messages += len(messages) if isinstance(messages, list) else 0
    if current_refs:
        batches.append(
            {
                "index": len(batches) + 1,
                "conversation_count": len(current_refs),
                "message_count": current_messages,
                "character_count": current_characters,
                "conversations": current_refs,
            }
        )
    return batches


def available_source_index(
    conversations: list[dict[str, object]],
) -> dict[tuple[str, str], set[str]]:
    """构造可见来源索引，供审核元数据验证真实消息引用。"""

    index: dict[tuple[str, str], set[str]] = {}
    for conversation in conversations:
        key = (
            str(conversation["source_sha256"]),
            str(conversation["conversation_id"]),
        )
        messages = conversation.get("messages", [])
        index[key] = {
            str(message["message_id"])
            for message in messages
            if isinstance(message, dict) and "message_id" in message
        }
    return index


def parse_review_input(
    raw_text: str,
    proposed_targets: set[str],
    conversations: list[dict[str, object]],
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    """严格校验标准输入中的文档级来源和风险元数据。"""

    if not raw_text.strip():
        raise RunError("prepare-review 必须从标准输入接收来源元数据 JSON")
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RunError("审核元数据不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise RunError("审核元数据顶层必须是对象")
    require_exact_fields(value, {"format", "outputs", "issues"}, "审核元数据 ")
    if value["format"] != REVIEW_INPUT_FORMAT:
        raise RunError(f"审核元数据 format 必须是 {REVIEW_INPUT_FORMAT}")
    if not isinstance(value["outputs"], list) or not isinstance(value["issues"], list):
        raise RunError("审核元数据 outputs 和 issues 必须是数组")

    available = available_source_index(conversations)
    source_map: dict[str, list[dict[str, object]]] = {}
    for output_index, output in enumerate(value["outputs"]):
        context = f"审核元数据 outputs[{output_index}]"
        if not isinstance(output, dict):
            raise RunError(f"{context} 必须是对象")
        require_exact_fields(output, {"target_path", "sources"}, f"{context} ")
        target_path = output["target_path"]
        if not isinstance(target_path, str):
            raise RunError(f"{context}.target_path 必须是字符串")
        safe_target_path(target_path)
        if target_path not in proposed_targets:
            raise RunError(f"审核元数据引用未知 proposed 目标：{target_path}")
        if target_path in source_map:
            raise RunError(f"审核元数据重复登记目标：{target_path}")
        sources = output["sources"]
        if not isinstance(sources, list) or not sources:
            raise RunError(f"{context}.sources 必须是非空数组")
        normalized_sources: list[dict[str, object]] = []
        seen_sources: set[tuple[str, str]] = set()
        for source_index, source in enumerate(sources):
            source_context = f"{context}.sources[{source_index}]"
            if not isinstance(source, dict):
                raise RunError(f"{source_context} 必须是对象")
            require_exact_fields(
                source,
                {"source_sha256", "conversation_id", "message_ids"},
                f"{source_context} ",
            )
            source_sha = source["source_sha256"]
            conversation_id = source["conversation_id"]
            message_ids = source["message_ids"]
            if (
                not isinstance(source_sha, str)
                or not SHA256_PATTERN.fullmatch(source_sha)
                or not isinstance(conversation_id, str)
                or not conversation_id
                or not isinstance(message_ids, list)
                or not message_ids
                or not all(
                    isinstance(message_id, str) and message_id
                    for message_id in message_ids
                )
            ):
                raise RunError(f"{source_context} 字段类型或取值无效")
            key = (source_sha, conversation_id)
            if key in seen_sources:
                raise RunError(f"{source_context} 重复登记同一对话")
            if key not in available:
                raise RunError(f"{source_context} 引用了未知来源或对话")
            if len(set(message_ids)) != len(message_ids):
                raise RunError(f"{source_context}.message_ids 不得重复")
            unknown_messages = sorted(set(message_ids) - available[key])
            if unknown_messages:
                raise RunError(
                    f"{source_context} 引用了未知可见消息：{unknown_messages}"
                )
            seen_sources.add(key)
            normalized_sources.append(
                {
                    "source_sha256": source_sha,
                    "conversation_id": conversation_id,
                    "message_ids": list(message_ids),
                }
            )
        source_map[target_path] = normalized_sources
    missing_targets = sorted(proposed_targets - set(source_map))
    if missing_targets:
        raise RunError(f"审核元数据未覆盖 proposed 目标：{missing_targets}")

    normalized_issues: list[dict[str, object]] = []
    for issue_index, issue in enumerate(value["issues"]):
        context = f"审核元数据 issues[{issue_index}]"
        if not isinstance(issue, dict):
            raise RunError(f"{context} 必须是对象")
        require_exact_fields(
            issue, {"kind", "message", "target_path"}, f"{context} "
        )
        kind = issue["kind"]
        message = issue["message"]
        target_path = issue["target_path"]
        if kind not in ISSUE_KINDS:
            raise RunError(f"{context}.kind 无效")
        if not isinstance(message, str) or not message.strip():
            raise RunError(f"{context}.message 必须是非空字符串")
        if target_path is not None:
            if not isinstance(target_path, str) or target_path not in proposed_targets:
                raise RunError(f"{context}.target_path 必须引用 proposed 目标或为 null")
        normalized_issues.append(
            {"kind": kind, "message": message, "target_path": target_path}
        )
    return source_map, normalized_issues


def initialize_run(
    knowledge_root: Path,
    input_path: Path,
    run_id: str,
    branch: str = "active",
) -> tuple[Path, dict[str, object]]:
    """完成全部前置校验后，原子创建仅含 run.json 和 proposed 的 run。"""

    normalized_id = validate_run_id(run_id)
    root = resolve_directory(knowledge_root, "知识库根目录")
    if branch not in {"active", "all"}:
        raise RunError("branch 只能是 active 或 all")
    if knowledge_inventory.is_link_like(input_path):
        raise RunError(f"输入路径不得是链接：{input_path}")
    try:
        source = input_path.resolve(strict=True)
    except OSError as exc:
        raise RunError(f"输入路径不存在或不可访问：{input_path}") from exc
    _, metadata = collect_visible_view(source, branch)
    inventory, snapshot_sha = inventory_snapshot(root)
    batches = build_batches(metadata["conversations"])
    run_root = expected_run_root(root, normalized_id)
    if run_root.exists():
        raise RunError(f"run 目录已存在，拒绝覆盖：{run_root}")
    curator_root = root / ".knowledge-curator"
    runs_root = curator_root / "runs"
    for path, label in ((curator_root, ".knowledge-curator"), (runs_root, "runs")):
        if path.exists() and (
            knowledge_inventory.is_link_like(path) or not path.is_dir()
        ):
            raise RunError(f"{label} 必须是普通目录")
    document: dict[str, object] = {
        "format": FORMAT_VERSION,
        "run_id": normalized_id,
        "status": "initialized",
        "knowledge_root": str(root),
        "input": {
            "path": str(source),
            "sha256": metadata["input_sha256"],
            "type": metadata["input_type"],
            "branch_policy": branch,
        },
        "parsing": {
            "conversation_count": metadata["conversation_count"],
            "message_count": metadata["message_count"],
            "warnings": metadata["warnings"],
            "batches": batches,
        },
        "knowledge_base": {
            "empty": not bool(inventory["files"]),
            "snapshot_sha256": snapshot_sha,
        },
        "outputs": [],
        "issues": [],
        "approval": None,
        "apply": {"status": "not_applied", "entries": []},
    }
    runs_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{normalized_id}.tmp-", dir=runs_root))
    try:
        (temporary / PROPOSED_DIRECTORY).mkdir()
        (temporary / "run.json").write_text(
            serialize_json(document), encoding="utf-8", newline="\n"
        )
        temporary.rename(run_root)
    except OSError as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise RunError("无法原子创建 run") from exc
    return run_root, document


def validate_run_document(document: object) -> dict[str, object]:
    """严格校验 run.v2 顶层和关键子结构。"""

    if not isinstance(document, dict):
        raise RunError("run.json 顶层必须是对象")
    require_exact_fields(
        document,
        {
            "format", "run_id", "status", "knowledge_root", "input",
            "parsing", "knowledge_base", "outputs", "issues", "approval", "apply",
        },
        "run.json ",
    )
    if document["format"] != FORMAT_VERSION:
        raise RunError("仅支持 conversation-knowledge-curator.run.v2")
    validate_run_id(document["run_id"])
    if document["status"] not in {"initialized", "review_ready", "applied"}:
        raise RunError("run.json.status 无效")
    if not isinstance(document["knowledge_root"], str):
        raise RunError("run.json.knowledge_root 必须是字符串")
    for name in ("input", "parsing", "knowledge_base", "apply"):
        if not isinstance(document[name], dict):
            raise RunError(f"run.json.{name} 必须是对象")
    if not isinstance(document["outputs"], list) or not isinstance(document["issues"], list):
        raise RunError("run.json.outputs 和 issues 必须是数组")
    input_value = document["input"]
    parsing = document["parsing"]
    knowledge_base = document["knowledge_base"]
    apply_value = document["apply"]
    assert isinstance(input_value, dict)
    assert isinstance(parsing, dict)
    assert isinstance(knowledge_base, dict)
    assert isinstance(apply_value, dict)
    require_exact_fields(
        input_value, {"path", "sha256", "type", "branch_policy"}, "run.json.input "
    )
    require_exact_fields(
        parsing,
        {"conversation_count", "message_count", "warnings", "batches"},
        "run.json.parsing ",
    )
    require_exact_fields(
        knowledge_base, {"empty", "snapshot_sha256"}, "run.json.knowledge_base "
    )
    require_exact_fields(apply_value, {"status", "entries"}, "run.json.apply ")
    if (
        not isinstance(input_value["path"], str)
        or not isinstance(input_value["sha256"], str)
        or input_value["branch_policy"] not in {"active", "all"}
    ):
        raise RunError("run.json.input 字段类型或取值无效")
    if (
        not isinstance(parsing["conversation_count"], int)
        or not isinstance(parsing["message_count"], int)
        or not isinstance(parsing["warnings"], list)
        or not isinstance(parsing["batches"], list)
    ):
        raise RunError("run.json.parsing 字段类型无效")
    if (
        not isinstance(knowledge_base["empty"], bool)
        or not isinstance(knowledge_base["snapshot_sha256"], str)
    ):
        raise RunError("run.json.knowledge_base 字段类型无效")
    if not isinstance(apply_value["status"], str) or not isinstance(
        apply_value["entries"], list
    ):
        raise RunError("run.json.apply 字段类型无效")
    for index, output in enumerate(document["outputs"]):
        if not isinstance(output, dict):
            raise RunError(f"run.json.outputs[{index}] 必须是对象")
        require_exact_fields(
            output,
            {
                "target_path", "proposed_path", "base_sha256",
                "proposed_sha256", "sources",
            },
            f"run.json.outputs[{index}] ",
        )
        if not all(
            isinstance(output[name], str)
            for name in (
                "target_path", "proposed_path", "base_sha256", "proposed_sha256"
            )
        ) or not isinstance(output["sources"], list):
            raise RunError(f"run.json.outputs[{index}] 字段类型无效")
        for source_index, source_ref in enumerate(output["sources"]):
            context = f"run.json.outputs[{index}].sources[{source_index}]"
            if not isinstance(source_ref, dict):
                raise RunError(f"{context} 必须是对象")
            require_exact_fields(
                source_ref,
                {"source_sha256", "conversation_id", "message_ids"},
                f"{context} ",
            )
            if (
                not isinstance(source_ref["source_sha256"], str)
                or not SHA256_PATTERN.fullmatch(source_ref["source_sha256"])
                or not isinstance(source_ref["conversation_id"], str)
                or not source_ref["conversation_id"]
                or not isinstance(source_ref["message_ids"], list)
                or not source_ref["message_ids"]
                or not all(
                    isinstance(message_id, str) and message_id
                    for message_id in source_ref["message_ids"]
                )
            ):
                raise RunError(f"{context} 字段类型或取值无效")
    for index, issue in enumerate(document["issues"]):
        context = f"run.json.issues[{index}]"
        if not isinstance(issue, dict):
            raise RunError(f"{context} 必须是对象")
        require_exact_fields(
            issue, {"kind", "message", "target_path"}, f"{context} "
        )
        if (
            issue["kind"] not in ISSUE_KINDS
            or not isinstance(issue["message"], str)
            or not issue["message"].strip()
            or (
                issue["target_path"] is not None
                and not isinstance(issue["target_path"], str)
            )
        ):
            raise RunError(f"{context} 字段类型或取值无效")
    approval = document["approval"]
    if approval is not None:
        if not isinstance(approval, dict):
            raise RunError("run.json.approval 必须是对象或 null")
        require_exact_fields(approval, {"entries"}, "run.json.approval ")
        if not isinstance(approval["entries"], list):
            raise RunError("run.json.approval.entries 必须是数组")
        for index, entry in enumerate(approval["entries"]):
            if not isinstance(entry, dict):
                raise RunError(f"run.json.approval.entries[{index}] 必须是对象")
            require_exact_fields(
                entry,
                {"target_path", "base_sha256", "proposed_sha256"},
                f"run.json.approval.entries[{index}] ",
            )
    return document


def load_run(run_directory: Path) -> tuple[Path, Path, dict[str, object]]:
    """读取 v2 run，并校验其固定位置与根目录绑定。"""

    run_root = resolve_directory(run_directory, "run 目录")
    manifest = run_root / "run.json"
    if knowledge_inventory.is_link_like(manifest):
        raise RunError("run.json 不得是链接")
    try:
        document = validate_run_document(
            json.loads(manifest.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError("run.json 不可读或不是有效 JSON") from exc
    knowledge_root_value = document["knowledge_root"]
    if not isinstance(knowledge_root_value, str):
        raise RunError("knowledge_root 必须是字符串")
    root = resolve_directory(Path(knowledge_root_value), "run 知识库根目录")
    run_id = validate_run_id(document["run_id"])
    if run_root != expected_run_root(root, run_id):
        raise RunError("run 目录不在绑定的 .knowledge-curator/runs/<run_id> 中")
    input_value = document["input"]
    assert isinstance(input_value, dict)
    path_value = input_value.get("path")
    if not isinstance(path_value, str):
        raise RunError("run.json.input.path 必须是字符串")
    return root, Path(path_value), document


def save_run(run_root: Path, document: dict[str, object]) -> None:
    """重新校验并原子更新 Python 维护的 run.json。"""

    validate_run_document(document)
    atomic_write(
        run_root / "run.json",
        serialize_json(document).encode("utf-8"),
    )


def current_view(
    source: Path, document: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """重新构造可见消息，并校验输入集合未变化。"""

    input_value = document["input"]
    assert isinstance(input_value, dict)
    branch = input_value.get("branch_policy")
    if branch not in {"active", "all"}:
        raise RunError("run.json.input.branch_policy 无效")
    view, metadata = collect_visible_view(source, str(branch))
    if metadata["input_sha256"] != input_value.get("sha256"):
        raise RunError("原始输入内容已变化，请创建新的 run")
    return view, metadata


def select_batch(
    view: dict[str, object], document: dict[str, object], batch_index: int | None
) -> dict[str, object]:
    """按 run 中的引用返回一个批次，或返回全部小输入。"""

    if batch_index is None:
        return view
    parsing = document["parsing"]
    assert isinstance(parsing, dict)
    batches = parsing.get("batches")
    if not isinstance(batches, list) or not 1 <= batch_index <= len(batches):
        raise RunError("batch 超出可用范围")
    batch = batches[batch_index - 1]
    assert isinstance(batch, dict)
    refs = {
        (str(item["source_sha256"]), str(item["conversation_id"]))
        for item in batch["conversations"]
        if isinstance(item, dict)
    }
    selected_sources: list[dict[str, object]] = []
    for source_item in view["sources"]:
        assert isinstance(source_item, dict)
        conversations = [
            conversation
            for conversation in source_item["conversations"]
            if (
                str(conversation["source_sha256"]),
                str(conversation["conversation_id"]),
            )
            in refs
        ]
        if conversations:
            selected_sources.append(
                {"source": source_item["source"], "conversations": conversations}
            )
    return {
        "format": VISIBLE_FORMAT,
        "branch_policy": view["branch_policy"],
        "batch": batch_index,
        "batch_count": len(batches),
        "sources": selected_sources,
    }


def ensure_snapshot(
    knowledge_root: Path, document: dict[str, object]
) -> dict[str, object]:
    """重新扫描知识库，确保 review 前的基线未变化。"""

    inventory, snapshot = inventory_snapshot(knowledge_root)
    knowledge_base = document["knowledge_base"]
    assert isinstance(knowledge_base, dict)
    if snapshot != knowledge_base.get("snapshot_sha256"):
        raise RunError("知识库快照已变化，请创建新的 run")
    return inventory


def safe_target_path(value: str) -> PurePosixPath:
    """校验知识库相对 Markdown 路径。"""

    path = PurePosixPath(value.replace("\\", "/"))
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix.casefold() != ".md"
        or path.parts[0].casefold() == ".knowledge-curator"
    ):
        raise RunError(f"非法知识文档路径：{value}")
    return path


def resolve_target(knowledge_root: Path, target_path: str) -> Path:
    """解析目标并拒绝路径逃逸、链接和非文件。"""

    relative = safe_target_path(target_path)
    root = knowledge_root.resolve(strict=True)
    target = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and knowledge_inventory.is_link_like(current):
            raise RunError(f"目标路径包含链接：{target_path}")
    if not knowledge_inventory.is_within(target.resolve(strict=False), root):
        raise RunError(f"目标路径超出知识库：{target_path}")
    if target.exists() and not target.is_file():
        raise RunError(f"目标不是普通文件：{target_path}")
    return target


def decode_markdown(content: bytes, source: str) -> str:
    """解码 UTF-8 Markdown。"""

    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RunError(f"Markdown 不是有效 UTF-8：{source}") from exc


def unified_diff(before: bytes, after: bytes, target_path: str) -> str:
    """在内存中计算准确 unified diff。"""

    import difflib

    return "".join(
        difflib.unified_diff(
            decode_markdown(before, target_path).splitlines(keepends=True),
            decode_markdown(after, target_path).splitlines(keepends=True),
            fromfile=f"a/{target_path}",
            tofile=f"b/{target_path}",
        )
    )


def document_outline(text: str) -> dict[str, object]:
    """返回新建文档审核所需的轻量结构，不重复输出正文。"""

    headings = knowledge_inventory.parse_headings(text)
    return {
        "title": next(
            (heading.text for heading in headings if heading.level == 1),
            None,
        ),
        "headings": [
            {"level": heading.level, "text": heading.text}
            for heading in headings
        ],
        "line_count": len(text.splitlines()),
    }


def discover_proposed(run_root: Path) -> list[tuple[str, Path]]:
    """发现 proposed 下的普通 Markdown，拒绝其他文件和链接。"""

    proposed_root = run_root / PROPOSED_DIRECTORY
    discovered: list[tuple[str, Path]] = []
    for path in sorted(proposed_root.rglob("*"), key=lambda item: str(item).casefold()):
        if knowledge_inventory.is_link_like(path):
            raise RunError(f"proposed 中不得包含链接：{path}")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix.casefold() != ".md":
            raise RunError(f"proposed 中只允许 Markdown 文件：{path}")
        relative = path.relative_to(proposed_root).as_posix()
        safe_target_path(relative)
        discovered.append((relative, path))
    if not discovered:
        raise RunError("proposed 目录中没有待审核 Markdown")
    return discovered


def prepare_review(
    knowledge_root: Path,
    source: Path,
    run_root: Path,
    document: dict[str, object],
    review_input_text: str,
    full_diff: bool,
) -> dict[str, object]:
    """校验 proposed 与来源元数据，并按场景返回审核材料。"""

    if document["status"] == "applied":
        raise RunError("已应用的 run 不能重新准备审核")
    ensure_snapshot(knowledge_root, document)
    _, metadata = current_view(source, document)
    proposed_documents = discover_proposed(run_root)
    proposed_targets = {target_path for target_path, _ in proposed_documents}
    source_map, issues = parse_review_input(
        review_input_text,
        proposed_targets,
        metadata["conversations"],
    )
    outputs: list[dict[str, object]] = []
    review_items: list[dict[str, object]] = []
    for target_path, proposed_path in proposed_documents:
        target = resolve_target(knowledge_root, target_path)
        proposed = proposed_path.read_bytes()
        before = target.read_bytes() if target.exists() else b""
        if not proposed:
            raise RunError(f"proposed 文档不能为空：{target_path}")
        text = decode_markdown(proposed, target_path)
        quality = knowledge_document_quality.analyze_document(
            text,
            "compatible" if target.exists() else "strict",
            target_path,
        )
        if quality["status"] != "ok":
            raise RunError(f"文档质量检查失败：{target_path}：{quality['issues']}")
        if proposed == before:
            raise RunError(f"proposed 与现有文档相同：{target_path}")
        output = {
            "target_path": target_path,
            "proposed_path": f"{PROPOSED_DIRECTORY}/{target_path}",
            "base_sha256": sha256_bytes(before),
            "proposed_sha256": sha256_bytes(proposed),
            "sources": source_map[target_path],
        }
        outputs.append(output)
        include_diff = target.exists() or full_diff
        review_item: dict[str, object] = {
            "target_path": target_path,
            "proposed_path": output["proposed_path"],
            "change": "update" if target.exists() else "create",
            "base_sha256": output["base_sha256"],
            "proposed_sha256": output["proposed_sha256"],
            "sources": source_map[target_path],
            "outline": document_outline(text),
            "diff_included": include_diff,
        }
        if include_diff:
            review_item["diff"] = unified_diff(before, proposed, target_path)
        review_items.append(review_item)
    document["outputs"] = outputs
    document["issues"] = issues
    document["approval"] = None
    document["apply"] = {"status": "not_applied", "entries": []}
    document["status"] = "review_ready"
    save_run(run_root, document)
    return {
        "format": REVIEW_FORMAT,
        "status": "review_ready",
        "run_id": document["run_id"],
        "outputs": review_items,
        "issues": document["issues"],
    }


def build_context(
    knowledge_root: Path,
    document: dict[str, object],
    queries: list[str],
    limit: int,
) -> dict[str, object]:
    """在非空知识库中以内存检索返回有限相关 Markdown 原文。"""

    if not 1 <= limit <= 20:
        raise RunError("limit 必须在 1 到 20 之间")
    knowledge_base = document["knowledge_base"]
    assert isinstance(knowledge_base, dict)
    if knowledge_base.get("empty") is True:
        return {
            "format": CONTEXT_FORMAT,
            "status": "skipped_empty_knowledge_base",
            "queries": queries,
            "matches": [],
        }
    ensure_snapshot(knowledge_root, document)
    sections: list[knowledge_retrieval.SearchSection] = []
    files, warnings = knowledge_inventory.discover_markdown_files(knowledge_root)
    root = knowledge_root.resolve(strict=True)
    for path in files:
        relative = knowledge_inventory.relative_path(path, root)
        raw = path.read_bytes()
        text = knowledge_inventory.decode_markdown(raw, relative)
        title = path.stem
        headings = knowledge_inventory.parse_headings(text)
        if headings:
            title = headings[0].text or title
        for order, fragment in enumerate(
            knowledge_inventory.parse_section_fragments(text, relative)
        ):
            sections.append(
                knowledge_retrieval.SearchSection(
                    path=relative,
                    file_sha256=sha256_bytes(raw),
                    title=title,
                    section_id=fragment.section_id,
                    section_sha256=sha256_bytes(fragment.markdown.encode("utf-8")),
                    heading_path=tuple(fragment.heading_path),
                    order=order,
                    markdown=fragment.markdown,
                )
            )
    terms = knowledge_retrieval.query_terms(" ".join(queries))
    ranked: list[tuple[int, str, int, dict[str, object]]] = []
    for section in sections:
        score, matched = knowledge_retrieval.score_section(section, terms)
        if score:
            ranked.append(
                (
                    -score,
                    section.path.casefold(),
                    section.order,
                    {
                        "path": section.path,
                        "heading_path": list(section.heading_path),
                        "score": score,
                        "matched_terms": matched,
                        "markdown": section.markdown,
                    },
                )
            )
    ranked.sort(key=lambda item: item[:3])
    return {
        "format": CONTEXT_FORMAT,
        "status": "ok" if not warnings else "completed_with_issues",
        "queries": queries,
        "matches": [item[3] for item in ranked[:limit]],
        "warnings": warnings,
    }


def prepare_apply_state(
    knowledge_root: Path,
    run_root: Path,
    outputs: list[dict[str, object]],
) -> None:
    """校验全部审核绑定，并保存可恢复的临时状态。"""

    state_path = run_root / APPLY_STATE_FILE
    if state_path.exists():
        raise RunError("检测到未完成的应用状态，请先运行 recover")
    entries: list[dict[str, object]] = []
    for output in outputs:
        target_path = str(output["target_path"])
        target = resolve_target(knowledge_root, target_path)
        proposed = run_root / str(output["proposed_path"])
        before = target.read_bytes() if target.exists() else b""
        after = proposed.read_bytes()
        if sha256_bytes(before) != output["base_sha256"]:
            raise RunError(f"知识库内容已变化，批准失效：{target_path}")
        if sha256_bytes(after) != output["proposed_sha256"]:
            raise RunError(f"proposed 内容已变化，批准失效：{target_path}")
        entries.append(
            {
                "target_path": target_path,
                "existed": target.exists(),
                "base_sha256": str(output["base_sha256"]),
                "proposed_sha256": str(output["proposed_sha256"]),
                "before_base64": base64.b64encode(before).decode("ascii"),
            }
        )
    atomic_write(state_path, serialize_json({"entries": entries}).encode("utf-8"))


def apply_outputs(
    knowledge_root: Path,
    run_root: Path,
    document: dict[str, object],
    approved_paths: list[str],
) -> dict[str, object]:
    """只在全部目标被明确批准且哈希仍匹配时应用。"""

    if document["status"] != "review_ready":
        raise RunError("run 尚未准备好审核，不能应用")
    outputs = document["outputs"]
    assert isinstance(outputs, list)
    expected = [str(output["target_path"]) for output in outputs]
    if sorted(set(approved_paths)) != sorted(expected):
        raise RunError("apply 必须用 --approve 明确列出本次全部目标文档")
    approval_entries = [
        {
            "target_path": output["target_path"],
            "base_sha256": output["base_sha256"],
            "proposed_sha256": output["proposed_sha256"],
        }
        for output in outputs
    ]
    prepare_apply_state(knowledge_root, run_root, outputs)
    document["approval"] = {"entries": approval_entries}
    save_run(run_root, document)
    applied: list[dict[str, str]] = []
    try:
        for output in outputs:
            target_path = str(output["target_path"])
            target = resolve_target(knowledge_root, target_path)
            proposed = run_root / str(output["proposed_path"])
            atomic_write(target, proposed.read_bytes())
            applied.append(
                {
                    "target_path": target_path,
                    "sha256": str(output["proposed_sha256"]),
                }
            )
    except (OSError, RunError) as exc:
        raise RunError("应用中断；请运行 recover 恢复知识库") from exc
    document["status"] = "applied"
    document["apply"] = {"status": "applied", "entries": applied}
    save_run(run_root, document)
    (run_root / APPLY_STATE_FILE).unlink(missing_ok=True)
    return {
        "status": "applied",
        "applied_file_count": len(applied),
        "entries": applied,
    }


def recover_apply(
    knowledge_root: Path, run_root: Path, document: dict[str, object]
) -> dict[str, object]:
    """恢复中断应用前的全部目标内容。"""

    state_path = run_root / APPLY_STATE_FILE
    if not state_path.is_file():
        raise RunError("没有可恢复的应用状态")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        entries = state["entries"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RunError("应用恢复状态损坏，不能自动恢复") from exc
    if not isinstance(entries, list):
        raise RunError("应用恢复状态 entries 必须是数组")
    actions: list[tuple[Path, bool, bytes]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RunError("应用恢复条目无效")
        require_exact_fields(
            entry,
            {
                "target_path", "existed", "base_sha256",
                "proposed_sha256", "before_base64",
            },
            "应用恢复条目 ",
        )
        target_path = str(entry.get("target_path", ""))
        target = resolve_target(knowledge_root, target_path)
        try:
            before = base64.b64decode(str(entry["before_base64"]), validate=True)
        except (KeyError, ValueError) as exc:
            raise RunError("无法解码恢复内容") from exc
        if sha256_bytes(before) != entry["base_sha256"]:
            raise RunError("恢复状态中的基线内容与哈希不一致")
        current = target.read_bytes() if target.exists() else None
        current_sha = sha256_bytes(current) if current is not None else None
        if current_sha == entry["proposed_sha256"]:
            actions.append((target, bool(entry["existed"]), before))
            continue
        if entry["existed"] is True and current_sha == entry["base_sha256"]:
            continue
        if entry["existed"] is False and current is None:
            continue
        raise RunError(f"目标在应用中断后被外部修改，拒绝恢复：{target_path}")
    restored = 0
    for target, existed, before in reversed(actions):
        if existed:
            atomic_write(target, before)
        else:
            target.unlink(missing_ok=True)
        restored += 1
    state_path.unlink(missing_ok=True)
    document["status"] = "review_ready"
    document["approval"] = None
    document["apply"] = {"status": "recovered", "entries": []}
    save_run(run_root, document)
    return {"status": "recovered", "restored_file_count": restored}


def execute_command(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    """执行一个 v2 命令并返回结构化结果。"""

    if args.command == "init":
        run_root, document = initialize_run(
            args.knowledge_root, args.input_path, args.run_id, args.branch
        )
        parsing = document["parsing"]
        assert isinstance(parsing, dict)
        knowledge_base = document["knowledge_base"]
        assert isinstance(knowledge_base, dict)
        return {
            "status": "initialized",
            "run_directory": str(run_root),
            "conversation_count": parsing["conversation_count"],
            "message_count": parsing["message_count"],
            "batch_count": len(parsing["batches"]),
            "knowledge_base_empty": knowledge_base["empty"],
        }, 0

    run_root = resolve_directory(args.run_directory, "run 目录")
    root, source, document = load_run(run_root)
    if args.command == "read-input":
        view, _ = current_view(source, document)
        return select_batch(view, document, args.batch), 0
    if args.command == "context":
        return build_context(root, document, args.query, args.limit), 0
    if args.command == "prepare-review":
        return prepare_review(
            root,
            source,
            run_root,
            document,
            sys.stdin.read(),
            args.full_diff,
        ), 0
    if args.command == "apply":
        return apply_outputs(root, run_root, document, args.approve), 0
    if args.command == "recover":
        return recover_apply(root, run_root, document), 0
    raise RunError(f"不支持的命令：{args.command}")


def create_parser() -> argparse.ArgumentParser:
    """创建唯一公开运行 CLI。"""

    parser = argparse.ArgumentParser(
        description="以单一 run.v2 流程整理 ChatGPT 对话知识。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="检查输入并创建 run")
    init_parser.add_argument("knowledge_root", type=Path)
    init_parser.add_argument("input_path", type=Path)
    init_parser.add_argument("run_id")
    init_parser.add_argument("--branch", choices=("active", "all"), default="active")

    read_parser = subparsers.add_parser("read-input", help="动态读取可见消息")
    read_parser.add_argument("run_directory", type=Path)
    read_parser.add_argument("--batch", type=int)

    context_parser = subparsers.add_parser("context", help="按需读取相关知识原文")
    context_parser.add_argument("run_directory", type=Path)
    context_parser.add_argument("--query", action="append", required=True)
    context_parser.add_argument("--limit", type=int, default=20)

    review_parser = subparsers.add_parser(
        "prepare-review", help="从标准输入登记来源并准备审核"
    )
    review_parser.add_argument("run_directory", type=Path)
    review_parser.add_argument(
        "--full-diff",
        action="store_true",
        help="同时返回新建文档的完整 diff",
    )

    apply_parser = subparsers.add_parser("apply", help="应用明确批准的全部目标")
    apply_parser.add_argument("run_directory", type=Path)
    apply_parser.add_argument("--approve", action="append", required=True)

    recover_parser = subparsers.add_parser("recover", help="恢复中断的应用")
    recover_parser.add_argument("run_directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行 CLI；标准输出只写 JSON，错误写标准错误。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = execute_command(args)
    except (
        RunError,
        chatgpt_export.ExportError,
        knowledge_document_quality.DocumentQualityError,
        knowledge_inventory.InventoryError,
        knowledge_retrieval.RetrievalError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(serialize_json(result), end="")
    return exit_code
