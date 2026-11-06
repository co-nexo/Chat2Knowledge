# run 目录与路径契约

当前格式版本：`conversation-knowledge-curator.run.v8`

完整知识整理任务只使用 `scripts/knowledge_run.py`。模型生成的唯一持久化内容是最终 proposed Markdown；其余阶段数据由 Python 动态构造或保存在一个 `run.json` 中。

## 版本兼容性

| 版本 | 状态 | 主要内容 |
|---|---|---|
| v1 | 不支持 | 多阶段目录、候选、Proposal、Review 包与决定文件 |
| v2 | 不支持 | 单一 run 与 proposed 目录；按字符分批，不含知识库领域线索 |
| v3 | 不支持 | Token 预算批次和知识库领域线索，但只保存聚合输入 Hash |
| v4 | 不支持 | 逐来源清单和消息边界批次；单条消息超预算时初始化失败 |
| v5 | 不支持 | 固定 12,000 字节读取预算、超长消息虚拟分片、按需 diff 与整体快照批准 |
| v6 | 不支持 | 按输入规模选择预算、精简可见输入与来源引用；只校验消息存在性，完整 diff 可选 |
| v7 | 不支持 | 角色感知的来源证据链、证据类型与官方资料；要求一次返回全部完整 diff 才能应用 |
| v8 | 当前版本 | 全量 diff 在内部生成并绑定；默认返回简报，按目标展开详情 |

相关临时契约同样拒绝旧格式：

| 契约 | 当前版本 | 旧版本主要差异 |
|---|---|---|
| `visible-input` | v3 | v2 在每批重复返回来源路径、Hash、时间戳和完整分片审计字段 |
| `review-input` | v3 | v2 没有证据类型、官方资料和角色感知来源链约束 |
| `review` | v5 | v4 把展示模式误作应用门禁，要求一次返回全部完整 diff |

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
  "format": "conversation-knowledge-curator.run.v8",
  "run_id": "20261104-database-comparison",
  "status": "review_ready",
  "knowledge_root": "absolute/path/to/knowledge-root",
  "input": {
    "path": "absolute/path/to/export.json",
    "sha256": "...",
    "type": "json_file",
    "branch_policy": "active",
    "sources": [{
      "kind": "json_file",
      "path": "absolute/path/to/conversations.json",
      "member_path": null,
      "sha256": "...",
      "size_bytes": 12345
    }]
  },
  "parsing": {
    "conversation_count": 1,
    "message_count": 2,
    "warnings": [],
    "read_output_budget_bytes": 48000,
    "batches": [{
      "index": 1,
      "conversation_count": 1,
      "message_count": 2,
      "part_count": 2,
      "output_bytes": 4200,
      "conversations": [{
        "source_sha256": "...",
        "conversation_id": "conv-001",
        "message_count": 2,
        "part_count": 2,
        "parts": [{
          "message_id": "msg-user-001",
          "part_index": 1,
          "part_count": 1,
          "start_byte": 0,
          "end_byte": 300,
          "message_sha256": "..."
        }, {
          "message_id": "msg-assistant-001",
          "part_index": 1,
          "part_count": 1,
          "start_byte": 0,
          "end_byte": 600,
          "message_sha256": "..."
        }]
      }]
    }]
  },
  "knowledge_base": {
    "empty": false,
    "snapshot_sha256": "...",
    "document_count": 3,
    "domains": ["软件工程", "数据库"]
  },
  "outputs": [{
    "target_path": "数据库/达梦与MySQL对比.md",
    "proposed_path": "proposed/数据库/达梦与MySQL对比.md",
    "base_sha256": "...",
    "proposed_sha256": "...",
    "sources": [{
      "source_sha256": "...",
      "conversation_id": "conv-001",
      "message_ids": ["msg-user-001", "msg-assistant-001"]
    }],
    "evidence_kinds": [
      "conversation_statement",
      "official_verification"
    ],
    "official_references": [{
      "title": "达梦数据库官方手册",
      "url": "https://example.com/official-manual",
      "supports": "支持外键索引行为的版本范围说明"
    }]
  }],
  "issues": [{
    "kind": "time_sensitive",
    "message": "版本范围需要复核",
    "target_path": "数据库/达梦与MySQL对比.md"
  }],
  "review": {
    "entries": [{
      "target_path": "数据库/达梦与MySQL对比.md",
      "base_sha256": "...",
      "proposed_sha256": "...",
      "diff_sha256": "..."
    }]
  },
  "approval": null,
  "apply": {"status": "not_applied", "entries": []}
}
```

- `run_id` 只允许小写字母、数字和单连字符，长度 1 到 64；目标 run 已存在时拒绝覆盖或追加。
- `knowledge_root`、输入内容哈希和知识库快照在后续命令中重新校验；输入或基线变化时必须新建 run。
- `input.sources` 按发现顺序记录每个 JSON 文件或 ZIP 成员的绝对容器路径、成员路径、内容 Hash 和字节数；`input.sha256` 必须由完整来源清单确定性派生。内容相同的重复来源保留在清单中，但只处理一次语义内容。
- `parsing.read_output_budget_bytes` 约束完整 `read-input` JSON 的实际 UTF-8 字节数，不把字节数误称为 Token 数。Python 根据精简可见输入体积自动选择 48,000、32,000 或 24,000 字节：小输入尽量一次返回，输入越大越保守。程序优先保留完整 Conversation，其次保留连续 user/assistant 消息单元；单条消息超过预算时在内存中规划虚拟分片，不拒绝整个 Run。
- `parsing.batches[].parts` 只保存 message ID、分片序号、UTF-8 字节范围和原消息 Hash，不保存正文。分片优先选择段落、换行和句末边界，必要时退到 Unicode 安全边界；所有分片必须连续、无重复、无遗漏，并能按顺序无损重组为标准化后的完整可见消息。
- `knowledge_base.domains` 是从现有相对路径和根级文档标题提取的顶层领域线索，只帮助 Codex 判断哪些领域值得调用 `context`，不自动决定分类。
- `outputs[].sources` 保存最终文档级来源标识；`evidence_kinds` 区分对话陈述、官方核验与工程判断，`official_references` 保存直接支持主要结论的官方资料。run 不建立 Candidate 到 Proposal 的对象图。
- `review.entries` 绑定每个目标的基线、proposed 和确定性 diff 哈希。展示简报还是详情不改变绑定；只要审核准备成功，准确 diff 已经在内存中生成并核验。
- `approval.entries[]` 只绑定 `target_path`、`base_sha256` 和 `proposed_sha256`；proposed 或目标内容变化后拒绝应用。
- `apply` 在任何正式写入前重新计算整个知识库快照；即使变化发生在本次目标之外，旧审核也失效。
- `.knowledge-curator` 和 `runs` 已存在时必须是知识库内普通目录，不得是符号链接或目录联接。

## 审核元数据标准输入

`prepare-review` 从标准输入读取 `conversation-knowledge-curator.review-input.v3`，不创建元数据文件。模型提交读取时看见的精简来源链、证据类型和官方资料，Python 负责恢复完整来源：

```json
{
  "format": "conversation-knowledge-curator.review-input.v3",
  "outputs": [
    {
      "target_path": "数据库/达梦与MySQL对比.md",
      "sources": [
        {
          "conversation_ref": "c1",
          "message_ids": ["msg-user-001", "msg-assistant-001"]
        }
      ],
      "evidence_kinds": [
        "conversation_statement",
        "official_verification"
      ],
      "official_references": [{
        "title": "达梦数据库官方手册",
        "url": "https://example.com/official-manual",
        "supports": "支持外键索引行为的版本范围说明"
      }]
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
- 每个目标必须至少登记一组来源。`conversation_ref` 和全部 `message_ids` 必须来自本次 `read-input` 的可见消息，并按原对话顺序排列；Python 拒绝伪造、隐藏、不存在或乱序的 ID，并把通过校验的引用恢复为 `source_sha256`、`conversation_id` 与 `message_ids` 写入 `run.json.outputs[].sources`。
- 选中的每条 assistant 消息应有更早的已选 user 消息作为问题或约束上下文。没有前置 user 时，对应目标必须登记 `assistant_only` issue；纯 user 来源不受此限制。
- `evidence_kinds` 必须包含 `conversation_statement`，并可按实际内容增加 `official_verification`、`engineering_judgment`。不得登记没有实际内容支撑的证据类型。
- `official_verification` 与非空 `official_references` 必须同时出现。每条官方资料包含非空的 `title`、HTTP(S) `url` 和 `supports`；URL 必须原样出现在对应 proposed Markdown 中。Python 只校验结构与链接保留，资料是否确属权威官方来源仍由 Codex 在写作和审核时判断。
- `issues[].kind` 只允许 `uncertain`、`time_sensitive`、`sensitive`、`conflict`、`assistant_only` 或 `other`；`target_path` 可以是对应目标，也可以为 `null` 表示整个 run。
- 顶层和嵌套对象都拒绝未知字段。校验失败不得更新 `run.json`。
- 输入解码接受 UTF-8、UTF-8 BOM 和带 BOM 的 UTF-16。Windows PowerShell 5.1 使用管道时，先执行 `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`；PowerShell 7 可直接把 JSON 字符串通过管道传给命令。

## 命令顺序

初始化：

```powershell
python scripts/knowledge_run.py init <knowledge-root> <input-path> <run_id>
```

`<input-path>` 可以是单个 ChatGPT JSON、包含一个或多个 ChatGPT JSON 的 ZIP，或包含这些来源的目录。Python 对目录进行确定性递归发现，跳过符号链接和目录联接；所有来源必须进入同一 Run，调用者不得自行拆成多个 Run。

之后所有阶段只接收同一个 `<run-directory>`：

```powershell
python scripts/knowledge_run.py read-input <run-directory> [--batch N]
python scripts/knowledge_run.py context <run-directory> --query <主题>
<审核元数据 JSON> | python scripts/knowledge_run.py prepare-review <run-directory>
<审核元数据 JSON> | python scripts/knowledge_run.py prepare-review <run-directory> --full-diff --target <target>
```

第一条命令完成全部校验、生成并绑定全部准确 diff，但只返回可直接用于批准询问的简短报告。用户点名某个目标后再运行第二条命令；可重复 `--target` 展开多个目标，明确要求全部详情时才省略 `--target`。`--target` 必须与 `--full-diff` 同时使用。每次调用仍会重新校验全部 proposed 和审核元数据，后续 proposed、目标基线或审核元数据变化都会使旧批准失效。

Codex 直接写完整的 `proposed/<target>.md`。用户完成审核后运行：

```powershell
python scripts/knowledge_run.py apply <run-directory> --approve <target> [...]
```

检测到中断状态时使用 `recover`。恢复采用全量状态预检后再回滚；成功恢复后重复执行返回 `already_recovered`，不会二次修改文件。不再提供 inspect、normalize、candidate、proposal、decide 等运行阶段。

当 `batch_count > 1` 时，`read-input` 必须显式指定 `--batch N`，程序拒绝一次返回全部消息。每个批次的 `output_bytes` 不超过 `read_output_budget_bytes`；调用者必须处理全部批次，不能用终端显示截断代替批次读取。

`read-input` 使用 `conversation-knowledge-curator.visible-input.v3`，顶层只返回批次信息和 `conversations`。每个对话只包含 `conversation_ref`、`conversation_id`、标题及消息；每条消息只包含 `message_id`、角色和正文。来源路径、来源 Hash、时间戳、分片字节范围和消息 Hash 只保存在 Python 维护的 `run.json`，不在每批中重复消耗模型上下文。

```json
{
  "format": "conversation-knowledge-curator.visible-input.v3",
  "batch": 1,
  "batch_count": 1,
  "output_bytes": 4200,
  "conversations": [{
    "conversation_ref": "c1",
    "conversation_id": "conv-001",
    "title": "示例对话",
    "messages": [{
      "message_id": "msg-001",
      "role": "user",
      "text": "可见正文"
    }]
  }]
}
```

未分片消息保持普通消息形态；只有 `part_count > 1` 时才在消息上增加只含 `part_index` 与 `part_count` 的 `fragment`。同一 `message_id` 的所有分片仍是一条原始消息，不能独立解释、分别归档或重复登记来源；完整字节范围和 Hash 仍由 Python 校验。

批次边界由输入内容确定，相同输入和批次编号会返回相同的可见消息，因此允许安全重读。`run.json` 不记录模型无法被 Python 验证的“语义处理完成”状态，也不提供 `checkpoint-batch`；中断时重读当前批次，最终由 `prepare-review` 校验实际引用的来源和消息。

审核输出按渐进披露控制体积：

- 审核输出使用 `conversation-knowledge-curator.review.v5`。顶层 `presentation` 为 `summary` 或 `full_diff`；审核成功后 `apply_ready` 均为 `true`，因为两种展示模式都会生成并绑定全部准确 diff。
- 默认 `summary` 为每个目标只返回路径、创建或更新、标题、行数、增删行数、标题与章节变化数量、来源对话与消息数量、证据类型、官方资料数量和风险数量。不返回正文、哈希、完整标题树、来源消息预览或完整 diff。
- Codex 应把默认结果压缩为总体数量、风险和精简文件清单，直接询问是否批准；不得把命令 JSON 逐字段复述给用户。
- `--full-diff --target <target>` 只返回指定目标的详细项，包括哈希、完整标题树、来源标识、按原顺序排列的消息角色与最多 240 字符预览、官方资料、结构化差异摘要和准确 unified diff。预览不写入 `run.json`。
- 指定 `--target` 时，响应中的 `issues` 只返回全局风险和所选目标风险；默认简报仍返回全部风险。
- 多次使用 `--target` 可展开多个目标；省略 `--target` 会返回全部目标详情，只能在用户明确要求查看全部时使用。
- 顶层 `output_count` 始终表示全部审核目标数，`returned_output_count` 表示本次实际返回的简报或详细项数量。

当前大输入批次没有语义检查点。任务在处理中断后，应重新读取并处理全部批次；不要从部分 proposed 内容推断哪些批次已经覆盖。只有真实样本证明重新处理成本不可接受时，才扩展检查点协议。

## 隐私与 Git

- 整个 run 是私密运行数据，默认不得提交 Git。
- 正式知识库应忽略 `.knowledge-curator/runs/`；`run.json` 只用于本地来源追踪、审核绑定和恢复，不作为正式知识提交。
- run 初始化和审核准备都不构成修改正式 Markdown 的授权；只有用户根据简报及其主动查看的详情明确批准后才能运行 `apply`。
- 当前安全目标是检测输入、消息分片、proposed、知识库目标与运行记录之间的不一致，拒绝不兼容旧格式，并让内容变化后的批准失效。`run.json` 没有外部签名，不能抵御能够同时改写本地文件、哈希和审批记录的恶意本地操作者；不得把 Schema 与一致性校验表述为密码学防篡改。
