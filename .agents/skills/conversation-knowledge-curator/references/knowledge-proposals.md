# knowledge proposals 格式契约

当前格式版本：`conversation-knowledge-curator.knowledge-proposals.v1`

Knowledge proposals 连接第三阶段知识候选与正式 Markdown。Codex 负责语义判断，确定性脚本负责校验候选引用、路径、哈希和有限修改操作。所有提案在审核通过前都只是建议。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 分离语义 action 与确定性 operation，禁止自动删除 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.knowledge-proposals.v1",
  "inputs": {
    "knowledge_candidates_sha256": "hex",
    "knowledge_inventory_sha256": "hex",
    "knowledge_retrieval_sha256": "hex"
  },
  "proposals": []
}
```

输入哈希绑定本次判断所使用的候选、知识库快照和召回结果。任一输入变化时必须重新校验并重新生成审核包。召回结果只提供阅读范围，Codex仍需读取候选和必要的 Markdown 原文后才能作出 action 判断。

## Proposal draft

Codex 先生成 `conversation-knowledge-curator.proposal-draft.v1` 草稿：

```json
{
  "format": "conversation-knowledge-curator.proposal-draft.v1",
  "inputs": {
    "knowledge_candidates_sha256": "hex",
    "knowledge_inventory_sha256": "hex",
    "knowledge_retrieval_sha256": "hex"
  },
  "proposals": [
    {
      "candidate_ids": ["kc_<hash>"],
      "action": "merge",
      "target": {},
      "rationale": "现有章节已包含同一主题，应合并而不是创建重复文档。",
      "operation": {},
      "flags": []
    }
  ]
}
```

草稿不包含 `proposal_id`，其余字段与最终 Proposal 相同。每个输入候选必须且只能出现在一个 Proposal 中；多个候选可以合并为一个提案。`flags` 必须等于相关候选 flags 的去重并集，不能由 Codex 自行删减。

## Proposal

```json
{
  "proposal_id": "kp_<hash>",
  "candidate_ids": ["kc_<hash>"],
  "action": "merge",
  "target": {
    "path": "preferences/development-tools.md",
    "file_sha256": "hex",
    "section_id": "ks_<hash>",
    "section_sha256": "hex"
  },
  "rationale": "现有章节已讨论默认开发环境，应在同一处补充。",
  "operation": {
    "type": "replace_section",
    "markdown": "## 默认开发环境\n\n...\n"
  },
  "flags": []
}
```

- `proposal_id`：由确定性脚本根据规范化后的提案内容生成；Codex草稿不得自行决定最终 ID。
- `candidate_ids`：至少一个真实存在的知识候选；每个输入候选在本批次中必须且只能属于一个提案。
- `action`：语义判断，说明为什么需要或不需要变更。
- `target`：目标文件与可选章节。现有文件必须绑定 inventory 中的文件哈希；章节操作还必须绑定章节 ID 与哈希。
- `rationale`：简要解释候选与现有知识的关系，不复制完整对话。
- `operation`：可确定执行的有限修改；不产生写入的 action 必须为 `null`。
- `flags`：汇总相关候选的 `assistant_only`、`uncertain`、`time_sensitive` 和 `sensitive`；不得静默丢弃风险标记。

最终输出按 `proposal_id` 排序。`proposal_id` 由不含 ID 的规范化 Proposal JSON 计算 SHA-256，取前 20 位并添加 `kp_` 前缀。

## Action

| 值 | 含义 | 是否允许写操作 |
|---|---|---|
| `add` | 现有知识库没有合适内容，需要新增 | 是 |
| `merge` | 应融合进已有文件或章节 | 是 |
| `no_change` | 已有内容等价且足够 | 否 |
| `conflict` | 候选与已有知识不一致，需要人工判断 | 否 |
| `defer` | 信息、真实性、时效性或敏感性尚不足 | 否 |

不得使用分数替代 action。`conflict` 和 `defer` 第一版只进入审核说明，不能携带可自动应用的 operation。

第一版的 action 与 operation 组合固定为：

| action | 允许的 operation |
|---|---|
| `add` | `create_document`、`insert_section` |
| `merge` | `append_to_section`、`replace_section` |
| `no_change` | `null` |
| `conflict` | `null` |
| `defer` | `null` |

## Operation

第一版只允许：

- `create_document`：创建尚不存在的 Markdown 文件；`markdown` 是完整文件。
- `insert_section`：在目标章节之后插入完整新章节；目标章节必须存在且哈希匹配。
- `append_to_section`：在目标章节正文末尾追加 Markdown 片段，不改变其子章节。
- `replace_section`：用完整 Markdown 片段替换目标章节，但不包含其子章节。

第一版不允许删除文档、删除章节、重命名文件、移动文件或执行自由形式 patch。任何 operation 都必须包含最终 Markdown 内容，且不得包含未经候选支持的新事实。

Operation 固定包含 `type` 和 `markdown` 两个字段。`markdown` 必须是非空字符串并以换行结束：

- `create_document` 必须提供完整文档，首个非空行必须是一级 ATX 标题；
- `insert_section` 和 `replace_section` 必须提供以 ATX 标题开头的完整章节；
- `append_to_section` 提供追加到现有章节直属正文末尾的 Markdown 片段。

## Target

`target` 为 `null` 或包含以下固定字段：

```json
{
  "path": "preferences/development-tools.md",
  "file_sha256": "hex-or-null",
  "section_id": "ks_<hash>-or-null",
  "section_sha256": "hex-or-null"
}
```

- `create_document`：`target` 必须存在，但三个哈希或 ID 字段必须为 `null`，目标路径当前不得存在；
- `insert_section`、`append_to_section`、`replace_section`：四个字段都必须存在且与 inventory 中同一章节完全一致；
- `no_change` 和 `conflict`：必须定位一个现有章节，便于审核已有内容；
- `defer`：可以没有目标；如提供目标，也必须定位真实现有章节；
- 召回结果是阅读辅助，不是目标白名单。目标可以不在 Top N 中，但必须属于本次 inventory，并由 Codex实际阅读后判断。

## 路径与组合规则

- 目标必须是知识库根目录内规范化的相对 `.md` 路径；拒绝绝对路径、`..`、保留运行目录和符号链接越界。
- `create_document` 的目标必须不存在；其他操作的目标必须与 inventory 文件及章节哈希一致。
- 一个审核包中每个目标文件最多有一个可写提案。多个候选需要修改同一文件时，应合并成一个文件级提案，避免多个操作基于同一个旧哈希相互覆盖。
- 现有风格明确时应沿用原文标题层级、语言和组织方式；不得为了应用候选而顺带格式化无关内容。

## 草稿与最终化

运行 `scripts/knowledge_proposals.py finalize <knowledge-root> <inventory.json> <knowledge-candidates.json> <knowledge-retrieval.json> <proposal-draft.json> <new-output-file>`。确定性脚本必须：

1. 校验顶层格式与输入哈希；
2. 重新验证 inventory 与当前知识库一致，并确定性重算 retrieval，拒绝被修改或过期的召回结果；
3. 校验所有候选 ID、action、flags、完整覆盖和唯一性；
4. 校验目标路径、文件哈希、章节定位和 operation 组合；
5. 生成稳定 `proposal_id`，并按 ID 排序输出最终 `knowledge-proposals.v1`；
6. 在全部校验成功后创建新输出文件，目标已存在时拒绝覆盖。

该脚本只最终化提案，不生成 after 文件、不生成 diff，也不修改知识库。完整 after 和准确 diff 由下一步审核包脚本生成。

退出码 `0` 表示成功，`2` 表示失败且没有正式输出。

