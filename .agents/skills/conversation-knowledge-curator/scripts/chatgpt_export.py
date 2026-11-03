#!/usr/bin/env python3
"""检查并标准化 ChatGPT 对话导出 JSON。

本模块只使用 Python 标准库。它不会修改输入文件，只会新建标准化输出；
如果目标文件已经存在，则视为错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


FORMAT_VERSION = "conversation-knowledge-curator.normalized.v3"
INSPECTION_FORMAT_VERSION = "conversation-knowledge-curator.inspection.v1"
MANIFEST_FORMAT_VERSION = "conversation-knowledge-curator.manifest.v1"
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


@dataclass(frozen=True)
class InspectionLimits:
    """限制检查阶段允许读取和解压的数据规模。"""

    max_json_bytes: int = 1024 * 1024 * 1024
    max_archive_json_bytes: int = 4 * 1024 * 1024 * 1024
    max_archive_members: int = 10_000
    max_compression_ratio: float = 200.0


@dataclass(frozen=True)
class InputSource:
    """一个可独立检查的 JSON 来源，可以来自普通文件或 ZIP 成员。"""

    kind: str
    path: Path
    size_bytes: int
    member_path: str | None = None
    archive_index: int | None = None
    compressed_size_bytes: int | None = None


@dataclass(frozen=True)
class ResolvedInput:
    """输入路径解析结果以及发现阶段的统计和警告。"""

    input_path: Path
    input_type: str
    sources: tuple[InputSource, ...]
    archives: tuple[dict[str, Any], ...]
    json_file_count: int
    zip_file_count: int
    other_file_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class NormalizationResult:
    """分离精简内容、完整来源信息和标准化诊断。"""

    document: dict[str, Any]
    source: dict[str, Any]
    diagnostics: dict[str, Any]


DEFAULT_INSPECTION_LIMITS = InspectionLimits()


class ExportError(ValueError):
    """输入 JSON 不符合受支持的导出容器时抛出。"""


class InputSafetyError(ExportError):
    """输入超过安全限制或包含不安全路径时抛出。"""


def archive_member_is_safe(member_name: str) -> bool:
    """判断 ZIP 成员名是否为安全的相对路径。"""

    normalized = member_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(member_name)
    return (
        bool(normalized)
        and not posix_path.is_absolute()
        and not windows_path.is_absolute()
        and not windows_path.drive
        and ".." not in posix_path.parts
    )


def compression_ratio(uncompressed_size: int, compressed_size: int) -> float:
    """计算压缩比；非空内容的压缩大小为零时返回无穷大。"""

    if uncompressed_size == 0:
        return 0.0
    if compressed_size == 0:
        return float("inf")
    return uncompressed_size / compressed_size


def discover_archive_sources(
    path: Path, limits: InspectionLimits
) -> tuple[list[InputSource], dict[str, Any], list[str]]:
    """只读枚举 ZIP 中可安全检查的 JSON 成员。"""

    sources: list[InputSource] = []
    warnings: list[str] = []
    report: dict[str, Any] = {
        "path": str(path),
        "status": "ok",
        "member_count": 0,
        "json_member_count": 0,
        "candidate_json_count": 0,
        "ignored_member_count": 0,
        "skipped_json_count": 0,
        "json_uncompressed_bytes": 0,
        "warnings": [],
    }

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        message = f"ZIP 无法读取：{exc}"
        report.update(status="invalid", error=message)
        warnings.append(f"{path}：{message}")
        return sources, report, warnings

    report["member_count"] = len(infos)
    if len(infos) > limits.max_archive_members:
        message = (
            f"ZIP 成员数量 {len(infos)} 超过限制 "
            f"{limits.max_archive_members}"
        )
        report.update(status="rejected", error=message)
        warnings.append(f"{path}：{message}")
        return sources, report, warnings

    json_total_bytes = 0
    for index, info in enumerate(infos):
        if info.is_dir():
            continue
        if Path(info.filename).suffix.lower() != ".json":
            report["ignored_member_count"] += 1
            continue

        report["json_member_count"] += 1
        if not archive_member_is_safe(info.filename):
            message = f"跳过不安全的 ZIP 成员路径：{info.filename!r}"
            report["skipped_json_count"] += 1
            warnings.append(f"{path}：{message}")
            continue
        if info.file_size > limits.max_json_bytes:
            message = (
                f"跳过过大的 JSON 成员 {info.filename!r}："
                f"{info.file_size} 字节，限制为 {limits.max_json_bytes} 字节"
            )
            report["skipped_json_count"] += 1
            warnings.append(f"{path}：{message}")
            continue
        ratio = compression_ratio(info.file_size, info.compress_size)
        if ratio > limits.max_compression_ratio:
            message = (
                f"跳过压缩比异常的 JSON 成员 {info.filename!r}："
                f"{ratio:.1f}，限制为 {limits.max_compression_ratio:.1f}"
            )
            report["skipped_json_count"] += 1
            warnings.append(f"{path}：{message}")
            continue

        json_total_bytes += info.file_size
        sources.append(
            InputSource(
                kind="zip_member",
                path=path,
                size_bytes=info.file_size,
                member_path=info.filename,
                archive_index=index,
                compressed_size_bytes=info.compress_size,
            )
        )

    report["json_uncompressed_bytes"] = json_total_bytes
    if json_total_bytes > limits.max_archive_json_bytes:
        message = (
            f"ZIP 中候选 JSON 的总解压大小 {json_total_bytes} 字节超过限制 "
            f"{limits.max_archive_json_bytes} 字节"
        )
        report.update(status="rejected", error=message)
        warnings.append(f"{path}：{message}")
        sources.clear()
    elif report["json_member_count"] == 0:
        message = "ZIP 中未发现 JSON 文件"
        warnings.append(f"{path}：{message}")
    elif not sources:
        report["status"] = "rejected"
        report["error"] = "ZIP 中没有通过安全检查的 JSON 成员"

    report["candidate_json_count"] = len(sources)
    report["warnings"] = [
        warning.removeprefix(f"{path}：")
        for warning in warnings
        if warning.startswith(f"{path}：")
    ]
    return sources, report, warnings


def resolve_input(
    input_path: Path, limits: InspectionLimits = DEFAULT_INSPECTION_LIMITS
) -> ResolvedInput:
    """把文件、目录或 ZIP 输入解析为确定顺序的 JSON 来源。"""

    path = input_path.expanduser().resolve()
    if not path.exists():
        raise ExportError(f"输入路径不存在：{path}")

    sources: list[InputSource] = []
    archives: list[dict[str, Any]] = []
    warnings: list[str] = []
    json_file_count = 0
    zip_file_count = 0
    other_file_count = 0

    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == ".json":
            sources.append(
                InputSource(
                    kind="json_file",
                    path=path,
                    size_bytes=path.stat().st_size,
                )
            )
            json_file_count = 1
            input_type = "json_file"
        elif suffix == ".zip":
            archive_sources, archive_report, archive_warnings = (
                discover_archive_sources(path, limits)
            )
            sources.extend(archive_sources)
            archives.append(archive_report)
            warnings.extend(archive_warnings)
            zip_file_count = 1
            input_type = "zip_archive"
        else:
            raise ExportError(
                f"不支持的输入文件类型 {suffix or '<无扩展名>'}；"
                "只支持 .json 和 .zip"
            )
    elif path.is_dir():
        input_type = "directory"
        try:
            entries = sorted(
                path.rglob("*"),
                key=lambda entry: entry.relative_to(path).as_posix().casefold(),
            )
        except OSError as exc:
            raise ExportError(f"无法扫描输入目录 {path}：{exc}") from exc

        for entry in entries:
            if entry.is_symlink():
                warnings.append(f"跳过符号链接：{entry}")
                continue
            try:
                if not entry.is_file():
                    continue
                suffix = entry.suffix.lower()
                if suffix == ".json":
                    sources.append(
                        InputSource(
                            kind="json_file",
                            path=entry,
                            size_bytes=entry.stat().st_size,
                        )
                    )
                    json_file_count += 1
                elif suffix == ".zip":
                    archive_sources, archive_report, archive_warnings = (
                        discover_archive_sources(entry, limits)
                    )
                    sources.extend(archive_sources)
                    archives.append(archive_report)
                    warnings.extend(archive_warnings)
                    zip_file_count += 1
                else:
                    other_file_count += 1
            except OSError as exc:
                warnings.append(f"无法检查文件 {entry}：{exc}")
    else:
        raise ExportError(f"输入路径既不是普通文件也不是目录：{path}")

    return ResolvedInput(
        input_path=path,
        input_type=input_type,
        sources=tuple(sources),
        archives=tuple(archives),
        json_file_count=json_file_count,
        zip_file_count=zip_file_count,
        other_file_count=other_file_count,
        warnings=tuple(warnings),
    )


def read_input_source(
    source: InputSource, limits: InspectionLimits = DEFAULT_INSPECTION_LIMITS
) -> bytes:
    """读取一个已发现的 JSON 来源，不把 ZIP 成员解压到磁盘。"""

    if source.size_bytes > limits.max_json_bytes:
        raise InputSafetyError(
            f"JSON 大小 {source.size_bytes} 字节超过限制 "
            f"{limits.max_json_bytes} 字节"
        )

    if source.kind == "json_file":
        payload = source.path.read_bytes()
    elif source.kind == "zip_member":
        if source.archive_index is None or source.member_path is None:
            raise ExportError("ZIP 来源缺少成员索引或成员路径")
        try:
            with zipfile.ZipFile(source.path) as archive:
                infos = archive.infolist()
                if source.archive_index >= len(infos):
                    raise ExportError("ZIP 内容已变化，成员索引不再有效")
                info = infos[source.archive_index]
                if info.filename != source.member_path:
                    raise ExportError("ZIP 内容已变化，成员路径不再匹配")
                with archive.open(info) as handle:
                    payload = handle.read(limits.max_json_bytes + 1)
        except zipfile.BadZipFile as exc:
            raise ExportError(f"ZIP 无法读取：{exc}") from exc
    else:
        raise ExportError(f"未知输入来源类型：{source.kind}")

    if len(payload) > limits.max_json_bytes:
        raise InputSafetyError(
            f"JSON 实际读取大小超过限制 {limits.max_json_bytes} 字节"
        )
    if len(payload) != source.size_bytes:
        raise ExportError("输入在发现与读取之间发生变化，请重新执行 inspect")
    return payload


def source_descriptor(
    source: InputSource, content_sha256: str | None = None
) -> dict[str, Any]:
    """生成不包含内容正文的来源描述。"""

    descriptor: dict[str, Any] = {
        "kind": source.kind,
        "size_bytes": source.size_bytes,
    }
    if source.kind == "json_file":
        descriptor["path"] = str(source.path)
    else:
        descriptor.update(
            archive_path=str(source.path),
            member_path=source.member_path,
            compressed_size_bytes=source.compressed_size_bytes,
        )
    if content_sha256 is not None:
        descriptor["sha256"] = content_sha256
    return descriptor


def sha256_bytes(payload: bytes) -> str:
    """计算内存数据的 SHA-256。"""

    return hashlib.sha256(payload).hexdigest()


def json_shape(data: Any) -> dict[str, Any]:
    """描述 JSON 顶层结构，不包含任何值或消息正文。"""

    if isinstance(data, dict):
        return {
            "top_level_type": "object",
            "top_level_keys": sorted(str(key) for key in data),
            "top_level_size": len(data),
        }
    if isinstance(data, list):
        return {"top_level_type": "array", "top_level_size": len(data)}
    return {"top_level_type": type(data).__name__, "top_level_size": None}


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
        return [], "current_node 缺失或无效；已跳过当前对话"

    reversed_path: list[str] = []
    seen: set[str] = set()
    node_id: str | None = current
    while node_id is not None:
        if node_id in seen:
            return [], "活动分支的 parent 链存在循环；已跳过当前对话"
        if node_id not in mapping:
            return [], "活动分支的 parent 链引用未知节点；已跳过当前对话"
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
    if (
        role == "assistant"
        and recipient
        and recipient not in VISIBLE_ASSISTANT_RECIPIENTS
    ):
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
        pieces.extend(
            text
            for part in parts
            if (text := text_from_part(part)) is not None
        )
    elif isinstance(content.get("text"), str):
        pieces.append(content["text"])
    return "\n".join(piece for piece in pieces if piece).strip()


def selected_nodes(
    conversation: dict[str, Any], branch: str
) -> tuple[dict[str, Any], list[str], list[str], str | None]:
    """根据分支策略选择节点，并返回不会中断处理的结构警告。"""

    mapping = mapping_for(conversation)
    warnings: list[str] = []
    if not mapping:
        warnings.append("对话缺少 mapping 对象，或 mapping 为空")
        return mapping, [], warnings, "missing_mapping"
    if branch == "all":
        return mapping, stable_all_node_ids(mapping), warnings, None
    node_ids, warning = active_node_ids(conversation, mapping)
    if warning:
        warnings.append(warning)
        return mapping, node_ids, warnings, "active_branch_unavailable"
    return mapping, node_ids, warnings, None


def synthetic_conversation_id(
    source_sha256: str,
    source_index: int,
    nonce: int = 0,
) -> str:
    """根据来源、索引和冲突序号生成稳定的合成对话 ID。"""

    identity = f"{source_sha256}\0{source_index}\0{nonce}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    return f"synthetic-conversation-{suffix}"


def stable_conversation_id(
    conversation: dict[str, Any],
    source_sha256: str,
    source_index: int,
) -> str:
    """返回原始对话 ID，缺失时根据来源和索引生成稳定 ID。"""

    original_id = conversation.get("id") or conversation.get("conversation_id")
    if isinstance(original_id, str) and original_id.strip():
        return original_id
    if not source_sha256:
        raise ExportError("生成合成 conversation_id 时缺少来源 SHA-256")
    return synthetic_conversation_id(source_sha256, source_index)


def stable_message_id(
    message: dict[str, Any],
    node_id: str,
    source_sha256: str,
    source_index: int,
    used_ids: set[str],
) -> str:
    """保留有效消息 ID，并在缺失或重复时生成稳定替代值。"""

    original_id = message.get("id")
    if isinstance(original_id, str) and original_id.strip():
        candidate = original_id
    else:
        candidate = node_id
    if candidate and candidate not in used_ids:
        return candidate
    identity = f"{source_sha256}\0{source_index}\0{node_id}".encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    return f"synthetic-message-{suffix}"


def normalize_conversation(
    conversation: dict[str, Any],
    branch: str,
    source_index: int = 0,
    source_sha256: str = "",
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """把一个对话标准化为用户可见消息列表。"""

    mapping, node_ids, warnings, skip_reason = selected_nodes(conversation, branch)
    if skip_reason is not None:
        return None, warnings, skip_reason

    messages: list[dict[str, Any]] = []
    used_message_ids: set[str] = set()
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
        message_id = stable_message_id(
            message,
            node_id,
            source_sha256,
            source_index,
            used_message_ids,
        )
        used_message_ids.add(message_id)
        messages.append(
            {
                "message_id": message_id,
                "role": lower_string(author.get("role")),
                "create_time": message.get("create_time"),
                "text": text,
            }
        )

    return (
        {
            "source_index": source_index,
            "conversation_id": stable_conversation_id(
                conversation,
                source_sha256,
                source_index,
            ),
            "title": (
                conversation.get("title")
                if isinstance(conversation.get("title"), str)
                else None
            ),
            "create_time": conversation.get("create_time"),
            "update_time": conversation.get("update_time"),
            "messages": messages,
        },
        warnings,
        None,
    )


def sha256_file(path: Path) -> str:
    """流式计算文件的 SHA-256，避免一次性载入大文件。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_loaded_export(
    data: Any,
    source: dict[str, Any],
    branch: str = "active",
) -> NormalizationResult:
    """标准化已加载且结构受支持的导出数据。"""

    source_sha256 = source.get("sha256")
    if not isinstance(source_sha256, str) or not source_sha256:
        raise ExportError("标准化来源缺少有效的 SHA-256")

    conversations, envelope = conversation_objects(data)
    normalized: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    used_conversation_ids: set[str] = set()
    for index, conversation in enumerate(conversations):
        item, item_warnings, skip_reason = normalize_conversation(
            conversation,
            branch,
            source_index=index,
            source_sha256=source_sha256,
        )
        if item is not None:
            conversation_id = item["conversation_id"]
            if conversation_id in used_conversation_ids:
                nonce = 0
                conversation_id = synthetic_conversation_id(
                    source_sha256, index, nonce
                )
                while conversation_id in used_conversation_ids:
                    nonce += 1
                    conversation_id = synthetic_conversation_id(
                        source_sha256, index, nonce
                    )
                item["conversation_id"] = conversation_id
                item_warnings.append(
                    "原始 conversation_id 重复；已生成稳定替代 ID"
                )
            used_conversation_ids.add(conversation_id)
            normalized.append(item)
        else:
            skipped.append(
                {
                    "source_index": index,
                    "conversation_id": (
                        conversation.get("id")
                        or conversation.get("conversation_id")
                    ),
                    "reason_code": skip_reason,
                    "message": item_warnings[0] if item_warnings else None,
                }
            )
        warnings.extend(
            {"conversation_index": index, "message": warning}
            for warning in item_warnings
        )
    source_with_container = {**source, "container": envelope}
    diagnostics = {
        "branch_policy": branch,
        "source_conversation_count": len(conversations),
        "normalized_conversation_count": len(normalized),
        "skipped_conversation_count": len(skipped),
        "message_count": sum(len(item["messages"]) for item in normalized),
        "skipped_conversations": skipped,
        "warnings": warnings,
    }
    document = {
        "format": FORMAT_VERSION,
        "source_sha256": source_sha256,
        "conversations": normalized,
    }
    return NormalizationResult(
        document=document,
        source=source_with_container,
        diagnostics=diagnostics,
    )


def normalize_export(path: Path, branch: str = "active") -> dict[str, Any]:
    """兼容旧调用方式，标准化单个普通 JSON 文件。"""

    resolved_path = path.expanduser().resolve()
    data = load_json(resolved_path)
    source = {
        "kind": "json_file",
        "path": str(resolved_path),
        "size_bytes": resolved_path.stat().st_size,
        "sha256": sha256_file(resolved_path),
    }
    inspect_loaded_export(data, source)
    return normalize_loaded_export(data, source, branch).document


def inspect_loaded_export(
    data: Any, source: dict[str, Any]
) -> dict[str, Any]:
    """检查已加载的导出数据，并生成不包含消息正文的结构报告。"""

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
        "source": source,
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


def inspect_export(path: Path) -> dict[str, Any]:
    """兼容旧调用方式，检查单个 JSON 文件。"""

    resolved_path = path.expanduser().resolve()
    data = load_json(resolved_path)
    source = {
        "kind": "json_file",
        "path": str(resolved_path),
        "size_bytes": resolved_path.stat().st_size,
        "sha256": sha256_file(resolved_path),
    }
    return inspect_loaded_export(data, source)


def inspect_source(
    source: InputSource,
    limits: InspectionLimits = DEFAULT_INSPECTION_LIMITS,
) -> dict[str, Any]:
    """检查单个 JSON 来源，并把可恢复错误转换为报告项。"""

    descriptor = source_descriptor(source)
    try:
        payload = read_input_source(source, limits)
    except InputSafetyError as exc:
        return {"status": "rejected", "source": descriptor, "error": str(exc)}
    except (ExportError, OSError, RuntimeError) as exc:
        return {"status": "read_error", "source": descriptor, "error": str(exc)}

    descriptor = source_descriptor(source, sha256_bytes(payload))
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "invalid_json", "source": descriptor, "error": str(exc)}

    shape = json_shape(data)
    try:
        report = inspect_loaded_export(data, descriptor)
    except ExportError as exc:
        return {
            "status": "unsupported",
            "source": descriptor,
            "json": shape,
            "error": str(exc),
        }
    return {"status": "supported", "json": shape, **report}


def inspect_input(
    input_path: Path,
    limits: InspectionLimits = DEFAULT_INSPECTION_LIMITS,
) -> dict[str, Any]:
    """统一检查 JSON 文件、ZIP 压缩包或包含这些文件的目录。"""

    resolved = resolve_input(input_path, limits)
    items = [inspect_source(source, limits) for source in resolved.sources]
    status_counts = Counter(item["status"] for item in items)
    archive_issue_count = sum(
        archive.get("status") != "ok" for archive in resolved.archives
    )
    warning_item_count = sum(bool(item.get("warnings")) for item in items)
    issue_count = (
        sum(count for status, count in status_counts.items() if status != "supported")
        + archive_issue_count
    )
    if not items:
        issue_count += 1

    return {
        "format": INSPECTION_FORMAT_VERSION,
        "status": "ok" if issue_count == 0 else "issues_found",
        "input": {
            "path": str(resolved.input_path),
            "type": resolved.input_type,
        },
        "discovery": {
            "json_file_count": resolved.json_file_count,
            "zip_file_count": resolved.zip_file_count,
            "other_file_count": resolved.other_file_count,
            "candidate_json_count": len(resolved.sources),
        },
        "archives": list(resolved.archives),
        "items": items,
        "summary": {
            "candidate_count": len(items),
            "supported_count": status_counts.get("supported", 0),
            "unsupported_count": status_counts.get("unsupported", 0),
            "invalid_json_count": status_counts.get("invalid_json", 0),
            "rejected_count": status_counts.get("rejected", 0),
            "read_error_count": status_counts.get("read_error", 0),
            "archive_issue_count": archive_issue_count,
            "warning_item_count": warning_item_count,
        },
        "warnings": list(resolved.warnings),
    }


def inspection_exit_code(report: dict[str, Any]) -> int:
    """根据检查结果返回适合自动化调用的退出码。"""

    return 0 if report.get("status") == "ok" else 1


def normalize_source(
    source: InputSource,
    branch: str = "active",
    limits: InspectionLimits = DEFAULT_INSPECTION_LIMITS,
    inspection: dict[str, Any] | None = None,
) -> NormalizationResult:
    """只标准化已经通过相同结构检查逻辑的单个输入来源。"""

    source_inspection = inspection or inspect_source(source, limits)
    if source_inspection.get("status") != "supported":
        raise ExportError(
            "输入来源未通过 inspect，不能标准化："
            f"{source_inspection.get('status', 'unknown')}"
        )

    payload = read_input_source(source, limits)
    descriptor = source_descriptor(source, sha256_bytes(payload))
    inspected_sha256 = source_inspection.get("source", {}).get("sha256")
    if inspected_sha256 != descriptor["sha256"]:
        raise ExportError("输入内容在 inspect 后发生变化，请重新执行检查")

    data = json.loads(payload.decode("utf-8-sig"))
    inspect_loaded_export(data, descriptor)
    return normalize_loaded_export(data, descriptor, branch)


def normalized_output_filename(source: InputSource) -> str:
    """根据来源身份生成稳定且适合 Windows 的唯一输出文件名。"""

    if source.kind == "zip_member":
        member = PurePosixPath((source.member_path or "source.json").replace("\\", "/"))
        label = f"{source.path.stem}__{'__'.join(member.with_suffix('').parts)}"
        identity = f"zip_member\0{source.path}\0{source.member_path}"
    else:
        label = source.path.stem
        identity = f"json_file\0{source.path}"

    safe_label = re.sub(r"[^\w.-]+", "_", label, flags=re.UNICODE).strip("._")
    safe_label = (safe_label or "source")[:96]
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"{safe_label}--{identity_hash}.normalized.json"


def normalize_input(
    input_path: Path,
    output_directory: Path,
    branch: str = "active",
    limits: InspectionLimits = DEFAULT_INSPECTION_LIMITS,
) -> dict[str, Any]:
    """批量标准化受支持来源，并在新目录中写入结果和 manifest。"""

    output_path = output_directory.expanduser().resolve()
    if output_path.exists():
        raise ExportError(f"输出目录已存在，拒绝覆盖或追加：{output_path}")

    resolved = resolve_input(input_path, limits)
    inspections = [inspect_source(source, limits) for source in resolved.sources]
    supported_count = sum(
        inspection.get("status") == "supported" for inspection in inspections
    )
    if supported_count == 0:
        raise ExportError("没有通过 inspect 的受支持 JSON 来源，不生成标准化输出")

    output_path.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    normalized_source_count = 0
    skipped_source_count = 0
    failed_source_count = 0
    normalized_conversation_count = 0
    skipped_conversation_count = 0
    message_count = 0
    normalization_warning_count = 0

    for source, inspection in zip(resolved.sources, inspections, strict=True):
        if inspection.get("status") != "supported":
            skipped_source_count += 1
            entries.append(
                {
                    "status": "skipped",
                    "source": inspection.get("source", source_descriptor(source)),
                    "inspection_status": inspection.get("status"),
                    "reason": inspection.get("error"),
                }
            )
            continue

        output_name = normalized_output_filename(source)
        try:
            result = normalize_source(source, branch, limits, inspection)
            write_new_json(output_path / output_name, result.document)
        except (
            ExportError,
            FileExistsError,
            json.JSONDecodeError,
            UnicodeError,
            OSError,
        ) as exc:
            failed_source_count += 1
            entries.append(
                {
                    "status": "failed",
                    "source": inspection["source"],
                    "reason": str(exc),
                }
            )
            continue

        normalization = result.diagnostics
        normalized_source_count += 1
        normalized_conversation_count += normalization[
            "normalized_conversation_count"
        ]
        skipped_conversation_count += normalization["skipped_conversation_count"]
        message_count += normalization["message_count"]
        normalization_warning_count += len(normalization["warnings"])
        entries.append(
            {
                "status": "normalized",
                "source": result.source,
                "output": output_name,
                "normalization": normalization,
            }
        )

    has_issues = any(
        (
            skipped_source_count,
            failed_source_count,
            skipped_conversation_count,
            normalization_warning_count,
            resolved.warnings,
            any(archive.get("status") != "ok" for archive in resolved.archives),
        )
    )
    manifest = {
        "format": MANIFEST_FORMAT_VERSION,
        "status": "completed_with_issues" if has_issues else "ok",
        "input": {
            "path": str(resolved.input_path),
            "type": resolved.input_type,
        },
        "output_directory": str(output_path),
        "branch_policy": branch,
        "discovery": {
            "json_file_count": resolved.json_file_count,
            "zip_file_count": resolved.zip_file_count,
            "other_file_count": resolved.other_file_count,
            "candidate_json_count": len(resolved.sources),
        },
        "archives": list(resolved.archives),
        "summary": {
            "candidate_source_count": len(resolved.sources),
            "normalized_source_count": normalized_source_count,
            "skipped_source_count": skipped_source_count,
            "failed_source_count": failed_source_count,
            "normalized_conversation_count": normalized_conversation_count,
            "skipped_conversation_count": skipped_conversation_count,
            "message_count": message_count,
            "normalization_warning_count": normalization_warning_count,
        },
        "entries": entries,
        "warnings": list(resolved.warnings),
    }
    write_new_json(output_path / "manifest.json", manifest)
    return manifest


def normalization_exit_code(manifest: dict[str, Any]) -> int:
    """根据批量标准化结果返回适合自动化调用的退出码。"""

    return 0 if manifest.get("status") == "ok" else 1


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
        "inspect", help="检查 JSON、ZIP 或目录并输出结构报告"
    )
    inspect_parser.add_argument(
        "input",
        type=Path,
        help="待检查的 JSON 文件、ZIP 压缩包或目录",
    )

    normalize_parser = subparsers.add_parser(
        "normalize", help="把受支持的 JSON、ZIP 或目录标准化到新目录"
    )
    normalize_parser.add_argument(
        "input", type=Path, help="待标准化的 JSON、ZIP 或目录"
    )
    normalize_parser.add_argument(
        "output", type=Path, help="必须尚不存在的新输出目录"
    )
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
            report = inspect_input(args.input)
            json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return inspection_exit_code(report)
        else:
            if args.input.resolve() == args.output.resolve():
                raise ExportError("输出路径必须与输入路径不同")
            manifest = normalize_input(args.input, args.output, args.branch)
            json.dump(manifest, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return normalization_exit_code(manifest)
    except (ExportError, FileExistsError, json.JSONDecodeError, OSError) as exc:
        parser.exit(2, f"错误：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

