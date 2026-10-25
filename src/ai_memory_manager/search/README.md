# Search（检索）

本模块为 `archive/Session_Lib` 中的本地会话归档提供全文检索。模块使用 SQLite FTS5 建立可重建的索引，不会改写归档原文。索引内容包括 JSON/JSONL 中的文本字段及纯文本文件；若会话目录含有 `session.meta.json`，则从中读取会话元数据，否则根据归档路径推断。

## 使用方法

在仓库根目录运行：

```bash
PYTHONPATH=src python -m ai_memory_manager.search.search "关键词"
PYTHONPATH=src python -m ai_memory_manager.search.search "关键词" --provider ChatGPT --persona-id persona-1 --date-from 2026-01-01 --date-to 2026-12-31
```

默认索引文件为 `src/ai_memory_manager/search/search-index.sqlite3`。使用 `--index` 可指定其他索引路径，使用 `--archive-dir` 可指定其他会话库目录。每次搜索前都会增量扫描会话库，新增或更新变更文件，并清除已删除文件对应的索引记录。删除索引文件后再次搜索，即可从归档重新建立完整索引。

索引包含可搜索的会话文本和元数据，敏感程度与会话归档相同。请将生成的数据库保存在本地，不要提交到版本控制。运行环境中的 Python SQLite 必须启用 FTS5。
