# Privacy：遗忘与删除

该模块处理本地归档中的精确文本遗忘和整段会话删除。默认只预览；执行变更必须显式传入 `--apply`（命令行）或 `确认=True`（Python API）。执行后会调用搜索索引的增量扫描，更新被修改文件的索引，移除已删除文件的索引项，并压缩 SQLite 索引、截断 WAL。

## 预览文本遗忘

```bash
PYTHONPATH=src python -m ai_memory_manager.privacy.privacy forget "要遗忘的原文"
```

确认后执行：

```bash
PYTHONPATH=src python -m ai_memory_manager.privacy.privacy forget "要遗忘的原文" --apply
```

精确匹配归档文件中的字符串值和可检索文本，替换为 `[已遗忘]`。JSON/JSONL 结构和其他字段保留。执行后会从更新后的原文重建每日概要和全文索引；概要不会作为原文被检索。

对默认会话库执行遗忘或会话删除时，还会清理受影响会话产生的长期记忆、候选、修订和提取进度，防止衍生记忆保留被遗忘的事实。文本遗忘会清理整段受影响会话的衍生记忆，以涵盖模型改写后的表述。对自定义会话库，Python API 可传入 `记忆目录` 指定要同步清理的人设数据库目录。

## 预览并删除会话

```bash
PYTHONPATH=src python -m ai_memory_manager.privacy.privacy delete-session ChatGPT conversation-id
```

确认删除：

```bash
PYTHONPATH=src python -m ai_memory_manager.privacy.privacy delete-session ChatGPT conversation-id --apply
```

按 `session.meta.json` 中完全相等的来源和会话编号匹配，删除匹配的会话目录及其原始文件，并重建概要和搜索索引。对于缺少或损坏清单的目录，不会猜测编号或删除。

两种操作都支持 `--archive-dir` 和 `--index` 指定路径。Python API：`预览遗忘(...)`、`遗忘内容(..., 确认=True)`、`删除会话(..., 确认=True)`。

索引压缩用于清理 SQLite 可复用页和 WAL 中的旧索引内容。文件系统或 SSD 的写时复制、日志和闪存磨损均衡机制可能保留底层旧数据；应用无法保证对介质进行取证级擦除。若数据目录位于用户控制的加密卷，删除密钥或销毁加密卷是更强的介质级处置方式。
