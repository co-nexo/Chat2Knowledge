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
├── decisions.json          # prepare 不生成；用户作出决定后另行创建
├── after/
│   └── <proposal-id>.md
└── patches/
    └── <proposal-id>.diff
```

创建审核包时目标目录必须不存在，并且不得位于正式知识库内部。`after/` 用于安全应用，`patches/` 用于人工阅读；不得把 diff 当作未经校验的命令执行。

运行 `scripts/knowledge_review.py prepare <knowledge-root> <inventory.json> <knowledge-candidates.json> <knowledge-retrieval.json> <knowledge-proposals.json> <new-review-directory>`。程序必须重新校验 inventory、retrieval 和 proposals 的完整关系；任一输入过期或被修改时停止，且不得留下正式审核目录。

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

哈希计算规则：

- `proposal_sha256`：对单个规范化 Proposal 使用 UTF-8、按键排序、无多余空白的 JSON 计算；
- `after_sha256`：对 after 文件的实际字节计算，包含可能保留的 UTF-8 BOM 和 CRLF；
- `diff_sha256`：对审核包中 UTF-8、LF 换行的 diff 文件实际字节计算；
- `knowledge_proposals_sha256`：对输入 proposals 文件原始字节计算，审核包中的 `proposals.json` 保留相同字节。

## After 与 diff 生成规则

- `create_document` 生成完整新文件，第一版统一使用 UTF-8、无 BOM、LF 换行；
- 修改现有文件时保留原文件是否含 UTF-8 BOM，并把 operation Markdown 转为该文件的主要换行风格；
- `append_to_section` 追加到目标章节直属正文末尾，不越过其子章节；
- `replace_section` 只替换目标章节标题和直属正文，不替换子章节；
- `insert_section` 插入到目标章节完整子树之后，避免把新章节意外插入现有子章节之前；
- diff 使用稳定的 unified diff，不包含时间戳；现有文件使用 `a/<path>` 和 `b/<path>`，新文件从 `/dev/null` 开始；
- 可写提案如果没有产生实际字节变化，必须失败，不能生成空 diff。

`prepare` 只读取知识库并写入新的审核目录，不创建 `decisions.json`，也不修改、暂存或提交正式 Markdown。

退出码 `0` 表示审核包完整生成，`2` 表示失败且没有正式审核目录。

## Decisions

人工审核后，先准备只包含用户明确选择的草稿：

```json
{
  "format": "conversation-knowledge-curator.review-decision-draft.v1",
  "review_id": "kr_<hash>",
  "decisions": [
    {
      "proposal_id": "kp_<hash>",
      "decision": "approved",
      "note": null
    }
  ]
}
```

运行 `scripts/knowledge_review.py decide <review-directory> <decision-draft.json>`。脚本重新校验 `review-manifest.json`、`proposals.json`、全部 after 和 diff，再从 manifest 派生 `proposal_sha256` 与 `diff_sha256`，独占创建 `decisions.json`。草稿不得自行提供或覆盖这些哈希。

只有用户明确点名或明确确认的 Proposal 才能进入草稿。Codex 不得把“继续”“看起来可以”或生成审核包的请求推断为批准。草稿可以只包含部分 Proposal；未出现的 Proposal 等同于未批准。

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
- `decisions.json` 已存在时拒绝覆盖；如需改变决定，应生成新的审核包，不能原地修改或沿用旧批准。

`decide` 退出码 `0` 表示决定文件成功创建，`2` 表示审核包、草稿或输出校验失败且没有新的决定文件。

## 审核展示要求

每个提案至少展示：

- action、目标文件和目标章节；
- 候选陈述及对应的来源对话和消息 ID；
- rationale 与所有风险 flags；
- 可写提案的完整 diff；
- `conflict` 两侧内容和不确定性；
- `defer` 的停止原因。

审核界面可以隐藏重复技术字段，但保存的决定必须绑定上述哈希。批准一个提案不代表批准其他提案、Git 提交、Git 推送或知识库范围外的写入。

