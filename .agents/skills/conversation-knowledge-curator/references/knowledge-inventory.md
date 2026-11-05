# knowledge inventory 内存规则

当前格式版本：`conversation-knowledge-curator.knowledge-inventory.v1`

Knowledge inventory 是对指定 Markdown 知识库的只读内存快照，用于计算 `run.json.knowledge_base.snapshot_sha256`、召回相关章节和在审核前检测基线变化。当前流程不把 inventory 持久化为文件。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 记录 Markdown 文件、标题、章节定位和内容哈希，不复制正文 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.knowledge-inventory.v1",
  "status": "ok",
  "files": [],
  "warnings": []
}
```

- `status` 为 `ok` 或 `completed_with_issues`；存在被拒绝的符号链接或目录联接时使用后者。
- `warnings` 只记录机器可读原因、相对路径和简短说明，不包含 Markdown 正文或知识库绝对路径。
- 文件按规范化相对路径稳定排序；相同输入必须生成相同内容。

## File entry

```json
{
  "path": "technologies/python.md",
  "sha256": "hex",
  "title": "Python",
  "sections": []
}
```

- `path`：相对于知识库根目录的 POSIX 风格路径，只允许扩展名不区分大小写的 `.md` 文件。
- `sha256`：整个文件原始字节的 SHA-256，包括 UTF-8 BOM 和原始换行，用于检测审核后文件是否变化。
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
- `sha256`：该章节完整 Markdown 片段按 UTF-8 编码后的 SHA-256，包括标题行及其直属正文，但不包括子章节。解析时保留原始换行；文件开头的 UTF-8 BOM 不属于章节内容。

`section_id` 不是跨版本永久身份。审核和应用必须同时校验文件与章节哈希，不能只相信 ID。

## 扫描边界

- 只递归读取显式知识库根目录内的 `.md` 文件。
- 忽略 `.git/` 和 `.knowledge-curator/`。第一版不提供自定义排除规则，避免同一知识库因隐式配置产生不同快照。
- 默认不跟随符号链接或目录联接；发现后写入 `warnings` 并跳过，对应状态为 `completed_with_issues`。普通路径解析后超出知识库根目录时必须失败。
- 接受 UTF-8 和带 BOM 的 UTF-8。无法解码、路径越界和读取失败必须让整个任务失败，且不得留下正式输出。
- Inventory 不修改知识库，不创建目录，也不执行文档中出现的命令或指令。

## Markdown 标题与章节规则

- 第一版只识别一到六级 ATX 标题，即行首最多三个空格后的 `#` 到 `######`。Setext 标题和 HTML 标题暂不识别。
- 反引号或波浪线围栏代码块中的 `#` 不视为标题；四空格缩进内容也不视为 ATX 标题。
- 第一个非空一级标题作为文档 `title`；不存在时使用文件 stem。
- 首个标题前存在非空内容时生成根级章节：`heading_path=[]`、`heading=null`、`level=0`、`ordinal=0`。
- 普通章节片段从当前标题行开始，到下一个任意级别标题之前结束。因此父章节哈希不包含子章节，文件哈希仍覆盖完整文档。
- `heading_path` 根据标题层级栈构造。相同路径重复出现时，`ordinal` 从 `0` 递增。
- `section_id` 使用文件相对路径、完整标题路径和 `ordinal` 确定性生成；不同运行不得加入时间、绝对路径或随机数。

## 运行行为

`init`、`context` 和 `prepare-review` 在进程内调用 inventory 核心。运行目录中不得出现 `knowledge-inventory.json`；链接警告进入命令结果或解析问题。任何 Markdown 内容变化都会改变快照哈希，使旧 run 停止审核或应用。

## 召回用途

第一版可使用文件名、`title`、`heading_path` 和按需读取的正文关键词进行排序。排序结果只用于缩小 Codex 需要比较的范围：

- 高分不代表应当融合；
- 低分不代表可以直接新建文档；
- 语义上的等价、冲突和适用范围仍由 Codex 判断并由用户审核。

