# review package 格式契约

当前格式版本：

- `conversation-knowledge-curator.review-manifest.v1`
- `conversation-knowledge-curator.review-decisions.v1`

审核包把结构化提案、修改后的完整文件和准确 diff 固定为一次可验证审核。它是私密临时产物，不属于正式知识库。

## 目录结构

```text
review/
├── review-manifest.json
├── proposals.json
├── decisions.json          # 用户作出决定后生成
├── after/
│   └── <proposal-id>.md
└── patches/
    └── <proposal-id>.diff
```

创建审核包时目标目录必须不存在。`after/` 用于安全应用，`patches/` 用于人工阅读；不得把 diff 当作未经校验的命令执行。

## Review manifest

```json
{
  "format": "conversation-knowledge-curator.review-manifest.v1",
  "review_id": "kr_<hash>",
  "inputs": {
    "knowledge_candidates_sha256": "hex",
    "knowledge_inventory_sha256": "hex",
    "knowledge_proposals_sha256": "hex"
  },
  "entries": [
    {
      "proposal_id": "kp_<hash>",
      "proposal_sha256": "hex",
      "candidate_ids": ["kc_<hash>"],
      "action": "merge",
      "target_path": "preferences/development-tools.md",
      "base_file_sha256": "hex",
      "after_path": "after/kp_<hash>.md",
      "after_sha256": "hex",
      "diff_path": "patches/kp_<hash>.diff",
      "diff_sha256": "hex"
    }
  ]
}
```

没有写操作的 `no_change`、`conflict` 和 `defer` 也应出现在审核清单中，但其 after、diff 和文件哈希字段为 `null`，便于用户确认处置结论。

`review_id` 由规范化后的输入哈希和 entries 生成。审核包任一提案、after 或 diff 变化都必须产生新的身份，不能继承旧批准。

## Decisions

```json
{
  "format": "conversation-knowledge-curator.review-decisions.v1",
  "review_id": "kr_<hash>",
  "decisions": [
    {
      "proposal_id": "kp_<hash>",
      "proposal_sha256": "hex",
      "diff_sha256": "hex",
      "decision": "approved",
      "note": null
    }
  ]
}
```

- `decision` 只能是 `approved` 或 `rejected`。
- 可写提案的批准必须同时绑定 `proposal_sha256` 和 `diff_sha256`。
- 非写入提案的 `diff_sha256` 为 `null`，决定只确认其处置结论。
- 缺少决定等同于未批准，不得默认通过。
- `note` 可记录用户说明，但不能作为修改 operation 的隐式命令；内容变化必须生成新提案和新审核包。

## 审核展示要求

每个提案至少展示：

- action、目标文件和目标章节；
- 候选陈述及对应的来源对话和消息 ID；
- rationale 与所有风险 flags；
- 可写提案的完整 diff；
- `conflict` 两侧内容和不确定性；
- `defer` 的停止原因。

审核界面可以隐藏重复技术字段，但保存的决定必须绑定上述哈希。批准一个提案不代表批准其他提案、Git 提交、Git 推送或知识库范围外的写入。

