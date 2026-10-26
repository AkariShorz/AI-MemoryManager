# 每日概要

概要模块从本地会话归档生成每日 JSON 摘要，不调用云端服务或模型。当前采用摘录式概要：按会话展示来源、人设、标题、消息数量，以及少量用户和助手消息片段；它提供会话定位信息，不声称理解或概括整段对话语义。

从仓库根目录运行：

```bash
PYTHONPATH=src python -m ai_memory_manager.summaries.summaries generate
PYTHONPATH=src python -m ai_memory_manager.summaries.summaries generate 2026-10-26
PYTHONPATH=src python -m ai_memory_manager.summaries.summaries list
PYTHONPATH=src python -m ai_memory_manager.summaries.summaries show 2026-10-26
PYTHONPATH=src python -m ai_memory_manager.summaries.summaries locate conversation-id --date 2026-10-26
```

概要保存在 `Session_Lib/summaries/YYYY-MM-DD.json`，包含相对原始会话库的目录和文件清单。Python API：`生成概要(日期=None, 会话库=...)`、`读取概要(日期, 会话库=...)`、`列出概要(会话库=...)`、`定位原始会话(日期, 会话编号, 会话库=...)`。生成全量概要时会清理没有现存归档支持的旧日期；隐私模块在遗忘或删除会话后自动重建概要。

桌面应用的「每日概要」按每页最多 850 字展示，并依据阅读区高度在接近页末的位置换页，使用「上一页」「下一页」阅读；分页只影响显示，不修改概要 JSON。

概要是归档内容的衍生数据，文件权限为仅当前用户读写。不要将实际概要提交到版本控制。重新生成可从原始归档恢复概要。
