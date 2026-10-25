#!/usr/bin/env node
/**
 * 监听本机调试端口中的 OpenAI 相关请求，并在首次发现时启动 GPT 实时捕获脚本。
 */

import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const 参数 = process.argv.slice(2);
const 取参数 = (名称, 默认值) => {
  const 位置 = 参数.indexOf(`--${名称}`);
  return 位置 >= 0 && 参数[位置 + 1] ? 参数[位置 + 1] : 默认值;
};

const 端口 = Number(取参数('port', '9222'));
const 轮询间隔 = Math.max(1000, Number(取参数('poll-ms', '2000')));
const 输出目录 = 取参数('output-dir', '');
const 域名白名单 = (取参数('domains', 'openai.com,chatgpt.com,oaistatic.com,oaiusercontent.com'))
  .split(',').map((域名) => 域名.trim().toLowerCase()).filter(Boolean);
const 当前目录 = dirname(fileURLToPath(import.meta.url));
const 采集脚本 = resolve(当前目录, '..', 'connectors', 'gpt', 'live_capture.mjs');

if (参数.includes('--help') || 参数.includes('-h')) {
  console.log(`用法：node openai_connection_watcher.mjs [选项]

监听本机 Chrome DevTools Protocol 请求；发现 OpenAI 相关域名后，自动启动 GPT 实时捕获脚本。

选项：
  --port <端口>          调试端口，默认 9222
  --poll-ms <毫秒>       页面检查间隔，默认 2000，最小 1000
  --domains <域名列表>   逗号分隔，默认 ${域名白名单.join(',')}
  --output-dir <目录>    传递给实时捕获脚本的会话保存目录
  --help                 显示帮助

检测仅适用于该调试端口暴露的页面或 WebView。`);
  process.exit(0);
}

function 属于目标域名(地址文本) {
  try {
    const 主机名 = new URL(地址文本).hostname.toLowerCase();
    return 域名白名单.some((域名) => 主机名 === 域名 || 主机名.endsWith(`.${域名}`));
  } catch {
    return false;
  }
}

class 调试客户端 {
  constructor(地址, 收到事件) {
    this.套接字 = new WebSocket(地址);
    this.序号 = 0;
    this.等待中 = new Map();
    this.已连接 = new Promise((resolve, reject) => {
      const 定时器 = setTimeout(() => reject(new Error('连接调试页面超时')), 8000);
      this.套接字.addEventListener('open', () => {
        clearTimeout(定时器);
        resolve();
      }, { once: true });
      this.套接字.addEventListener('error', () => {
        clearTimeout(定时器);
        reject(new Error('无法连接调试页面'));
      }, { once: true });
    });
    this.套接字.addEventListener('message', (事件) => {
      let 消息;
      try {
        消息 = JSON.parse(事件.data);
      } catch {
        return;
      }
      if (消息.method) 收到事件(消息);
      if (!消息.id || !this.等待中.has(消息.id)) return;
      const { resolve, reject, timer } = this.等待中.get(消息.id);
      clearTimeout(timer);
      this.等待中.delete(消息.id);
      if (消息.error) reject(new Error(消息.error.message));
      else resolve(消息.result);
    });
  }

  async 发送(方法, 参数 = {}) {
    await this.已连接;
    const id = ++this.序号;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.等待中.delete(id);
        reject(new Error(`${方法} 调用超时`));
      }, 5000);
      this.等待中.set(id, { resolve, reject, timer });
      this.套接字.send(JSON.stringify({ id, method: 方法, params: 参数 }));
    });
  }

  关闭() {
    try {
      this.套接字.close();
    } catch {
      // 调试页面可能已关闭。
    }
  }
}

const 页面客户端 = new Map();
let 采集进程 = null;
let 上次启动时间 = 0;
let 正在停止 = false;
let 上次检查错误 = '';

function 启动采集器(主机名) {
  if (采集进程 || 正在停止 || Date.now() - 上次启动时间 < 10000) return;
  上次启动时间 = Date.now();
  const 子进程参数 = [采集脚本, '--port', String(端口)];
  if (输出目录) 子进程参数.push('--output-dir', 输出目录);
  console.log(`检测到 OpenAI 相关请求（${主机名}），正在启动实时捕获。`);
  采集进程 = spawn(process.execPath, 子进程参数, { stdio: 'inherit', env: process.env });
  采集进程.once('error', (错误) => {
    console.error(`无法启动实时捕获脚本：${错误.message}`);
    采集进程 = null;
  });
  采集进程.once('exit', (代码, 信号) => {
    console.log(`实时捕获脚本已退出（${信号 || 代码}）。`);
    采集进程 = null;
  });
}

async function 检查页面() {
  const 响应 = await fetch(`http://127.0.0.1:${端口}/json/list`);
  if (!响应.ok) throw new Error(`调试端口返回 ${响应.status}`);
  const 页面列表 = await 响应.json();
  const 可监测页面 = 页面列表.filter((页面) =>
    页面.webSocketDebuggerUrl && ['page', 'webview'].includes(页面.type));
  const 当前编号 = new Set(可监测页面.map((页面) => 页面.id));

  for (const [编号, 客户端] of 页面客户端) {
    if (当前编号.has(编号)) continue;
    客户端.关闭();
    页面客户端.delete(编号);
  }

  for (const 页面 of 可监测页面) {
    if (属于目标域名(页面.url)) 启动采集器(new URL(页面.url).hostname);

    let 客户端 = 页面客户端.get(页面.id);
    if (!客户端) {
      客户端 = new 调试客户端(页面.webSocketDebuggerUrl, (消息) => {
        if (消息.method !== 'Network.requestWillBeSent') return;
        const 请求地址 = 消息.params?.request?.url;
        if (属于目标域名(请求地址)) 启动采集器(new URL(请求地址).hostname);
      });
      页面客户端.set(页面.id, 客户端);
      try {
        await 客户端.发送('Network.enable');
      } catch (错误) {
        客户端.关闭();
        页面客户端.delete(页面.id);
        console.log(`无法监听页面 ${页面.id} 的网络请求：${错误.message}`);
      }
    }
  }
}

async function 停止() {
  if (正在停止) return;
  正在停止 = true;
  console.log('\n连接监测器已停止。');
  for (const 客户端 of 页面客户端.values()) 客户端.关闭();
  页面客户端.clear();
  if (采集进程) {
    const 子进程 = 采集进程;
    const 已退出 = new Promise((完成) => {
      if (子进程.exitCode !== null || 子进程.signalCode !== null) {
        完成(true);
        return;
      }
      子进程.once('exit', () => 完成(true));
    });
    采集进程.kill('SIGTERM');
    const 优雅退出 = await Promise.race([
      已退出,
      new Promise((完成) => setTimeout(() => 完成(false), 3000)),
    ]);
    if (!优雅退出 && 子进程.exitCode === null && 子进程.signalCode === null) {
      console.log('实时捕获未在 3 秒内退出，正在结束残留进程。');
      子进程.kill('SIGKILL');
      await Promise.race([
        已退出,
        new Promise((完成) => setTimeout(完成, 1000)),
      ]);
    }
  }
  process.exit(0);
}

process.on('SIGINT', 停止);
process.on('SIGTERM', 停止);
console.log(`OpenAI 连接监测已启动，调试端口：${端口}；等待相关请求。`);

while (!正在停止) {
  try {
    await 检查页面();
    上次检查错误 = '';
  } catch (错误) {
    if (上次检查错误 !== 错误.message) {
      console.log(`暂时无法检查调试页面：${错误.message}`);
      上次检查错误 = 错误.message;
    }
  }
  await new Promise((完成) => setTimeout(完成, 轮询间隔));
}
