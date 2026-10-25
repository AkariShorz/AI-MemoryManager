# 后台服务

## OpenAI 连接监测器

`openai_connection_watcher.mjs` 通过本机 Chrome DevTools Protocol 监测页面和 WebView 的请求地址。检测到 `openai.com`、`chatgpt.com`、`oaistatic.com` 或 `oaiusercontent.com` 域名后，会自动启动 GPT 实时捕获脚本。

```bash
node src/ai_memory_manager/service/openai_connection_watcher.mjs
```

常用选项：

```bash
node src/ai_memory_manager/service/openai_connection_watcher.mjs \
  --port 9222 --poll-ms 2000 --output-dir src/ai_memory_manager/archive/Session_Lib
```

监测器仅观察请求 URL 的主机名，不读取请求正文。它只能监测此调试端口暴露的页面；未启用本机调试端口或不在该端口中的客户端不会被发现。按 `Ctrl+C` 会停止监测器并结束其启动的实时捕获脚本。若已手动启动实时捕获，请先停止手动进程，避免重复采集。

### 登录后自动运行

仓库提供 `ai-memory-manager-openai-watcher.service` 用户级 systemd 单元。安装后，监测器会在用户登录时启动：

```bash
mkdir -p ~/.config/systemd/user
ln -s /home/user/Ai-MemoryManager/src/ai_memory_manager/service/ai-memory-manager-openai-watcher.service ~/.config/systemd/user/ai-memory-manager-openai-watcher.service
systemctl --user daemon-reload
systemctl --user enable --now ai-memory-manager-openai-watcher.service
```

可用 `systemctl --user status ai-memory-manager-openai-watcher.service` 查看状态；日志可用 `journalctl --user -u ai-memory-manager-openai-watcher.service` 查看。
