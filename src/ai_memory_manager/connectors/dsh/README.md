# DeepSeek Harness（DSH）会话接入

本目录读取本机 DSH 会话，并与共用的 `src/ai_memory_manager/archive/Session_Lib/` 归档流程衔接。会话来源只读，归档不修改 DSH 自己的数据。

## 来源位置与格式

| 内容 | 位置 |
|---|---|
| 会话事件流 | `$DSH_HOME/sessions/<工作区>/session-<编号>/session.v4.jsonl.zstd` |
| 投影缓存（标题等） | `$DSH_HOME/storages/session_projcache/sessions/session-<编号>.json` |

`DSH_HOME` 未设置时使用 `~/.dsh`。会话文件是 zstd 压缩的 JSONL，首行是 `{"type":"session","version":4,...}` 的会话头，之后是 `user/message`、`assistant/message`、`tool/call`、`tool/result`、`step/*` 等事件。一个文件可能由多个 zstd 帧拼接而成，并在对话进行中持续追加；本连接器每次归档都重新解压完整内容，并比较来源文件在读取前后的状态，检测到变化时提示稍后重试。

## 归档结果

```text
Session_Lib/DeepSeek/<日期>/<会话编号>/session.jsonl
Session_Lib/DeepSeek/<日期>/<会话编号>/session.meta.json
```

`session.jsonl` 是解压后的完整事件流，与 DSH 自带 `/export` 导出的 `session.v4.jsonl` 内容一致。`session.meta.json` 中的 `provider` 为 `DeepSeek`，并额外记录：

- `source_connector: "dsh"`、`source_path`、`source_size`、`source_mtime_ns`；
- `source_sha256`：压缩来源文件的哈希，用于判断来源是否更新；
- `transcript_sha256`：归档 `session.jsonl` 的哈希，用于幂等写入；
- `transcript_format`、`source_encoding`：说明归档文件是解压后的 v4 JSONL。

重复归档同一会话时沿用已有日期目录；内容未变则不重写文件，只刷新元数据。归档日期使用来源文件的最后修改日期。

## 显示文本

预览、每日概要和记忆提取只读取可显示的对白：

- `user/message` 中 `source.kind == "user"` 的文本；
- `assistant/message` 中类型为 `text` 的部分。

推理内容、工具调用、工具结果以及运行时注入的上下文（`runtime-context`、`time-context`、`user-approval` 等）仍保留在 `session.jsonl` 原文中，但不进入预览、概要或长期记忆提取。标题优先取 DSH 投影缓存中的 `title`，缺失时回退到会话事件里的标题或第一条用户消息。

## 使用方式

在桌面应用中点击「导入会话」→「本机编码助手会话…」，来源选择「DeepSeek (DSH)」，即可预览标题与更新时间并选择归档。归档完成后，会话出现在「会话库」的 DeepSeek 分组，并可参与搜索、每日概要和长期记忆提取。

```python
from ai_memory_manager.connectors.local_sessions import (
    discover_local_sessions, archive_local_session, transcript_messages,
)

for session in discover_local_sessions("dsh"):
    print(session.session_id, session.title)
```

## 能力边界

- 读取的是 DSH 本机存储格式，不是 DSH 对外承诺的稳定接口；格式升级时可能需要更新本连接器。
- 不做实时捕获，也没有 DSH 侧的会话结束钩子；需要时在界面中重新导入，来源有新内容时会标记「来源有更新」。
- 正在写入的会话可能暂时无法解压，此时归档会提示稍后重试，不会写入半截内容。
- DSH 自带的会话全文索引由 DSH 进程独占，本连接器不打开它的索引库；搜索与概要在本项目的归档和独立索引上完成。
