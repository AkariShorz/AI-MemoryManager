#!/usr/bin/env node
/**
 * ChatGPT 与 Codex 桌面端实时消息捕获原型。
 *
 * 通过本机 Chrome DevTools Protocol 观察页面中显示的消息，并将新增或变化的
 * 可见文本追加为 JSONL 事件。此方式依赖客户端界面结构，不能保证捕获完整历史、
 * 隐藏内容、附件或原始消息结构。
 */

import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { 创建可见消息归档器 } from '../../archive/visible_message_archive.mjs';

const 参数 = process.argv.slice(2);
const 取参数 = (名称, 默认值) => {
  const 位置 = 参数.indexOf(`--${名称}`);
  return 位置 >= 0 && 参数[位置 + 1] ? 参数[位置 + 1] : 默认值;
};

const 端口 = Number(取参数('port', '9222'));
const 轮询间隔 = Math.max(250, Number(取参数('poll-ms', '700')));
const 当前目录 = dirname(fileURLToPath(import.meta.url));
const 默认输出目录 = resolve(当前目录, '..', '..', 'archive', 'Session_Lib');
const 输出目录 = 取参数('output-dir', 取参数('output', 默认输出目录));
const 归档器 = 创建可见消息归档器(resolve(输出目录));
const 帮助 = 参数.includes('--help') || 参数.includes('-h');

if (帮助) {
  console.log(`用法：node live_capture.mjs [选项]

选项：
  --port <端口>          本机 ChatGPT/Codex 调试端口，默认 9222
  --poll-ms <毫秒>       检查间隔，默认 700，最小 250
  --output-dir <目录>    会话目录根路径，默认 ${默认输出目录}
  --output <目录>        --output-dir 的兼容别名
  --help                 显示帮助

按 Ctrl+C 停止。捕获的是界面可见文本，不是完整原始会话数据。`);
  process.exit(0);
}

const 输出绝对目录 = resolve(输出目录);

const 采集器脚本 = (目标编号) => `(() => {
  const 全局键 = '__aiMemoryManagerLiveCapture';
  if (window[全局键]?.version === 2 && window[全局键].observer) return true;

  const 目标编号 = ${JSON.stringify(目标编号)};
  const 旧采集器 = window[全局键];
  旧采集器?.observer?.disconnect();
  // 新进程需要独立的消息队列，因此重新快照当前已加载的会话消息。
  const 采集器 = { queue: new Map(), seen: new Map() };
  采集器.version = 2;
  采集器.lastPath = location.pathname;
  采集器.observer = null;
  const 是ChatGPT页面 = location.hostname === 'chatgpt.com';
  const 来源 = 是ChatGPT页面 ? 'chatgpt-visible-dom' : 'codex-visible-dom';
  const 会话编号 = () => {
    if (是ChatGPT页面) {
      return location.pathname.match(/\\/(?:c|conversation)\\/([^/?]+)/)?.[1] ?? 目标编号;
    }
    return document.querySelector('[data-above-composer-conversation-id]')
      ?.getAttribute('data-above-composer-conversation-id') || 目标编号;
  };
  const 会话名称 = () => {
    const 标题 = (document.title || '').replace(/\\s*[|—-]\\s*(?:ChatGPT|Codex)\\s*$/i, '').trim();
    return 标题 && !/^(?:ChatGPT|Codex|新聊天|New chat|新对话|New conversation)$/i.test(标题)
      ? 标题
      : '未命名会话';
  };
  const 检查页面 = () => {
    const 当前路径 = location.pathname;
    const 路径改变 = 当前路径 !== 采集器.lastPath;
    if (路径改变) {
      采集器.lastPath = 当前路径;
      // 保留已见消息指纹，避免切换地址时把已显示的旧消息重复写入。
    }

    const 当前内容 = new Map();
    const 角色序号 = new Map();
    const 选择器 = 是ChatGPT页面
      ? '[data-message-author-role]'
      : '[data-user-message-bubble], [data-local-conversation-final-assistant]';
    for (const 元素 of document.querySelectorAll(选择器)) {
      const 角色 = 是ChatGPT页面
        ? (元素.getAttribute('data-message-author-role') || '').trim().toLowerCase()
        : 元素.matches('[data-user-message-bubble]') ? 'user' : 'assistant';
      if (角色 !== 'user' && 角色 !== 'assistant') continue;
      let 内容 = 元素.innerText ?? 元素.textContent ?? '';
      if (!内容.trim() && 是ChatGPT页面) {
        const 文本节点 = 元素.querySelector('[data-message-content], .markdown, [class*="markdown"]');
        内容 = 文本节点?.innerText ?? 文本节点?.textContent ?? '';
      }
      if (!内容.trim()) continue;
      const 序号 = 角色序号.get(角色) ?? 0;
      角色序号.set(角色, 序号 + 1);
      const 回合编号 = 元素.closest('[data-turn-key]')?.getAttribute('data-turn-key');
      const 编号 = 元素.getAttribute('data-message-id') || 回合编号 || 角色 + '-' + 序号;
      const 当前会话编号 = 会话编号();
      const 键 = 来源 + ':' + 当前会话编号 + ':' + 角色 + ':' + 编号;
      当前内容.set(键, { 角色, 编号, 内容 });
    }

    for (const [键, 项] of 当前内容) {
      if (采集器.seen.get(键) === 项.内容) continue;
      采集器.seen.set(键, 项.内容);
      采集器.queue.set(键, {
        provider: 'openai',
        source: 来源,
        fidelity: 'visible-text',
        conversation_id: 会话编号(),
        conversation_title: 会话名称(),
        message_id: 项.角色 + ':' + 项.编号,
        role: 项.角色,
        content: 项.内容,
        observed_at: new Date().toISOString(),
      });
    }

    // 清理已从页面卸载的消息，避免长时间运行时指纹表无限增长。
    if (采集器.seen.size > 2000) {
      for (const 键 of 采集器.seen.keys()) {
        if (!当前内容.has(键)) 采集器.seen.delete(键);
        if (采集器.seen.size <= 1500) break;
      }
    }
  };

  window[全局键] = 采集器;
  检查页面();
  const 观察器 = new MutationObserver(检查页面);
  采集器.observer = 观察器;
  观察器.observe(document.documentElement, { subtree: true, childList: true, characterData: true });
  return true;
})()`;

class 调试协议客户端 {
  constructor(地址) {
    this.套接字 = new WebSocket(地址);
    this.序号 = 0;
    this.等待中 = new Map();
    this.已连接 = new Promise((resolve, reject) => {
      const 定时器 = setTimeout(() => reject(new Error('连接调试端口超时')), 8000);
      this.套接字.addEventListener('open', () => {
        clearTimeout(定时器);
        resolve();
      }, { once: true });
      this.套接字.addEventListener('error', () => {
        clearTimeout(定时器);
        reject(new Error('无法连接调试端口'));
      }, { once: true });
    });
    this.套接字.addEventListener('message', (事件) => {
      let 消息;
      try {
        消息 = JSON.parse(事件.data);
      } catch {
        return;
      }
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
      // 连接可能已经关闭。
    }
  }
}

async function 查找接入页面() {
  const 响应 = await fetch(`http://127.0.0.1:${端口}/json/list`);
  if (!响应.ok) throw new Error(`本机调试端口返回 ${响应.status}`);
  const 页面列表 = await 响应.json();
  return 页面列表.filter((页面) => {
    if (!页面.webSocketDebuggerUrl) return false;
    try {
      const 地址 = new URL(页面.url);
      return ((页面.type === 'webview' || 页面.type === 'page')
          && 地址.hostname === 'chatgpt.com')
        || (页面.type === 'page' && 地址.protocol === 'app:'
          && 地址.pathname === '/index.html' && !地址.search);
    } catch {
      return false;
    }
  });
}

const 接入客户端 = new Map();
let 停止中 = false;
let 上次提示 = '';
const 页面状态提示 = new Map();

function 停止() {
  if (停止中) return;
  停止中 = true;
  console.log('\n实时捕获已停止，正在清理页面观察器。');
}

process.on('SIGINT', 停止);
process.on('SIGTERM', 停止);

console.log(`实时捕获已启动，等待 ChatGPT 或 Codex 页面；会话目录：${输出绝对目录}`);
console.log('捕获范围为界面可见文本，按 Ctrl+C 停止。');

while (!停止中) {
  try {
    const 页面列表 = await 查找接入页面();
    if (!页面列表.length) {
      if (上次提示 !== 'waiting') console.log('尚未找到 ChatGPT 或 Codex 页面，请打开客户端。');
      上次提示 = 'waiting';
      await new Promise((resolve) => setTimeout(resolve, 1500));
      continue;
    }

    const 当前页面编号 = new Set(页面列表.map((页面) => 页面.id));
    for (const [目标编号, 客户端] of 接入客户端) {
      if (当前页面编号.has(目标编号)) continue;
      客户端.关闭();
      接入客户端.delete(目标编号);
    }

    for (const 页面 of 页面列表) {
      let 客户端 = 接入客户端.get(页面.id);
      if (!客户端) {
        客户端 = new 调试协议客户端(页面.webSocketDebuggerUrl);
        await 客户端.发送('Runtime.enable');
        接入客户端.set(页面.id, 客户端);
        const 地址 = new URL(页面.url);
        const 来源名称 = 地址.hostname === 'chatgpt.com' ? 'ChatGPT' : 'Codex';
        console.log(`已连接 ${来源名称} 页面。`);
      }

      try {
        await 客户端.发送('Runtime.evaluate', {
          expression: 采集器脚本(页面.id),
          returnByValue: true,
        });
        const 地址 = new URL(页面.url);
        if (地址.hostname === 'chatgpt.com') {
          const 状态 = await 客户端.发送('Runtime.evaluate', {
            expression: `(() => { const es = [...document.querySelectorAll('[data-message-author-role]')]; return { messages: es.length, assistants: es.filter(e => (e.getAttribute('data-message-author-role') || '').trim().toLowerCase() === 'assistant').length, accountError: [...document.querySelectorAll('h1,h2')].some(e => (e.innerText || '').includes('无法加载你的账户')) }; })()`,
            returnByValue: true,
          });
          const 页面状态 = 状态.result?.value?.accountError
            ? 'account-error'
            : 状态.result?.value?.messages === 0 ? 'no-session'
              : 状态.result?.value?.assistants === 0 ? 'no-assistant' : 'ready';
          if (页面状态 !== 页面状态提示.get(页面.id)) {
            if (页面状态 === 'account-error') {
              console.log('ChatGPT 页面提示账户加载失败，无法读取会话。请确认此桌面端已登录并能打开聊天。');
            } else if (页面状态 === 'no-session') {
              console.log('ChatGPT 页面已连接，但当前没有会话消息节点；请打开具体会话。');
            } else if (页面状态 === 'no-assistant') {
              console.log('ChatGPT 页面有消息节点，但未识别到助手消息；请确认会话已加载完整。');
            } else {
              console.log('ChatGPT 页面检测到助手消息，可开始捕获。');
            }
            页面状态提示.set(页面.id, 页面状态);
          }
        }
        const 结果 = await 客户端.发送('Runtime.evaluate', {
          expression: `(() => { const c = window.__aiMemoryManagerLiveCapture; if (!c) return []; const items = Array.from(c.queue.values()); c.queue.clear(); return items; })()`,
          returnByValue: true,
        });
        const 事件列表 = 结果.result?.value;
        if (Array.isArray(事件列表)) {
          for (const 事件 of 事件列表) {
            const 保存结果 = 归档器.保存快照(事件);
            if (!保存结果 || 保存结果.duplicate) continue;
            console.log(`已归档 ${事件.source.startsWith('codex') ? 'Codex' : 'ChatGPT'} ${事件.role} 消息（${事件.content.length} 字符）：${保存结果.path}`);
          }
        } else {
          console.log('页面没有返回可保存的消息快照。');
        }
      } catch (错误) {
        客户端.关闭();
        接入客户端.delete(页面.id);
        if (上次提示 !== 错误.message) console.log(`页面连接中断，等待重连：${错误.message}`);
        上次提示 = 错误.message;
      }
    }
  } catch (错误) {
    for (const 客户端 of 接入客户端.values()) 客户端.关闭();
    接入客户端.clear();
    if (上次提示 !== 错误.message) console.log(`等待重连：${错误.message}`);
    上次提示 = 错误.message;
  }

  await new Promise((resolve) => setTimeout(resolve, 轮询间隔));
}

for (const 客户端 of 接入客户端.values()) {
  try {
    await 客户端.发送('Runtime.evaluate', {
      expression: `(() => { const c = window.__aiMemoryManagerLiveCapture; if (c?.observer) c.observer.disconnect(); delete window.__aiMemoryManagerLiveCapture; return true; })()`,
      returnByValue: true,
    });
  } catch {
    // 页面可能已关闭，直接断开本机调试连接即可。
  }
  客户端.关闭();
}
接入客户端.clear();
归档器.刷新全部();
console.log('页面观察器已清理。');
