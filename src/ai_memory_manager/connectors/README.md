# 本机会话连接器

参考 [OpenMemory 的 adapter 契约与本机发现流程](https://github.com/mem0ai/openmemory)，本目录把“来源检测、会话发现、预览、选择归档”作为独立步骤。当前桌面应用支持从本机选择 Codex 和 Claude Code 会话；ChatGPT 官方导出文件仍走 `gpt/export_import.py`。

| 来源 | 发现位置 | 可用操作 | 保存内容 |
|---|---|---|---|
| Codex | `$CODEX_HOME/sessions`，默认 `~/.codex/sessions` | 检测、预览、选择归档 | `rollout-*.jsonl` 原始字节 |
| Claude Code | `~/.claude/projects` | 检测、预览、选择归档；跳过 `subagents` | 会话 JSONL 原始字节 |
| DeepSeek (DSH) | `$DSH_HOME/sessions`，默认 `~/.dsh/sessions` | 检测、预览、选择归档 | 解压后的 `session.v4.jsonl` 事件流 |
| ChatGPT | 用户提供的官方 ZIP 或 `conversations.json` | 导入完整会话对象 | 官方导出 JSON |

点击应用右上角「导入会话」→「本机编码助手会话…」，可以按来源浏览预览、选择一个或多个会话再归档。已有会话标记为「已归档」；来源文件的大小或修改时间变化时标记为「来源有更新」。再次选择会更新其本地副本。原始文件不会被修改；Codex 会话写入 `Session_Lib/OpenAI/Codex/<日期>/<会话编号>/session.jsonl`，DeepSeek 会话写入 `Session_Lib/DeepSeek/<日期>/<会话编号>/session.jsonl`，其他来源仍写入 `Session_Lib/<来源>/<日期>/<会话编号>/session.jsonl`，并在 `session.meta.json` 记录具体来源、连接器、来源路径、大小、修改时间和 SHA-256。ChatGPT 与 Codex 在应用中合并到 OpenAI 分组，但每段会话仍标明具体客户端。相同会话编号会沿用已存在的归档日期，避免更新时产生第二份目录。

DeepSeek 会话在 DSH 本机是 zstd 压缩的多帧 JSONL；归档时解压为 DSH 自带导出格式，来源文件哈希和归档文件哈希都写入清单，便于判断来源更新与重复导入。格式与限制见 `dsh/README.md`。

`local_sessions.py` 提供可供其他界面调用的接口：`detect_local_connectors()`、`discover_local_sessions()`、`archive_local_session()`、`transcript_messages()`。发现只读取会话开头用于生成标题；归档时原样复制整个文件。预览和每日概要只提取可显示的用户及助手文本，跳过工具调用和推理记录；这些记录仍在原始副本中。若来源文件正在写入且复制期间发生变化，归档会提示稍后重试。

此流程仅把本机会话导入知忆的归档库，不会往 Codex、Claude Code 或 DSH 写入会话。来源客户端的私有转录格式可能变化；格式变化时原文副本仍可保存，但文字预览可能需要更新连接器。
