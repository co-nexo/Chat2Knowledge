#!/usr/bin/env python3
"""校验语义提取草稿并生成知识候选与审计清单。

本模块只负责确定性处理，不判断知识是否有长期价值。输入中的对话文本和
候选陈述都按非可信数据处理，不会作为命令执行。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NORMALIZED_FORMAT = "conversation-knowledge-curator.normalized.v3"
NORMALIZATION_MANIFEST_FORMAT = (
    "conversation-knowledge-curator.manifest.v1"
)
DRAFT_FORMAT = "conversation-knowledge-curator.extraction-draft.v1"
CANDIDATES_FORMAT = "conversation-knowledge-curator.knowledge-candidates.v1"
EXTRACTION_MANIFEST_FORMAT = (
    "conversation-knowledge-curator.extraction-manifest.v1"
)
EXTRACTION_RULES_VERSION = "conversation-knowledge-curator.extraction-rules.v1"
CANDIDATES_FILENAME = "knowledge-candidates.json"
MANIFEST_FILENAME = "extraction-manifest.json"
ALLOWED_FLAGS = {
    "assistant_only",
    "sensitive",
    "time_sensitive",
    "uncertain",
}
FLAG_ORDER = (
    "assistant_only",
    "uncertain",
    "time_sensitive",
    "sensitive",
)
PROCESSING_STATUSES = {"completed", "failed", "skipped"}
REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CandidateError(ValueError):
    """输入契约、来源引用或输出条件不满足时抛出。"""


@dataclass(frozen=True)
class ConversationIndex:
    """一段标准化对话中可用于证据校验的确定性索引。"""

    source_sha256: str
    conversation_id: str
    message_roles: dict[str, str]
    message_positions: dict[str, int]


@dataclass(frozen=True)
class NormalizedBundle:
    """标准化输出目录的已校验索引和审计信息。"""

    input_directory: Path
    manifest_sha256: str
    conversations: dict[tuple[str, str], ConversationIndex]
    conversation_order: tuple[tuple[str, str], ...]
    normalized_files: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]


def load_json(path: Path) -> Any:
    """以 UTF-8 读取 JSON，不修改原文件。"""

    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def sha256_bytes(payload: bytes) -> str:
    """返回字节内容的 SHA-256。"""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_object(value: Any, location: str) -> dict[str, Any]:
    """校验值为 JSON 对象并返回。"""

    if not isinstance(value, dict):
        raise CandidateError(f"{location} 必须是 JSON 对象")
    return value


def require_list(value: Any, location: str) -> list[Any]:
    """校验值为 JSON 数组并返回。"""

    if not isinstance(value, list):
        raise CandidateError(f"{location} 必须是 JSON 数组")
    return value


def require_nonempty_string(value: Any, location: str) -> str:
    """校验并返回去除首尾空白后的非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise CandidateError(f"{location} 必须是非空字符串")
    return value.strip()


def require_sha256(value: Any, location: str) -> str:
    """校验小写十六进制 SHA-256。"""

    text = require_nonempty_string(value, location)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise CandidateError(f"{location} 必须是 64 位小写 SHA-256")
    return text


def require_reason_code(value: Any, location: str) -> str:
    """校验适合机器处理的原因码。"""

    text = require_nonempty_string(value, location)
    if REASON_CODE_PATTERN.fullmatch(text) is None:
        raise CandidateError(f"{location} 必须使用小写 snake_case")
    return text


def safe_output_path(root: Path, output_name: Any) -> Path:
    """把 manifest 中的输出名限制为目录直属文件。"""

    name = require_nonempty_string(output_name, "manifest entry.output")
    relative = Path(name)
    if relative.is_absolute() or relative.name != name:
        raise CandidateError("manifest entry.output 必须是直属文件名")
    path = (root / name).resolve()
    if path.parent != root:
        raise CandidateError("manifest entry.output 超出标准化目录")
    return path


def index_normalized_document(
    document: Any,
    expected_source_sha256: str,
    conversations: dict[tuple[str, str], ConversationIndex],
    conversation_order: list[tuple[str, str]],
) -> None:
    """校验一个 normalized.v3 文档并合并到证据索引。"""

    root = require_object(document, "normalized")
    if root.get("format") != NORMALIZED_FORMAT:
        raise CandidateError("normalized 文件格式不受支持")
    source_sha256 = require_sha256(
        root.get("source_sha256"), "normalized.source_sha256"
    )
    if source_sha256 != expected_source_sha256:
        raise CandidateError("normalized.source_sha256 与 manifest 不一致")

    items = require_list(root.get("conversations"), "normalized.conversations")
    for conversation_position, raw_conversation in enumerate(items):
        location = f"normalized.conversations[{conversation_position}]"
        conversation = require_object(raw_conversation, location)
        conversation_id = require_nonempty_string(
            conversation.get("conversation_id"),
            f"{location}.conversation_id",
        )
        messages = require_list(
            conversation.get("messages"), f"{location}.messages"
        )
        message_roles: dict[str, str] = {}
        message_positions: dict[str, int] = {}
        for message_position, raw_message in enumerate(messages):
            message_location = f"{location}.messages[{message_position}]"
            message = require_object(raw_message, message_location)
            message_id = require_nonempty_string(
                message.get("message_id"), f"{message_location}.message_id"
            )
            role = require_nonempty_string(
                message.get("role"), f"{message_location}.role"
            )
            if role not in {"user", "assistant"}:
                raise CandidateError(
                    f"{message_location}.role 只能是 user 或 assistant"
                )
            if message_id in message_roles:
                raise CandidateError(f"{location} 存在重复 message_id")
            message_roles[message_id] = role
            message_positions[message_id] = message_position

        key = (source_sha256, conversation_id)
        indexed = ConversationIndex(
            source_sha256=source_sha256,
            conversation_id=conversation_id,
            message_roles=message_roles,
            message_positions=message_positions,
        )
        previous = conversations.get(key)
        if previous is not None and previous != indexed:
            raise CandidateError(
                "相同 source_sha256 和 conversation_id 对应不同消息结构"
            )
        if previous is None:
            conversations[key] = indexed
            conversation_order.append(key)


def load_normalized_bundle(input_directory: Path) -> NormalizedBundle:
    """读取标准化输出目录并建立可验证的对话、消息索引。"""

    root = input_directory.expanduser().resolve()
    if not root.is_dir():
        raise CandidateError(f"标准化输入目录不存在：{root}")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise CandidateError(f"缺少标准化 manifest：{manifest_path}")

    manifest = require_object(load_json(manifest_path), "manifest")
    if manifest.get("format") != NORMALIZATION_MANIFEST_FORMAT:
        raise CandidateError("标准化 manifest 格式不受支持")
    entries = require_list(manifest.get("entries"), "manifest.entries")

    conversations: dict[tuple[str, str], ConversationIndex] = {}
    conversation_order: list[tuple[str, str]] = []
    normalized_files: list[dict[str, str]] = []
    warnings: list[str] = []
    if manifest.get("status") != "ok":
        warnings.append("标准化 manifest 状态不是 ok，请结合原清单检查问题")

    normalized_entry_count = 0
    for entry_position, raw_entry in enumerate(entries):
        entry = require_object(
            raw_entry, f"manifest.entries[{entry_position}]"
        )
        if entry.get("status") != "normalized":
            continue
        normalized_entry_count += 1
        source = require_object(
            entry.get("source"),
            f"manifest.entries[{entry_position}].source",
        )
        source_sha256 = require_sha256(
            source.get("sha256"),
            f"manifest.entries[{entry_position}].source.sha256",
        )
        normalized_path = safe_output_path(root, entry.get("output"))
        if not normalized_path.is_file():
            raise CandidateError(f"normalized 输出不存在：{normalized_path.name}")
        index_normalized_document(
            load_json(normalized_path),
            source_sha256,
            conversations,
            conversation_order,
        )
        normalized_files.append(
            {
                "output": normalized_path.name,
                "sha256": sha256_file(normalized_path),
                "source_sha256": source_sha256,
            }
        )

    if normalized_entry_count == 0:
        raise CandidateError("manifest 中没有 normalized 来源")
    return NormalizedBundle(
        input_directory=root,
        manifest_sha256=sha256_file(manifest_path),
        conversations=conversations,
        conversation_order=tuple(conversation_order),
        normalized_files=tuple(normalized_files),
        warnings=tuple(warnings),
    )


def validate_review_notes(
    value: Any,
    location: str,
    conversation: ConversationIndex,
) -> list[dict[str, Any]]:
    """校验无法归属于具体候选的对话级审核说明。"""

    if value is None:
        return []
    notes = require_list(value, location)
    normalized: list[dict[str, Any]] = []
    for note_position, raw_note in enumerate(notes):
        note_location = f"{location}[{note_position}]"
        note = require_object(raw_note, note_location)
        if set(note) != {"reason", "message_ids"}:
            raise CandidateError(
                f"{note_location} 只能包含 reason 和 message_ids"
            )
        reason = require_reason_code(
            note.get("reason"), f"{note_location}.reason"
        )
        message_ids = validate_message_ids(
            note.get("message_ids"),
            f"{note_location}.message_ids",
            conversation,
        )
        normalized.append({"reason": reason, "message_ids": message_ids})
    return normalized


def validate_message_ids(
    value: Any,
    location: str,
    conversation: ConversationIndex,
) -> list[str]:
    """校验消息引用存在于指定标准化对话中。"""

    raw_ids = require_list(value, location)
    if not raw_ids:
        raise CandidateError(f"{location} 不得为空")
    message_ids: list[str] = []
    seen: set[str] = set()
    for message_position, raw_message_id in enumerate(raw_ids):
        message_id = require_nonempty_string(
            raw_message_id, f"{location}[{message_position}]"
        )
        if message_id not in conversation.message_roles:
            raise CandidateError(f"{location} 引用了不存在的 message_id")
        if message_id not in seen:
            seen.add(message_id)
            message_ids.append(message_id)
    return sorted(
        message_ids,
        key=lambda item: conversation.message_positions[item],
    )


def validate_conversation_results(
    draft: dict[str, Any],
    bundle: NormalizedBundle,
) -> dict[tuple[str, str], dict[str, Any]]:
    """校验每段对话的处理记录，但不承担候选语义判断。"""

    raw_results = require_list(
        draft.get("conversation_results"), "draft.conversation_results"
    )
    results: dict[tuple[str, str], dict[str, Any]] = {}
    allowed_fields = {
        "source_sha256",
        "conversation_id",
        "processing_status",
        "no_candidate_reason",
        "review_notes",
        "reason",
    }
    for result_position, raw_result in enumerate(raw_results):
        location = f"draft.conversation_results[{result_position}]"
        result = require_object(raw_result, location)
        unknown = set(result) - allowed_fields
        if unknown:
            raise CandidateError(f"{location} 包含未知字段：{sorted(unknown)}")
        source_sha256 = require_sha256(
            result.get("source_sha256"), f"{location}.source_sha256"
        )
        conversation_id = require_nonempty_string(
            result.get("conversation_id"), f"{location}.conversation_id"
        )
        key = (source_sha256, conversation_id)
        conversation = bundle.conversations.get(key)
        if conversation is None:
            raise CandidateError(f"{location} 引用了不存在的对话")
        if key in results:
            raise CandidateError(f"{location} 重复记录同一对话")
        status = require_nonempty_string(
            result.get("processing_status"),
            f"{location}.processing_status",
        )
        if status not in PROCESSING_STATUSES:
            raise CandidateError(f"{location}.processing_status 不受支持")

        normalized: dict[str, Any] = {
            "source_sha256": source_sha256,
            "conversation_id": conversation_id,
            "processing_status": status,
        }
        if "no_candidate_reason" in result:
            normalized["no_candidate_reason"] = require_reason_code(
                result["no_candidate_reason"],
                f"{location}.no_candidate_reason",
            )
        notes = validate_review_notes(
            result.get("review_notes"),
            f"{location}.review_notes",
            conversation,
        )
        if notes:
            normalized["review_notes"] = notes
        if "reason" in result:
            normalized["reason"] = require_reason_code(
                result["reason"], f"{location}.reason"
            )
        results[key] = normalized

    expected = set(bundle.conversations)
    actual = set(results)
    if actual != expected:
        missing_count = len(expected - actual)
        extra_count = len(actual - expected)
        raise CandidateError(
            "conversation_results 必须完整覆盖标准化对话："
            f"缺少 {missing_count}，多余 {extra_count}"
        )
    return results


def validate_flags(value: Any, location: str) -> set[str]:
    """校验候选 flags；assistant_only 会在后续按证据重新计算。"""

    raw_flags = require_list(value, location)
    flags: set[str] = set()
    for flag_position, raw_flag in enumerate(raw_flags):
        flag = require_nonempty_string(
            raw_flag, f"{location}[{flag_position}]"
        )
        if flag not in ALLOWED_FLAGS:
            raise CandidateError(f"{location} 包含不支持的 flag：{flag}")
        flags.add(flag)
    flags.discard("assistant_only")
    return flags


def validate_evidence(
    value: Any,
    location: str,
    bundle: NormalizedBundle,
    conversation_results: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """校验候选的多来源证据及其消息引用。"""

    raw_evidence = require_list(value, location)
    if not raw_evidence:
        raise CandidateError(f"{location} 不得为空")
    evidence: list[dict[str, Any]] = []
    expected_fields = {"source_sha256", "conversation_id", "message_ids"}
    for evidence_position, raw_item in enumerate(raw_evidence):
        item_location = f"{location}[{evidence_position}]"
        item = require_object(raw_item, item_location)
        if set(item) != expected_fields:
            raise CandidateError(
                f"{item_location} 必须且只能包含来源、对话和消息引用"
            )
        source_sha256 = require_sha256(
            item.get("source_sha256"), f"{item_location}.source_sha256"
        )
        conversation_id = require_nonempty_string(
            item.get("conversation_id"),
            f"{item_location}.conversation_id",
        )
        key = (source_sha256, conversation_id)
        conversation = bundle.conversations.get(key)
        if conversation is None:
            raise CandidateError(f"{item_location} 引用了不存在的对话")
        if conversation_results[key]["processing_status"] != "completed":
            raise CandidateError(
                f"{item_location} 不能引用未完成处理的对话"
            )
        message_ids = validate_message_ids(
            item.get("message_ids"),
            f"{item_location}.message_ids",
            conversation,
        )
        evidence.append(
            {
                "source_sha256": source_sha256,
                "conversation_id": conversation_id,
                "message_ids": message_ids,
            }
        )
    return evidence


def candidate_id_for(statement: str) -> str:
    """根据候选陈述生成稳定 ID。"""

    suffix = hashlib.sha256(statement.encode("utf-8")).hexdigest()[:20]
    return f"kc_{suffix}"


def normalize_candidates(
    draft: dict[str, Any],
    bundle: NormalizedBundle,
    conversation_results: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """精确去重候选、合并证据，并派生 assistant_only。"""

    raw_candidates = require_list(draft.get("candidates"), "draft.candidates")
    grouped: dict[str, dict[str, Any]] = {}
    for candidate_position, raw_candidate in enumerate(raw_candidates):
        location = f"draft.candidates[{candidate_position}]"
        candidate = require_object(raw_candidate, location)
        if set(candidate) != {"statement", "evidence", "flags"}:
            raise CandidateError(
                f"{location} 必须且只能包含 statement、evidence 和 flags"
            )
        statement = require_nonempty_string(
            candidate.get("statement"), f"{location}.statement"
        )
        evidence = validate_evidence(
            candidate.get("evidence"),
            f"{location}.evidence",
            bundle,
            conversation_results,
        )
        flags = validate_flags(candidate.get("flags"), f"{location}.flags")
        group = grouped.setdefault(
            statement,
            {"flags": set(), "message_ids": {}},
        )
        group["flags"].update(flags)
        for item in evidence:
            key = (item["source_sha256"], item["conversation_id"])
            group["message_ids"].setdefault(key, set()).update(
                item["message_ids"]
            )

    normalized: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}
    for statement, group in grouped.items():
        candidate_id = candidate_id_for(statement)
        previous_statement = seen_ids.get(candidate_id)
        if previous_statement is not None and previous_statement != statement:
            raise CandidateError("candidate_id 哈希发生冲突")
        seen_ids[candidate_id] = statement
        flags = set(group["flags"])
        evidence: list[dict[str, Any]] = []
        roles: list[str] = []
        for key in sorted(group["message_ids"]):
            conversation = bundle.conversations[key]
            message_ids = sorted(
                group["message_ids"][key],
                key=lambda item: conversation.message_positions[item],
            )
            roles.extend(conversation.message_roles[item] for item in message_ids)
            evidence.append(
                {
                    "source_sha256": key[0],
                    "conversation_id": key[1],
                    "message_ids": message_ids,
                }
            )
        if roles and all(role == "assistant" for role in roles):
            flags.add("assistant_only")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "statement": statement,
                "evidence": evidence,
                "flags": [flag for flag in FLAG_ORDER if flag in flags],
            }
        )
    return sorted(normalized, key=lambda item: item["candidate_id"])


def finalize_conversation_results(
    bundle: NormalizedBundle,
    raw_results: dict[tuple[str, str], dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按标准化顺序生成仅表达处理状态的对话级记录。"""

    candidate_ids: dict[tuple[str, str], set[str]] = {
        key: set() for key in bundle.conversations
    }
    for candidate in candidates:
        for evidence in candidate["evidence"]:
            key = (evidence["source_sha256"], evidence["conversation_id"])
            candidate_ids[key].add(candidate["candidate_id"])

    finalized: list[dict[str, Any]] = []
    for key in bundle.conversation_order:
        result = dict(raw_results[key])
        linked_ids = sorted(candidate_ids[key])
        status = result["processing_status"]
        if status != "completed" and linked_ids:
            raise CandidateError("未完成处理的对话不能关联知识候选")
        if status == "completed" and linked_ids:
            if "no_candidate_reason" in result:
                raise CandidateError(
                    "已关联候选的对话不能设置 no_candidate_reason"
                )
        elif status == "completed":
            if (
                "no_candidate_reason" not in result
                and "review_notes" not in result
            ):
                raise CandidateError(
                    "未产生候选的 completed 对话必须说明原因或审核问题"
                )
        else:
            if "reason" not in result:
                raise CandidateError("failed 或 skipped 对话必须提供 reason")
            if "no_candidate_reason" in result or "review_notes" in result:
                raise CandidateError(
                    "failed 或 skipped 对话只使用 reason 说明处理结果"
                )
        result["candidate_ids"] = linked_ids
        finalized.append(result)
    return finalized


def build_outputs(
    bundle: NormalizedBundle,
    draft: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把语义草稿转换为两个互相独立的正式输出对象。"""

    draft_root = require_object(draft, "draft")
    if draft_root.get("format") != DRAFT_FORMAT:
        raise CandidateError("语义提取草稿格式不受支持")
    if set(draft_root) != {"format", "candidates", "conversation_results"}:
        raise CandidateError(
            "语义提取草稿顶层只能包含 format、candidates 和 "
            "conversation_results"
        )

    raw_results = validate_conversation_results(draft_root, bundle)
    candidates = normalize_candidates(draft_root, bundle, raw_results)
    conversation_results = finalize_conversation_results(
        bundle, raw_results, candidates
    )
    candidates_document = {
        "format": CANDIDATES_FORMAT,
        "candidates": candidates,
    }

    status_counts = {
        status: sum(
            result["processing_status"] == status
            for result in conversation_results
        )
        for status in sorted(PROCESSING_STATUSES)
    }
    review_note_count = sum(
        len(result.get("review_notes", []))
        for result in conversation_results
    )
    no_candidate_count = sum(
        result["processing_status"] == "completed"
        and not result["candidate_ids"]
        for result in conversation_results
    )
    has_issues = bool(
        status_counts["failed"]
        or status_counts["skipped"]
        or review_note_count
        or bundle.warnings
    )
    manifest = {
        "format": EXTRACTION_MANIFEST_FORMAT,
        "status": "completed_with_issues" if has_issues else "ok",
        "input": {
            "normalization_directory": str(bundle.input_directory),
            "normalization_manifest_sha256": bundle.manifest_sha256,
            "normalized_files": list(bundle.normalized_files),
        },
        "rules_version": EXTRACTION_RULES_VERSION,
        "summary": {
            "conversation_count": len(conversation_results),
            "completed_conversation_count": status_counts["completed"],
            "failed_conversation_count": status_counts["failed"],
            "skipped_conversation_count": status_counts["skipped"],
            "no_candidate_conversation_count": no_candidate_count,
            "candidate_count": len(candidates),
            "flagged_candidate_count": sum(
                bool(candidate["flags"]) for candidate in candidates
            ),
            "review_note_count": review_note_count,
        },
        "conversation_results": conversation_results,
        "warnings": list(bundle.warnings),
    }
    return candidates_document, manifest


def json_bytes(value: Any) -> bytes:
    """把 JSON 值序列化为稳定、可读且以换行结束的 UTF-8。"""

    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8")


def write_new_bytes(path: Path, payload: bytes) -> None:
    """新建文件并写入字节；目标已存在时拒绝覆盖。"""

    with path.open("xb") as handle:
        handle.write(payload)


def finalize_to_directory(
    normalization_directory: Path,
    draft_path: Path,
    output_directory: Path,
) -> dict[str, Any]:
    """完整校验后在新目录中写入候选文件和提取 manifest。"""

    output_path = output_directory.expanduser().resolve()
    if output_path.exists():
        raise CandidateError(f"输出目录已存在，拒绝覆盖或追加：{output_path}")
    draft_file = draft_path.expanduser().resolve()
    if not draft_file.is_file():
        raise CandidateError(f"语义提取草稿不存在：{draft_file}")

    bundle = load_normalized_bundle(normalization_directory)
    candidates_document, manifest = build_outputs(
        bundle, load_json(draft_file)
    )
    candidates_payload = json_bytes(candidates_document)
    manifest["outputs"] = {
        "candidates": CANDIDATES_FILENAME,
        "candidates_sha256": sha256_bytes(candidates_payload),
    }
    manifest_payload = json_bytes(manifest)

    output_path.mkdir(parents=True, exist_ok=False)
    write_new_bytes(output_path / CANDIDATES_FILENAME, candidates_payload)
    write_new_bytes(output_path / MANIFEST_FILENAME, manifest_payload)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    """构建非交互式命令行解析器。"""

    parser = argparse.ArgumentParser(
        description="校验语义提取草稿并生成可审核的知识候选。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize_parser = subparsers.add_parser(
        "finalize",
        help="校验草稿，并写入新的知识候选输出目录",
    )
    finalize_parser.add_argument(
        "normalization_directory",
        type=Path,
        help="包含 manifest.json 的第二阶段输出目录",
    )
    finalize_parser.add_argument(
        "draft",
        type=Path,
        help="Codex 生成的 extraction-draft.v1 JSON",
    )
    finalize_parser.add_argument(
        "output",
        type=Path,
        help="必须尚不存在的新输出目录",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行命令并以结构化标准输出返回 extraction manifest。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = finalize_to_directory(
            args.normalization_directory,
            args.draft,
            args.output,
        )
    except (
        CandidateError,
        FileExistsError,
        json.JSONDecodeError,
        UnicodeError,
        OSError,
    ) as exc:
        parser.exit(2, f"错误：{exc}\n")
    json.dump(manifest, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
