# 会话对账

实时捕获器在消息到达时立即写入 `snapshots.jsonl`，并更新按 `message_id` 去重的 `messages.jsonl`。本模块每轮扫描快照日志，并在物化消息视图缺失、损坏或落后时重建它；快照是本地已观察消息的依据，原始来源会话文件不会被改写。

当前连接器没有受支持的接口来重新拉取 ChatGPT 或 Codex 的完整历史。因此该对账修复本地捕获数据的一致性，不代表与服务端完整历史同步；未加载或未捕获的消息无法由本模块补回。

手动运行一轮：

```bash
PYTHONPATH=src python -m ai_memory_manager.reconciliation.reconciliation
```

每 2 分钟持续对账：

```bash
PYTHONPATH=src python -m ai_memory_manager.reconciliation.reconciliation --watch
```

默认间隔为 120 秒，可通过 `--interval-seconds` 调整，也可用 `--archive-dir` 指定会话库。实时捕获仍负责消息到达后的即时保存，持续对账作为一致性修复机制运行。
