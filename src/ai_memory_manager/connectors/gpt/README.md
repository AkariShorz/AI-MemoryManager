# GPT 会话接入

本目录提供 ChatGPT 导出归档、ChatGPT/Codex 可见文本捕获，以及 Codex 会话结束归档。所有数据写入共用的 `src/ai_memory_manager/archive/Session_Lib/`。

## 导出文件导入

`export_import.py` 可读取 ChatGPT 导出的 ZIP 压缩包，或解压后的 `conversations.json`，并把每个会话的完整 JSON 对象写入独立文件。输入文件不被修改，消息分支、附件引用和原字段都会保留。

```python
from ai_memory_manager.connectors.gpt import read_export

conversations = read_export("/路径/到/导出文件.zip")
for conversation in conversations:
    print(conversation.conversation_id, len(list(conversation.message_nodes())))
```

运行前在仓库根目录设置模块搜索路径：

```bash
PYTHONPATH=src python src/ai_memory_manager/connectors/gpt/export_import.py /路径/到/导出文件.zip
```

可自动监视用户指定的下载目录；ChatGPT 官方 ZIP 下载完成并稳定后会自动归档：

```bash
PYTHONPATH=src python src/ai_memory_manager/connectors/gpt/export_import.py \
  --watch-dir ~/Downloads --poll-seconds 3
```

也可指定 `--output-dir <目录>`。ZIP 导入按会话 ID 覆盖同一会话文件，因此重复导入具有幂等性。

归档路径为：

```text
Session_Lib/ChatGPT/<创建日期>/<会话名称>--<会话编号>/<会话名称>.json
```

## 实时捕获原型

`live_capture.mjs` 通过本机 Chrome DevTools Protocol 观察 ChatGPT 浏览器页/内嵌页和 Codex 桌面会话里的可见消息，并将消息新增或变化记录为 JSONL。需要 Node.js 21 或更高版本，以及已启用本机调试端口的客户端。

```bash
node src/ai_memory_manager/connectors/gpt/live_capture.mjs
```

默认按来源、日期和会话分目录写入 `src/ai_memory_manager/archive/Session_Lib/`。每个实时捕获会话包含两个文件：

```text
Session_Lib/
├── ChatGPT/日期/会话名称--会话编号/messages.jsonl
├── ChatGPT/日期/会话名称--会话编号/snapshots.jsonl
└── Codex/日期/会话编号/{messages.jsonl,snapshots.jsonl,session.jsonl}
```

`snapshots.jsonl` 保留每次发生变化的可见文本快照；`messages.jsonl` 按消息编号合并为每条消息的最新版本。相同内容不会重复追加，助手流式生成的中间版本会更新到同一条消息。程序重新启动时会加载已有归档，并兼容读取升级前的实时日志。

也可以指定端口、轮询间隔和输出目录：

```bash
node src/ai_memory_manager/connectors/gpt/live_capture.mjs \
  --port 9222 --poll-ms 700 --output-dir /本地路径/Session_Lib
```

捕获开始后，在 ChatGPT 或 Codex 会话中发送或接收消息。终端显示归档记录后，可查看对应会话文件；按 `Ctrl+C` 停止。快照和合并消息都包含提供方、来源类型、会话编号、会话名称、消息编号、角色、可见文本和采集时间。

## Codex 会话结束时自动归档

Codex 提供 `SessionEnd` 生命周期钩子。本目录的 `codex_session_end_export.py` 在钩子触发时读取事件中的 `transcript_path`，把转录文件逐字节复制到 `Session_Lib/Codex/<结束日期>/<会话编号>/session.jsonl`，并写入 `session.meta.json` 记录会话编号、时间和来源路径。导出过程不解析或改写转录内容；重复结束同一会话时，会用最新完整副本原子替换旧副本。

安装或移除用户级钩子：

```bash
python3 src/ai_memory_manager/connectors/gpt/install_codex_session_end_hook.py install
python3 src/ai_memory_manager/connectors/gpt/install_codex_session_end_hook.py uninstall
```

安装只会增改 `~/.codex/hooks.json` 中本项目的 `SessionEnd` 处理器，保留其他钩子配置。重启 Codex 后，需要审阅并信任该钩子才能运行。若使用自定义 `CODEX_HOME`，安装器会写入该目录。

Codex 在会话归档或删除、正常退出，或会话闲置 30 分钟且没有连接客户端时触发 `SessionEnd`。仅离开会话页面或取消订阅不会立即结束会话。钩子事件是受支持的生命周期接口，但官方说明转录文件格式并非稳定接口；因此本功能只做不透明原样备份，不解析内部数据。钩子事件不提供会话标题，故此路径按会话编号归档。钩子失败会报告错误，但不会阻止会话关闭。

## 已知限制

- 实时捕获依赖客户端页面结构和本机调试端口，不属于 OpenAI 提供的受支持会话 API；页面更新后可能需要调整。
- 只捕获页面中可见的用户和助手文本，无法保证覆盖未加载的历史、隐藏内容、附件或平台内部消息结构。
- ChatGPT 没有可用于个人聊天历史的受支持逐会话自动导出接口。实时捕获只是尽力采集；官方导出 ZIP 需要用户主动申请，下载完成后可由目录监视器自动导入。
- 实时 JSONL 记录是界面文本快照，不满足完整原始会话归档的保真要求。需要完整会话时，应使用官方导出文件补档。
- Codex 自动归档依赖 `SessionEnd` 钩子和事件提供的本地转录路径；原文件格式由 Codex 管理，本项目按不透明文件复制，不依赖其内部结构。
- ZIP 导入、实时捕获和 Codex 钩子均写入共用 `Session_Lib`，会话日期、来源和会话标识作为目录层级。
- 实时采集文件可能包含私人对话，请妥善保管本地文件，并在不需要时自行删除。
- ChatGPT 页面若未登录、提示账户加载失败，或未打开具体会话，就没有可捕获的消息；请先在被监测的桌面端页面登录并打开会话。
