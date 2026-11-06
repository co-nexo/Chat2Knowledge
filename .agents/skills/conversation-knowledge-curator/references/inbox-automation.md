# 固定收件箱与定时处理

当前本地配置格式：`conversation-knowledge-curator.local-settings.v2`

当前收件箱报告格式：`conversation-knowledge-curator.inbox.v1`

仅在用户要求固定输入输出目录、检查未处理导出或安排定时任务时读取本文件。普通的显式路径整理仍使用 `run-layout.md` 中的 `init` 流程。

## 设计边界

- 固定目录属于单台电脑的本地偏好，不得硬编码进 `SKILL.md`，也不得提交包含绝对路径的配置。
- 默认配置文件是仓库根目录的 `.knowledge-curator.local.json`；该路径已加入 `.gitignore`。
- 输入目录和知识库根目录必须预先存在、互不相同且不是链接或目录联接。配置时在输入目录下创建普通目录 `_processed`，并将其记录为 `processed_dir`。
- 收件箱扫描只读原始投递单元。只有对应 Run 成功应用后，才允许把投递单元移动到 `processed_dir`；不会自动删除文件。
- 自动处理最多推进到 `review_ready`。正式 Markdown 仍需用户明确批准后才可应用；定时任务不得调用 `apply`、`recover` 或任何 Git 命令。
- 定时任务需要能够读取输入目录，并能在知识库根目录的 `.knowledge-curator/runs/` 下写入 Run。若运行环境没有这些权限，应停止并报告，不得改写到其他目录规避权限边界。

## 一次性配置

```powershell
python scripts/knowledge_run.py configure-inbox <input-dir> <knowledge-root>
```

命令校验两个目录后，以原子写入方式保存：

```json
{
  "format": "conversation-knowledge-curator.local-settings.v2",
  "input_dir": "C:\\absolute\\inputDir",
  "processed_dir": "C:\\absolute\\inputDir\\_processed",
  "knowledge_root": "C:\\absolute\\Knowledge-Base",
  "branch_policy": "active"
}
```

默认只处理每个对话的活动分支。只有用户明确需要保留全部分支时才使用 `--branch all`。配置对象拒绝未知字段；三个目录在每次使用时都会重新解析和校验。

兼容性：v1 不包含 `processed_dir`，当前命令拒绝继续使用，重新运行一次 `configure-inbox` 即可生成 v2 配置。重新配置不会移动现有投递文件。

配置完成后，日常扫描不再接收输入路径或知识库路径：

```powershell
python scripts/knowledge_run.py inbox
```

## 投递单元

收件箱只检查输入目录的一级内容：

- 一级 `.json` 文件：一个投递单元；
- 一级 `.zip` 文件：一个投递单元；
- 一级子目录：一个投递单元，其中递归发现的全部支持来源进入同一个 Run；
- 配置指向的 `_processed`：保留目录，不参与收件箱发现，也不计入 `ignored`；
- 其他文件、链接和目录联接：不处理，并在 `ignored` 中给出精简原因。

如果几份 JSON 需要结合起来理解，应把它们放进同一个一级子目录或 ZIP。不要把每个文件分别处理后再人工合并。

## 状态与去重

Python 使用完整来源身份和内容 Hash 生成 `unit_ref`，再与知识库中的 Run 对照。若一个单独来源已经包含在旧的已应用组合 Run 中，也按来源内容 Hash 识别为已处理，避免改变投递边界后重复整理：

| 状态 | 含义 | 后续动作 |
|---|---|---|
| `pending` | 没有匹配的当前 Run | 调用 `init-inbox <unit_ref>` |
| `resumable` | 匹配 Run 仍为 `initialized` | 延续该 `run_directory` |
| `awaiting_review` | 匹配 Run 已为 `review_ready` | 只汇报，等待用户审核 |
| `processed` | 当前格式或旧格式中存在已应用 Run | 不重复处理 |
| `invalid` | 无法安全解析投递单元 | 报告名称和错误，不创建 Run |

修改文件内容、增删目录内来源或改变来源位置都会产生新的输入身份，并重新成为 `pending`。删除或移动原始来源会使未完成 Run 无法继续；因此至少保留到审核和应用结束。

`inbox` 只展开需要动作的 `pending`、`resumable`、`awaiting_review` 与 `invalid`，已处理单元只返回数量，避免日常输出随历史增长。

初始化待处理单元：

```powershell
python scripts/knowledge_run.py init-inbox <unit_ref>
```

Run ID 由完整输入 Hash 确定性生成。重复调用不会创建第二个 Run，而是返回现有的可恢复、待审核或已处理状态。

## 应用后的归档

只有 `apply` 已成功完成并使 Run 进入 `applied` 状态后，才运行：

```powershell
python scripts/knowledge_run.py archive-inbox <run-directory>
```

归档前，Python 会重新检查以下条件：

- Run 属于配置绑定的知识库；
- Run 状态确实是 `applied`；
- 输入仍是收件箱中的一级投递单元；
- 原始内容、来源清单和 Hash 与应用时一致；
- 来源和目标不是链接，目标不存在冲突。

归档布局为：

```text
<processed_dir>/
└── <unit_ref>/
    └── <原文件名或原目录名>
```

Hash 子目录避免不同批次的同名文件互相覆盖。重复执行同一归档命令返回 `already_archived`，不会二次移动。归档失败不撤销已经成功应用的 Markdown：输入仍在原位置时可以修正问题后重试；若目标目录存在异常内容则停止并要求人工检查。

## 定时任务调用约定

先在交互任务中完成配置并手动跑通一次 `inbox` 与完整整理流程，再创建每日定时任务。任务应绑定本地项目，并明确调用 `$conversation-knowledge-curator`。推荐任务提示词：

```text
使用 $conversation-knowledge-curator 检查已配置的固定收件箱。处理所有 pending 和 resumable 投递单元，分别推进到 review_ready；awaiting_review 只计入汇总，不重复生成。不要应用正式 Markdown，不要移动或删除输入文件，不要执行任何 Git 操作。最后只报告新增待审核 Run 数、仍待审核 Run 数、无效输入数和需要我处理的阻塞；没有变化时只说“收件箱没有新内容”。
```

自动任务的报告必须保持简短。只有用户随后点名某个 Run 或文档时，才展开来源、风险和完整 diff。

本地定时任务依赖电脑开机且 Codex 桌面应用正在运行。调度时间、实际目录和运行权限属于本机状态，不写入本 Skill 或仓库文档。
