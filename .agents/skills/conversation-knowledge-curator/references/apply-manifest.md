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

- v1 的 `status` 只允许 `applied`。输入校验或写入失败时不得生成成功 manifest。
- v1 的 `warnings` 保留为空数组；后续如需记录非致命诊断，内容不得包含知识正文。
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

## 命令与输出位置

运行：

```text
scripts/knowledge_apply.py apply <knowledge-root> <review-directory>
```

`knowledge-root` 必须是用户明确提供的正式知识库根目录，`review-directory` 必须是已经生成 `decisions.json` 的审核包。成功后，标准输出只给出结构化摘要，完整清单独占创建在 `<review-directory>/apply-manifest.json`。清单已存在时拒绝覆盖，也不会再次应用同一审核包。

只有同时满足以下条件的 Proposal 才会写入：决定中明确为 `approved`，并且 action 为 `add` 或 `merge`。`rejected`、缺少决定及 `no_change`、`conflict`、`defer` 不会修改知识库，也不会出现在 apply manifest entries 中。若没有获批写操作，仍生成 entries 为空的成功清单，用于明确记录本次审核没有知识写入。

## 写入与恢复边界

- 先在目标目录内写入临时文件、校验内容，再替换正式文件；不得直接逐行修改原文件。
- 多文件替换不能假定跨文件原子。脚本在写入前创建 `<review-directory>/apply-state.json`，用绝对根路径的 SHA-256 绑定原知识库但不保存路径正文，并把现有目标的原始字节保存到 `<review-directory>/rollback/`；状态与回滚副本属于私密运行数据，不写入知识库。
- 普通可捕获失败会自动回滚已替换目标，并清理暂存状态。进程被强制终止或自动回滚未完成时，保留状态供显式恢复。
- 检测到 apply state 且没有 apply manifest 时，运行 `scripts/knowledge_apply.py recover <knowledge-root> <review-directory>`。恢复会先校验全部目标：只有当前内容仍等于审核前或审核后的哈希时才继续；发现用户或其他程序的额外修改时拒绝覆盖。
- 恢复按哈希还原原文件、删除本次已创建的新文件，并仅删除本次创建且仍为空的目录。成功后删除 apply state、回滚副本和残留暂存文件，可安全重试。
- 正常应用成功后保留 `apply-state.json` 与 `rollback/` 作为该私密审核包的一部分；apply manifest 已存在时 `recover` 拒绝把已完成应用当作中断。用户确认知识库 Git diff 并不再需要审核包后，可以整体删除该审核目录。
- 应用成功后可以报告 `git status`、`git diff` 和 `git diff --check` 结果，但不得自动暂存、提交或推送。
- 当前阶段不更新 SQLite、向量数据库或其他持久化索引。

`apply` 和 `recover` 的退出码 `0` 表示操作完成，`2` 表示校验、写入或恢复失败。失败原因写入标准错误，知识内容不写入诊断日志。

Apply manifest 是否进入知识库 Git 由知识库自身规范决定。若保存，只应放在类似 `.knowledge-curator/audit/` 的专用目录，并保持最小化；审核包和运行数据仍应忽略。

