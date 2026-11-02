# ChatGPT 导出 JSON：已观察结构与不确定性

本文档有意区分仓库证据与兼容性假设。检查真实导出后再更新本文档，不得把猜测字段写成稳定保证。

## 证据状态

项目于 2026-11-02 初始化时，仓库中没有真实 ChatGPT 导出样本。当前测试夹具为合成数据，只用于验证解析器行为。因此，本仓库尚未使用用户真实数据验证任何导出字段。

## 当前解析器接受的输入结构

解析器保守地接受三种容器：

1. 顶层为对话对象列表；
2. 顶层为包含 `mapping` 的单个对话对象；
3. 顶层对象的 `conversations` 字段为列表。

其他容器会被报告为不支持，不会进行猜测性解析。

## 暂定支持的常见结构

以下结构通常与 ChatGPT 数据导出相关，但在与真实样本核对之前，在本项目中仍属于暂定结构：

```text
conversation
|- id, title, create_time, update_time
|- current_node
`- mapping: {node_id: node}
   `- node
      |- id, parent, children
      `- message
         |- id
         |- author: {role, name, metadata}
         |- create_time, update_time
         |- content: {content_type, parts}
         |- metadata
         |- recipient
         `- channel
```

当 `node.id` 缺失时，解析器使用 mapping 字典键作为节点 ID。解析器从有效的 `current_node` 沿 `parent` 链选择活动分支。如果该指针缺失或无效，解析器会发出警告，并确定性地遍历全部节点。

## 可见性规则

只有作者角色严格为 `user` 或 `assistant` 的文本才符合条件。出现下列任一情况时排除消息：

- role 不是 `user` 或 `assistant`；
- 内容类型是 `thoughts`、`reasoning`、`analysis`、`model_editable_context` 或 `system_error`；
- channel 是 `analysis` 或 `reasoning`；
- 消息元数据通过 `is_visually_hidden_from_conversation`、`is_hidden` 或 `hidden` 标记为隐藏；
- assistant 消息的 recipient 不是缺省值、`all` 或 `user`；
- 无法安全提取纯文本。

对于 `content.parts`，只读取字符串值，以及明确包含字符串 `text` 字段且类型为文本的字典值。资源指针、工具数据和未知结构化片段不会被展开为知识文本。

这些规则有意保持保守。当真实样本包含新的用户可见格式时，应先在本文档记录已观察结构，添加脱敏夹具，然后再扩展代码和测试。

## 有意不视为稳定的字段

- ChatGPT 内部或非官方 API URL、身份验证细节和请求结构。
- 内容类型、角色、元数据标记、channel、recipient 和附件编码的完整集合。
- 是否每个导出都提供 `current_node`，以及它是否总能标识用户看到的分支。
- 时间戳单位和可空性。标准化器保留时间戳原值，不解释其含义。
- 未知元数据的语义。未知元数据不会复制到标准化知识输入。

## 安全样本处理流程

修改 schema 处理逻辑前：

1. 对真实导出运行 `inspect`；
2. 只比较结构键、数量、角色、内容类型和警告；
3. 添加夹具前，对消息正文、姓名、标识符和 URL 进行脱敏；
4. 记录实际观察到的字段；
5. 为新支持的变体添加聚焦测试。

除非用户明确批准，否则原始导出不得放入测试夹具。

