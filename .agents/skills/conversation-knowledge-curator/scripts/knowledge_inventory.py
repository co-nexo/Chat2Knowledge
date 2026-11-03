#!/usr/bin/env python3
"""只读扫描 Markdown 知识库并生成确定性的结构目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

FORMAT_VERSION = "conversation-knowledge-curator.knowledge-inventory.v1"
EXCLUDED_DIRECTORIES = {".git", ".knowledge-curator"}
ATX_HEADING_PATTERN = re.compile(
    r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$"
)
FENCE_OPEN_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class InventoryError(Exception):
    """表示知识目录输入、读取、解析或写入失败。"""


@dataclass(frozen=True)
class Heading:
    """保存一个已确认不在代码围栏中的 ATX 标题。"""

    level: int
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class SectionFragment:
    """保存章节定位信息和仅供当前进程检索的 Markdown 片段。"""

    section_id: str
    heading_path: tuple[str, ...]
    heading: str | None
    level: int
    ordinal: int
    markdown: str

    def inventory_entry(self) -> dict[str, object]:
        """转换为不包含正文的公开 inventory 章节条目。"""

        return {
            "section_id": self.section_id,
            "heading_path": list(self.heading_path),
            "heading": self.heading,
            "level": self.level,
            "ordinal": self.ordinal,
            "sha256": sha256_bytes(self.markdown.encode("utf-8")),
        }


def sha256_bytes(content: bytes) -> str:
    """返回字节内容的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(content).hexdigest()


def is_link_like(path: Path) -> bool:
    """判断路径是否为符号链接或当前 Python 支持识别的目录联接。"""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def is_within(path: Path, root: Path) -> bool:
    """判断解析后的路径是否位于指定根目录内。"""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def relative_path(path: Path, root: Path) -> str:
    """返回相对于知识库根目录的 POSIX 风格路径。"""

    return path.relative_to(root).as_posix()


def link_warning(path: Path, root: Path) -> dict[str, str]:
    """为被跳过的链接生成不含绝对路径的结构化警告。"""

    return {
        "code": "link_skipped",
        "path": relative_path(path, root),
        "message": "符号链接或目录联接未被扫描",
    }


def discover_markdown_files(
    knowledge_root: Path,
) -> tuple[list[Path], list[dict[str, str]]]:
    """安全发现知识库内的 Markdown 文件并报告被跳过的链接。"""

    try:
        root = knowledge_root.resolve(strict=True)
    except OSError as exc:
        raise InventoryError(
            f"无法解析知识库根目录：{knowledge_root}"
        ) from exc
    if not root.is_dir():
        raise InventoryError(f"知识库根路径不是目录：{knowledge_root}")
    if is_link_like(knowledge_root):
        raise InventoryError("知识库根目录不能是符号链接或目录联接")

    discovered: list[Path] = []
    warnings: list[dict[str, str]] = []

    def on_walk_error(exc: OSError) -> None:
        raise InventoryError(f"扫描知识库失败：{exc}") from exc

    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=on_walk_error,
    ):
        current_path = Path(current)
        try:
            resolved_current = current_path.resolve(strict=True)
        except OSError as exc:
            raise InventoryError(
                f"无法解析目录：{relative_path(current_path, root)}"
            ) from exc
        if not is_within(resolved_current, root):
            raise InventoryError("扫描路径超出知识库根目录")

        safe_directories: list[str] = []
        for name in sorted(
            directory_names,
            key=lambda value: (value.casefold(), value),
        ):
            if name in EXCLUDED_DIRECTORIES:
                continue
            child = current_path / name
            if is_link_like(child):
                warnings.append(link_warning(child, root))
                continue
            try:
                resolved_child = child.resolve(strict=True)
            except OSError as exc:
                raise InventoryError(
                    f"无法解析目录：{relative_path(child, root)}"
                ) from exc
            if not is_within(resolved_child, root):
                raise InventoryError(
                    f"目录超出知识库根目录：{relative_path(child, root)}"
                )
            safe_directories.append(name)
        directory_names[:] = safe_directories

        for name in sorted(
            file_names,
            key=lambda value: (value.casefold(), value),
        ):
            child = current_path / name
            if child.suffix.casefold() != ".md":
                continue
            if is_link_like(child):
                warnings.append(link_warning(child, root))
                continue
            try:
                resolved_child = child.resolve(strict=True)
            except OSError as exc:
                raise InventoryError(
                    f"无法解析文件：{relative_path(child, root)}"
                ) from exc
            if not is_within(resolved_child, root):
                raise InventoryError(
                    f"文件超出知识库根目录：{relative_path(child, root)}"
                )
            if not resolved_child.is_file():
                raise InventoryError(
                    f"Markdown 路径不是普通文件：{relative_path(child, root)}"
                )
            discovered.append(resolved_child)

    discovered.sort(
        key=lambda path: (
            relative_path(path, root).casefold(),
            relative_path(path, root),
        )
    )
    warnings.sort(
        key=lambda warning: (
            warning["path"].casefold(),
            warning["path"],
        )
    )
    return discovered, warnings


def strip_line_ending(line: str) -> str:
    """移除单行末尾的换行符，同时保留其他字符。"""

    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line


def parse_heading_text(raw_text: str) -> str:
    """按 ATX 规则移除可选的闭合井号和周围空白。"""

    without_closing = re.sub(r"[ \t]+#+[ \t]*$", "", raw_text)
    return without_closing.strip(" \t")


def parse_headings(text: str) -> list[Heading]:
    """识别不在反引号或波浪线代码围栏中的 ATX 标题。"""

    headings: list[Heading] = []
    offset = 0
    fence_marker: str | None = None
    fence_length = 0

    for line in text.splitlines(keepends=True):
        line_without_ending = strip_line_ending(line)
        fence_match = FENCE_OPEN_PATTERN.match(line_without_ending)
        if fence_marker is not None:
            if fence_match is not None:
                marker = fence_match.group(1)
                remainder = line_without_ending[fence_match.end() :]
                if (
                    marker[0] == fence_marker
                    and len(marker) >= fence_length
                    and not remainder.strip(" \t")
                ):
                    fence_marker = None
                    fence_length = 0
            offset += len(line)
            continue

        if fence_match is not None:
            marker = fence_match.group(1)
            fence_marker = marker[0]
            fence_length = len(marker)
            offset += len(line)
            continue

        heading_match = ATX_HEADING_PATTERN.match(line_without_ending)
        if heading_match is not None:
            marker = heading_match.group(1)
            raw_text = heading_match.group(2) or ""
            headings.append(
                Heading(
                    level=len(marker),
                    text=parse_heading_text(raw_text),
                    start=offset,
                    end=offset + len(line),
                )
            )
        offset += len(line)

    return headings


def section_id(
    document_path: str,
    heading_path: Sequence[str],
    ordinal: int,
) -> str:
    """根据相对路径、标题路径和序号生成稳定章节 ID。"""

    encoded_path = json.dumps(
        list(heading_path),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    identity = f"{document_path}\0{encoded_path}\0{ordinal}".encode()
    return f"ks_{sha256_bytes(identity)[:20]}"


def parse_section_fragments(
    text: str,
    document_path: str,
) -> list[SectionFragment]:
    """切分章节并保留供检索使用、不会写入 inventory 的正文片段。"""

    headings = parse_headings(text)
    sections: list[SectionFragment] = []
    path_ordinals: dict[tuple[str, ...], int] = {}

    first_heading_start = headings[0].start if headings else len(text)
    root_text = text[:first_heading_start]
    if root_text.strip():
        sections.append(
            SectionFragment(
                section_id=section_id(document_path, [], 0),
                heading_path=(),
                heading=None,
                level=0,
                ordinal=0,
                markdown=root_text,
            )
        )

    heading_stack: list[Heading] = []
    for index, heading in enumerate(headings):
        while heading_stack and heading_stack[-1].level >= heading.level:
            heading_stack.pop()
        heading_path = tuple(
            [stacked.text for stacked in heading_stack] + [heading.text]
        )
        ordinal = path_ordinals.get(heading_path, 0)
        path_ordinals[heading_path] = ordinal + 1
        next_start = (
            headings[index + 1].start
            if index + 1 < len(headings)
            else len(text)
        )
        fragment = text[heading.start:next_start]
        sections.append(
            SectionFragment(
                section_id=section_id(
                    document_path,
                    heading_path,
                    ordinal,
                ),
                heading_path=heading_path,
                heading=heading.text,
                level=heading.level,
                ordinal=ordinal,
                markdown=fragment,
            )
        )
        heading_stack.append(heading)

    return sections


def parse_sections(text: str, document_path: str) -> list[dict[str, object]]:
    """把 Markdown 转换为不包含正文的公开 inventory 章节条目。"""

    return [
        fragment.inventory_entry()
        for fragment in parse_section_fragments(text, document_path)
    ]


def decode_markdown(raw_content: bytes, document_path: str) -> str:
    """解码 UTF-8 Markdown，并允许文件开头存在 BOM。"""

    try:
        return raw_content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InventoryError(
            f"Markdown 不是有效 UTF-8：{document_path}"
        ) from exc


def build_file_entry(path: Path, root: Path) -> dict[str, object]:
    """读取一个 Markdown 文件并生成 inventory 文件条目。"""

    document_path = relative_path(path, root)
    try:
        raw_content = path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"无法读取 Markdown：{document_path}") from exc
    text = decode_markdown(raw_content, document_path)
    headings = parse_headings(text)
    title = next(
        (
            heading.text
            for heading in headings
            if heading.level == 1 and heading.text
        ),
        path.stem,
    )
    return {
        "path": document_path,
        "sha256": sha256_bytes(raw_content),
        "title": title,
        "sections": parse_sections(text, document_path),
    }


def build_inventory(knowledge_root: Path) -> dict[str, object]:
    """为知识库生成只读、确定性的 inventory 文档。"""

    files, warnings = discover_markdown_files(knowledge_root)
    try:
        root = knowledge_root.resolve(strict=True)
    except OSError as exc:
        raise InventoryError(
            f"无法解析知识库根目录：{knowledge_root}"
        ) from exc
    entries = [build_file_entry(path, root) for path in files]
    return {
        "format": FORMAT_VERSION,
        "status": "completed_with_issues" if warnings else "ok",
        "files": entries,
        "warnings": warnings,
    }


def serialize_json(value: object) -> str:
    """按仓库约定序列化稳定、可读且保留中文的 JSON。"""

    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_new_file(path: Path, content: str) -> None:
    """以独占模式创建完整输出，失败时清理且绝不覆盖已有文件。"""

    parent = path.parent
    if not parent.is_dir():
        raise InventoryError(f"输出父目录不存在：{parent}")
    if path.exists():
        raise InventoryError(f"输出文件已存在：{path}")

    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError as exc:
        raise InventoryError(f"输出文件已存在：{path}") from exc
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise InventoryError(f"无法创建输出文件：{path}") from exc


def create_inventory_file(
    knowledge_root: Path,
    output_path: Path,
) -> dict[str, object]:
    """在全部扫描成功后写入一个新的 inventory 文件。"""

    if output_path.exists():
        raise InventoryError(f"输出文件已存在：{output_path}")
    inventory = build_inventory(knowledge_root)
    write_new_file(output_path, serialize_json(inventory))
    return inventory


def create_parser() -> argparse.ArgumentParser:
    """创建非交互式命令行解析器。"""

    parser = argparse.ArgumentParser(
        description="只读扫描 Markdown 知识库并生成结构目录。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser(
        "inventory",
        help="生成 knowledge-inventory.v1 JSON 文件",
    )
    inventory_parser.add_argument("knowledge_root", type=Path)
    inventory_parser.add_argument("output_file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行 CLI，并按契约返回成功、带警告或失败退出码。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "inventory":
            parser.error("未知命令")
        inventory = create_inventory_file(
            args.knowledge_root,
            args.output_file,
        )
    except (InventoryError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    section_count = sum(
        len(entry["sections"])
        for entry in inventory["files"]
        if isinstance(entry, dict)
    )
    summary = {
        "status": inventory["status"],
        "file_count": len(inventory["files"]),
        "section_count": section_count,
        "warning_count": len(inventory["warnings"]),
        "output": str(args.output_file.resolve()),
    }
    print(serialize_json(summary), end="")
    return 1 if inventory["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
