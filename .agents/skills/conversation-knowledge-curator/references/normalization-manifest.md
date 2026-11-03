# normalization manifest 格式契约

当前格式版本：`conversation-knowledge-curator.manifest.v1`

`conversation-knowledge-curator.manifest.v1` 是一次批量标准化的审计清单，记录输入来源、`normalized.v3` 输出文件、处理状态和诊断信息。输出目录必须是新目录，manifest 固定写入其中的 `manifest.json`。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 记录批量标准化来源、诊断和输出映射 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.manifest.v1",
  "status": "ok",
  "input": {},
  "output_directory": "absolute/path",
  "branch_policy": "active",
  "discovery": {},
  "archives": [],
  "summary": {},
  "entries": [],
  "warnings": []
}
```

- `status` 为 `ok` 或 `completed_with_issues`。
- `input` 记录输入绝对路径和 `json_file`、`zip_archive` 或 `directory` 类型。
- `output_directory` 是本次新建目录的绝对路径。
- `branch_policy` 为 `active` 或 `all`。
- `discovery` 和 `archives` 沿用 inspect 的发现结果。
- `summary` 汇总整个批次的来源、对话和消息数量。
- `entries` 与候选 JSON 来源一一对应，保持确定性的发现顺序。
- `warnings` 保存输入发现阶段的警告，不包含消息正文。

## Entry 状态

### Normalized

```json
{
  "status": "normalized",
  "source": {
    "kind": "json_file",
    "path": "absolute/path/conversations.json",
    "size_bytes": 123,
    "sha256": "hex",
    "container": "list"
  },
  "output": "source--hash.normalized.json",
  "normalization": {
    "branch_policy": "active",
    "source_conversation_count": 2,
    "normalized_conversation_count": 1,
    "skipped_conversation_count": 1,
    "message_count": 10,
    "skipped_conversations": [],
    "warnings": []
  }
}
```

只有 inspect 状态为 `supported` 的来源可以进入该状态。

- `source` 保存完整来源描述。ZIP 成员还包含 `archive_path`、`member_path` 和 `compressed_size_bytes`。
- `output` 是输出目录下的相对文件名。
- `normalization` 集中保存该来源的分支策略、统计和诊断，避免把审计字段复制进内容文件。
- `source.sha256` 必须等于对应输出文件的 `source_sha256`。
- `skipped_conversations` 只记录来源索引、对话 ID、原因码和说明，不得包含消息正文。
- `warnings` 是带 `conversation_index` 的标准化警告，不得包含完整私密对话。

使用 `active` 时，如果无法确认 `current_node` 到根节点的完整路径，必须跳过该对话并记录 `active_branch_unavailable`；不得隐式回退到全部分支。只有显式使用 `all` 时才能遍历全部分支。

### Skipped

```json
{
  "status": "skipped",
  "source": {},
  "inspection_status": "invalid_json",
  "reason": "错误说明"
}
```

`unsupported`、`invalid_json`、`rejected` 和 `read_error` 来源不得调用标准化逻辑，必须记录为 skipped。

### Failed

```json
{
  "status": "failed",
  "source": {},
  "reason": "错误说明"
}
```

来源通过 inspect 后，如果在再次读取、哈希校验、标准化或写入时失败，记录为 failed。一个来源失败不应阻止其他来源处理。

## 输出命名

每个来源独立生成一个 `.normalized.json` 文件。名称由可读来源标签和来源身份哈希组成：

```text
<source-label>--<identity-hash>.normalized.json
```

- 普通 JSON 使用文件 stem 作为标签。
- ZIP 成员使用压缩包 stem 与成员相对路径作为标签。
- 身份哈希来自来源类型、绝对文件路径和 ZIP 成员路径，用于避免重名。
- 文件名需要替换 Windows 不允许的字符，并限制标签长度。

## 汇总不变量

- `normalized_source_count + skipped_source_count + failed_source_count` 等于 `candidate_source_count`。
- 顶层对话和消息统计等于所有 normalized entry 的 `normalization` 统计之和。
- `normalization_warning_count` 等于所有 normalized entry 中标准化警告的数量之和。
- 每个 normalized entry 的 `normalized_conversation_count + skipped_conversation_count` 等于 `source_conversation_count`。
- 每个 normalized entry 的 `message_count` 等于对应内容文件全部消息数量之和。
- 任一来源被跳过、失败，任一对话被跳过，出现标准化警告，或发现阶段存在归档问题时，顶层状态应为 `completed_with_issues`。
- manifest 不包含消息正文。
