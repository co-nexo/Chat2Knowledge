# extraction manifest 格式契约

当前格式版本：`conversation-knowledge-curator.extraction-manifest.v1`

`conversation-knowledge-curator.extraction-manifest.v1` 记录一次第三阶段最终化过程的输入、规则版本、对话级处理结果和输出哈希。它不保存消息正文。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 记录语义提取输入、对话处理结果和候选输出 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.extraction-manifest.v1",
  "status": "ok",
  "input": {},
  "rules_version": "conversation-knowledge-curator.extraction-rules.v1",
  "summary": {},
  "conversation_results": [],
  "warnings": [],
  "outputs": {}
}
```

- `status` 为 `ok` 或 `completed_with_issues`。
- `input` 记录第二阶段输出目录、其 manifest 哈希，以及每个 normalized 文件的文件哈希和来源哈希。
- `rules_version` 标识本次使用的语义提取规则。
- `summary` 记录对话状态、候选、无候选对话和审核说明数量。
- `conversation_results` 记录每段对话是否完成处理以及关联的候选。
- `warnings` 记录输入清单的批次级问题，不包含消息正文。
- `outputs` 记录候选文件名及其 SHA-256。

## Conversation result

```json
{
  "source_sha256": "hex",
  "conversation_id": "conversation-id",
  "processing_status": "completed",
  "candidate_ids": ["kc_<hash>"]
}
```

`processing_status` 只描述处理过程，不判断整段对话的知识价值：

- `completed`：该对话已经完成语义提取，可以产生零个或多个候选。
- `failed`：处理过程中发生错误，必须提供机器可读的 `reason`。
- `skipped`：根据输入条件主动跳过，必须提供机器可读的 `reason`。

不得添加整段对话级的 `decision: extract/no_candidate/needs_review`。

当 completed 对话没有候选时，必须至少提供以下一种信息：

- `no_candidate_reason`：例如 `ordinary_qa`、`social_chatter`、`transient_status` 或 `no_stable_conclusion`；
- `review_notes`：无法归属于具体候选的对话级问题，每项包含 `reason` 和真实的 `message_ids`。

候选自身的不确定性必须写入 candidate flags，不能用对话级 review note 代替。

## `extraction-draft.v1` 交接格式

Codex 先生成供 Python 最终化的语义草稿：

```json
{
  "format": "conversation-knowledge-curator.extraction-draft.v1",
  "candidates": [
    {
      "statement": "候选陈述",
      "evidence": [
        {
          "source_sha256": "hex",
          "conversation_id": "conversation-id",
          "message_ids": ["message-id"]
        }
      ],
      "flags": []
    }
  ],
  "conversation_results": [
    {
      "source_sha256": "hex",
      "conversation_id": "conversation-id",
      "processing_status": "completed"
    }
  ]
}
```

草稿必须完整覆盖输入中的每段标准化对话。草稿不提供 `candidate_id` 或 `candidate_ids`；Python 在校验、精确去重和证据合并后生成这些字段，避免相信模型生成的身份关联。

## 最终化规则

`scripts/knowledge_candidates.py finalize` 必须在写文件前完成全部校验：

- normalized 与第二阶段 manifest 的格式和来源哈希一致；
- 每个 conversation 和 message 引用真实存在；
- 每段对话恰好有一条处理记录；
- failed 或 skipped 对话不能被候选引用；
- 候选字段和 flags 符合契约；
- `assistant_only` 根据证据角色派生；
- 精确重复候选合并 Evidence 而不是丢弃证据。

校验失败时不得创建输出目录。输出目录已存在时拒绝覆盖或追加。

CLI 在 `status=ok` 时返回退出码 `0`，成功生成但包含处理问题时返回 `1`，输入、校验或写入失败时返回 `2`。结构化 manifest 写入标准输出，错误说明写入标准错误。
