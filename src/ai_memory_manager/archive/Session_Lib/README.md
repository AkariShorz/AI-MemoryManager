# 会话文件库

此目录保存导入或实时捕获的会话。ChatGPT 和 Codex 共用 OpenAI 会话库，并按客户端分目录；其他连接器仍按来源直接分目录：

```text
OpenAI/<客户端>/<日期>/<会话名称>--<会话编号>/<会话文件>
<其他来源>/<日期>/<会话名称>--<会话编号>/<会话文件>
```

OpenAI 当前包含 `ChatGPT` 和 `Codex`。应用将它们归入同一个 OpenAI 分组，列表和元数据仍保留具体客户端来源。历史上已归档在 `Session_Lib/ChatGPT` 或 `Session_Lib/Codex` 的会话继续兼容读取，不会自动搬动或改写。

DeepSeek Harness（DSH）会话归档在 `DeepSeek/<日期>/<会话编号>/session.jsonl`，内容是解压后的 DSH v4 事件流；来源路径、压缩文件哈希和归档文件哈希记录在 `session.meta.json`。

ChatGPT ZIP 导入保存完整会话 JSON；界面实时捕获保存 `messages.jsonl` 合并视图和 `snapshots.jsonl` 变化记录；Codex 会话结束钩子原样保存本地转录。

支持统一清单的归档入口会在会话目录旁写入 `session.meta.json`，记录 `schema_version`、提供方、可选的人设编号、会话编号、日期、归档方式及文件列表。清单是可重建的索引信息，不替代或改写会话原文。暂时没有人设映射时 `persona_id` 为 `null`。

Codex 的实时捕获与会话结束转录使用同一会话编号目录，标题可从消息记录和元数据中查看。

会话内容可能包含私人信息。本目录中的实际会话数据默认由 Git 忽略，仅保留本说明和目录占位文件。
