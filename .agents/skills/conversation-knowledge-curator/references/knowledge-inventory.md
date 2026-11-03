# knowledge inventory 格式契约

当前格式版本：`conversation-knowledge-curator.knowledge-inventory.v1`

Knowledge inventory 是对指定 Markdown 知识库的只读快照，用于召回相关文档、定位章节和在生成修改前绑定当前内容。它是可重新生成的私密运行数据，不是正式知识内容。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 记录 Markdown 文件、标题、章节定位和内容哈希，不复制正文 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.knowledge-inventory.v1",
  "files": []
}
```

顶层只包含 `format` 和 `files`。文件按规范化相对路径稳定排序；相同输入必须生成相同内容。

## File entry

```json
{
  "path": "technologies/python.md",
  "sha256": "hex",
  "title": "Python",
  "sections": []
}
```

- `path`：相对于知识库根目录的 POSIX 风格路径，只允许 `.md` 文件。
- `sha256`：整个文件原始字节的 SHA-256，用于检测审核后文件是否变化。
- `title`：第一个一级标题；不存在时使用文件 stem。它只用于召回，不会自动改写原文。
- `sections`：按文档顺序列出可定位章节，包括首个标题前存在的根级内容。

Inventory 不保存知识库绝对路径，调用方单独持有本次运行的根目录，避免产物泄露本地目录结构或被错误用于另一个知识库。

## Section entry

```json
{
  "section_id": "ks_<hash>",
  "heading_path": ["Python", "虚拟环境"],
  "heading": "虚拟环境",
  "level": 2,
  "ordinal": 0,
  "sha256": "hex"
}
```

- `section_id`：根据文件路径、标题路径和同路径序号确定性生成，只用于当前 inventory。
- `heading_path`：从文档一级标题到当前标题的完整路径。根级内容使用空数组。
- `heading`：当前标题文本；根级内容为 `null`。
- `level`：Markdown 标题级别；根级内容为 `0`。
- `ordinal`：相同 `heading_path` 在文件中从零开始的序号，用于保留重复标题而不误认唯一性。
- `sha256`：该章节完整 Markdown 片段的 SHA-256，包括标题行及其正文，但不包括子章节。

`section_id` 不是跨版本永久身份。审核和应用必须同时校验文件与章节哈希，不能只相信 ID。

## 扫描边界

- 只递归读取显式知识库根目录内的 `.md` 文件。
- 忽略 `.git/`、`.knowledge-curator/` 和调用方明确配置的运行目录。
- 默认不跟随符号链接或目录联接；解析后超出知识库根目录的路径必须拒绝。
- 无法按 UTF-8 读取的文件、路径越界和读取失败必须报告，不能静默跳过后仍宣称快照完整。
- Inventory 不修改知识库，不创建目录，也不执行文档中出现的命令或指令。

## 召回用途

第一版可使用文件名、`title`、`heading_path` 和按需读取的正文关键词进行排序。排序结果只用于缩小 Codex 需要比较的范围：

- 高分不代表应当融合；
- 低分不代表可以直接新建文档；
- 语义上的等价、冲突和适用范围仍由 Codex 判断并由用户审核。

