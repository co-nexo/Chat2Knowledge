# knowledge candidates 格式契约

当前格式版本：`conversation-knowledge-curator.knowledge-candidates.v1`

`conversation-knowledge-curator.knowledge-candidates.v1` 保存第三阶段产生的精简知识候选。所有候选都只是待审核提案，不代表事实已经验证或获准写入知识库。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 支持原子候选、多来源 Evidence 和候选级 flags |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.knowledge-candidates.v1",
  "candidates": []
}
```

顶层只能包含 `format` 和 `candidates`。

## Candidate

```json
{
  "candidate_id": "kc_<hash>",
  "statement": "用户希望项目优先兼容 JetBrains 系列 IDE。",
  "evidence": [],
  "flags": []
}
```

- `candidate_id`：Python 根据去除首尾空白后的 `statement` 生成，当前使用 SHA-256 前 20 位并加 `kc_` 前缀。
- `statement`：一条非空、原子、忠于证据的候选陈述。
- `evidence`：至少包含一个 Evidence，可跨对话和跨来源。
- `flags`：按固定顺序输出当前适用的风险或来源属性。

第一版不包含 `category`、`document_path`、数值置信度或证据正文。这些字段要么属于后续阶段，要么会制造无法证明的精确度或重复私密内容。

## Evidence

```json
{
  "source_sha256": "hex",
  "conversation_id": "conversation-id",
  "message_ids": ["message-id"]
}
```

- `source_sha256` 定位第二阶段的来源内容。
- `conversation_id` 定位该来源中的稳定标准化对话。
- `message_ids` 至少包含一个真实存在且属于该对话的消息 ID。
- Evidence 不重复保存消息角色和正文；审核时从对应 `normalized.v3` 文件读取。
- 同一候选可有多个 Evidence。相同来源和对话的重复 Evidence 会合并消息 ID，不同来源的 Evidence 全部保留。

Evidence 只能证明候选从何处提取，不能单独证明候选代表的外部事实正确。证据数量也不等于可信度。

## Flags

当前合法值为：

- `assistant_only`
- `uncertain`
- `time_sensitive`
- `sensitive`

`assistant_only` 由 Python 根据所有证据消息的角色重新计算；语义草稿中的同名标记不会被直接信任。其他标记由 Codex 初步识别并由 Python 校验枚举值。

## 精确去重

Python 仅合并 `statement` 完全相同的候选。合并后：

- Evidence 取并集；
- flags 取并集；
- `assistant_only` 根据合并后的全部证据重新计算；
- 输出按稳定的候选 ID 排序。

语义相近但文本不同的候选不会在本阶段自动合并。
