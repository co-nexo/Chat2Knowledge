#!/usr/bin/env python3
"""检查知识文档的标题层级与编号等确定性质量规则。"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import knowledge_inventory

REPORT_FORMAT = "conversation-knowledge-curator.document-quality.v1"
CHECK_MODES = {"compatible", "strict"}
NUMBER_PATTERNS = {
    2: re.compile(r"^(?P<first>[1-9]\d*)\.\s+\S"),
    3: re.compile(r"^(?P<first>[1-9]\d*)\.(?P<second>[1-9]\d*)\s+\S"),
    4: re.compile(
        r"^(?P<first>[1-9]\d*)\.(?P<second>[1-9]\d*)\."
        r"(?P<third>[1-9]\d*)\s+\S"
    ),
}
H1_NUMBER_PATTERN = re.compile(r"^[1-9]\d*\.\s+")


class DocumentQualityError(Exception):
    """表示检查输入无法读取或检查模式不受支持。"""


def line_number(text: str, offset: int) -> int:
    """把字符偏移转换为从 1 开始的行号。"""

    return text.count("\n", 0, offset) + 1


def issue(
    code: str,
    message: str,
    line: int | None = None,
) -> dict[str, object]:
    """构造稳定、可序列化的质量问题。"""

    return {"code": code, "line": line, "message": message}


def check_heading_structure(
    text: str,
    headings: list[knowledge_inventory.Heading],
) -> list[dict[str, object]]:
    """检查所有模式共享的 H1、层级跳跃和最大深度规则。"""

    issues: list[dict[str, object]] = []
    if not headings:
        return [issue("missing_heading", "文档必须至少包含一个 ATX 标题。")]

    first = headings[0]
    if first.level != 1:
        issues.append(
            issue(
                "first_heading_not_h1",
                "文档的第一个标题必须是 H1 主标题。",
                line_number(text, first.start),
            )
        )

    h1_headings = [heading for heading in headings if heading.level == 1]
    if len(h1_headings) != 1:
        line = (
            line_number(text, h1_headings[1].start)
            if len(h1_headings) > 1
            else None
        )
        issues.append(
            issue(
                "invalid_h1_count",
                f"文档必须且只能包含一个 H1，实际为 {len(h1_headings)} 个。",
                line,
            )
        )

    previous_level = headings[0].level
    for heading in headings:
        current_line = line_number(text, heading.start)
        if heading.level > 4:
            issues.append(
                issue(
                    "heading_too_deep",
                    "知识文档最多使用到 H4；请合并或重组过深章节。",
                    current_line,
                )
            )
        if heading is not headings[0] and heading.level > previous_level + 1:
            issues.append(
                issue(
                    "heading_level_jump",
                    f"标题从 H{previous_level} 跳到 H{heading.level}。",
                    current_line,
                )
            )
        previous_level = heading.level
    return issues


def check_strict_numbering(
    text: str,
    headings: list[knowledge_inventory.Heading],
) -> list[dict[str, object]]:
    """检查新建文档的 H1 位置及 H2 到 H4 连续编号。"""

    issues: list[dict[str, object]] = []
    first_nonempty = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        "",
    )
    if not first_nonempty.startswith("# "):
        issues.append(
            issue(
                "content_before_h1",
                "新建文档的首个非空行必须是 H1 主标题。",
                1,
            )
        )

    if headings and headings[0].level == 1 and H1_NUMBER_PATTERN.match(
        headings[0].text
    ):
        issues.append(
            issue(
                "numbered_h1",
                "H1 主标题不得编号。",
                line_number(text, headings[0].start),
            )
        )

    if not any(heading.level == 2 for heading in headings):
        issues.append(
            issue(
                "missing_h2",
                "新建知识文档必须至少包含一个带编号的 H2 章节。",
            )
        )

    counters = {2: 0, 3: 0, 4: 0}
    for heading in headings:
        if heading.level not in NUMBER_PATTERNS:
            continue
        current_line = line_number(text, heading.start)
        match = NUMBER_PATTERNS[heading.level].match(heading.text)
        if match is None:
            issues.append(
                issue(
                    "invalid_heading_number",
                    f"H{heading.level} 标题必须使用对应层级编号。",
                    current_line,
                )
            )
            continue

        numbers = [int(value) for value in match.groupdict().values() if value]
        expected: list[int]
        if heading.level == 2:
            expected = [counters[2] + 1]
        elif heading.level == 3:
            expected = [counters[2], counters[3] + 1]
        else:
            expected = [counters[2], counters[3], counters[4] + 1]
        if numbers != expected:
            rendered = ".".join(str(value) for value in expected)
            if heading.level == 2:
                rendered += "."
            issues.append(
                issue(
                    "nonsequential_heading_number",
                    f"H{heading.level} 编号应为 {rendered}。",
                    current_line,
                )
            )

        if heading.level == 2:
            counters[2] = numbers[0]
            counters[3] = 0
            counters[4] = 0
        elif heading.level == 3:
            counters[3] = numbers[1]
            counters[4] = 0
        else:
            counters[4] = numbers[2]
    return issues


def analyze_document(
    text: str,
    mode: str,
    source: str,
) -> dict[str, object]:
    """返回单篇 Markdown 的确定性质量检查报告。"""

    if mode not in CHECK_MODES:
        raise DocumentQualityError(f"不支持的检查模式：{mode}")
    headings = knowledge_inventory.parse_headings(text)
    issues = check_heading_structure(text, headings)
    if mode == "strict":
        issues.extend(check_strict_numbering(text, headings))
    issues.sort(
        key=lambda item: (
            item["line"] is None,
            item["line"] if isinstance(item["line"], int) else 0,
            str(item["code"]),
        )
    )
    return {
        "format": REPORT_FORMAT,
        "status": "ok" if not issues else "failed",
        "source": source,
        "mode": mode,
        "summary": {
            "heading_count": len(headings),
            "issue_count": len(issues),
        },
        "issues": issues,
    }


def check_file(path: Path, mode: str) -> dict[str, object]:
    """读取一个 UTF-8 Markdown 文件并返回质量报告。"""

    if path.suffix.casefold() != ".md":
        raise DocumentQualityError("检查目标必须是 .md 文件")
    try:
        raw = path.read_bytes()
        text = knowledge_inventory.decode_markdown(raw, str(path))
    except (OSError, knowledge_inventory.InventoryError) as exc:
        raise DocumentQualityError(f"无法读取 Markdown：{path}：{exc}") from exc
    return analyze_document(text, mode, str(path.resolve()))


def create_parser() -> argparse.ArgumentParser:
    """创建中文命令行解析器。"""

    parser = argparse.ArgumentParser(
        description="检查知识文档的标题层级与编号，不修改原文件。"
    )
    parser.add_argument("command", choices=("check",))
    parser.add_argument("markdown_file", type=Path)
    parser.add_argument(
        "--mode",
        choices=sorted(CHECK_MODES),
        default="strict",
        help="strict 用于新建文档；compatible 用于既有文档。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行只读检查；质量不合格或输入失败时返回 2。"""

    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        report = check_file(args.markdown_file, args.mode)
    except DocumentQualityError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(knowledge_inventory.serialize_json(report), end="")
    return 0 if report["status"] == "ok" else 2

