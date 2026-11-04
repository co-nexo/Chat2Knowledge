#!/usr/bin/env python3
"""校验语义提案草稿并生成确定性的知识修改提案。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

import knowledge_inventory
import knowledge_retrieval

FORMAT_VERSION = "conversation-knowledge-curator.knowledge-proposals.v1"
DRAFT_FORMAT = "conversation-knowledge-curator.proposal-draft.v1"
RETRIEVAL_FORMAT = "conversation-knowledge-curator.knowledge-retrieval.v2"
FLAG_ORDER = (
    "assistant_only",
    "uncertain",
    "time_sensitive",
    "sensitive",
)
WRITABLE_ACTIONS = {"add", "merge"}
NON_WRITABLE_ACTIONS = {"no_change", "conflict", "defer"}
ACTION_OPERATIONS = {
    "add": {"create_document", "insert_section"},
    "merge": {"append_to_section", "replace_section"},
}
ATX_HEADING_PATTERN = re.compile(r" {0,3}#{1,6}[ \t]+\S")
H1_HEADING_PATTERN = re.compile(r" {0,3}#[ \t]+\S")


class ProposalError(Exception):
    """表示提案输入、引用或操作不符合公开契约。"""


def require_exact_fields(
    value: dict[str, object],
    expected: set[str],
    context: str,
) -> None:
    """拒绝缺失或契约未声明的字段。"""

    try:
        knowledge_retrieval.require_exact_fields(value, expected, context)
    except knowledge_retrieval.RetrievalError as exc:
        raise ProposalError(str(exc)) from exc


def require_string(value: object, context: str) -> str:
    """返回去除首尾空白的非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ProposalError(f"{context} 必须是非空字符串")
    return value.strip()


def require_sha256(value: object, context: str) -> str:
    """校验小写 SHA-256 字符串。"""

    text = require_string(value, context)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ProposalError(f"{context} 必须是小写 SHA-256")
    return text


def validate_input_hashes(
    value: object,
    expected: dict[str, str],
    context: str,
) -> dict[str, str]:
    """校验阶段输入哈希与本次实际文件完全一致。"""

    if not isinstance(value, dict):
        raise ProposalError(f"{context} 必须是对象")
    require_exact_fields(value, set(expected), context)
    normalized = {
        name: require_sha256(value[name], f"{context}.{name}")
        for name in expected
    }
    if normalized != expected:
        raise ProposalError(f"{context} 与实际输入文件哈希不一致")
    return normalized


def validate_retrieval_document(
    document: dict[str, object],
    candidates_document: dict[str, object],
    candidates_sha256: str,
    inventory_document: dict[str, object],
    inventory_sha256: str,
    knowledge_root: Path,
) -> tuple[list[dict[str, object]], list[knowledge_retrieval.SearchSection]]:
    """重算召回结果，拒绝过期、篡改或版本不受支持的文件。"""

    require_exact_fields(
        document,
        {"format", "status", "inputs", "parameters", "results", "warnings"},
        "knowledge retrieval 顶层",
    )
    if document["format"] != RETRIEVAL_FORMAT:
        raise ProposalError("不支持的 knowledge retrieval 格式")
    parameters = document["parameters"]
    if not isinstance(parameters, dict):
        raise ProposalError("knowledge retrieval.parameters 必须是对象")
    require_exact_fields(parameters, {"limit"}, "knowledge retrieval.parameters")
    limit = parameters["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ProposalError("knowledge retrieval.parameters.limit 必须在 1 到 20 之间")

    try:
        expected_retrieval = knowledge_retrieval.build_retrieval(
            candidates_document,
            candidates_sha256,
            inventory_document,
            inventory_sha256,
            knowledge_root,
            limit,
        )
        candidates = knowledge_retrieval.validate_candidates(candidates_document)
        sections, _, _ = knowledge_retrieval.validate_inventory(
            inventory_document,
            knowledge_root,
        )
    except knowledge_retrieval.RetrievalError as exc:
        raise ProposalError(str(exc)) from exc
    if document != expected_retrieval:
        raise ProposalError("knowledge retrieval 已被修改、过期或不符合确定性结果")
    return candidates, sections


def first_nonempty_line(markdown: str) -> str:
    """返回 Markdown 的首个非空行。"""

    return next((line for line in markdown.splitlines() if line.strip()), "")


def validate_operation(
    value: object,
    action: str,
    context: str,
) -> dict[str, str] | None:
    """校验 action 与有限 Markdown 操作的组合。"""

    if action in NON_WRITABLE_ACTIONS:
        if value is not None:
            raise ProposalError(f"{context} 在 {action} action 下必须为 null")
        return None
    if not isinstance(value, dict):
        raise ProposalError(f"{context} 在 {action} action 下必须是对象")
    require_exact_fields(value, {"type", "markdown"}, context)
    operation_type = require_string(value["type"], f"{context}.type")
    if operation_type not in ACTION_OPERATIONS[action]:
        raise ProposalError(f"{action} 不允许 operation.type={operation_type}")
    markdown_value = value["markdown"]
    if not isinstance(markdown_value, str) or not markdown_value.strip():
        raise ProposalError(f"{context}.markdown 必须是非空字符串")
    if not markdown_value.endswith("\n"):
        raise ProposalError(f"{context}.markdown 必须以换行结束")
    first_line = first_nonempty_line(markdown_value)
    if operation_type == "create_document" and not H1_HEADING_PATTERN.match(
        first_line
    ):
        raise ProposalError("create_document 的首个非空行必须是一级 ATX 标题")
    if operation_type in {"insert_section", "replace_section"} and not (
        ATX_HEADING_PATTERN.match(first_line)
    ):
        raise ProposalError(f"{operation_type} 必须以 ATX 标题开始")
    return {"type": operation_type, "markdown": markdown_value}


def validate_target_shape(
    value: object,
    context: str,
) -> tuple[dict[str, object], PurePosixPath]:
    """校验固定 Target 字段和安全 POSIX Markdown 路径。"""

    if not isinstance(value, dict):
        raise ProposalError(f"{context} 必须是对象")
    require_exact_fields(
        value,
        {"path", "file_sha256", "section_id", "section_sha256"},
        context,
    )
    try:
        path = knowledge_retrieval.safe_inventory_path(value["path"])
    except knowledge_retrieval.RetrievalError as exc:
        raise ProposalError(str(exc)) from exc
    return value, path


def validate_new_target_path(
    knowledge_root: Path,
    path: PurePosixPath,
) -> None:
    """确认新建目标尚不存在且已有祖先不包含链接或越界。"""

    resolved_root = knowledge_root.resolve(strict=True)
    current = resolved_root
    for index, part in enumerate(path.parts):
        current = current / part
        if current.exists() and knowledge_inventory.is_link_like(current):
            raise ProposalError(f"create_document 目标包含链接：{path.as_posix()}")
        if current.exists():
            resolved = current.resolve(strict=True)
            if not knowledge_inventory.is_within(resolved, resolved_root):
                raise ProposalError(f"create_document 目标超出知识库：{path.as_posix()}")
            if index < len(path.parts) - 1 and not current.is_dir():
                raise ProposalError(
                    f"create_document 目标的中间路径不是目录：{path.as_posix()}"
                )
    if current.exists():
        raise ProposalError(f"create_document 目标已经存在：{path.as_posix()}")


def normalize_new_target(
    value: object,
    knowledge_root: Path,
    context: str,
) -> dict[str, object]:
    """校验 create_document 使用的空哈希目标。"""

    target, path = validate_target_shape(value, context)
    for field in ("file_sha256", "section_id", "section_sha256"):
        if target[field] is not None:
            raise ProposalError(f"{context}.{field} 在 create_document 下必须为 null")
    validate_new_target_path(knowledge_root, path)
    return {
        "path": path.as_posix(),
        "file_sha256": None,
        "section_id": None,
        "section_sha256": None,
    }


def normalize_existing_target(
    value: object,
    sections: dict[tuple[str, str], knowledge_retrieval.SearchSection],
    context: str,
) -> dict[str, object]:
    """校验目标精确引用 inventory 中的同一文件和章节快照。"""

    target, path = validate_target_shape(value, context)
    section_id = require_string(target["section_id"], f"{context}.section_id")
    if not re.fullmatch(r"ks_[0-9a-f]{20}", section_id):
        raise ProposalError(f"{context}.section_id 格式非法")
    section = sections.get((path.as_posix(), section_id))
    if section is None:
        raise ProposalError(f"{context} 未定位到 inventory 中的真实章节")
    file_sha256 = require_sha256(target["file_sha256"], f"{context}.file_sha256")
    section_sha256 = require_sha256(
        target["section_sha256"],
        f"{context}.section_sha256",
    )
    if file_sha256 != section.file_sha256 or section_sha256 != section.section_sha256:
        raise ProposalError(f"{context} 的文件或章节哈希已失效")
    return {
        "path": section.path,
        "file_sha256": section.file_sha256,
        "section_id": section.section_id,
        "section_sha256": section.section_sha256,
    }


def normalize_flags(
    value: object,
    candidate_ids: list[str],
    candidates: dict[str, dict[str, object]],
    context: str,
) -> list[str]:
    """校验草稿 flags 等于关联候选风险标记的并集。"""

    if not isinstance(value, list) or any(not isinstance(flag, str) for flag in value):
        raise ProposalError(f"{context} 必须是字符串数组")
    if len(value) != len(set(value)) or any(flag not in FLAG_ORDER for flag in value):
        raise ProposalError(f"{context} 包含重复或非法值")
    expected = {
        str(flag)
        for candidate_id in candidate_ids
        for flag in candidates[candidate_id]["flags"]
    }
    if set(value) != expected:
        raise ProposalError(f"{context} 必须等于关联候选 flags 的并集")
    return [flag for flag in FLAG_ORDER if flag in expected]


def proposal_id_for(proposal: dict[str, object]) -> str:
    """根据规范化 Proposal 内容生成稳定 ID。"""

    payload = json.dumps(
        proposal,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"kp_{hashlib.sha256(payload).hexdigest()[:20]}"


def normalize_proposals(
    value: object,
    candidates_list: list[dict[str, object]],
    sections_list: list[knowledge_retrieval.SearchSection],
    knowledge_root: Path,
) -> list[dict[str, object]]:
    """校验草稿提案完整覆盖候选并生成稳定最终结构。"""

    if not isinstance(value, list):
        raise ProposalError("proposal draft.proposals 必须是数组")
    candidates = {str(item["candidate_id"]): item for item in candidates_list}
    sections = {(item.path, item.section_id): item for item in sections_list}
    seen_candidates: set[str] = set()
    writable_paths: set[str] = set()
    normalized: list[dict[str, object]] = []

    for index, raw_proposal in enumerate(value):
        context = f"proposal draft.proposals[{index}]"
        if not isinstance(raw_proposal, dict):
            raise ProposalError(f"{context} 必须是对象")
        require_exact_fields(
            raw_proposal,
            {"candidate_ids", "action", "target", "rationale", "operation", "flags"},
            context,
        )
        raw_candidate_ids = raw_proposal["candidate_ids"]
        if not isinstance(raw_candidate_ids, list) or not raw_candidate_ids:
            raise ProposalError(f"{context}.candidate_ids 必须是非空数组")
        if any(not isinstance(item, str) for item in raw_candidate_ids):
            raise ProposalError(f"{context}.candidate_ids 必须只包含字符串")
        candidate_ids = sorted(raw_candidate_ids)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ProposalError(f"{context}.candidate_ids 不得重复")
        for candidate_id in candidate_ids:
            if candidate_id not in candidates:
                raise ProposalError(f"{context} 引用了不存在的候选：{candidate_id}")
            if candidate_id in seen_candidates:
                raise ProposalError(f"候选出现在多个提案中：{candidate_id}")
            seen_candidates.add(candidate_id)

        action = require_string(raw_proposal["action"], f"{context}.action")
        if action not in WRITABLE_ACTIONS | NON_WRITABLE_ACTIONS:
            raise ProposalError(f"{context}.action 非法：{action}")
        rationale = require_string(raw_proposal["rationale"], f"{context}.rationale")
        operation = validate_operation(
            raw_proposal["operation"],
            action,
            f"{context}.operation",
        )
        flags = normalize_flags(
            raw_proposal["flags"],
            candidate_ids,
            candidates,
            f"{context}.flags",
        )

        operation_type = operation["type"] if operation is not None else None
        if operation_type == "create_document":
            target = normalize_new_target(
                raw_proposal["target"],
                knowledge_root,
                f"{context}.target",
            )
        elif action in {"add", "merge", "no_change", "conflict"}:
            target = normalize_existing_target(
                raw_proposal["target"],
                sections,
                f"{context}.target",
            )
        elif raw_proposal["target"] is None:
            target = None
        else:
            target = normalize_existing_target(
                raw_proposal["target"],
                sections,
                f"{context}.target",
            )

        if action in WRITABLE_ACTIONS:
            assert isinstance(target, dict)
            target_path = str(target["path"])
            if target_path in writable_paths:
                raise ProposalError(f"同一目标文件存在多个可写提案：{target_path}")
            writable_paths.add(target_path)

        proposal_without_id: dict[str, object] = {
            "candidate_ids": candidate_ids,
            "action": action,
            "target": target,
            "rationale": rationale,
            "operation": operation,
            "flags": flags,
        }
        normalized.append(
            {
                "proposal_id": proposal_id_for(proposal_without_id),
                **proposal_without_id,
            }
        )

    missing = sorted(set(candidates) - seen_candidates)
    if missing:
        raise ProposalError(f"以下候选未被提案覆盖：{missing}")
    normalized.sort(key=lambda item: str(item["proposal_id"]))
    return normalized


def build_proposals(
    candidates_document: dict[str, object],
    candidates_sha256: str,
    inventory_document: dict[str, object],
    inventory_sha256: str,
    retrieval_document: dict[str, object],
    retrieval_sha256: str,
    draft_document: dict[str, object],
    knowledge_root: Path,
) -> dict[str, object]:
    """校验全部阶段输入并构造最终 knowledge proposals。"""

    input_hashes = {
        "knowledge_candidates_sha256": require_sha256(
            candidates_sha256,
            "knowledge_candidates_sha256",
        ),
        "knowledge_inventory_sha256": require_sha256(
            inventory_sha256,
            "knowledge_inventory_sha256",
        ),
        "knowledge_retrieval_sha256": require_sha256(
            retrieval_sha256,
            "knowledge_retrieval_sha256",
        ),
    }
    candidates, sections = validate_retrieval_document(
        retrieval_document,
        candidates_document,
        candidates_sha256,
        inventory_document,
        inventory_sha256,
        knowledge_root,
    )
    require_exact_fields(draft_document, {"format", "inputs", "proposals"}, "草稿顶层")
    if draft_document["format"] != DRAFT_FORMAT:
        raise ProposalError("不支持的 proposal draft 格式")
    validate_input_hashes(draft_document["inputs"], input_hashes, "草稿 inputs")
    proposals = normalize_proposals(
        draft_document["proposals"],
        candidates,
        sections,
        knowledge_root,
    )
    return {
        "format": FORMAT_VERSION,
        "inputs": input_hashes,
        "proposals": proposals,
    }


def create_proposals_file(
    knowledge_root: Path,
    inventory_path: Path,
    candidates_path: Path,
    retrieval_path: Path,
    draft_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """在全部校验成功后独占创建最终提案文件。"""

    if output_path.exists():
        raise ProposalError(f"输出文件已存在：{output_path}")
    inventory_document, inventory_raw = knowledge_retrieval.load_json_document(
        inventory_path,
        "knowledge inventory",
    )
    candidates_document, candidates_raw = knowledge_retrieval.load_json_document(
        candidates_path,
        "knowledge candidates",
    )
    retrieval_document, retrieval_raw = knowledge_retrieval.load_json_document(
        retrieval_path,
        "knowledge retrieval",
    )
    draft_document, _ = knowledge_retrieval.load_json_document(
        draft_path,
        "proposal draft",
    )
    result = build_proposals(
        candidates_document,
        knowledge_inventory.sha256_bytes(candidates_raw),
        inventory_document,
        knowledge_inventory.sha256_bytes(inventory_raw),
        retrieval_document,
        knowledge_inventory.sha256_bytes(retrieval_raw),
        draft_document,
        knowledge_root,
    )
    knowledge_inventory.write_new_file(
        output_path,
        knowledge_inventory.serialize_json(result),
    )
    return result


def create_parser() -> argparse.ArgumentParser:
    """创建非交互式提案最终化 CLI。"""

    parser = argparse.ArgumentParser(
        description="校验 proposal draft 并生成确定性的 knowledge proposals。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize_parser = subparsers.add_parser(
        "finalize",
        help="生成 knowledge-proposals.v1 JSON 文件",
    )
    finalize_parser.add_argument("knowledge_root", type=Path)
    finalize_parser.add_argument("inventory_file", type=Path)
    finalize_parser.add_argument("candidates_file", type=Path)
    finalize_parser.add_argument("retrieval_file", type=Path)
    finalize_parser.add_argument("draft_file", type=Path)
    finalize_parser.add_argument("output_file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行提案最终化并返回成功或失败退出码。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "finalize":
            parser.error("未知命令")
        result = create_proposals_file(
            args.knowledge_root,
            args.inventory_file,
            args.candidates_file,
            args.retrieval_file,
            args.draft_file,
            args.output_file,
        )
    except (
        ProposalError,
        knowledge_retrieval.RetrievalError,
        knowledge_inventory.InventoryError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    action_counts = {
        action: 0 for action in sorted(WRITABLE_ACTIONS | NON_WRITABLE_ACTIONS)
    }
    for proposal in result["proposals"]:
        if isinstance(proposal, dict):
            action_counts[str(proposal["action"])] += 1
    summary = {
        "status": "ok",
        "proposal_count": len(result["proposals"]),
        "action_counts": action_counts,
        "output": str(args.output_file.resolve()),
    }
    print(knowledge_inventory.serialize_json(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
