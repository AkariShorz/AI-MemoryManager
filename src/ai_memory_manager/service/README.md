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

## 前端生命周期接入

前端启动时调用 `startServices()`，前端关闭时调用返回对象的 `stop()`。监测器作为前端子进程运行，不会在前端未启动时单独驻留。可选传入调试端口、轮询间隔和归档目录：

```js
import { startServices } from './service/service_manager.mjs';

const services = startServices({
  port: 9222,
  pollMs: 2000,
  outputDir: '/path/to/Session_Lib',
});

// 在前端的正常关闭流程中调用并等待：
await services.stop();
```

监测器 stdout/stderr 会分别交给 `onMessage` 和 `onError` 回调；默认输出到控制台。重复调用 `stop()` 安全，SIGINT/SIGTERM 也会触发子进程清理。

如果此前启用了旧版用户级单元，可执行以下命令停止并取消登录自启（按需删除旧符号链接）：

```bash
systemctl --user disable --now ai-memory-manager-openai-watcher.service
rm -f ~/.config/systemd/user/ai-memory-manager-openai-watcher.service
systemctl --user daemon-reload
```
