#!/usr/bin/env python3
"""实现 conversation-knowledge-curator.run.v6 的单流程运行核心。"""

from __future__ import annotations

import argparse
import base64
import bisect
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

FORMAT_VERSION = "conversation-knowledge-curator.run.v6"
VISIBLE_FORMAT = "conversation-knowledge-curator.visible-input.v3"
CONTEXT_FORMAT = "conversation-knowledge-curator.context.v1"
REVIEW_FORMAT = "conversation-knowledge-curator.review.v3"
REVIEW_INPUT_FORMAT = "conversation-knowledge-curator.review-input.v2"
RUN_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
RUN_RELATIVE_ROOT = PurePosixPath(".knowledge-curator/runs")
PROPOSED_DIRECTORY = "proposed"
APPLY_STATE_FILE = "apply-state.json"
SMALL_INPUT_OUTPUT_BUDGET_BYTES = 48_000
MEDIUM_INPUT_OUTPUT_BUDGET_BYTES = 32_000
LARGE_INPUT_OUTPUT_BUDGET_BYTES = 24_000
SMALL_INPUT_MAX_BYTES = 48_000
MEDIUM_INPUT_MAX_BYTES = 512_000
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


def inventory_domains(inventory: dict[str, object]) -> list[str]:
    """从现有相对路径提取稳定的顶层领域线索，供 Codex 决定是否检索。"""

    domains: set[str] = set()
    files = inventory.get("files", [])
    if not isinstance(files, list):
        return []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = PurePosixPath(str(entry["path"]))
        if len(path.parts) > 1:
            domains.add(path.parts[0])
        else:
            title = entry.get("title")
            domains.add(str(title) if isinstance(title, str) and title else path.stem)
    return sorted(domains, key=lambda value: (value.casefold(), value))


def input_source_record(source: dict[str, object]) -> dict[str, object]:
    """把解析器来源描述收敛为 run 中稳定、无正文的来源记录。"""

    kind = str(source.get("kind", ""))
    path = (
        source.get("path")
        if kind == "json_file"
        else source.get("archive_path")
    )
    return {
        "kind": kind,
        "path": str(path or ""),
        "member_path": (
            str(source["member_path"])
            if source.get("member_path") is not None
            else None
        ),
        "sha256": str(source.get("sha256", "")),
        "size_bytes": source.get("size_bytes"),
    }


def aggregate_source_sha256(records: list[dict[str, object]]) -> str:
    """根据完整来源身份和内容哈希生成输入集合哈希。"""

    identities = sorted(records, key=serialize_json)
    return sha256_bytes(serialize_json(identities).encode("utf-8"))


def collect_visible_view(
    input_path: Path, branch: str
) -> tuple[dict[str, object], dict[str, object]]:
    """检查并在内存中构造用户可见消息视图，不写 normalized 文件。"""

    resolved = chatgpt_export.resolve_input(input_path)
    if resolved.input_type not in {"json_file", "zip_archive", "directory"}:
        raise RunError("当前运行入口只接受 ChatGPT JSON、ZIP 或来源目录")
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
    source_records: list[dict[str, object]] = []
    seen_source_hashes: set[str] = set()
    warnings: list[object] = list(resolved.warnings)
    for source, inspection in zip(resolved.sources, inspections, strict=True):
        result = chatgpt_export.normalize_source(
            source,
            branch=branch,
            inspection=inspection,
        )
        source_info = dict(result.source)
        source_sha = str(source_info["sha256"])
        source_records.append(input_source_record(source_info))
        if source_sha in seen_source_hashes:
            warnings.append(
                {
                    "code": "duplicate_source_content",
                    "source_sha256": source_sha,
                    "message": "来源内容与前序来源完全相同，已避免重复处理",
                }
            )
            continue
        seen_source_hashes.add(source_sha)
        source_conversations: list[dict[str, object]] = []
        for conversation in result.document["conversations"]:
            item = dict(conversation)
            item["source_sha256"] = source_sha
            item["conversation_ref"] = f"c{len(conversations) + 1}"
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
        "input_sha256": aggregate_source_sha256(source_records),
        "sources": source_records,
        "conversation_count": len(conversations),
        "message_count": sum(
            len(item.get("messages", [])) for item in conversations
        ),
        "warnings": warnings,
        "conversations": conversations,
    }
    return view, metadata


def message_units(messages: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    """优先把一个 user 消息及其后续 assistant 回复保留在同一处理单元。"""

    units: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for message in messages:
        if message.get("role") == "user" and current:
            units.append(current)
            current = []
        current.append(message)
    if current:
        units.append(current)
    return units


def natural_utf8_ranges(text: str, max_part_bytes: int) -> list[tuple[int, int]]:
    """优先按段落、行和句末拆分文本，并保证 UTF-8 字符不被截断。"""

    if max_part_bytes < 1:
        raise RunError("消息分片字节预算必须是正整数")
    positions = [0]
    paragraph_boundaries: list[int] = []
    line_boundaries: list[int] = []
    sentence_boundaries: list[int] = []
    byte_offset = 0
    for index, character in enumerate(text):
        byte_offset += len(character.encode("utf-8"))
        positions.append(byte_offset)
        if character == "\n":
            line_boundaries.append(byte_offset)
            if index > 0 and text[index - 1] == "\n":
                paragraph_boundaries.append(byte_offset)
        elif character in "。！？.!?；;":
            sentence_boundaries.append(byte_offset)
    total_bytes = positions[-1]
    if total_bytes == 0:
        return [(0, 0)]

    def last_within(boundaries: list[int], start: int, limit: int) -> int | None:
        """返回区间内最后一个边界。"""

        position = bisect.bisect_right(boundaries, limit) - 1
        if position >= 0 and boundaries[position] > start:
            return boundaries[position]
        return None

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total_bytes:
        limit = min(total_bytes, start + max_part_bytes)
        end = None
        for boundaries in (
            paragraph_boundaries,
            line_boundaries,
            sentence_boundaries,
        ):
            end = last_within(boundaries, start, limit)
            if end is not None:
                break
        if end is None:
            position = bisect.bisect_right(positions, limit) - 1
            if position < 0 or positions[position] <= start:
                position = bisect.bisect_right(positions, start)
            if position >= len(positions):
                raise RunError("无法找到安全的 UTF-8 消息分片边界")
            end = positions[position]
        ranges.append((start, end))
        start = end
    return ranges


def public_part(part: dict[str, object]) -> dict[str, object]:
    """移除只用于内存编排的来源与对话字段。"""

    return {
        key: part[key]
        for key in (
            "message_id",
            "part_index",
            "part_count",
            "start_byte",
            "end_byte",
            "message_sha256",
        )
    }


def flatten_batch_parts(batch: dict[str, object]) -> list[dict[str, object]]:
    """把 run 中按对话分组的分片引用恢复为有序内存列表。"""

    flattened: list[dict[str, object]] = []
    conversations = batch.get("conversations")
    if not isinstance(conversations, list):
        raise RunError("批次 conversations 必须是数组")
    for reference in conversations:
        if not isinstance(reference, dict) or not isinstance(
            reference.get("parts"), list
        ):
            raise RunError("批次对话分片引用无效")
        for part in reference["parts"]:
            if not isinstance(part, dict):
                raise RunError("批次消息分片引用无效")
            flattened.append(
                {
                    "source_sha256": reference["source_sha256"],
                    "conversation_id": reference["conversation_id"],
                    **part,
                }
            )
    return flattened


def render_batch_view(
    view: dict[str, object],
    parts: list[dict[str, object]],
    batch_index: int,
    batch_count: int,
) -> dict[str, object]:
    """从原始可见视图按字节偏移动态重建一个只读批次。"""

    parts_by_message: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for part in parts:
        key = (
            str(part["source_sha256"]),
            str(part["conversation_id"]),
            str(part["message_id"]),
        )
        parts_by_message.setdefault(key, []).append(part)

    selected_conversations: list[dict[str, object]] = []
    sources = view.get("sources")
    if not isinstance(sources, list):
        raise RunError("可见消息视图 sources 必须是数组")
    for source_item in sources:
        if not isinstance(source_item, dict):
            raise RunError("可见消息来源必须是对象")
        conversations = source_item.get("conversations")
        if not isinstance(conversations, list):
            raise RunError("可见消息来源 conversations 必须是数组")
        for conversation in conversations:
            if not isinstance(conversation, dict):
                raise RunError("可见对话必须是对象")
            source_sha256 = str(conversation.get("source_sha256", ""))
            conversation_id = str(conversation.get("conversation_id", ""))
            conversation_ref = str(conversation.get("conversation_ref", ""))
            if not conversation_ref:
                raise RunError("可见对话缺少 conversation_ref")
            selected_messages: list[dict[str, object]] = []
            messages = conversation.get("messages")
            if not isinstance(messages, list):
                raise RunError("可见对话 messages 必须是数组")
            for message in messages:
                if not isinstance(message, dict):
                    raise RunError("可见消息必须是对象")
                message_id = str(message.get("message_id", ""))
                key = (source_sha256, conversation_id, message_id)
                message_parts = parts_by_message.pop(key, [])
                if not message_parts:
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    raise RunError(f"可见消息缺少文本：{message_id}")
                raw_text = text.encode("utf-8")
                message_sha256 = sha256_bytes(raw_text)
                for part in message_parts:
                    if part["message_sha256"] != message_sha256:
                        raise RunError(f"消息分片 Hash 不一致：{message_id}")
                    start_byte = int(part["start_byte"])
                    end_byte = int(part["end_byte"])
                    if not 0 <= start_byte < end_byte <= len(raw_text):
                        raise RunError(f"消息分片字节范围越界：{message_id}")
                    try:
                        fragment_text = raw_text[start_byte:end_byte].decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise RunError(
                            f"消息分片破坏 UTF-8 字符边界：{message_id}"
                        ) from exc
                    selected_message = {
                        "message_id": message_id,
                        "role": message.get("role"),
                        "text": fragment_text,
                    }
                    if int(part["part_count"]) > 1:
                        selected_message["fragment"] = {
                            key_name: part[key_name]
                            for key_name in (
                                "part_index",
                                "part_count",
                            )
                        }
                    selected_messages.append(selected_message)
            if selected_messages:
                selected_conversations.append(
                    {
                        "conversation_ref": conversation_ref,
                        "conversation_id": conversation_id,
                        "title": conversation.get("title"),
                        "messages": selected_messages,
                    }
                )
    if parts_by_message:
        raise RunError("批次引用了不存在的可见消息")
    result: dict[str, object] = {
        "format": VISIBLE_FORMAT,
        "batch": batch_index,
        "batch_count": batch_count,
        "output_bytes": 0,
        "conversations": selected_conversations,
    }
    for _ in range(8):
        output_bytes = len(serialize_json(result).encode("utf-8"))
        if result["output_bytes"] == output_bytes:
            return result
        result["output_bytes"] = output_bytes
    raise RunError("无法稳定计算 read-input 输出字节数")


def split_message_parts(
    view: dict[str, object],
    source_sha256: str,
    conversation_id: str,
    message: dict[str, object],
    max_output_bytes: int,
) -> list[dict[str, object]]:
    """为单条消息规划不落盘的分片，直到每片能独立安全输出。"""

    message_id = message.get("message_id")
    text = message.get("text")
    if not isinstance(message_id, str) or not message_id or not isinstance(text, str):
        raise RunError("可见消息缺少有效 message_id 或 text")
    raw_text = text.encode("utf-8")
    if not raw_text:
        raise RunError(f"可见消息文本不能为空：{message_id}")
    raw_limit = max_output_bytes
    message_sha256 = sha256_bytes(raw_text)
    while True:
        ranges = natural_utf8_ranges(text, raw_limit)
        part_count = len(ranges)
        parts = [
            {
                "source_sha256": source_sha256,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "part_index": index,
                "part_count": part_count,
                "start_byte": start_byte,
                "end_byte": end_byte,
                "message_sha256": message_sha256,
            }
            for index, (start_byte, end_byte) in enumerate(ranges, start=1)
        ]
        if all(
            render_batch_view(
                view,
                [part],
                999_999_999_999,
                999_999_999_999,
            )["output_bytes"]
            <= max_output_bytes
            for part in parts
        ):
            return parts
        if raw_limit == 1:
            raise RunError(
                "read-input 固定元数据超过输出预算，无法安全返回消息："
                f"{message_id}"
            )
        raw_limit = max(1, raw_limit // 2)


def batch_manifest(
    parts: list[dict[str, object]],
    index: int,
    output_bytes: int,
) -> dict[str, object]:
    """把有序消息分片转换为不含正文的 run 批次记录。"""

    references: list[dict[str, object]] = []
    reference_map: dict[tuple[str, str], dict[str, object]] = {}
    for part in parts:
        key = (str(part["source_sha256"]), str(part["conversation_id"]))
        reference = reference_map.get(key)
        if reference is None:
            reference = {
                "source_sha256": key[0],
                "conversation_id": key[1],
                "message_count": 0,
                "part_count": 0,
                "parts": [],
            }
            reference_map[key] = reference
            references.append(reference)
        reference_parts = reference["parts"]
        assert isinstance(reference_parts, list)
        reference_parts.append(public_part(part))
        reference["part_count"] = len(reference_parts)
        reference["message_count"] = len(
            {str(item["message_id"]) for item in reference_parts}
        )
    return {
        "index": index,
        "conversation_count": len(references),
        "message_count": len(
            {
                (
                    str(part["source_sha256"]),
                    str(part["conversation_id"]),
                    str(part["message_id"]),
                )
                for part in parts
            }
        ),
        "part_count": len(parts),
        "output_bytes": output_bytes,
        "conversations": references,
    }


def estimate_visible_input_bytes(view: dict[str, object]) -> int:
    """估算精简可见视图体积，只用于选择默认读取预算档位。"""

    total = 256
    sources = view.get("sources")
    if not isinstance(sources, list):
        raise RunError("可见消息视图 sources 必须是数组")
    for source_item in sources:
        if not isinstance(source_item, dict):
            raise RunError("可见消息来源必须是对象")
        conversations = source_item.get("conversations")
        if not isinstance(conversations, list):
            raise RunError("可见消息来源 conversations 必须是数组")
        for conversation in conversations:
            if not isinstance(conversation, dict):
                raise RunError("可见对话必须是对象")
            total += len(str(conversation.get("conversation_ref", "")).encode("utf-8"))
            total += len(str(conversation.get("conversation_id", "")).encode("utf-8"))
            total += len(str(conversation.get("title", "")).encode("utf-8")) + 128
            messages = conversation.get("messages")
            if not isinstance(messages, list):
                raise RunError("可见对话 messages 必须是数组")
            for message in messages:
                if not isinstance(message, dict):
                    raise RunError("可见消息必须是对象")
                total += len(str(message.get("message_id", "")).encode("utf-8"))
                total += len(str(message.get("role", "")).encode("utf-8"))
                total += len(str(message.get("text", "")).encode("utf-8")) + 96
    return total


def choose_read_output_budget(view: dict[str, object]) -> int:
    """按输入规模选择兼顾一次读取和单次输出安全的默认预算。"""

    estimated_bytes = estimate_visible_input_bytes(view)
    if estimated_bytes <= SMALL_INPUT_MAX_BYTES:
        return SMALL_INPUT_OUTPUT_BUDGET_BYTES
    if estimated_bytes <= MEDIUM_INPUT_MAX_BYTES:
        return MEDIUM_INPUT_OUTPUT_BUDGET_BYTES
    return LARGE_INPUT_OUTPUT_BUDGET_BYTES


def build_batches(
    view: dict[str, object],
    max_output_bytes: int | None = None,
) -> list[dict[str, object]]:
    """按实际序列化输出预算分批，并为超长单条消息规划虚拟分片。"""

    if max_output_bytes is None:
        max_output_bytes = choose_read_output_budget(view)
    if type(max_output_bytes) is not int or max_output_bytes < 1:
        raise RunError("read-input 输出字节预算必须是正整数")
    sources = view.get("sources")
    if not isinstance(sources, list):
        raise RunError("可见消息视图 sources 必须是数组")
    conversation_groups: list[
        tuple[list[dict[str, object]], list[list[dict[str, object]]]]
    ] = []
    for source_item in sources:
        if not isinstance(source_item, dict) or not isinstance(
            source_item.get("conversations"), list
        ):
            raise RunError("可见消息来源结构无效")
        for conversation in source_item["conversations"]:
            if not isinstance(conversation, dict):
                raise RunError("可见对话必须是对象")
            source_sha256 = str(conversation.get("source_sha256", ""))
            conversation_id = str(conversation.get("conversation_id", ""))
            messages = conversation.get("messages")
            if not source_sha256 or not conversation_id or not isinstance(
                messages, list
            ) or not all(isinstance(message, dict) for message in messages):
                raise RunError("可见对话来源、ID 或 messages 无效")
            parts_by_message: dict[str, list[dict[str, object]]] = {}
            for message in messages:
                message_id = str(message.get("message_id", ""))
                parts_by_message[message_id] = split_message_parts(
                    view,
                    source_sha256,
                    conversation_id,
                    message,
                    max_output_bytes,
                )
            unit_groups = [
                [
                    part
                    for message in unit
                    for part in parts_by_message[str(message["message_id"])]
                ]
                for unit in message_units(list(messages))
            ]
            conversation_groups.append(
                ([part for group in unit_groups for part in group], unit_groups)
            )

    conservative_index = 999_999_999_999

    def fits(parts: list[dict[str, object]]) -> bool:
        """用保守批次位数判断候选输出是否落在预算内。"""

        return (
            render_batch_view(
                view,
                parts,
                conservative_index,
                conservative_index,
            )["output_bytes"]
            <= max_output_bytes
        )

    part_batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []

    def flush() -> None:
        """保存当前批次并清空缓冲。"""

        nonlocal current
        if current:
            part_batches.append(current)
            current = []

    def pack_group(group: list[dict[str, object]]) -> None:
        """尽量完整保留消息单元，必要时才退到逐分片装箱。"""

        nonlocal current
        if fits([*current, *group]):
            current.extend(group)
            return
        flush()
        if fits(group):
            current.extend(group)
            return
        for part in group:
            if fits([*current, part]):
                current.append(part)
                continue
            flush()
            if not fits([part]):
                raise RunError("单个消息分片仍超过 read-input 输出字节预算")
            current.append(part)

    for conversation_parts, unit_groups in conversation_groups:
        if fits([*current, *conversation_parts]):
            current.extend(conversation_parts)
            continue
        flush()
        if fits(conversation_parts):
            current.extend(conversation_parts)
            continue
        for group in unit_groups:
            pack_group(group)
    flush()

    batch_count = len(part_batches)
    batches: list[dict[str, object]] = []
    for index, parts in enumerate(part_batches, start=1):
        output = render_batch_view(view, parts, index, batch_count)
        output_bytes = int(output["output_bytes"])
        if output_bytes > max_output_bytes:
            raise RunError("read-input 批次输出超过字节预算")
        batches.append(batch_manifest(parts, index, output_bytes))
    return batches


def available_source_index(
    conversations: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """按精简对话引用构造来源索引，供审核时恢复完整来源。"""

    index: dict[str, dict[str, object]] = {}
    for conversation in conversations:
        conversation_ref = str(conversation.get("conversation_ref", ""))
        if not conversation_ref or conversation_ref in index:
            raise RunError("可见对话 conversation_ref 缺失或重复")
        messages = conversation.get("messages", [])
        index[conversation_ref] = {
            "source_sha256": str(conversation["source_sha256"]),
            "conversation_id": str(conversation["conversation_id"]),
            "message_ids": {
                str(message["message_id"])
                for message in messages
                if isinstance(message, dict) and "message_id" in message
            },
        }
    return index


def parse_review_input(
    raw_text: str,
    proposed_targets: set[str],
    conversations: list[dict[str, object]],
) -> tuple[dict[str, list[dict[str, object]]], list[dict[str, object]]]:
    """严格校验标准输入中的文档级来源和风险元数据。"""

    raw_text = raw_text.removeprefix("\ufeff")
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
        seen_sources: set[str] = set()
        for source_index, source in enumerate(sources):
            source_context = f"{context}.sources[{source_index}]"
            if not isinstance(source, dict):
                raise RunError(f"{source_context} 必须是对象")
            require_exact_fields(
                source,
                {"conversation_ref", "message_ids"},
                f"{source_context} ",
            )
            conversation_ref = source["conversation_ref"]
            message_ids = source["message_ids"]
            if (
                not isinstance(conversation_ref, str)
                or not conversation_ref
                or not isinstance(message_ids, list)
                or not message_ids
                or not all(
                    isinstance(message_id, str) and message_id
                    for message_id in message_ids
                )
            ):
                raise RunError(f"{source_context} 字段类型或取值无效")
            if conversation_ref in seen_sources:
                raise RunError(f"{source_context} 重复登记同一对话")
            if conversation_ref not in available:
                raise RunError(f"{source_context} 引用了未知 conversation_ref")
            if len(set(message_ids)) != len(message_ids):
                raise RunError(f"{source_context}.message_ids 不得重复")
            available_source = available[conversation_ref]
            available_messages = available_source["message_ids"]
            assert isinstance(available_messages, set)
            unknown_messages = sorted(set(message_ids) - available_messages)
            if unknown_messages:
                raise RunError(
                    f"{source_context} 引用了未知可见消息：{unknown_messages}"
                )
            seen_sources.add(conversation_ref)
            normalized_sources.append(
                {
                    "source_sha256": available_source["source_sha256"],
                    "conversation_id": available_source["conversation_id"],
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


def read_standard_input() -> str:
    """读取审核 JSON，兼容 UTF-8 BOM 和带 BOM 的 UTF-16 管道输入。"""

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    raw = buffer.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise RunError("审核元数据标准输入必须使用 UTF-8 或带 BOM 的 UTF-16") from exc


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
    view, metadata = collect_visible_view(source, branch)
    inventory, snapshot_sha = inventory_snapshot(root)
    read_output_budget = choose_read_output_budget(view)
    batches = build_batches(view, read_output_budget)
    if not batches:
        raise RunError("输入中没有可用于知识整理的可见 user/assistant 消息")
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
            "sources": metadata["sources"],
        },
        "parsing": {
            "conversation_count": metadata["conversation_count"],
            "message_count": metadata["message_count"],
            "warnings": metadata["warnings"],
            "read_output_budget_bytes": read_output_budget,
            "batches": batches,
        },
        "knowledge_base": {
            "empty": not bool(inventory["files"]),
            "snapshot_sha256": snapshot_sha,
            "document_count": len(inventory["files"]),
            "domains": inventory_domains(inventory),
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
    """严格校验 run.v6 顶层和关键子结构。"""

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
        raise RunError("仅支持 conversation-knowledge-curator.run.v6")
    validate_run_id(document["run_id"])
    if (
        not isinstance(document["status"], str)
        or document["status"] not in {"initialized", "review_ready", "applied"}
    ):
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
        input_value,
        {"path", "sha256", "type", "branch_policy", "sources"},
        "run.json.input ",
    )
    require_exact_fields(
        parsing,
        {
            "conversation_count",
            "message_count",
            "warnings",
            "read_output_budget_bytes",
            "batches",
        },
        "run.json.parsing ",
    )
    require_exact_fields(
        knowledge_base,
        {"empty", "snapshot_sha256", "document_count", "domains"},
        "run.json.knowledge_base ",
    )
    require_exact_fields(apply_value, {"status", "entries"}, "run.json.apply ")
    if (
        not isinstance(input_value["path"], str)
        or not input_value["path"]
        or not isinstance(input_value["sha256"], str)
        or not SHA256_PATTERN.fullmatch(input_value["sha256"])
        or not isinstance(input_value["type"], str)
        or input_value["type"] not in {"json_file", "zip_archive", "directory"}
        or not isinstance(input_value["branch_policy"], str)
        or input_value["branch_policy"] not in {"active", "all"}
        or not isinstance(input_value["sources"], list)
        or not input_value["sources"]
    ):
        raise RunError("run.json.input 字段类型或取值无效")
    source_identities: set[tuple[str, str, str | None]] = set()
    for index, source_record in enumerate(input_value["sources"]):
        context = f"run.json.input.sources[{index}]"
        if not isinstance(source_record, dict):
            raise RunError(f"{context} 必须是对象")
        require_exact_fields(
            source_record,
            {"kind", "path", "member_path", "sha256", "size_bytes"},
            f"{context} ",
        )
        kind = source_record["kind"]
        member_path = source_record["member_path"]
        if (
            not isinstance(kind, str)
            or kind not in {"json_file", "zip_member"}
            or not isinstance(source_record["path"], str)
            or not source_record["path"]
            or not Path(source_record["path"]).is_absolute()
            or not isinstance(source_record["sha256"], str)
            or not SHA256_PATTERN.fullmatch(source_record["sha256"])
            or type(source_record["size_bytes"]) is not int
            or source_record["size_bytes"] < 0
            or (kind == "json_file" and member_path is not None)
            or (
                kind == "zip_member"
                and (not isinstance(member_path, str) or not member_path)
            )
        ):
            raise RunError(f"{context} 字段类型或取值无效")
        identity = (
            str(kind), str(source_record["path"]),
            str(member_path) if member_path is not None else None,
        )
        if identity in source_identities:
            raise RunError(f"{context} 来源身份重复")
        source_identities.add(identity)
    if input_value["sha256"] != aggregate_source_sha256(input_value["sources"]):
        raise RunError("run.json.input.sha256 与来源清单不一致")
    if (
        type(parsing["conversation_count"]) is not int
        or parsing["conversation_count"] < 1
        or type(parsing["message_count"]) is not int
        or parsing["message_count"] < 1
        or not isinstance(parsing["warnings"], list)
        or parsing["read_output_budget_bytes"]
        not in {
            SMALL_INPUT_OUTPUT_BUDGET_BYTES,
            MEDIUM_INPUT_OUTPUT_BUDGET_BYTES,
            LARGE_INPUT_OUTPUT_BUDGET_BYTES,
        }
        or not isinstance(parsing["batches"], list)
        or not parsing["batches"]
    ):
        raise RunError("run.json.parsing 字段类型或取值无效")
    seen_conversations: set[tuple[str, str]] = set()
    message_parts: dict[
        tuple[str, str, str], list[dict[str, object]]
    ] = {}
    for batch_index, batch in enumerate(parsing["batches"]):
        context = f"run.json.parsing.batches[{batch_index}]"
        if not isinstance(batch, dict):
            raise RunError(f"{context} 必须是对象")
        require_exact_fields(
            batch,
            {
                "index",
                "conversation_count",
                "message_count",
                "part_count",
                "output_bytes",
                "conversations",
            },
            f"{context} ",
        )
        if (
            not all(
                type(batch[name]) is int and batch[name] >= 0
                for name in (
                    "index",
                    "conversation_count",
                    "message_count",
                    "part_count",
                    "output_bytes",
                )
            )
            or not isinstance(batch["conversations"], list)
            or batch["index"] != batch_index + 1
            or not 1
            <= batch["output_bytes"]
            <= parsing["read_output_budget_bytes"]
        ):
            raise RunError(f"{context} 字段类型或取值无效")
        batch_conversations: set[tuple[str, str]] = set()
        batch_messages: set[tuple[str, str, str]] = set()
        batch_part_count = 0
        for ref_index, ref in enumerate(batch["conversations"]):
            ref_context = f"{context}.conversations[{ref_index}]"
            if not isinstance(ref, dict):
                raise RunError(f"{ref_context} 必须是对象")
            require_exact_fields(
                ref,
                {
                    "source_sha256",
                    "conversation_id",
                    "message_count",
                    "part_count",
                    "parts",
                },
                f"{ref_context} ",
            )
            if (
                not isinstance(ref["source_sha256"], str)
                or not SHA256_PATTERN.fullmatch(ref["source_sha256"])
                or not isinstance(ref["conversation_id"], str)
                or not ref["conversation_id"]
                or type(ref["message_count"]) is not int
                or ref["message_count"] < 1
                or type(ref["part_count"]) is not int
                or ref["part_count"] < 1
                or not isinstance(ref["parts"], list)
                or not ref["parts"]
            ):
                raise RunError(f"{ref_context} 字段类型或取值无效")
            conversation_key = (
                str(ref["source_sha256"]),
                str(ref["conversation_id"]),
            )
            if conversation_key in batch_conversations:
                raise RunError(f"{ref_context} 重复引用同一对话")
            batch_conversations.add(conversation_key)
            seen_conversations.add(conversation_key)
            ref_messages: set[tuple[str, str, str]] = set()
            for part_index, part in enumerate(ref["parts"]):
                part_context = f"{ref_context}.parts[{part_index}]"
                if not isinstance(part, dict):
                    raise RunError(f"{part_context} 必须是对象")
                require_exact_fields(
                    part,
                    {
                        "message_id",
                        "part_index",
                        "part_count",
                        "start_byte",
                        "end_byte",
                        "message_sha256",
                    },
                    f"{part_context} ",
                )
                if (
                    not isinstance(part["message_id"], str)
                    or not part["message_id"]
                    or type(part["part_index"]) is not int
                    or type(part["part_count"]) is not int
                    or not 1 <= part["part_index"] <= part["part_count"]
                    or type(part["start_byte"]) is not int
                    or type(part["end_byte"]) is not int
                    or not 0 <= part["start_byte"] < part["end_byte"]
                    or not isinstance(part["message_sha256"], str)
                    or not SHA256_PATTERN.fullmatch(part["message_sha256"])
                ):
                    raise RunError(f"{part_context} 字段类型或取值无效")
                message_key = (*conversation_key, str(part["message_id"]))
                message_parts.setdefault(message_key, []).append(part)
                ref_messages.add(message_key)
                batch_messages.add(message_key)
                batch_part_count += 1
            if (
                ref["message_count"] != len(ref_messages)
                or ref["part_count"] != len(ref["parts"])
            ):
                raise RunError(f"{ref_context} 汇总计数与消息分片不一致")
        if (
            batch["conversation_count"] != len(batch_conversations)
            or batch["message_count"] != len(batch_messages)
            or batch["part_count"] != batch_part_count
        ):
            raise RunError(f"{context} 汇总计数与消息分片不一致")
    for message_key, parts in message_parts.items():
        expected_count = int(parts[0]["part_count"])
        expected_hash = str(parts[0]["message_sha256"])
        if (
            len(parts) != expected_count
            or [int(part["part_index"]) for part in parts]
            != list(range(1, expected_count + 1))
            or any(
                part["part_count"] != expected_count
                or part["message_sha256"] != expected_hash
                for part in parts
            )
            or int(parts[0]["start_byte"]) != 0
            or any(
                int(previous["end_byte"]) != int(current["start_byte"])
                for previous, current in zip(parts, parts[1:], strict=False)
            )
        ):
            raise RunError(
                "run.json 消息分片缺失、重复、乱序或不连续："
                f"{message_key[2]}"
            )
    if (
        parsing["conversation_count"] != len(seen_conversations)
        or parsing["message_count"] != len(message_parts)
    ):
        raise RunError("run.json.parsing 汇总计数与消息分片不一致")
    if (
        not isinstance(knowledge_base["empty"], bool)
        or not isinstance(knowledge_base["snapshot_sha256"], str)
        or not SHA256_PATTERN.fullmatch(knowledge_base["snapshot_sha256"])
        or type(knowledge_base["document_count"]) is not int
        or knowledge_base["document_count"] < 0
        or not isinstance(knowledge_base["domains"], list)
        or not all(
            isinstance(domain, str) and domain
            for domain in knowledge_base["domains"]
        )
        or knowledge_base["empty"] != (knowledge_base["document_count"] == 0)
        or len(set(knowledge_base["domains"])) != len(knowledge_base["domains"])
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
    if (
        metadata["input_sha256"] != input_value.get("sha256")
        or metadata["sources"] != input_value.get("sources")
    ):
        raise RunError("原始输入内容已变化，请创建新的 run")
    parsing = document.get("parsing")
    if not isinstance(parsing, dict):
        raise RunError("run.json.parsing 必须是对象")
    planned_batches = build_batches(
        view,
        int(parsing["read_output_budget_bytes"]),
    )
    if planned_batches != parsing.get("batches"):
        raise RunError("run.json 批次或消息分片计划与原始输入不一致")
    return view, metadata


def select_batch(
    view: dict[str, object], document: dict[str, object], batch_index: int | None
) -> dict[str, object]:
    """按分片引用返回预算内批次；多批输入禁止一次读取全部内容。"""

    parsing = document["parsing"]
    assert isinstance(parsing, dict)
    batches = parsing.get("batches")
    if not isinstance(batches, list) or not batches:
        raise RunError("run.json.parsing.batches 必须是非空数组")
    if batch_index is None:
        if len(batches) > 1:
            raise RunError("输入包含多个批次，必须使用 read-input --batch N 逐批读取")
        batch_index = 1
    if not isinstance(batches, list) or not 1 <= batch_index <= len(batches):
        raise RunError("batch 超出可用范围")
    batch = batches[batch_index - 1]
    assert isinstance(batch, dict)
    result = render_batch_view(
        view,
        flatten_batch_parts(batch),
        batch_index,
        len(batches),
    )
    if result["output_bytes"] != batch.get("output_bytes"):
        raise RunError("read-input 实际输出大小与 run.json 批次记录不一致")
    budget = parsing.get("read_output_budget_bytes")
    if type(budget) is not int or int(result["output_bytes"]) > budget:
        raise RunError("read-input 实际输出超过字节预算")
    return result


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


def diff_summary(before: bytes, after: bytes, target_path: str) -> dict[str, object]:
    """返回更新文档的确定性轻量差异摘要，不重复输出完整正文。"""

    import difflib

    before_text = decode_markdown(before, target_path)
    after_text = decode_markdown(after, target_path)
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    matcher = difflib.SequenceMatcher(
        None,
        before_lines,
        after_lines,
        autojunk=False,
    )
    added_line_count = 0
    removed_line_count = 0
    for (
        operation,
        before_start,
        before_end,
        after_start,
        after_end,
    ) in matcher.get_opcodes():
        if operation in {"replace", "delete"}:
            removed_line_count += before_end - before_start
        if operation in {"replace", "insert"}:
            added_line_count += after_end - after_start

    before_headings = [
        (heading.level, heading.text)
        for heading in knowledge_inventory.parse_headings(before_text)
    ]
    after_headings = [
        (heading.level, heading.text)
        for heading in knowledge_inventory.parse_headings(after_text)
    ]

    def heading_difference(
        primary: list[tuple[int, str]],
        secondary: list[tuple[int, str]],
    ) -> list[dict[str, object]]:
        """按出现次数和原顺序计算单向标题差集。"""

        remaining = list(secondary)
        difference: list[dict[str, object]] = []
        for level, text in primary:
            try:
                remaining.remove((level, text))
            except ValueError:
                difference.append({"level": level, "text": text})
        return difference

    before_sections = {
        (fragment.heading_path, fragment.ordinal): fragment.markdown
        for fragment in knowledge_inventory.parse_section_fragments(
            before_text,
            target_path,
        )
    }
    after_sections = {
        (fragment.heading_path, fragment.ordinal): fragment.markdown
        for fragment in knowledge_inventory.parse_section_fragments(
            after_text,
            target_path,
        )
    }
    changed_sections = [
        {
            "heading_path": list(heading_path),
            "ordinal": ordinal,
        }
        for (heading_path, ordinal), markdown in after_sections.items()
        if (heading_path, ordinal) in before_sections
        and before_sections[(heading_path, ordinal)] != markdown
    ]
    return {
        "before_line_count": len(before_lines),
        "after_line_count": len(after_lines),
        "added_line_count": added_line_count,
        "removed_line_count": removed_line_count,
        "added_headings": heading_difference(after_headings, before_headings),
        "removed_headings": heading_difference(before_headings, after_headings),
        "changed_sections": changed_sections,
    }


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
        include_diff = full_diff
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
        if target.exists():
            review_item["diff_summary"] = diff_summary(
                before,
                proposed,
                target_path,
            )
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
    ensure_snapshot(knowledge_root, document)
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
        apply_value = document.get("apply")
        if isinstance(apply_value, dict) and apply_value.get("status") == "recovered":
            return {"status": "already_recovered", "restored_file_count": 0}
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
    """执行一个 run.v6 命令并返回结构化结果。"""

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
            "knowledge_base_document_count": knowledge_base["document_count"],
            "knowledge_base_domains": knowledge_base["domains"],
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
            read_standard_input(),
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
        description="以单一 run.v6 流程整理 ChatGPT 对话知识。"
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
