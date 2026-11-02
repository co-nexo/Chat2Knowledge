#!/usr/bin/env python3
"""检查并标准化 ChatGPT 对话导出 JSON。

本模块只使用 Python 标准库。它不会修改输入文件，只会新建标准化输出；
如果目标文件已经存在，则视为错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


FORMAT_VERSION = "conversation-knowledge-curator.normalized.v1"
ELIGIBLE_ROLES = {"user", "assistant"}
HIDDEN_CONTENT_TYPES = {
    "analysis",
    "model_editable_context",
    "reasoning",
    "system_error",
    "thoughts",
}
HIDDEN_CHANNELS = {"analysis", "reasoning"}
HIDDEN_METADATA_FLAGS = {
    "hidden",
    "is_hidden",
    "is_visually_hidden_from_conversation",
}
VISIBLE_ASSISTANT_RECIPIENTS = {"all", "user"}


class ExportError(ValueError):
    """输入 JSON 不符合受支持的导出容器时抛出。"""


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON 文件，并兼容可能存在的 BOM。"""

    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def conversation_objects(data: Any) -> tuple[list[dict[str, Any]], str]:
    """提取对话对象列表，并返回已识别的顶层容器类型。"""

    if isinstance(data, list):
        values = data
        envelope = "list"
    elif isinstance(data, dict) and isinstance(data.get("conversations"), list):
        values = data["conversations"]
        envelope = "object.conversations"
    elif isinstance(data, dict) and isinstance(data.get("mapping"), dict):
        values = [data]
        envelope = "single-conversation"
    else:
        raise ExportError(
            "不支持的 JSON 容器；应提供单个对话对象、对话对象列表，"
            "或包含 conversations 列表的对象"
        )

    conversations = [value for value in values if isinstance(value, dict)]
    if len(conversations) != len(values):
        raise ExportError("对话容器中包含非对象元素")
    return conversations, envelope


def message_for_node(node: Any) -> dict[str, Any] | None:
    """从 mapping 节点中提取消息对象。"""

    if not isinstance(node, dict):
        return None
    message = node.get("message")
    return message if isinstance(message, dict) else None


def mapping_for(conversation: dict[str, Any]) -> dict[str, Any]:
    """返回对话的 mapping；缺失或类型无效时返回空字典。"""

    mapping = conversation.get("mapping")
    return mapping if isinstance(mapping, dict) else {}


def stable_all_node_ids(mapping: dict[str, Any]) -> list[str]:
    """按父节点优先且尊重 children 声明顺序，稳定返回全部节点 ID。"""

    result: list[str] = []
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited or node_id not in mapping:
            return
        visited.add(node_id)
        result.append(node_id)
        node = mapping[node_id]
        if isinstance(node, dict) and isinstance(node.get("children"), list):
            for child in node["children"]:
                if isinstance(child, str):
                    visit(child)

    roots: list[str] = []
    for node_id, node in mapping.items():
        if not isinstance(node_id, str):
            continue
        parent = node.get("parent") if isinstance(node, dict) else None
        if not isinstance(parent, str) or parent not in mapping:
            roots.append(node_id)
    for node_id in roots:
        visit(node_id)
    for node_id in mapping:
        if isinstance(node_id, str):
            visit(node_id)
    return result


def active_node_ids(
    conversation: dict[str, Any], mapping: dict[str, Any]
) -> tuple[list[str], str | None]:
    """返回活动分支节点；无法确认活动分支时回退到稳定全量遍历。"""

    current = conversation.get("current_node")
    if not isinstance(current, str) or current not in mapping:
        return stable_all_node_ids(mapping), (
            "current_node 缺失或无效；已确定性遍历全部 mapping 节点"
        )

    reversed_path: list[str] = []
    seen: set[str] = set()
    node_id: str | None = current
    while node_id is not None:
        if node_id in seen:
            return stable_all_node_ids(mapping), (
                "活动分支的 parent 链存在循环；已确定性遍历全部 mapping 节点"
            )
        if node_id not in mapping:
            return stable_all_node_ids(mapping), (
                "活动分支的 parent 链引用未知节点；已确定性遍历全部 mapping 节点"
            )
        seen.add(node_id)
        reversed_path.append(node_id)
        node = mapping[node_id]
        parent = node.get("parent") if isinstance(node, dict) else None
        node_id = parent if isinstance(parent, str) else None
    return list(reversed(reversed_path)), None


def lower_string(value: Any) -> str:
    """将字符串去除首尾空白并转换为小写；非字符串返回空串。"""

    return value.strip().lower() if isinstance(value, str) else ""


def exclusion_reason(message: dict[str, Any]) -> str | None:
    """返回消息不可作为用户可见知识输入的原因。"""

    author = message.get("author")
    role = lower_string(author.get("role")) if isinstance(author, dict) else ""
    if role not in ELIGIBLE_ROLES:
        return "ineligible_role"

    content = message.get("content")
    content_type = (
        lower_string(content.get("content_type")) if isinstance(content, dict) else ""
    )
    if content_type in HIDDEN_CONTENT_TYPES:
        return "hidden_content_type"

    metadata = message.get("metadata")
    author_metadata = author.get("metadata") if isinstance(author, dict) else None
    metadata_objects = [
        value for value in (metadata, author_metadata) if isinstance(value, dict)
    ]
    if metadata_objects:
        if any(
            metadata_object.get(flag) is True
            for metadata_object in metadata_objects
            for flag in HIDDEN_METADATA_FLAGS
        ):
            return "hidden_metadata"
        metadata_channel = next(
            (
                metadata_object.get("channel")
                for metadata_object in metadata_objects
                if metadata_object.get("channel")
            ),
            None,
        )
        channel = lower_string(message.get("channel") or metadata_channel)
    else:
        channel = lower_string(message.get("channel"))
    if channel in HIDDEN_CHANNELS:
        return "hidden_channel"

    recipient = lower_string(message.get("recipient"))
    if role == "assistant" and recipient and recipient not in VISIBLE_ASSISTANT_RECIPIENTS:
        return "non_user_recipient"
    return None


def text_from_part(part: Any) -> str | None:
    """从安全的纯文本 part 中提取文本，忽略未知结构化内容。"""

    if isinstance(part, str):
        return part
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        part_type = lower_string(part.get("content_type") or part.get("type"))
        if part_type not in {"", "text"}:
            return None
        return part["text"]
    return None


def extract_text(message: dict[str, Any]) -> str:
    """提取消息中的可见纯文本，并保持片段顺序。"""

    content = message.get("content")
    if not isinstance(content, dict):
        return ""

    pieces: list[str] = []
    parts = content.get("parts")
    if isinstance(parts, list):
        pieces.extend(text for part in parts if (text := text_from_part(part)) is not None)
    elif isinstance(content.get("text"), str):
        pieces.append(content["text"])
    return "\n".join(piece for piece in pieces if piece).strip()


def selected_nodes(
    conversation: dict[str, Any], branch: str
) -> tuple[dict[str, Any], list[str], list[str]]:
    """根据分支策略选择节点，并返回不会中断处理的结构警告。"""

    mapping = mapping_for(conversation)
    warnings: list[str] = []
    if not mapping:
        warnings.append("对话缺少 mapping 对象，或 mapping 为空")
        return mapping, [], warnings
    if branch == "all":
        return mapping, stable_all_node_ids(mapping), warnings
    node_ids, warning = active_node_ids(conversation, mapping)
    if warning:
        warnings.append(warning)
    return mapping, node_ids, warnings


def normalize_conversation(
    conversation: dict[str, Any], branch: str
) -> tuple[dict[str, Any], list[str]]:
    """把一个对话标准化为用户可见消息列表。"""

    mapping, node_ids, warnings = selected_nodes(conversation, branch)
    messages: list[dict[str, Any]] = []
    for node_id in node_ids:
        node = mapping.get(node_id)
        message = message_for_node(node)
        if message is None or exclusion_reason(message) is not None:
            continue
        text = extract_text(message)
        if not text:
            continue
        author = message.get("author")
        assert isinstance(author, dict)  # exclusion_reason 已确认作者结构有效
        parent = node.get("parent") if isinstance(node, dict) else None
        messages.append(
            {
                "message_id": message.get("id") or node_id,
                "node_id": node_id,
                "parent_node_id": parent if isinstance(parent, str) else None,
                "role": lower_string(author.get("role")),
                "author_name": (
                    author.get("name")
                    if isinstance(author.get("name"), str)
                    else None
                ),
                "create_time": message.get("create_time"),
                "content": {"type": "text", "text": text},
            }
        )

    return (
        {
            "conversation_id": (
                conversation.get("id") or conversation.get("conversation_id")
            ),
            "title": (
                conversation.get("title")
                if isinstance(conversation.get("title"), str)
                else None
            ),
            "create_time": conversation.get("create_time"),
            "update_time": conversation.get("update_time"),
            "branch_selection": branch,
            "messages": messages,
        },
        warnings,
    )


def sha256_file(path: Path) -> str:
    """流式计算文件的 SHA-256，避免一次性载入大文件。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_export(path: Path, branch: str = "active") -> dict[str, Any]:
    """标准化整个导出文件，并附加来源和警告信息。"""

    data = load_json(path)
    conversations, envelope = conversation_objects(data)
    normalized: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, conversation in enumerate(conversations):
        item, item_warnings = normalize_conversation(conversation, branch)
        normalized.append(item)
        warnings.extend(
            {"conversation_index": index, "message": warning}
            for warning in item_warnings
        )
    return {
        "format": FORMAT_VERSION,
        "source": {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "container": envelope,
        },
        "conversations": normalized,
        "warnings": warnings,
    }


def inspect_export(path: Path) -> dict[str, Any]:
    """生成不包含消息正文的导出结构检查报告。"""

    data = load_json(path)
    conversations, envelope = conversation_objects(data)
    roles: Counter[str] = Counter()
    content_types: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    conversation_keys: Counter[str] = Counter()
    mapping_nodes = 0
    messages = 0
    active_pointer_valid = 0
    warnings: list[dict[str, Any]] = []

    for index, conversation in enumerate(conversations):
        conversation_keys.update(str(key) for key in conversation)
        mapping = mapping_for(conversation)
        mapping_nodes += len(mapping)
        if not mapping:
            warnings.append(
                {"conversation_index": index, "message": "mapping 缺失或为空"}
            )
        if mapping:
            current_node = conversation.get("current_node")
            if isinstance(current_node, str) and current_node in mapping:
                active_pointer_valid += 1
            _, active_warning = active_node_ids(conversation, mapping)
            if active_warning:
                warnings.append(
                    {"conversation_index": index, "message": active_warning}
                )
        for node in mapping.values():
            message = message_for_node(node)
            if message is None:
                continue
            messages += 1
            author = message.get("author")
            role = lower_string(author.get("role")) if isinstance(author, dict) else ""
            roles[role or "<missing>"] += 1
            content = message.get("content")
            content_type = (
                lower_string(content.get("content_type"))
                if isinstance(content, dict)
                else ""
            )
            content_types[content_type or "<missing>"] += 1
            reason = exclusion_reason(message)
            if reason is None and not extract_text(message):
                reason = "no_plain_text"
            exclusions[reason or "included"] += 1

    return {
        "source": {"path": str(path.resolve()), "sha256": sha256_file(path)},
        "container": envelope,
        "conversation_count": len(conversations),
        "conversation_keys": dict(sorted(conversation_keys.items())),
        "mapping_node_count": mapping_nodes,
        "message_count": messages,
        "valid_current_node_count": active_pointer_valid,
        "roles": dict(sorted(roles.items())),
        "content_types": dict(sorted(content_types.items())),
        "visibility": dict(sorted(exclusions.items())),
        "warnings": warnings,
    }


def write_new_json(path: Path, value: Any) -> None:
    """把值写入新的 JSON 文件；目标已存在时拒绝覆盖。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        description="检查或标准化 ChatGPT 对话导出 JSON。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="输出不包含消息正文的结构报告"
    )
    inspect_parser.add_argument("input", type=Path, help="待检查的导出 JSON")

    normalize_parser = subparsers.add_parser(
        "normalize", help="把用户可见消息写入新的标准化 JSON"
    )
    normalize_parser.add_argument("input", type=Path, help="待标准化的导出 JSON")
    normalize_parser.add_argument("output", type=Path, help="新建的输出 JSON")
    normalize_parser.add_argument(
        "--branch",
        choices=("active", "all"),
        default="active",
        help="分支选择策略：active 为当前分支，all 为全部分支",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令行入口，并把可预期错误转换为非零退出码。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            json.dump(inspect_export(args.input), sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            if args.input.resolve() == args.output.resolve():
                raise ExportError("输出路径必须与输入路径不同")
            write_new_json(args.output, normalize_export(args.input, args.branch))
    except (ExportError, FileExistsError, json.JSONDecodeError, OSError) as exc:
        parser.exit(2, f"错误：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

