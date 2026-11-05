#!/usr/bin/env python3
"""校验知识提案并生成不修改知识库的审核包。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

import knowledge_document_quality
import knowledge_inventory
import knowledge_proposals
import knowledge_retrieval

FORMAT_VERSION = "conversation-knowledge-curator.review-manifest.v1"
REPORT_FORMAT = "conversation-knowledge-curator.review-report.v1"
DECISIONS_FORMAT = "conversation-knowledge-curator.review-decisions.v1"
DECISION_DRAFT_FORMAT = "conversation-knowledge-curator.review-decision-draft.v1"
UTF8_BOM = b"\xef\xbb\xbf"
WRITABLE_ACTIONS = {"add", "merge"}
NON_WRITABLE_ACTIONS = {"no_change", "conflict", "defer"}
DECISION_VALUES = {"approved", "rejected"}
RUN_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class ReviewError(Exception):
    """表示审核包输入、修改模拟或输出不符合契约。"""


def require_exact_fields(
    value: dict[str, object],
    expected: set[str],
    context: str,
) -> None:
    """拒绝缺失或契约未声明的 JSON 字段。"""

    try:
        knowledge_proposals.require_exact_fields(value, expected, context)
    except knowledge_proposals.ProposalError as exc:
        raise ReviewError(str(exc)) from exc


def require_string(value: object, context: str) -> str:
    """返回去除首尾空白后的非空字符串。"""

    try:
        return knowledge_proposals.require_string(value, context)
    except knowledge_proposals.ProposalError as exc:
        raise ReviewError(str(exc)) from exc


def require_sha256(value: object, context: str) -> str:
    """校验小写 SHA-256 字符串。"""

    try:
        return knowledge_proposals.require_sha256(value, context)
    except knowledge_proposals.ProposalError as exc:
        raise ReviewError(str(exc)) from exc


def canonical_json_bytes(value: object) -> bytes:
    """按稳定紧凑格式序列化需要参与身份计算的 JSON。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """返回字节内容的小写 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def proposal_sha256(proposal: dict[str, object]) -> str:
    """计算单个规范化 Proposal 的内容哈希。"""

    return sha256_bytes(canonical_json_bytes(proposal))


def normalize_newlines(value: str, newline: str) -> str:
    """把外部 Markdown 片段规范化为目标文件的换行风格。"""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


def detect_newline(text: str) -> str:
    """根据现有文件中 LF 与 CRLF 数量选择主要换行风格。"""

    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    return "\r\n" if crlf_count > lf_count else "\n"


def locate_section(
    text: str,
    document_path: str,
    section_id: str,
) -> tuple[int, int, int, knowledge_inventory.SectionFragment]:
    """定位章节直属片段及包含子章节的完整子树结束位置。"""

    fragments = knowledge_inventory.parse_section_fragments(text, document_path)
    cursor = 0
    target_start = -1
    target_end = -1
    target_fragment: knowledge_inventory.SectionFragment | None = None
    for fragment in fragments:
        start = text.find(fragment.markdown, cursor)
        if start < 0:
            raise ReviewError(f"无法重新定位 Markdown 章节：{document_path}")
        end = start + len(fragment.markdown)
        if fragment.section_id == section_id:
            target_start = start
            target_end = end
            target_fragment = fragment
            break
        cursor = end
    if target_fragment is None:
        raise ReviewError(f"目标章节不再存在：{document_path}#{section_id}")

    subtree_end = target_end
    if target_fragment.level > 0:
        for heading in knowledge_inventory.parse_headings(text):
            if heading.start <= target_start:
                continue
            if heading.level <= target_fragment.level:
                subtree_end = heading.start
                break
        else:
            subtree_end = len(text)
    return target_start, target_end, subtree_end, target_fragment


def encode_markdown(text: str, include_bom: bool) -> bytes:
    """按原文件 BOM 策略编码 Markdown。"""

    content = text.encode("utf-8")
    return UTF8_BOM + content if include_bom else content


def apply_existing_operation(
    before_bytes: bytes,
    path: str,
    section_id: str,
    operation: dict[str, str],
) -> bytes:
    """在内存中对现有文件执行一个已校验的章节操作。"""

    before_text = knowledge_inventory.decode_markdown(before_bytes, path)
    start, end, subtree_end, _ = locate_section(before_text, path, section_id)
    newline = detect_newline(before_text)
    markdown = normalize_newlines(operation["markdown"], newline)
    operation_type = operation["type"]
    if operation_type == "append_to_section":
        after_text = before_text[:end] + markdown + before_text[end:]
    elif operation_type == "replace_section":
        after_text = before_text[:start] + markdown + before_text[end:]
    elif operation_type == "insert_section":
        after_text = before_text[:subtree_end] + markdown + before_text[subtree_end:]
    else:
        raise ReviewError(f"不支持的现有文件操作：{operation_type}")
    return encode_markdown(after_text, before_bytes.startswith(UTF8_BOM))


def display_text(content: bytes, path: str) -> str:
    """将 Markdown 解码并统一为 LF，供稳定 diff 展示。"""

    text = knowledge_inventory.decode_markdown(content, path)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def unified_diff_bytes(
    before_bytes: bytes | None,
    after_bytes: bytes,
    target_path: str,
) -> bytes:
    """生成不含时间戳、使用 LF 的稳定 unified diff。"""

    before_text = "" if before_bytes is None else display_text(before_bytes, target_path)
    after_text = display_text(after_bytes, target_path)
    from_name = "/dev/null" if before_bytes is None else f"a/{target_path}"
    diff = "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=from_name,
            tofile=f"b/{target_path}",
            lineterm="\n",
        )
    )
    if not diff:
        raise ReviewError(f"可写提案没有产生内容变化：{target_path}")
    return diff.encode("utf-8")


def target_path_of(proposal: dict[str, object]) -> str | None:
    """从已校验 Proposal 中提取可选目标路径。"""

    target = proposal["target"]
    if target is None:
        return None
    if not isinstance(target, dict) or not isinstance(target.get("path"), str):
        raise ReviewError("Proposal target 结构非法")
    return target["path"]


def validate_after_document(
    after_bytes: bytes,
    target_path: str,
    operation_type: str,
) -> None:
    """在生成审核包前检查完整 after 文档的确定性结构质量。"""

    text = display_text(after_bytes, target_path)
    mode = "strict" if operation_type == "create_document" else "compatible"
    report = knowledge_document_quality.analyze_document(
        text,
        mode,
        target_path,
    )
    issues = report["issues"]
    assert isinstance(issues, list)
    if not issues:
        return
    details = []
    for item in issues[:5]:
        assert isinstance(item, dict)
        line = item.get("line")
        location = f"第 {line} 行" if isinstance(line, int) else "文档"
        details.append(f"{location}：{item['message']}")
    suffix = "" if len(issues) <= 5 else f"；另有 {len(issues) - 5} 项"
    raise ReviewError(
        f"知识文档质量检查失败：{target_path}：{'；'.join(details)}{suffix}"
    )


def prepare_write_artifacts(
    proposal: dict[str, object],
    knowledge_root: Path,
) -> tuple[bytes, bytes, str | None]:
    """在内存中生成单个可写提案的 after、diff 和基础文件哈希。"""

    target = proposal["target"]
    operation = proposal["operation"]
    if not isinstance(target, dict) or not isinstance(operation, dict):
        raise ReviewError("可写 Proposal 缺少 target 或 operation")
    target_path = str(target["path"])
    operation_type = str(operation["type"])
    markdown = str(operation["markdown"])
    if operation_type == "create_document":
        after_bytes = normalize_newlines(markdown, "\n").encode("utf-8")
        before_bytes = None
        base_sha256 = None
    else:
        try:
            relative_path = knowledge_retrieval.safe_inventory_path(target_path)
            markdown_path = knowledge_retrieval.resolve_markdown_path(
                knowledge_root.resolve(strict=True),
                relative_path,
            )
            before_bytes = markdown_path.read_bytes()
        except (knowledge_retrieval.RetrievalError, OSError) as exc:
            raise ReviewError(str(exc)) from exc
        section_id = str(target["section_id"])
        after_bytes = apply_existing_operation(
            before_bytes,
            target_path,
            section_id,
            {"type": operation_type, "markdown": markdown},
        )
        base_sha256 = sha256_bytes(before_bytes)
    validate_after_document(after_bytes, target_path, operation_type)
    diff_bytes = unified_diff_bytes(before_bytes, after_bytes, target_path)
    return after_bytes, diff_bytes, base_sha256


def review_id_for(inputs: dict[str, str], entries: list[dict[str, object]]) -> str:
    """根据审核输入和条目生成稳定 review ID。"""

    identity = canonical_json_bytes({"inputs": inputs, "entries": entries})
    return f"kr_{sha256_bytes(identity)[:20]}"


def build_review_package(
    candidates_document: dict[str, object],
    candidates_sha256: str,
    inventory_document: dict[str, object],
    inventory_sha256: str,
    retrieval_document: dict[str, object],
    retrieval_sha256: str,
    proposals_document: dict[str, object],
    proposals_sha256: str,
    knowledge_root: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """校验全部输入并在内存中生成审核清单与衍生文件。"""

    try:
        proposals = knowledge_proposals.validate_final_proposals(
            proposals_document,
            candidates_document,
            candidates_sha256,
            inventory_document,
            inventory_sha256,
            retrieval_document,
            retrieval_sha256,
            knowledge_root,
        )
    except knowledge_proposals.ProposalError as exc:
        raise ReviewError(str(exc)) from exc
    inputs = {
        "knowledge_candidates_sha256": candidates_sha256,
        "knowledge_inventory_sha256": inventory_sha256,
        "knowledge_proposals_sha256": proposals_sha256,
    }
    for name, value in inputs.items():
        try:
            knowledge_proposals.require_sha256(value, name)
        except knowledge_proposals.ProposalError as exc:
            raise ReviewError(str(exc)) from exc

    entries: list[dict[str, object]] = []
    artifacts: dict[str, bytes] = {}
    for proposal in proposals:
        proposal_id = str(proposal["proposal_id"])
        action = str(proposal["action"])
        target_path = target_path_of(proposal)
        entry: dict[str, object] = {
            "proposal_id": proposal_id,
            "proposal_sha256": proposal_sha256(proposal),
            "candidate_ids": list(proposal["candidate_ids"]),
            "action": action,
            "target_path": target_path,
            "base_file_sha256": None,
            "after_path": None,
            "after_sha256": None,
            "diff_path": None,
            "diff_sha256": None,
        }
        if action in WRITABLE_ACTIONS:
            after_bytes, diff_bytes, base_sha256 = prepare_write_artifacts(
                proposal,
                knowledge_root,
            )
            after_path = f"after/{proposal_id}.md"
            diff_path = f"patches/{proposal_id}.diff"
            artifacts[after_path] = after_bytes
            artifacts[diff_path] = diff_bytes
            entry.update(
                {
                    "base_file_sha256": base_sha256,
                    "after_path": after_path,
                    "after_sha256": sha256_bytes(after_bytes),
                    "diff_path": diff_path,
                    "diff_sha256": sha256_bytes(diff_bytes),
                }
            )
        entries.append(entry)

    manifest = {
        "format": FORMAT_VERSION,
        "review_id": review_id_for(inputs, entries),
        "inputs": inputs,
        "entries": entries,
    }
    return manifest, artifacts


def load_review_json(path: Path, context: str) -> tuple[dict[str, object], bytes]:
    """读取审核包 JSON，并把底层读取错误统一为审核错误。"""

    try:
        return knowledge_retrieval.load_json_document(path, context)
    except knowledge_retrieval.RetrievalError as exc:
        raise ReviewError(str(exc)) from exc


def read_review_artifact(
    review_root: Path,
    relative_path: str,
    context: str,
) -> bytes:
    """读取审核包内固定相对路径，并拒绝链接和目录逃逸。"""

    path = review_root.joinpath(*PurePosixPath(relative_path).parts)
    if knowledge_inventory.is_link_like(path):
        raise ReviewError(f"{context} 不得是链接：{relative_path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReviewError(f"{context} 不存在：{relative_path}") from exc
    if not knowledge_inventory.is_within(resolved, review_root) or not resolved.is_file():
        raise ReviewError(f"{context} 不是审核包内普通文件：{relative_path}")
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise ReviewError(f"无法读取{context}：{relative_path}") from exc


def validate_proposals_copy(
    document: dict[str, object],
) -> dict[str, dict[str, object]]:
    """校验审核包 proposals 副本的公开结构和唯一 ID。"""

    require_exact_fields(document, {"format", "inputs", "proposals"}, "proposals 顶层")
    if document["format"] != knowledge_proposals.FORMAT_VERSION:
        raise ReviewError("审核包包含不支持的 knowledge proposals 格式")
    inputs = document["inputs"]
    if not isinstance(inputs, dict):
        raise ReviewError("proposals.inputs 必须是对象")
    require_exact_fields(
        inputs,
        {
            "knowledge_candidates_sha256",
            "knowledge_inventory_sha256",
            "knowledge_retrieval_sha256",
        },
        "proposals.inputs",
    )
    for name, value in inputs.items():
        require_sha256(value, f"proposals.inputs.{name}")

    raw_proposals = document["proposals"]
    if not isinstance(raw_proposals, list):
        raise ReviewError("proposals.proposals 必须是数组")
    proposals: dict[str, dict[str, object]] = {}
    proposal_ids: list[str] = []
    for index, proposal in enumerate(raw_proposals):
        context = f"proposals.proposals[{index}]"
        if not isinstance(proposal, dict):
            raise ReviewError(f"{context} 必须是对象")
        require_exact_fields(
            proposal,
            {
                "proposal_id",
                "candidate_ids",
                "action",
                "target",
                "rationale",
                "operation",
                "flags",
            },
            context,
        )
        proposal_id = require_string(proposal["proposal_id"], f"{context}.proposal_id")
        if not re.fullmatch(r"kp_[0-9a-f]{20}", proposal_id):
            raise ReviewError(f"{context}.proposal_id 格式非法")
        if proposal_id in proposals:
            raise ReviewError(f"proposal_id 重复：{proposal_id}")
        candidate_ids = proposal["candidate_ids"]
        if not isinstance(candidate_ids, list) or not candidate_ids or any(
            not isinstance(candidate_id, str) for candidate_id in candidate_ids
        ):
            raise ReviewError(f"{context}.candidate_ids 必须是非空字符串数组")
        action = require_string(proposal["action"], f"{context}.action")
        if action not in WRITABLE_ACTIONS | NON_WRITABLE_ACTIONS:
            raise ReviewError(f"{context}.action 非法")
        proposals[proposal_id] = proposal
        proposal_ids.append(proposal_id)
    if proposal_ids != sorted(proposal_ids):
        raise ReviewError("proposals 必须按 proposal_id 排序")
    return proposals


def validate_review_entry(
    entry: object,
    proposal: dict[str, object],
    review_root: Path,
    context: str,
) -> dict[str, object]:
    """校验一个 manifest entry 与 Proposal 和审核文件完全一致。"""

    if not isinstance(entry, dict):
        raise ReviewError(f"{context} 必须是对象")
    require_exact_fields(
        entry,
        {
            "proposal_id",
            "proposal_sha256",
            "candidate_ids",
            "action",
            "target_path",
            "base_file_sha256",
            "after_path",
            "after_sha256",
            "diff_path",
            "diff_sha256",
        },
        context,
    )
    proposal_id = require_string(entry["proposal_id"], f"{context}.proposal_id")
    if proposal_id != proposal["proposal_id"]:
        raise ReviewError(f"{context}.proposal_id 与 proposals 不一致")
    expected_proposal_sha256 = proposal_sha256(proposal)
    if require_sha256(
        entry["proposal_sha256"],
        f"{context}.proposal_sha256",
    ) != expected_proposal_sha256:
        raise ReviewError(f"{context}.proposal_sha256 不匹配")
    if entry["candidate_ids"] != proposal["candidate_ids"]:
        raise ReviewError(f"{context}.candidate_ids 与 Proposal 不一致")
    if entry["action"] != proposal["action"]:
        raise ReviewError(f"{context}.action 与 Proposal 不一致")
    expected_target_path = target_path_of(proposal)
    if entry["target_path"] != expected_target_path:
        raise ReviewError(f"{context}.target_path 与 Proposal 不一致")

    action = str(proposal["action"])
    if action in WRITABLE_ACTIONS:
        after_path = require_string(entry["after_path"], f"{context}.after_path")
        diff_path = require_string(entry["diff_path"], f"{context}.diff_path")
        if after_path != f"after/{proposal_id}.md":
            raise ReviewError(f"{context}.after_path 非法")
        if diff_path != f"patches/{proposal_id}.diff":
            raise ReviewError(f"{context}.diff_path 非法")
        after_bytes = read_review_artifact(review_root, after_path, "after 文件")
        diff_bytes = read_review_artifact(review_root, diff_path, "diff 文件")
        if not diff_bytes:
            raise ReviewError(f"{context} 的 diff 不得为空")
        if require_sha256(entry["after_sha256"], f"{context}.after_sha256") != (
            sha256_bytes(after_bytes)
        ):
            raise ReviewError(f"{context}.after_sha256 不匹配")
        if require_sha256(entry["diff_sha256"], f"{context}.diff_sha256") != (
            sha256_bytes(diff_bytes)
        ):
            raise ReviewError(f"{context}.diff_sha256 不匹配")
        operation = proposal["operation"]
        target = proposal["target"]
        if not isinstance(operation, dict) or not isinstance(target, dict):
            raise ReviewError(f"{context} 的可写 Proposal 结构非法")
        validate_after_document(
            after_bytes,
            str(entry["target_path"]),
            str(operation["type"]),
        )
        if operation["type"] == "create_document":
            if entry["base_file_sha256"] is not None:
                raise ReviewError(f"{context}.base_file_sha256 应为 null")
        else:
            base_sha256 = require_sha256(
                entry["base_file_sha256"],
                f"{context}.base_file_sha256",
            )
            if base_sha256 != target["file_sha256"]:
                raise ReviewError(f"{context}.base_file_sha256 与 Proposal 不一致")
    else:
        for field in (
            "base_file_sha256",
            "after_path",
            "after_sha256",
            "diff_path",
            "diff_sha256",
        ):
            if entry[field] is not None:
                raise ReviewError(f"{context}.{field} 在非写入提案中必须为 null")
    return entry


def validate_review_package(
    review_directory: Path,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """校验审核清单、提案副本和全部 after/diff 文件的内部一致性。"""

    if knowledge_inventory.is_link_like(review_directory):
        raise ReviewError("审核目录不得是链接")
    try:
        review_root = review_directory.resolve(strict=True)
    except OSError as exc:
        raise ReviewError(f"审核目录不存在：{review_directory}") from exc
    if not review_root.is_dir():
        raise ReviewError(f"审核路径不是目录：{review_directory}")
    manifest, _ = load_review_json(
        review_root / "review-manifest.json",
        "review manifest",
    )
    proposals_document, proposals_raw = load_review_json(
        review_root / "proposals.json",
        "knowledge proposals",
    )
    require_exact_fields(
        manifest,
        {"format", "review_id", "inputs", "entries"},
        "review manifest 顶层",
    )
    if manifest["format"] != FORMAT_VERSION:
        raise ReviewError("不支持的 review manifest 格式")
    review_id = require_string(manifest["review_id"], "review manifest.review_id")
    if not re.fullmatch(r"kr_[0-9a-f]{20}", review_id):
        raise ReviewError("review manifest.review_id 格式非法")
    inputs = manifest["inputs"]
    if not isinstance(inputs, dict):
        raise ReviewError("review manifest.inputs 必须是对象")
    require_exact_fields(
        inputs,
        {
            "knowledge_candidates_sha256",
            "knowledge_inventory_sha256",
            "knowledge_proposals_sha256",
        },
        "review manifest.inputs",
    )
    normalized_inputs = {
        name: require_sha256(value, f"review manifest.inputs.{name}")
        for name, value in inputs.items()
    }
    if normalized_inputs["knowledge_proposals_sha256"] != sha256_bytes(proposals_raw):
        raise ReviewError("审核包 proposals.json 哈希不匹配")
    proposals = validate_proposals_copy(proposals_document)
    proposal_inputs = proposals_document["inputs"]
    assert isinstance(proposal_inputs, dict)
    if (
        proposal_inputs["knowledge_candidates_sha256"]
        != normalized_inputs["knowledge_candidates_sha256"]
        or proposal_inputs["knowledge_inventory_sha256"]
        != normalized_inputs["knowledge_inventory_sha256"]
    ):
        raise ReviewError("review manifest 与 proposals 的上游输入哈希不一致")

    raw_entries = manifest["entries"]
    if not isinstance(raw_entries, list):
        raise ReviewError("review manifest.entries 必须是数组")
    entries: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        context = f"review manifest.entries[{index}]"
        if not isinstance(raw_entry, dict):
            raise ReviewError(f"{context} 必须是对象")
        proposal_id = require_string(raw_entry.get("proposal_id"), f"{context}.proposal_id")
        if proposal_id in seen_ids:
            raise ReviewError(f"review manifest proposal_id 重复：{proposal_id}")
        proposal = proposals.get(proposal_id)
        if proposal is None:
            raise ReviewError(f"review manifest 引用了不存在的 Proposal：{proposal_id}")
        seen_ids.add(proposal_id)
        entries.append(
            validate_review_entry(raw_entry, proposal, review_root, context)
        )
    if seen_ids != set(proposals):
        raise ReviewError("review manifest 未完整覆盖 proposals")
    if [entry["proposal_id"] for entry in entries] != sorted(seen_ids):
        raise ReviewError("review manifest.entries 必须按 proposal_id 排序")
    expected_review_id = review_id_for(normalized_inputs, entries)
    if review_id != expected_review_id:
        raise ReviewError("review_id 与审核包内容不一致")
    return manifest, proposals


def section_change_record(
    fragment: knowledge_inventory.SectionFragment,
) -> dict[str, object]:
    """把章节片段转换为不复制正文的对话审核条目。"""

    return {
        "section_id": fragment.section_id,
        "heading": fragment.heading,
        "heading_path": list(fragment.heading_path),
        "level": fragment.level,
    }


def compare_document_sections(
    before_text: str,
    after_text: str,
    target_path: str,
) -> dict[str, list[dict[str, object]]]:
    """按稳定章节 ID 比较前后 Markdown，生成章节级结构变化。"""

    before = {
        fragment.section_id: fragment
        for fragment in knowledge_inventory.parse_section_fragments(
            before_text,
            target_path,
        )
        if fragment.level > 0
    }
    after = {
        fragment.section_id: fragment
        for fragment in knowledge_inventory.parse_section_fragments(
            after_text,
            target_path,
        )
        if fragment.level > 0
    }
    added = [
        section_change_record(after[section_id])
        for section_id in sorted(set(after) - set(before))
    ]
    removed = [
        section_change_record(before[section_id])
        for section_id in sorted(set(before) - set(after))
    ]
    modified = [
        section_change_record(after[section_id])
        for section_id in sorted(set(before) & set(after))
        if before[section_id].markdown != after[section_id].markdown
    ]
    return {"added": added, "modified": modified, "removed": removed}


def read_current_target(
    knowledge_root: Path,
    proposal: dict[str, object],
) -> tuple[bytes | None, str | None]:
    """读取并校验审核报告所依赖的当前目标文件及目标章节标题。"""

    target = proposal["target"]
    operation = proposal["operation"]
    if not isinstance(target, dict):
        return None, None
    target_path = str(target["path"])
    if isinstance(operation, dict) and operation["type"] == "create_document":
        try:
            relative_path = knowledge_retrieval.safe_inventory_path(target_path)
            knowledge_proposals.validate_new_target_path(
                knowledge_root,
                relative_path,
            )
        except (
            knowledge_retrieval.RetrievalError,
            knowledge_proposals.ProposalError,
        ) as exc:
            raise ReviewError(str(exc)) from exc
        return None, None
    try:
        relative_path = knowledge_retrieval.safe_inventory_path(target_path)
        markdown_path = knowledge_retrieval.resolve_markdown_path(
            knowledge_root.resolve(strict=True),
            relative_path,
        )
        before_bytes = markdown_path.read_bytes()
    except (knowledge_retrieval.RetrievalError, OSError) as exc:
        raise ReviewError(str(exc)) from exc
    expected_sha256 = target.get("file_sha256")
    if not isinstance(expected_sha256, str) or sha256_bytes(before_bytes) != (
        expected_sha256
    ):
        raise ReviewError(f"审核目标已变化，必须重新生成 Review：{target_path}")
    section_id = target.get("section_id")
    if not isinstance(section_id, str):
        return before_bytes, None
    text = display_text(before_bytes, target_path)
    for fragment in knowledge_inventory.parse_section_fragments(text, target_path):
        if fragment.section_id == section_id:
            return before_bytes, fragment.heading
    raise ReviewError(f"审核目标章节已变化：{target_path}#{section_id}")


def build_review_report(
    knowledge_root: Path,
    candidates_path: Path,
    review_directory: Path,
) -> dict[str, object]:
    """生成供 Codex 直接展示的章节级审核报告，不创建新文件。"""

    manifest, proposals = validate_review_package(review_directory)
    candidates_document, candidates_raw = knowledge_retrieval.load_json_document(
        candidates_path,
        "knowledge candidates",
    )
    expected_candidates_sha256 = manifest["inputs"][
        "knowledge_candidates_sha256"
    ]
    if sha256_bytes(candidates_raw) != expected_candidates_sha256:
        raise ReviewError("knowledge candidates 与 Review 绑定哈希不一致")
    try:
        candidate_list = knowledge_retrieval.validate_candidates(
            candidates_document
        )
    except knowledge_retrieval.RetrievalError as exc:
        raise ReviewError(str(exc)) from exc
    candidates = {
        str(candidate["candidate_id"]): candidate for candidate in candidate_list
    }
    manifest_entries = {
        str(entry["proposal_id"]): entry for entry in manifest["entries"]
    }
    action_counts = {
        action: 0 for action in sorted(WRITABLE_ACTIONS | NON_WRITABLE_ACTIONS)
    }
    entries: list[dict[str, object]] = []
    attention: list[dict[str, object]] = []
    review_root = review_directory.resolve(strict=True)
    for proposal_id in sorted(proposals):
        proposal = proposals[proposal_id]
        manifest_entry = manifest_entries[proposal_id]
        action = str(proposal["action"])
        action_counts[action] += 1
        candidate_details: list[dict[str, object]] = []
        for candidate_id in proposal["candidate_ids"]:
            candidate = candidates.get(str(candidate_id))
            if candidate is None:
                raise ReviewError(f"Review 引用了不存在的候选：{candidate_id}")
            candidate_details.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "statement": candidate["statement"],
                    "evidence": candidate["evidence"],
                }
            )

        target_path = manifest_entry["target_path"]
        before_bytes, target_heading = read_current_target(
            knowledge_root,
            proposal,
        )
        section_changes = {"added": [], "modified": [], "removed": []}
        after_path = manifest_entry["after_path"]
        if isinstance(after_path, str) and isinstance(target_path, str):
            after_bytes = read_review_artifact(
                review_root,
                after_path,
                "after 文件",
            )
            before_text = (
                "" if before_bytes is None else display_text(before_bytes, target_path)
            )
            section_changes = compare_document_sections(
                before_text,
                display_text(after_bytes, target_path),
                target_path,
            )

        flags = list(proposal["flags"])
        report_entry: dict[str, object] = {
            "proposal_id": proposal_id,
            "action": action,
            "target_path": target_path,
            "target_heading": target_heading,
            "rationale": proposal["rationale"],
            "flags": flags,
            "candidates": candidate_details,
            "after_path": after_path,
            "section_changes": section_changes,
        }
        entries.append(report_entry)
        if flags or action in {"conflict", "defer"}:
            attention.append(
                {
                    "proposal_id": proposal_id,
                    "action": action,
                    "target_path": target_path,
                    "flags": flags,
                    "reason": proposal["rationale"],
                }
            )

    return {
        "format": REPORT_FORMAT,
        "review_id": manifest["review_id"],
        "status": "needs_review",
        "summary": {
            "proposal_count": len(entries),
            "writable_count": sum(
                action_counts[action] for action in WRITABLE_ACTIONS
            ),
            "attention_count": len(attention),
            "action_counts": action_counts,
        },
        "entries": entries,
        "attention": attention,
    }


def build_review_decisions(
    review_manifest: dict[str, object],
    draft_document: dict[str, object],
) -> dict[str, object]:
    """把用户明确选择的 decision draft 绑定到具体审核条目哈希。"""

    require_exact_fields(
        draft_document,
        {"format", "review_id", "decisions"},
        "decision draft 顶层",
    )
    if draft_document["format"] != DECISION_DRAFT_FORMAT:
        raise ReviewError("不支持的 review decision draft 格式")
    review_id = require_string(draft_document["review_id"], "decision draft.review_id")
    if review_id != review_manifest["review_id"]:
        raise ReviewError("decision draft.review_id 与审核包不一致")
    raw_entries = review_manifest["entries"]
    assert isinstance(raw_entries, list)
    manifest_entries = {str(entry["proposal_id"]): entry for entry in raw_entries}
    raw_decisions = draft_document["decisions"]
    if not isinstance(raw_decisions, list):
        raise ReviewError("decision draft.decisions 必须是数组")
    seen_ids: set[str] = set()
    decisions: list[dict[str, object]] = []
    for index, raw_decision in enumerate(raw_decisions):
        context = f"decision draft.decisions[{index}]"
        if not isinstance(raw_decision, dict):
            raise ReviewError(f"{context} 必须是对象")
        require_exact_fields(
            raw_decision,
            {"proposal_id", "decision", "note"},
            context,
        )
        proposal_id = require_string(
            raw_decision["proposal_id"],
            f"{context}.proposal_id",
        )
        if proposal_id in seen_ids:
            raise ReviewError(f"同一 Proposal 存在多个决定：{proposal_id}")
        manifest_entry = manifest_entries.get(proposal_id)
        if manifest_entry is None:
            raise ReviewError(f"决定引用了不存在的 Proposal：{proposal_id}")
        seen_ids.add(proposal_id)
        decision = require_string(raw_decision["decision"], f"{context}.decision")
        if decision not in DECISION_VALUES:
            raise ReviewError(f"{context}.decision 只能是 approved 或 rejected")
        note_value = raw_decision["note"]
        if note_value is None:
            note = None
        elif isinstance(note_value, str) and note_value.strip():
            note = note_value.strip()
        else:
            raise ReviewError(f"{context}.note 必须是 null 或非空字符串")
        decisions.append(
            {
                "proposal_id": proposal_id,
                "proposal_sha256": manifest_entry["proposal_sha256"],
                "diff_sha256": manifest_entry["diff_sha256"],
                "decision": decision,
                "note": note,
            }
        )
    decisions.sort(key=lambda item: str(item["proposal_id"]))
    return {
        "format": DECISIONS_FORMAT,
        "review_id": review_id,
        "decisions": decisions,
    }


def validate_review_decisions(
    review_manifest: dict[str, object],
    decisions_document: dict[str, object],
) -> list[dict[str, object]]:
    """校验最终 decisions 与当前 review manifest 的哈希绑定。"""

    require_exact_fields(
        decisions_document,
        {"format", "review_id", "decisions"},
        "review decisions 顶层",
    )
    if decisions_document["format"] != DECISIONS_FORMAT:
        raise ReviewError("不支持的 review decisions 格式")
    review_id = require_string(
        decisions_document["review_id"],
        "review decisions.review_id",
    )
    if review_id != review_manifest["review_id"]:
        raise ReviewError("review decisions.review_id 与审核包不一致")
    raw_manifest_entries = review_manifest["entries"]
    assert isinstance(raw_manifest_entries, list)
    manifest_entries = {
        str(entry["proposal_id"]): entry for entry in raw_manifest_entries
    }
    raw_decisions = decisions_document["decisions"]
    if not isinstance(raw_decisions, list):
        raise ReviewError("review decisions.decisions 必须是数组")
    seen_ids: set[str] = set()
    decisions: list[dict[str, object]] = []
    for index, decision_entry in enumerate(raw_decisions):
        context = f"review decisions.decisions[{index}]"
        if not isinstance(decision_entry, dict):
            raise ReviewError(f"{context} 必须是对象")
        require_exact_fields(
            decision_entry,
            {
                "proposal_id",
                "proposal_sha256",
                "diff_sha256",
                "decision",
                "note",
            },
            context,
        )
        proposal_id = require_string(
            decision_entry["proposal_id"],
            f"{context}.proposal_id",
        )
        if proposal_id in seen_ids:
            raise ReviewError(f"同一 Proposal 存在多个最终决定：{proposal_id}")
        manifest_entry = manifest_entries.get(proposal_id)
        if manifest_entry is None:
            raise ReviewError(f"最终决定引用了不存在的 Proposal：{proposal_id}")
        seen_ids.add(proposal_id)
        if decision_entry["proposal_sha256"] != manifest_entry["proposal_sha256"]:
            raise ReviewError(f"{context}.proposal_sha256 与审核清单不一致")
        if decision_entry["diff_sha256"] != manifest_entry["diff_sha256"]:
            raise ReviewError(f"{context}.diff_sha256 与审核清单不一致")
        decision = require_string(decision_entry["decision"], f"{context}.decision")
        if decision not in DECISION_VALUES:
            raise ReviewError(f"{context}.decision 非法")
        note = decision_entry["note"]
        if note is not None and (not isinstance(note, str) or not note.strip()):
            raise ReviewError(f"{context}.note 必须是 null 或非空字符串")
        decisions.append(decision_entry)
    if [item["proposal_id"] for item in decisions] != sorted(seen_ids):
        raise ReviewError("review decisions 必须按 proposal_id 排序")
    return decisions


def create_decisions_file(
    review_directory: Path,
    draft_path: Path,
) -> dict[str, object]:
    """校验审核包和决定草稿后独占创建 decisions.json。"""

    output_path = review_directory / "decisions.json"
    if output_path.exists():
        raise ReviewError(f"审核决定文件已存在：{output_path}")
    review_manifest, _ = validate_review_package(review_directory)
    draft_document, _ = load_review_json(draft_path, "review decision draft")
    result = build_review_decisions(review_manifest, draft_document)
    try:
        knowledge_inventory.write_new_file(
            output_path,
            knowledge_inventory.serialize_json(result),
        )
    except knowledge_inventory.InventoryError as exc:
        raise ReviewError(str(exc)) from exc
    return result


def write_package_file(root: Path, relative_path: str, content: bytes) -> None:
    """在新建临时审核目录内独占写入一个文件。"""

    path = root.joinpath(*PurePosixPath(relative_path).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(content)
        output.flush()


def is_reserved_run_review_path(path: Path, knowledge_root: Path) -> bool:
    """判断审核目录是否为知识库内固定的 run/review 位置。"""

    try:
        relative = path.relative_to(knowledge_root)
    except ValueError:
        return False
    parts = relative.parts
    return (
        len(parts) == 4
        and parts[0] == ".knowledge-curator"
        and parts[1] == "runs"
        and len(parts[2]) <= 64
        and RUN_ID_PATTERN.fullmatch(parts[2]) is not None
        and parts[3] == "review"
    )


def create_review_package(
    knowledge_root: Path,
    inventory_path: Path,
    candidates_path: Path,
    retrieval_path: Path,
    proposals_path: Path,
    output_directory: Path,
) -> dict[str, object]:
    """完整生成审核包后再发布到尚不存在的目标目录。"""

    if output_directory.exists():
        raise ReviewError(f"审核目录已存在：{output_directory}")
    parent = output_directory.parent
    if not parent.is_dir():
        raise ReviewError(f"审核目录的父目录不存在：{parent}")
    try:
        resolved_root = knowledge_root.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise ReviewError("无法解析知识库或审核目录的父目录") from exc
    resolved_output = (resolved_parent / output_directory.name).resolve(strict=False)
    if knowledge_inventory.is_within(
        resolved_output,
        resolved_root,
    ) and not is_reserved_run_review_path(resolved_output, resolved_root):
        raise ReviewError(
            "知识库内的审核目录只能位于 "
            ".knowledge-curator/runs/<run_id>/review"
        )
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
    proposals_document, proposals_raw = knowledge_retrieval.load_json_document(
        proposals_path,
        "knowledge proposals",
    )
    manifest, artifacts = build_review_package(
        candidates_document,
        sha256_bytes(candidates_raw),
        inventory_document,
        sha256_bytes(inventory_raw),
        retrieval_document,
        sha256_bytes(retrieval_raw),
        proposals_document,
        sha256_bytes(proposals_raw),
        knowledge_root,
    )
    manifest_bytes = knowledge_inventory.serialize_json(manifest).encode("utf-8")

    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{output_directory.name}.tmp-", dir=parent)
    )
    try:
        write_package_file(temporary_directory, "proposals.json", proposals_raw)
        for relative_path, content in sorted(artifacts.items()):
            write_package_file(temporary_directory, relative_path, content)
        write_package_file(
            temporary_directory,
            "review-manifest.json",
            manifest_bytes,
        )
        if output_directory.exists():
            raise ReviewError(f"审核目录已存在：{output_directory}")
        temporary_directory.rename(output_directory)
    except (OSError, ReviewError):
        if temporary_directory.exists():
            shutil.rmtree(temporary_directory)
        raise
    return manifest


def create_parser() -> argparse.ArgumentParser:
    """创建非交互式审核包 CLI。"""

    parser = argparse.ArgumentParser(
        description="校验 knowledge proposals 并生成 after、diff 和审核清单。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser(
        "prepare",
        help="生成 review-manifest.v1 审核目录",
    )
    prepare_parser.add_argument("knowledge_root", type=Path)
    prepare_parser.add_argument("inventory_file", type=Path)
    prepare_parser.add_argument("candidates_file", type=Path)
    prepare_parser.add_argument("retrieval_file", type=Path)
    prepare_parser.add_argument("proposals_file", type=Path)
    prepare_parser.add_argument("output_directory", type=Path)
    decide_parser = subparsers.add_parser(
        "decide",
        help="校验用户决定并在审核目录中生成 decisions.json",
    )
    decide_parser.add_argument("review_directory", type=Path)
    decide_parser.add_argument("decision_draft", type=Path)
    report_parser = subparsers.add_parser(
        "report",
        help="校验审核包并输出供对话直接展示的章节级报告",
    )
    report_parser.add_argument("knowledge_root", type=Path)
    report_parser.add_argument("candidates_file", type=Path)
    report_parser.add_argument("review_directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行审核包生成并返回成功或失败退出码。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = create_review_package(
                args.knowledge_root,
                args.inventory_file,
                args.candidates_file,
                args.retrieval_file,
                args.proposals_file,
                args.output_directory,
            )
            entries = manifest["entries"]
            assert isinstance(entries, list)
            writable_count = sum(
                1
                for entry in entries
                if isinstance(entry, dict) and entry["after_path"] is not None
            )
            summary = {
                "status": "ok",
                "review_id": manifest["review_id"],
                "proposal_count": len(entries),
                "writable_count": writable_count,
                "output": str(args.output_directory.resolve()),
            }
        elif args.command == "report":
            summary = build_review_report(
                args.knowledge_root,
                args.candidates_file,
                args.review_directory,
            )
        elif args.command == "decide":
            decisions = create_decisions_file(
                args.review_directory,
                args.decision_draft,
            )
            raw_decisions = decisions["decisions"]
            assert isinstance(raw_decisions, list)
            summary = {
                "status": "ok",
                "review_id": decisions["review_id"],
                "decision_count": len(raw_decisions),
                "approved_count": sum(
                    1
                    for decision in raw_decisions
                    if isinstance(decision, dict)
                    and decision["decision"] == "approved"
                ),
                "rejected_count": sum(
                    1
                    for decision in raw_decisions
                    if isinstance(decision, dict)
                    and decision["decision"] == "rejected"
                ),
                "output": str((args.review_directory / "decisions.json").resolve()),
            }
        else:
            parser.error("未知命令")
    except (
        ReviewError,
        knowledge_proposals.ProposalError,
        knowledge_retrieval.RetrievalError,
        knowledge_inventory.InventoryError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(knowledge_inventory.serialize_json(summary), end="")
    return 0
