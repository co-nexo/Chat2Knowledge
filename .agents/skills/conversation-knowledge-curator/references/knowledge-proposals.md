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
    "knowledge_inventory_sha256": "hex"
  },
  "proposals": []
}
```

输入哈希绑定本次判断所使用的候选和知识库快照。任一输入变化时必须重新校验并重新生成审核包。

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
- `candidate_ids`：至少一个真实存在的知识候选；同一候选可以出现在冲突分析中，但同一审核包内只能属于一个可写提案。
- `action`：语义判断，说明为什么需要或不需要变更。
- `target`：目标文件与可选章节。现有文件必须绑定 inventory 中的文件哈希；章节操作还必须绑定章节 ID 与哈希。
- `rationale`：简要解释候选与现有知识的关系，不复制完整对话。
- `operation`：可确定执行的有限修改；不产生写入的 action 必须为 `null`。
- `flags`：汇总相关候选的 `assistant_only`、`uncertain`、`time_sensitive` 和 `sensitive`；不得静默丢弃风险标记。

## Action

| 值 | 含义 | 是否允许写操作 |
|---|---|---|
| `add` | 现有知识库没有合适内容，需要新增 | 是 |
| `merge` | 应融合进已有文件或章节 | 是 |
| `no_change` | 已有内容等价且足够 | 否 |
| `conflict` | 候选与已有知识不一致，需要人工判断 | 否 |
| `defer` | 信息、真实性、时效性或敏感性尚不足 | 否 |

不得使用分数替代 action。`conflict` 和 `defer` 第一版只进入审核说明，不能携带可自动应用的 operation。

## Operation

第一版只允许：

- `create_document`：创建尚不存在的 Markdown 文件；`markdown` 是完整文件。
- `insert_section`：在目标章节之后插入完整新章节；目标章节必须存在且哈希匹配。
- `append_to_section`：在目标章节正文末尾追加 Markdown 片段，不改变其子章节。
- `replace_section`：用完整 Markdown 片段替换目标章节，但不包含其子章节。

第一版不允许删除文档、删除章节、重命名文件、移动文件或执行自由形式 patch。任何 operation 都必须包含最终 Markdown 内容，且不得包含未经候选支持的新事实。

## 路径与组合规则

- 目标必须是知识库根目录内规范化的相对 `.md` 路径；拒绝绝对路径、`..`、保留运行目录和符号链接越界。
- `create_document` 的目标必须不存在；其他操作的目标必须与 inventory 文件及章节哈希一致。
- 一个审核包中每个目标文件最多有一个可写提案。多个候选需要修改同一文件时，应合并成一个文件级提案，避免多个操作基于同一个旧哈希相互覆盖。
- 现有风格明确时应沿用原文标题层级、语言和组织方式；不得为了应用候选而顺带格式化无关内容。

## 草稿与最终化

Codex 可以生成与上述结构相同但不包含 `proposal_id` 的语义草稿。确定性脚本在创建审核包前必须：

1. 校验顶层格式与输入哈希；
2. 校验所有候选 ID、action、flags 和唯一性；
3. 校验目标路径、文件哈希、章节定位和操作组合；
4. 生成稳定 `proposal_id`；
5. 生成完整 after 文件和 diff，不能直接执行 Codex 提供的自由文本命令。

