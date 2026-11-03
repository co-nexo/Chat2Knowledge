# knowledge retrieval 格式契约

当前格式版本：`conversation-knowledge-curator.knowledge-retrieval.v1`

Knowledge retrieval 是从知识候选到现有 Markdown 章节的确定性关键词召回结果。它只缩小 Codex 后续需要阅读的范围，不判断内容是否等价、冲突或应该写入知识库。

## 版本兼容性

| 版本 | 状态 | 主要差异 |
|---|---|---|
| v1 | 当前版本 | 按文件、标题、标题路径和章节正文进行可解释的关键词排序 |

## 顶层结构

```json
{
  "format": "conversation-knowledge-curator.knowledge-retrieval.v1",
  "status": "ok",
  "inputs": {
    "knowledge_candidates_sha256": "hex",
    "knowledge_inventory_sha256": "hex"
  },
  "parameters": {
    "limit": 5
  },
  "results": [],
  "warnings": []
}
```

- `status` 和 `warnings` 继承 inventory 的完整性状态。被跳过的链接不会因召回阶段而消失。
- 输入哈希针对实际 JSON 文件字节计算，用于把召回结果绑定到候选和知识库快照。
- `limit` 是每个候选最多保留的匹配章节数，第一版允许 `1` 到 `20`。
- `results` 与候选一一对应，按 `candidate_id` 稳定排序；没有匹配时保留空 `matches`。

## Result

```json
{
  "candidate_id": "kc_<hash>",
  "matches": []
}
```

召回结果不复制候选 statement 或 Markdown 正文。调用方根据候选 ID 和章节定位读取原始输入，避免产生额外私密内容副本。

## Match

```json
{
  "path": "preferences/development-tools.md",
  "file_sha256": "hex",
  "section_id": "ks_<hash>",
  "section_sha256": "hex",
  "heading_path": ["开发工具偏好", "默认 IDE"],
  "score": 18,
  "matched_terms": ["ide", "开发"]
}
```

- `path`、文件哈希和章节字段必须与输入 inventory 完全一致。
- `score` 只用于当前候选内部排序，不是相似度概率或知识价值评分。
- `matched_terms` 说明哪些规范化关键词参与得分，不代表语义等价。
- 排序先按 `score` 降序，再按路径、章节在文档中的顺序和 `section_id` 稳定决定。

## 关键词与计分规则

第一版不依赖分词库、Embedding 或数据库：

- 英文、数字和常见技术标识按连续词提取并转为小写。
- 连续中文文本只生成相邻双字词；单个汉字和完整长句不进入输出，避免通过 `matched_terms` 复制候选原文。
- 同一关键词在路径、文档标题、标题路径和章节正文中分别只计分一次。
- 路径命中权重为 `8`，文档标题为 `6`，标题路径为 `5`，章节正文为 `1`。
- 得分为 `0` 的章节不输出；算法只做字面包含匹配，不进行语义扩展。

## 输入与安全校验

运行 `scripts/knowledge_retrieval.py retrieve <knowledge-root> <inventory.json> <knowledge-candidates.json> <new-output-file> [--limit N]` 前必须：

- 校验候选和 inventory 的顶层 `format`、字段类型、唯一 ID 和引用形状；
- 确认 inventory 中的相对路径仍位于显式知识库根目录内；
- 拒绝绝对路径、`..`、反斜杠、保留运行目录和链接路径；
- 重新计算每个 Markdown 文件和章节哈希，任何变化都停止召回；
- 在全部输入与快照校验成功后才创建输出，目标已存在时拒绝覆盖。

退出码 `0` 表示完整召回，`1` 表示召回成功但继承了 inventory 警告，`2` 表示失败且没有正式输出。

