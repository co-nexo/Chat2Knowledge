# `normalized.v2` 格式契约

本文档定义 `conversation-knowledge-curator.normalized.v2` 的正式结构。该文件是供后续知识提取使用的精简内容载体，不承担批次审计职责。来源详情、处理统计、跳过原因和警告统一记录在同一输出目录的 `manifest.json` 中。

`normalized.v2` 是对既有 `normalized.v1` 的不兼容升级。读取方必须按 `format` 选择解析逻辑，不得把 v1 和 v2 视为相同结构。修改字段含义、必需性或安全语义时，必须同步更新实现和测试；后续不兼容变更必须再次升级格式版本。

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.normalized.v2",
  "source_sha256": "hex",
  "conversations": []
}
```

- `format`：必需，固定为当前格式标识。
- `source_sha256`：必需，对应实际被标准化的 JSON 内容。ZIP 来源对应成员内容，不是整个压缩包。
- `conversations`：必需，只包含成功标准化的对话。

完整来源路径、ZIP 成员路径和容器类型通过 manifest 中相同 SHA-256 的 normalized entry 查询，不在内容文件中重复保存。

## Conversation

```json
{
  "source_index": 0,
  "conversation_id": "id-or-null",
  "title": "title-or-null",
  "create_time": 0,
  "update_time": 0,
  "messages": []
}
```

- `source_index`：必需，对话在原始容器中的零基索引。
- `conversation_id`、`title` 和时间字段允许为 `null`；时间值保持原样，不解释单位。
- `messages`：必需，按选定活动路径或确定性的全树遍历顺序排列。
- 分支策略不在每个对话内重复记录，由 manifest 顶层和 entry 中的诊断信息说明。

## Message

```json
{
  "message_id": "message-or-node-id",
  "role": "user",
  "create_time": 0,
  "text": "用户可见文本"
}
```

- `message_id`：必需。原始消息 ID 缺失时使用所在节点 ID。
- `role`：必需，只能是 `user` 或 `assistant`。
- `create_time`：必需，但允许为 `null`；原值保持不变。
- `text`：必需，必须是非空字符串。
- 不保留节点 ID、父节点 ID、author name 或内容类型包装；这些字段不是后续知识提取的必要输入。
- 不得包含隐藏 reasoning、analysis、thoughts、工具载荷或未知结构化内容。

## 与 manifest 的关系

每个来源独立生成一个本格式文件。`manifest.json` 的 normalized entry 负责保存：

- 完整来源描述和输出文件名；
- 使用的分支策略；
- 来源、成功、跳过对话及消息数量；
- 跳过原因和标准化警告。

`source_sha256` 必须等于对应 normalized entry 的 `source.sha256`。关系和诊断结构见 [normalization-manifest-v1.md](normalization-manifest-v1.md)。

## 不变量

- 原始输入保持只读。
- 顶层只能包含 `format`、`source_sha256` 和 `conversations`。
- 标准化文件不得包含原始路径、ZIP 路径、警告、统计或跳过记录。
- 只保留用户可见的 `user` 和 `assistant` 非空文本。
- 输出文件不得静默覆盖已有文件。
