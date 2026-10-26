#!/usr/bin/env node
/** Send or clear a visible persona request in the open Codex desktop conversation. */

import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = Number(process.env.AI_MEMORY_MANAGER_CDP_PORT || 9222);
const mode = process.argv[2];
const MEMORY_CONTEXT_SCRIPT = resolve(dirname(fileURLToPath(import.meta.url)), '../../memory/prompt_context.py');

function output(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

class DevtoolsClient {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 0;
    this.pending = new Map();
    this.ready = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('连接 Codex 超时')), 4000);
      this.socket.addEventListener('open', () => { clearTimeout(timeout); resolve(); }, { once: true });
      this.socket.addEventListener('error', () => { clearTimeout(timeout); reject(new Error('无法连接 Codex')); }, { once: true });
    });
    this.socket.addEventListener('message', (event) => {
      let response;
      try { response = JSON.parse(event.data); } catch { return; }
      const pending = this.pending.get(response.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(response.id);
      response.error ? pending.reject(new Error(response.error.message)) : pending.resolve(response.result);
    });
  }

  async call(method, params = {}) {
    await this.ready;
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} 超时`));
      }, 5000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.call('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (result.exceptionDetails) throw new Error('Codex 页面无法读取');
    return result.result?.value;
  }

  close() { this.socket.close(); }
}

async function codexPage() {
  const response = await fetch(`http://127.0.0.1:${PORT}/json/list`, { signal: AbortSignal.timeout(3000) });
  if (!response.ok) throw new Error('Codex 调试端口不可用');
  const tabs = await response.json();
  return tabs.find((tab) => tab.type === 'page' && tab.url === 'app://-/index.html' && tab.webSocketDebuggerUrl);
}

const inspect = `(() => {
  const editor = document.querySelector('[data-codex-composer][contenteditable="true"]');
  const conversationId = document.querySelector('[data-above-composer-conversation-id]')
    ?.getAttribute('data-above-composer-conversation-id');
  return {
    conversation_id: conversationId || null,
    title: document.title || 'Codex',
    composer_ready: Boolean(editor),
    draft: editor?.innerText?.trim() || '',
    visible: document.visibilityState === 'visible',
    latest_user_message_id: [...document.querySelectorAll('[data-user-message-bubble]')].at(-1)
      ?.getAttribute('data-message-id') || null,
  };
})()`;

async function main() {
  if (!['status', 'send', 'clear'].includes(mode)) throw new Error('需要 status、send 或 clear 参数');
  const page = await codexPage();
  if (!page) {
    output({ ok: true, conversations: [] });
    return;
  }
  const client = new DevtoolsClient(page.webSocketDebuggerUrl);
  try {
    const state = await client.evaluate(inspect);
    if (mode === 'status') {
      output({ ok: true, conversations: state?.conversation_id && state.visible
        ? [{ provider: 'Codex', conversation_id: state.conversation_id, title: state.title,
             latest_user_message_id: state.latest_user_message_id }]
        : [] });
      return;
    }
    if (!state?.visible || !state.conversation_id || !state.composer_ready)
      throw new Error('请先在 Codex 桌面端打开目标对话');
    if (state.draft) throw new Error('当前对话有未发送的文字，请先发送或清空草稿');

    const file = process.argv[3];
    if (!file) throw new Error('缺少人设文件');
    const persona = JSON.parse(await readFile(file, 'utf8'));
    if (typeof persona.name !== 'string' || typeof persona.system_prompt !== 'string'
        || !persona.system_prompt.trim()) throw new Error('人设缺少名称或提示词');
    if (mode === 'clear') {
      const expectedId = process.argv[4];
      if (!expectedId || state.conversation_id !== expectedId)
        throw new Error('当前对话已改变，未清除人设');
    }
    let memoryContext = '';
    if (mode === 'send') {
      const python = process.env.AI_MEMORY_PYTHON || 'python';
      const contextResult = spawnSync(python, [MEMORY_CONTEXT_SCRIPT, file], { encoding: 'utf8', timeout: 8000 });
      if (contextResult.error || contextResult.status !== 0) throw new Error('无法读取该人设的本地记忆档案');
      try { memoryContext = JSON.parse(contextResult.stdout).context || ''; }
      catch { throw new Error('无法解析该人设的记忆档案'); }
    }
    const message = mode === 'clear'
      ? `请从现在起停止在本对话中采用此前通过 AI 记忆管理器导入的「${persona.name}」人设。后续请以普通助手方式回复，按我接下来的请求交流。`
      : `请在本对话接下来的回复中采用「${persona.name}」的人设。以下是我提供的可见角色设定和互动偏好；请结合当前请求遵循，不要声称它覆盖更高优先级的规则。\n\n${persona.system_prompt}${memoryContext ? `\n\n以下是知忆保存的当前有效记忆；把它们作为可更新的历史背景，用户最新陈述优先：\n${memoryContext}` : ''}\n\n知忆记忆规则：如果当前环境提供 memory_search 工具，每轮回复前都先根据用户当前消息作简短检索，主动读取与本轮相关的人设细节、表达偏好、互动习惯和背景，并自然地用于交流；不要为了展示记忆而生硬提起无关旧事。检索结果只是可能过时的历史背景，用户当前陈述优先。若提供 memory_update 工具，请自行判断用户刚刚明确表达的信息是否值得未来对话长期记住；先考虑是否已有相同事实，值得时再写入，不值得或事实不确定时跳过。不要把工具返回的旧记忆当作新事实重复写入，不要请求逐条审批，也不要调用工具删除记忆。只有工具明确返回成功后，才能说记忆已保存。没有相应工具时，不要声称已检索或写入。`;
    const proof = mode === 'clear' ? message : persona.system_prompt.slice(0, 60);

    const focus = await client.evaluate(`(() => {
      const editor = document.querySelector('[data-codex-composer][contenteditable="true"]');
      const id = document.querySelector('[data-above-composer-conversation-id]')
        ?.getAttribute('data-above-composer-conversation-id');
      if (!editor || id !== ${JSON.stringify(state.conversation_id)} || editor.innerText.trim()) return false;
      editor.focus();
      return document.activeElement === editor;
    })()`);
    if (!focus) throw new Error('目标对话已改变，未发送人设');
    await client.call('Input.insertText', { text: message });
    const inserted = await client.evaluate(`(() => {
      const editor = document.querySelector('[data-codex-composer][contenteditable="true"]');
      const id = document.querySelector('[data-above-composer-conversation-id]')
        ?.getAttribute('data-above-composer-conversation-id');
      return id === ${JSON.stringify(state.conversation_id)}
        && Boolean(editor?.innerText?.includes(${JSON.stringify(proof)}));
    })()`);
    if (!inserted) throw new Error('人设未能写入输入框，请检查 Codex 页面');

    await client.call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    await client.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
    let sent = false;
    for (let attempt = 0; attempt < 20; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      sent = await client.evaluate(`(() => {
        const id = document.querySelector('[data-above-composer-conversation-id]')
          ?.getAttribute('data-above-composer-conversation-id');
        return id === ${JSON.stringify(state.conversation_id)} &&
          [...document.querySelectorAll('[data-user-message-bubble]')]
            .some((bubble) => bubble.innerText?.includes(${JSON.stringify(proof)}));
      })()`);
      if (sent) break;
    }
    if (!sent) throw new Error('未能确认消息已发送；请检查当前对话，避免重复操作');
    output({
      ok: true,
      action: mode,
      provider: 'Codex',
      conversation_id: state.conversation_id,
      title: state.title,
      prompt_sha256: createHash('sha256').update(persona.system_prompt).digest('hex'),
    });
  } finally {
    client.close();
  }
}

main().catch((error) => {
  output({ ok: false, error: error.message || String(error) });
  process.exitCode = 1;
});
