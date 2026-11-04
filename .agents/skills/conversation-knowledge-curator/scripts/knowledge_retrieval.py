#!/usr/bin/env python3
"""校验知识快照并为知识候选生成确定性的关键词召回结果。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import knowledge_inventory

FORMAT_VERSION = "conversation-knowledge-curator.knowledge-retrieval.v2"
CANDIDATES_FORMAT = "conversation-knowledge-curator.knowledge-candidates.v1"
INVENTORY_FORMAT = "conversation-knowledge-curator.knowledge-inventory.v1"
ALLOWED_FLAGS = {
    "assistant_only",
    "uncertain",
    "time_sensitive",
    "sensitive",
}
RESERVED_PARTS = {".git", ".knowledge-curator"}
LATIN_TERM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\.[A-Za-z][A-Za-z0-9]*|"
    r"[A-Za-z0-9]+(?:\+[A-Za-z0-9]+)+|"
    r"[A-Za-z0-9]+(?:\+\+|#)?"
    r")"
)
CJK_TERM_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


class RetrievalError(Exception):
    """表示召回输入、知识库快照或输出不符合契约。"""


@dataclass(frozen=True)
class SearchSection:
    """保存一个已通过 inventory 哈希校验的可搜索章节。"""

    path: str
    file_sha256: str
    title: str
    section_id: str
    section_sha256: str
    heading_path: tuple[str, ...]
    order: int
    markdown: str


def require_exact_fields(
    value: dict[str, object],
    expected: set[str],
    context: str,
) -> None:
    """拒绝缺失或契约未声明的 JSON 字段。"""

    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise RetrievalError(
            f"{context} 字段不符合契约；缺失={missing}，未知={unknown}"
        )


def require_string(value: object, context: str) -> str:
    """返回非空字符串，否则报告带上下文的契约错误。"""

    if not isinstance(value, str) or not value.strip():
        raise RetrievalError(f"{context} 必须是非空字符串")
    return value


def require_sha256(value: object, context: str) -> str:
    """校验小写 SHA-256 十六进制字符串。"""

    text = require_string(value, context)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise RetrievalError(f"{context} 必须是小写 SHA-256")
    return text


def load_json_document(path: Path, context: str) -> tuple[dict[str, object], bytes]:
    """读取 UTF-8 JSON 对象并保留原始字节供输入哈希使用。"""

    try:
        raw_content = path.read_bytes()
    except OSError as exc:
        raise RetrievalError(f"无法读取{context}：{path}") from exc
    try:
        value = json.loads(raw_content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalError(f"{context}不是有效 UTF-8 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise RetrievalError(f"{context}顶层必须是对象")
    return value, raw_content


def validate_evidence(value: object, context: str) -> None:
    """校验候选 Evidence 的最小公开结构。"""

    if not isinstance(value, dict):
        raise RetrievalError(f"{context} 必须是对象")
    require_exact_fields(
        value,
        {"source_sha256", "conversation_id", "message_ids"},
        context,
    )
    require_sha256(value["source_sha256"], f"{context}.source_sha256")
    require_string(value["conversation_id"], f"{context}.conversation_id")
    message_ids = value["message_ids"]
    if not isinstance(message_ids, list) or not message_ids:
        raise RetrievalError(f"{context}.message_ids 必须是非空数组")
    for index, message_id in enumerate(message_ids):
        require_string(message_id, f"{context}.message_ids[{index}]")


def validate_candidates(document: dict[str, object]) -> list[dict[str, object]]:
    """校验最终化知识候选并返回按 ID 排序的候选列表。"""

    require_exact_fields(document, {"format", "candidates"}, "候选顶层")
    if document["format"] != CANDIDATES_FORMAT:
        raise RetrievalError("不支持的 knowledge candidates 格式")
    raw_candidates = document["candidates"]
    if not isinstance(raw_candidates, list):
        raise RetrievalError("candidates 必须是数组")

    candidates: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, candidate in enumerate(raw_candidates):
        context = f"candidates[{index}]"
        if not isinstance(candidate, dict):
            raise RetrievalError(f"{context} 必须是对象")
        require_exact_fields(
            candidate,
            {"candidate_id", "statement", "evidence", "flags"},
            context,
        )
        candidate_id = require_string(
            candidate["candidate_id"], f"{context}.candidate_id"
        )
        if not re.fullmatch(r"kc_[0-9a-f]{20}", candidate_id):
            raise RetrievalError(f"{context}.candidate_id 格式非法")
        if candidate_id in seen_ids:
            raise RetrievalError(f"candidate_id 重复：{candidate_id}")
        seen_ids.add(candidate_id)
        require_string(candidate["statement"], f"{context}.statement")

        evidence = candidate["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise RetrievalError(f"{context}.evidence 必须是非空数组")
        for evidence_index, item in enumerate(evidence):
            validate_evidence(item, f"{context}.evidence[{evidence_index}]")

        flags = candidate["flags"]
        if not isinstance(flags, list) or any(
            not isinstance(flag, str) or flag not in ALLOWED_FLAGS
            for flag in flags
        ):
            raise RetrievalError(f"{context}.flags 包含非法值")
        if len(flags) != len(set(flags)):
            raise RetrievalError(f"{context}.flags 不得重复")
        candidates.append(candidate)

    candidates.sort(key=lambda item: str(item["candidate_id"]))
    return candidates


def validate_warning(value: object, context: str) -> dict[str, str]:
    """校验 inventory 警告并返回收窄后的字符串字典。"""

    if not isinstance(value, dict):
        raise RetrievalError(f"{context} 必须是对象")
    require_exact_fields(value, {"code", "path", "message"}, context)
    warning_path = require_string(value["path"], f"{context}.path")
    if "\\" in warning_path or ":" in warning_path:
        raise RetrievalError(f"{context}.path 不是安全相对路径")
    parsed_path = PurePosixPath(warning_path)
    if not parsed_path.parts or parsed_path.is_absolute() or any(
        part in {"", ".", ".."} for part in parsed_path.parts
    ):
        raise RetrievalError(f"{context}.path 不是安全相对路径")
    return {
        "code": require_string(value["code"], f"{context}.code"),
        "path": warning_path,
        "message": require_string(value["message"], f"{context}.message"),
    }


def safe_inventory_path(path_value: object) -> PurePosixPath:
    """校验 inventory 中可移植且不能逃逸知识库的相对 Markdown 路径。"""

    text = require_string(path_value, "inventory file.path")
    if "\\" in text or ":" in text:
        raise RetrievalError(f"inventory 路径不是安全 POSIX 相对路径：{text}")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts:
        raise RetrievalError(f"inventory 路径必须是相对路径：{text}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RetrievalError(f"inventory 路径包含非法片段：{text}")
    if any(part in RESERVED_PARTS for part in path.parts):
        raise RetrievalError(f"inventory 路径进入保留目录：{text}")
    if path.suffix.casefold() != ".md":
        raise RetrievalError(f"inventory 路径不是 Markdown：{text}")
    return path


def resolve_markdown_path(knowledge_root: Path, path: PurePosixPath) -> Path:
    """在不跟随链接越界的前提下解析 inventory Markdown 路径。"""

    current = knowledge_root
    for part in path.parts:
        current = current / part
        if knowledge_inventory.is_link_like(current):
            raise RetrievalError(f"inventory 路径包含链接：{path.as_posix()}")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise RetrievalError(f"inventory 文件不存在：{path.as_posix()}") from exc
    if not knowledge_inventory.is_within(resolved, knowledge_root):
        raise RetrievalError(f"inventory 路径超出知识库：{path.as_posix()}")
    if not resolved.is_file():
        raise RetrievalError(f"inventory 路径不是普通文件：{path.as_posix()}")
    return resolved


def validate_inventory(
    document: dict[str, object],
    knowledge_root: Path,
) -> tuple[list[SearchSection], list[dict[str, str]], str]:
    """校验 inventory 与当前 Markdown 快照一致并构造搜索章节。"""

    require_exact_fields(
        document,
        {"format", "status", "files", "warnings"},
        "inventory 顶层",
    )
    if document["format"] != INVENTORY_FORMAT:
        raise RetrievalError("不支持的 knowledge inventory 格式")
    status = document["status"]
    if status not in {"ok", "completed_with_issues"}:
        raise RetrievalError("inventory.status 非法")
    raw_warnings = document["warnings"]
    if not isinstance(raw_warnings, list):
        raise RetrievalError("inventory.warnings 必须是数组")
    warnings = [
        validate_warning(value, f"inventory.warnings[{index}]")
        for index, value in enumerate(raw_warnings)
    ]
    if (status == "ok" and warnings) or (
        status == "completed_with_issues" and not warnings
    ):
        raise RetrievalError("inventory.status 与 warnings 不一致")

    raw_files = document["files"]
    if not isinstance(raw_files, list):
        raise RetrievalError("inventory.files 必须是数组")
    try:
        resolved_root = knowledge_root.resolve(strict=True)
    except OSError as exc:
        raise RetrievalError(f"无法解析知识库根目录：{knowledge_root}") from exc
    if not resolved_root.is_dir() or knowledge_inventory.is_link_like(
        knowledge_root
    ):
        raise RetrievalError("知识库根目录无效或属于链接")

    sections: list[SearchSection] = []
    seen_paths: set[str] = set()
    seen_section_ids: set[str] = set()
    for file_index, file_entry in enumerate(raw_files):
        context = f"inventory.files[{file_index}]"
        if not isinstance(file_entry, dict):
            raise RetrievalError(f"{context} 必须是对象")
        require_exact_fields(
            file_entry,
            {"path", "sha256", "title", "sections"},
            context,
        )
        posix_path = safe_inventory_path(file_entry["path"])
        path_text = posix_path.as_posix()
        if path_text in seen_paths:
            raise RetrievalError(f"inventory 文件路径重复：{path_text}")
        seen_paths.add(path_text)
        require_sha256(file_entry["sha256"], f"{context}.sha256")
        title = require_string(file_entry["title"], f"{context}.title")
        markdown_path = resolve_markdown_path(resolved_root, posix_path)
        actual_entry = knowledge_inventory.build_file_entry(
            markdown_path,
            resolved_root,
        )
        if actual_entry != file_entry:
            raise RetrievalError(f"知识库文件已不同于 inventory：{path_text}")

        raw_sections = file_entry["sections"]
        if not isinstance(raw_sections, list):
            raise RetrievalError(f"{context}.sections 必须是数组")
        raw_content = markdown_path.read_bytes()
        text = knowledge_inventory.decode_markdown(raw_content, path_text)
        fragments = knowledge_inventory.parse_section_fragments(text, path_text)
        if len(fragments) != len(raw_sections):
            raise RetrievalError(f"inventory 章节数量已失效：{path_text}")
        for order, (section_entry, fragment) in enumerate(
            zip(raw_sections, fragments, strict=True)
        ):
            section_context = f"{context}.sections[{order}]"
            if not isinstance(section_entry, dict):
                raise RetrievalError(f"{section_context} 必须是对象")
            require_exact_fields(
                section_entry,
                {
                    "section_id",
                    "heading_path",
                    "heading",
                    "level",
                    "ordinal",
                    "sha256",
                },
                section_context,
            )
            section_id = require_string(
                section_entry["section_id"],
                f"{section_context}.section_id",
            )
            if section_id in seen_section_ids:
                raise RetrievalError(f"section_id 重复：{section_id}")
            seen_section_ids.add(section_id)
            section_sha256 = require_sha256(
                section_entry["sha256"],
                f"{section_context}.sha256",
            )
            heading_path = section_entry["heading_path"]
            if not isinstance(heading_path, list) or any(
                not isinstance(item, str) for item in heading_path
            ):
                raise RetrievalError(
                    f"{section_context}.heading_path 必须是字符串数组"
                )
            sections.append(
                SearchSection(
                    path=path_text,
                    file_sha256=str(file_entry["sha256"]),
                    title=title,
                    section_id=section_id,
                    section_sha256=section_sha256,
                    heading_path=tuple(heading_path),
                    order=order,
                    markdown=fragment.markdown,
                )
            )
    return sections, warnings, str(status)


def query_terms(statement: str) -> list[str]:
    """从候选陈述提取稳定的英文技术词和中文相邻双字词。"""

    terms = {match.group(0).casefold() for match in LATIN_TERM_PATTERN.finditer(statement)}
    for match in CJK_TERM_PATTERN.finditer(statement):
        value = match.group(0)
        if len(value) >= 2:
            terms.update(value[index : index + 2] for index in range(len(value) - 1))
    return sorted(term for term in terms if len(term) >= 2)


def score_section(
    section: SearchSection,
    terms: list[str],
) -> tuple[int, list[str]]:
    """按完整词命中和公开权重计算一个章节的字面召回分数。"""

    # 查询和被检索字段使用同一套词项规则，避免 SQL 命中 NoSQL 等子串误判。
    path_terms = set(query_terms(section.path))
    title_terms = set(query_terms(section.title))
    heading_terms = set(query_terms(" ".join(section.heading_path)))
    body_terms = set(query_terms(section.markdown))
    score = 0
    matched: list[str] = []
    for term in terms:
        term_score = 0
        if term in path_terms:
            term_score += 8
        if term in title_terms:
            term_score += 6
        if term in heading_terms:
            term_score += 5
        if term in body_terms:
            term_score += 1
        if term_score:
            score += term_score
            matched.append(term)
    return score, matched


def build_retrieval(
    candidates_document: dict[str, object],
    candidates_sha256: str,
    inventory_document: dict[str, object],
    inventory_sha256: str,
    knowledge_root: Path,
    limit: int,
) -> dict[str, object]:
    """校验输入和知识快照后生成不复制正文的召回结果。"""

    if not 1 <= limit <= 20:
        raise RetrievalError("limit 必须在 1 到 20 之间")
    require_sha256(candidates_sha256, "knowledge_candidates_sha256")
    require_sha256(inventory_sha256, "knowledge_inventory_sha256")
    candidates = validate_candidates(candidates_document)
    sections, warnings, status = validate_inventory(
        inventory_document,
        knowledge_root,
    )
    results: list[dict[str, object]] = []
    for candidate in candidates:
        terms = query_terms(str(candidate["statement"]))
        ranked: list[tuple[int, str, int, str, dict[str, object]]] = []
        for section in sections:
            score, matched_terms = score_section(section, terms)
            if score == 0:
                continue
            match = {
                "path": section.path,
                "file_sha256": section.file_sha256,
                "section_id": section.section_id,
                "section_sha256": section.section_sha256,
                "heading_path": list(section.heading_path),
                "score": score,
                "matched_terms": matched_terms,
            }
            ranked.append(
                (
                    -score,
                    section.path.casefold(),
                    section.order,
                    section.section_id,
                    match,
                )
            )
        ranked.sort(key=lambda item: item[:4])
        results.append(
            {
                "candidate_id": candidate["candidate_id"],
                "matches": [item[4] for item in ranked[:limit]],
            }
        )

    return {
        "format": FORMAT_VERSION,
        "status": status,
        "inputs": {
            "knowledge_candidates_sha256": candidates_sha256,
            "knowledge_inventory_sha256": inventory_sha256,
        },
        "parameters": {"limit": limit},
        "results": results,
        "warnings": warnings,
    }


def create_retrieval_file(
    knowledge_root: Path,
    inventory_path: Path,
    candidates_path: Path,
    output_path: Path,
    limit: int,
) -> dict[str, object]:
    """生成召回结果并在全部校验成功后创建新输出文件。"""

    if output_path.exists():
        raise RetrievalError(f"输出文件已存在：{output_path}")
    inventory_document, inventory_raw = load_json_document(
        inventory_path,
        "knowledge inventory",
    )
    candidates_document, candidates_raw = load_json_document(
        candidates_path,
        "knowledge candidates",
    )
    result = build_retrieval(
        candidates_document,
        knowledge_inventory.sha256_bytes(candidates_raw),
        inventory_document,
        knowledge_inventory.sha256_bytes(inventory_raw),
        knowledge_root,
        limit,
    )
    knowledge_inventory.write_new_file(
        output_path,
        knowledge_inventory.serialize_json(result),
    )
    return result


def create_parser() -> argparse.ArgumentParser:
    """创建非交互式召回 CLI。"""

    parser = argparse.ArgumentParser(
        description="校验知识快照并为知识候选生成关键词召回结果。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    retrieve_parser = subparsers.add_parser(
        "retrieve",
        help="生成 knowledge-retrieval.v2 JSON 文件",
    )
    retrieve_parser.add_argument("knowledge_root", type=Path)
    retrieve_parser.add_argument("inventory_file", type=Path)
    retrieve_parser.add_argument("candidates_file", type=Path)
    retrieve_parser.add_argument("output_file", type=Path)
    retrieve_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="每个候选最多保留的章节数，默认为 20，允许 1 到 20",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行关键词召回并返回成功、带警告或失败退出码。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "retrieve":
            parser.error("未知命令")
        result = create_retrieval_file(
            args.knowledge_root,
            args.inventory_file,
            args.candidates_file,
            args.output_file,
            args.limit,
        )
    except (RetrievalError, knowledge_inventory.InventoryError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    match_count = sum(
        len(item["matches"])
        for item in result["results"]
        if isinstance(item, dict)
    )
    summary = {
        "status": result["status"],
        "candidate_count": len(result["results"]),
        "match_count": match_count,
        "warning_count": len(result["warnings"]),
        "output": str(args.output_file.resolve()),
    }
    print(knowledge_inventory.serialize_json(summary), end="")
    return 1 if result["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
