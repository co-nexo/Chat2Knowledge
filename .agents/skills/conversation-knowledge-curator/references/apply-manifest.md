# apply manifest 格式契约

当前格式版本：`conversation-knowledge-curator.apply-manifest.v1`

Apply manifest 记录一次已授权 Markdown 应用的最小审计信息。它证明哪些审核结果实际写入了哪些文件，不复制候选正文、原始对话或完整 diff。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 绑定审核决定并记录文件变更前后哈希 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.apply-manifest.v1",
  "review_id": "kr_<hash>",
  "review_manifest_sha256": "hex",
  "review_decisions_sha256": "hex",
  "status": "applied",
  "entries": [],
  "warnings": []
}
```

- `status` 为 `applied` 或 `applied_with_issues`。输入校验失败时不得生成成功 manifest。
- `warnings` 只记录不包含知识正文的应用诊断。
- 时间等环境信息可以由外层运行记录保存，不参与内容身份和批准绑定。

## Entry

```json
{
  "proposal_id": "kp_<hash>",
  "candidate_ids": ["kc_<hash>"],
  "target_path": "preferences/development-tools.md",
  "operation": "replace_section",
  "before_sha256": "hex",
  "after_sha256": "hex",
  "result": "applied"
}
```

新建文件的 `before_sha256` 为 `null`。第一版 entry 的 `result` 只允许 `applied`；被拒绝或未决定的提案不写入应用清单。

## 应用前校验

所有获批提案必须在任何正式写入发生前完成以下检查：

1. review manifest、decisions、proposal、after 和 diff 哈希完全匹配；
2. 每个决定属于当前 `review_id`，且明确为 `approved`；
3. 当前知识库文件哈希仍等于提案的 `base_file_sha256`；
4. `create_document` 目标仍不存在；
5. 目标相对路径解析后仍位于显式知识库根目录内；
6. after 文件哈希与审核清单一致；
7. 同一目标文件没有多个相互竞争的获批提案。

任何预检失败都必须停止，且不得应用其他文件。不能通过自动重算 diff、自动合并当前内容或忽略过期哈希来继续。

## 写入与恢复边界

- 先在目标目录内写入临时文件、校验内容，再替换正式文件；不得直接逐行修改原文件。
- 多文件替换不能假定跨文件原子。实现应用功能时必须提供可检测的中断状态或恢复策略，并在测试中覆盖。
- 应用成功后可以报告 `git status`、`git diff` 和 `git diff --check` 结果，但不得自动暂存、提交或推送。
- 当前阶段不更新 SQLite、向量数据库或其他持久化索引。

Apply manifest 是否进入知识库 Git 由知识库自身规范决定。若保存，只应放在类似 `.knowledge-curator/audit/` 的专用目录，并保持最小化；审核包和运行数据仍应忽略。

