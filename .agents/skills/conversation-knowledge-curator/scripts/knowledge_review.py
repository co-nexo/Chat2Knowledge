#!/usr/bin/env python3
"""校验知识提案并生成不修改知识库的审核包。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

import knowledge_inventory
import knowledge_proposals
import knowledge_retrieval

FORMAT_VERSION = "conversation-knowledge-curator.review-manifest.v1"
UTF8_BOM = b"\xef\xbb\xbf"
WRITABLE_ACTIONS = {"add", "merge"}


class ReviewError(Exception):
    """表示审核包输入、修改模拟或输出不符合契约。"""


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


def write_package_file(root: Path, relative_path: str, content: bytes) -> None:
    """在新建临时审核目录内独占写入一个文件。"""

    path = root.joinpath(*PurePosixPath(relative_path).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(content)
        output.flush()


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
    if knowledge_inventory.is_within(resolved_output, resolved_root):
        raise ReviewError("审核目录不得位于正式知识库内部")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行审核包生成并返回成功或失败退出码。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "prepare":
            parser.error("未知命令")
        manifest = create_review_package(
            args.knowledge_root,
            args.inventory_file,
            args.candidates_file,
            args.retrieval_file,
            args.proposals_file,
            args.output_directory,
        )
    except (
        ReviewError,
        knowledge_proposals.ProposalError,
        knowledge_retrieval.RetrievalError,
        knowledge_inventory.InventoryError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    entries = manifest["entries"]
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
    print(knowledge_inventory.serialize_json(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
