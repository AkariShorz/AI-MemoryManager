# 长期记忆提取与检索

`lifecycle.py` 提供模型无关的对话候选处理、冲突确认、过期、版本记录和提示词检索接口。`process_conversation` 接收归档消息与可替换抽取器；抽取器只给出 `MemoryCandidate`，候选来源必须映射到归档中的用户消息，来源提供方和会话编号由应用注入。结构校验和 SQLite 提交由应用代码完成。模块不读取或改写会话归档，来源通过 `source`、`conversation_id` 和 `source_message_id` 回溯。数据库默认应放在本地受控目录。

## 类型与默认策略

- `stable_preference`、`persona`、`episodic`、`project`、`schedule`、`inferred` 区分长期偏好/人设、事件、项目、时限安排和推断候选。
- 活跃模型通过 `MEMORY_UPDATE_TOOL` 自己判断是否调用 `apply_model_memory_update`；瞬时闲聊或信息不明时应跳过。有效候选自动写入，不弹审批；相同类型和主题的新内容会将旧版标记为 `superseded` 并留下修订记录。
- 相同类型、主题和规范化内容会去重；截止时间由 `expire_due` 标记为 `expired`。模型工具没有删除能力，只有用户界面可执行永久删除并清理该记忆的修订内容。
- `valid_from`/`valid_until` 使用 ISO-8601；`retrieve` 只返回当前有效的 active 记忆。默认结合本机 BGE-M3 向量相似度与完整短语、英文词、汉字二元组匹配，支持近义表达检索，不调用云端 API。
- 模型只提交受 JSON Schema 限制的字段；来源、人设数据库和会话上下文由可信连接器提供。模型不能指定任意路径、运行 SQL 或删除记忆。每个人设使用独立 SQLite 文件，避免不同 AI 身份之间串记忆。
- 桌面端「记忆更新」页显示记忆与最近写入记录，并提供用户专属的永久删除操作。

示例：

```python
from ai_memory_manager.memory.lifecycle import MemoryCandidate, process_conversation, retrieve

messages = [{"id": "message-id", "role": "user", "content": "喜欢简短温和的表达"}]
result = process_conversation(db_path, messages, provider="ChatGPT", conversation_id="conversation-id",
    extractor=lambda archived: [MemoryCandidate(
        memory_type="stable_preference", subject="交流风格", content="喜欢简短温和的表达",
        source="extractor-label", source_message_id="message-id")])[0]
# result.status 为 created 或 duplicate
context = retrieve(db_path, "交流风格")
```

Codex MCP 工具位于 `codex_mcp_server.py`。启用全局人设后，没有单独绑定的 Codex 会话可自动使用该人设的记忆库；单独绑定优先，明确清除的人设不会被全局回退重新启用。工具支持当前及旧版绑定文件格式，拥有调用会话编号时优先使用该编号，避免把另一可见会话的消息编号当作来源。全局人设指令要求模型每轮回复前按当前消息轻量检索相关记忆，并将有效的人设、表达偏好和互动习惯自然用于交流；不强行引用无关往事，且用户最新表达优先。发现有价值的新信息时直接调用 `memory_update`。全局人设指令含自动记忆规则，不再要求每个新会话重复导入。旧的已运行 MCP 进程需一次重新加载；新版 stdio 入口逐次重新加载请求分发代码。工具不含删除操作。其他客户端尚未连接实时工具。

## 自动双通道跟进

1. **当前模型主动写入：** 发现明确、未来有用的信息时自动调用 `memory_update`，只有成功返回后才能声称已保存。
2. **归档独立补录：** 归档完成立即建立本地持久任务；应用启动后台工作进程，每 10 秒扫描补建遗漏任务。任务按用户消息逐条提取重要事实和摘要并写入人设记忆，不需要点击「从归档提取」。当前已加载的 ChatGPT/Codex 可见会话也会自动归档。

任务保存在会话库内的 `.memory_jobs.sqlite3`，有待处理、处理中、已完成、待重试、待分配人设、已取消状态。失败按 5 秒至 5 分钟的退避间隔自动重试，已处理消息不重复送模型；进程退出或崩溃后可从进度继续。工作进程使用独占锁，防止多个窗口重复处理。程序关闭后任务仍在，下次打开继续。命令行可运行 `PYTHONPATH=src python -m ai_memory_manager.memory.worker --once` 处理当前任务，或不带 `--once` 持续处理。

同类型相同内容即使主题标签不同也会去重，提交加写事务锁。旧归档以消息观察时间参与冲突判断，不能覆盖同主题较新的事实。遗忘/删除来源前会取消任务，提取完成写入前再次检查来源版本及取消状态，防止在模型生成期间删除的信息重新写回。未能确定人设的任务会等待路由，不能随意写入其他身份。

对于 Codex 已归档的内嵌图片，可由本机视觉模型结合用户明确的归属确认提取摘要；只读取归档中的 `data:image`，不根据聊天文字任意读取路径或下载外部图片。图片没有归档、文字模糊或模型漏判时仍可能缺失信息。双通道提供补录、重试与可见状态，不能保证模型零遗漏。

已经可见捕获的 Codex 会话，会继续从经过连接器验证的本地转录目录刷新原文副本，包含新消息及内嵌图片；不改动来源文件。消息追加不会丢弃正在生成的未变事实，修改/删除原消息则会阻止旧候选落库。历史课表等资料按历史记忆保留，其适用日期与记忆是否可检索分开处理。

Codex 的 MCP 配置方式参见 [官方文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。本模块不是云端记忆同步服务。

## 从本地归档提取

桌面端「记忆更新」页的「从归档提取」按钮会打开会话选择框，已关联到当前人设的会话默认勾选，用户也可主动选择其他归档。提取器读取用户消息，通过本机 Ollama 的 `qwen3.5:9b` 模型提出结构化候选；近期消息只用于消解指代，已有记忆用于去重和沿用主题。应用验证每条候选的用户消息来源、类型、置信度和期限后写入该人设独立数据库。已处理且内容未变的消息不会再次送给模型，旧归档不会被修改。模型不可用时显示失败原因，不把未处理的消息标记为完成。

默认使用 `http://127.0.0.1:11434/api/chat`，只连接本机。可在启动应用前设置 `AI_MEMORY_OLLAMA_MODEL` 选择本机已安装的模型。命令行入口：

```bash
PYTHONPATH=src python -m ai_memory_manager.memory.archive_extraction src/ai_memory_manager/personas/example.json
```

命令行默认只扫描清单中带目标人设编号的会话，也可重复传入 `--session /完整/会话目录` 显式选择会话。当前的 Codex 对话可走 MCP 工具实时更新，归档提取适用于已有会话和其他有归档来源的客户端。提取依据用户明确陈述，可能漏掉隐含事实；误提取的条目可在界面中删除。通过 privacy 模块遗忘或删除来源会话时，会一并清理该会话产生的记忆、候选和修订。

实现参考 [mem0 的记忆写入与检索流程](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)：先检索相关旧记忆，再从新交互提取独立事实，最后去重、保存来源并按问题检索。本应用沿用自己的本地 SQLite 与人设边界，没有引入 mem0 的向量数据库或云端服务。

## 本地向量语义检索

`semantic.py` 通过本机 Ollama 的 `/api/embed` 生成向量，默认模型为 `bge-m3`。安装命令是 `ollama pull bge-m3`；可用环境变量 `AI_MEMORY_EMBED_MODEL` 切换本机嵌入模型。提取模型与嵌入模型各自独立。

向量保存在每个人设数据库的 `memory_vectors` 表。首次检索按需批量建立当前有效记忆的向量，之后复用缓存；内容哈希或模型摘要改变后重建。查询向量不落盘。永久删除记忆或来源会话时，外键级联清除对应向量；过期、被替代和其他人设的记忆不会参与召回。

混合排序权重为语义 70%、关键词 30%，仅语义命中的默认余弦相似度门槛为 0.45。`retrieve(..., semantic=False)` 可显式使用关键词检索；本机模型不可用或返回无效向量时自动回退。结果携带 `semantic_score`、`retrieval_score` 和 `retrieval_mode`，便于判断本次检索方式。桌面搜索延迟 350 毫秒后在独立进程执行，避免模型加载阻塞界面。当前以精确余弦计算扫描人设内有效向量，适合个人记忆库；大规模近似向量索引尚未实现。
