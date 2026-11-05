# run 目录与路径契约

当前格式版本：`conversation-knowledge-curator.run.v2`

完整知识整理任务只使用 `scripts/knowledge_run.py`。模型生成的唯一持久化内容是最终 proposed Markdown；其余阶段数据由 Python 动态构造或保存在一个 `run.json` 中。

## 版本兼容性

| 版本 | 状态 | 主要内容 |
|---|---|---|
| v1 | 不支持 | 多阶段目录、候选、Proposal、Review 包与决定文件 |
| v2 | 当前版本 | 一个 run 记录、一个 proposed 目录、按需 diff 与哈希批准 |

## 固定布局

```text
<knowledge-root>/
└── .knowledge-curator/
    └── runs/
        └── <run_id>/
            ├── run.json
            └── proposed/
                └── <知识库相对路径>.md
```

原始输入保持在用户提供的位置，不复制、移动或修改。应用中断时允许临时出现 `apply-state.json`；成功应用或恢复后必须删除。

## run manifest

```json
{
  "format": "conversation-knowledge-curator.run.v2",
  "run_id": "20261104-database-comparison",
  "status": "review_ready",
  "knowledge_root": "absolute/path/to/knowledge-root",
  "input": {
    "path": "absolute/path/to/export.json",
    "sha256": "...",
    "type": "json_file",
    "branch_policy": "active"
  },
  "parsing": {
    "conversation_count": 1,
    "message_count": 12,
    "warnings": [],
    "batches": []
  },
  "knowledge_base": {"empty": true, "snapshot_sha256": "..."},
  "outputs": [{
    "target_path": "数据库/达梦与MySQL对比.md",
    "proposed_path": "proposed/数据库/达梦与MySQL对比.md",
    "base_sha256": "...",
    "proposed_sha256": "...",
    "sources": [{
      "source_sha256": "...",
      "conversation_id": "conv-001",
      "message_ids": ["msg-001", "msg-002"]
    }]
  }],
  "issues": [{
    "kind": "time_sensitive",
    "message": "版本范围需要复核",
    "target_path": "数据库/达梦与MySQL对比.md"
  }],
  "approval": null,
  "apply": {"status": "not_applied", "entries": []}
}
```

- `run_id` 只允许小写字母、数字和单连字符，长度 1 到 64；目标 run 已存在时拒绝覆盖或追加。
- `knowledge_root`、输入内容哈希和知识库快照在后续命令中重新校验；输入或基线变化时必须新建 run。
- `outputs[].sources` 保存最终文档级来源标识，不建立 Candidate 到 Proposal 的对象图。
- `approval.entries[]` 只绑定 `target_path`、`base_sha256` 和 `proposed_sha256`；proposed 或目标内容变化后拒绝应用。
- `.knowledge-curator` 和 `runs` 已存在时必须是知识库内普通目录，不得是符号链接或目录联接。

## 审核元数据标准输入

`prepare-review` 从标准输入读取 `conversation-knowledge-curator.review-input.v1`，不创建元数据文件：

```json
{
  "format": "conversation-knowledge-curator.review-input.v1",
  "outputs": [
    {
      "target_path": "数据库/达梦与MySQL对比.md",
      "sources": [
        {
          "source_sha256": "...",
          "conversation_id": "conv-001",
          "message_ids": ["msg-001", "msg-002"]
        }
      ]
    }
  ],
  "issues": [
    {
      "kind": "time_sensitive",
      "message": "版本范围需要复核",
      "target_path": "数据库/达梦与MySQL对比.md"
    }
  ]
}
```

- `outputs` 必须与 `proposed/` 中的目标一一对应，不得缺失、重复或引用未知路径。
- 每个目标必须至少登记一组来源。`source_sha256`、`conversation_id` 和全部 `message_ids` 必须来自本次 `read-input` 的可见消息；Python 拒绝伪造、隐藏或不存在的 ID。
- `issues[].kind` 只允许 `uncertain`、`time_sensitive`、`sensitive`、`conflict`、`assistant_only` 或 `other`；`target_path` 可以是对应目标，也可以为 `null` 表示整个 run。
- 顶层和嵌套对象都拒绝未知字段。校验失败不得更新 `run.json`。

## 命令顺序

初始化：

```powershell
python scripts/knowledge_run.py init <knowledge-root> <input-path> <run_id>
```

之后所有阶段只接收同一个 `<run-directory>`：

```powershell
python scripts/knowledge_run.py read-input <run-directory> [--batch N]
python scripts/knowledge_run.py context <run-directory> --query <主题>
<审核元数据 JSON> | python scripts/knowledge_run.py prepare-review <run-directory>
```

Codex 直接写完整的 `proposed/<target>.md`。用户完成审核后运行：

```powershell
python scripts/knowledge_run.py apply <run-directory> --approve <target> [...]
```

检测到中断状态时使用 `recover`。不再提供 inspect、normalize、candidate、proposal、decide 等运行阶段。

默认审核输出按变更类型控制体积：

- 新建文档返回 `change=create`、`proposed_path`、标题结构、行数、来源和哈希，不重复输出完整文档 diff；
- 更新文档返回准确 unified diff；
- 用户明确要求查看完整 diff 时，使用 `prepare-review --full-diff`，此时新建文档也返回 diff。

当前大输入批次没有语义检查点。任务在处理中断后，应重新读取并处理全部批次；不要从部分 proposed 内容推断哪些批次已经覆盖。只有真实样本证明重新处理成本不可接受时，才扩展检查点协议。

## 隐私与 Git

- 整个 run 是私密运行数据，默认不得提交 Git。
- 正式知识库应忽略 `.knowledge-curator/runs/`；`run.json` 只用于本地来源追踪、审核绑定和恢复，不作为正式知识提交。
- run 初始化和准备 Review 不构成修改正式 Markdown 的授权；只有用户明确批准全部目标后才能运行 `apply`。
- 当前安全目标是检测输入、proposed、知识库目标与运行记录之间的不一致，拒绝非法 v2 状态，并让内容变化后的批准失效。`run.json` 没有外部签名，不能抵御能够同时改写本地文件、哈希和审批记录的恶意本地操作者；不得把 Schema 与一致性校验表述为密码学防篡改。
