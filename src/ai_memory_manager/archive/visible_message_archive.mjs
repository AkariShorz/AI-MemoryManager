/** 将界面捕获的消息快照归并到 Session_Lib。 */

import {
  appendFileSync,
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';

function 安全名称(值, 默认值 = '未命名会话') {
  return String(值 || '')
    .normalize('NFC')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_')
    .replace(/[. ]+$/g, '')
    .trim()
    .slice(0, 100) || 默认值;
}

function 读取消息行(文件路径, 消息表) {
  if (!existsSync(文件路径)) return;
  const 行列表 = readFileSync(文件路径, 'utf8').split(/\r?\n/);
  for (const 行 of 行列表) {
    if (!行.trim()) continue;
    try {
      const 消息 = JSON.parse(行);
      if (消息?.message_id && typeof 消息.content === 'string') {
        const 已有 = 消息表.get(消息.message_id);
        消息表.set(消息.message_id, {
          ...消息,
          first_observed_at: 已有?.first_observed_at || 消息.first_observed_at || 消息.observed_at,
        });
      }
    } catch {
      // 保留有效行，忽略意外损坏的尾行。
    }
  }
}

export function 创建可见消息归档器(输出根目录) {
  const 会话缓存 = new Map();

  function 获取会话(事件) {
    const 来源目录 = 事件.source?.startsWith('codex') ? 'Codex' : 'ChatGPT';
    const 标题 = 安全名称(事件.conversation_title);
    const 会话编号 = 安全名称(事件.conversation_id, 'unknown-id').slice(0, 80);
    const 观察时间 = new Date(事件.observed_at || Date.now());
    const 日期 = Number.isNaN(观察时间.valueOf())
      ? new Date().toLocaleDateString('sv-SE')
      : `${观察时间.getFullYear()}-${String(观察时间.getMonth() + 1).padStart(2, '0')}-${String(观察时间.getDate()).padStart(2, '0')}`;
    const 会话键 = `${来源目录}/${日期}/${标题}--${会话编号}`;
    if (会话缓存.has(会话键)) return 会话缓存.get(会话键);

    const 会话目录名 = 来源目录 === 'Codex' ? 会话编号 : `${标题}--${会话编号}`;
    const 会话目录 = join(输出根目录, 来源目录, 日期, 会话目录名);
    mkdirSync(会话目录, { recursive: true, mode: 0o700 });
    chmodSync(会话目录, 0o700);
    const 消息路径 = join(会话目录, 'messages.jsonl');
    const 快照路径 = join(会话目录, 'snapshots.jsonl');
    const 消息表 = new Map();
    // 兼容升级前按来源/标题保存的可见消息日志。
    const 旧日志 = join(输出根目录, 来源目录, `${标题}--${会话编号}`, `${标题}.jsonl`);
    读取消息行(旧日志, 消息表);
    读取消息行(快照路径, 消息表);
    读取消息行(消息路径, 消息表);

    const 会话 = { 消息表, 消息路径, 快照路径, 定时器: null };
    会话缓存.set(会话键, 会话);
    return 会话;
  }

  function 刷新(会话) {
    if (会话.定时器) {
      clearTimeout(会话.定时器);
      会话.定时器 = null;
    }
    const 临时路径 = `${会话.消息路径}.${process.pid}.tmp`;
    const 内容 = [...会话.消息表.values()].map((消息) => JSON.stringify(消息)).join('\n');
    writeFileSync(临时路径, 内容 ? `${内容}\n` : '', { mode: 0o600 });
    chmodSync(临时路径, 0o600);
    renameSync(临时路径, 会话.消息路径);
  }

  function 保存快照(事件) {
    if (!事件?.message_id || typeof 事件.content !== 'string') return null;
    const 会话 = 获取会话(事件);
    const 已有 = 会话.消息表.get(事件.message_id);
    if (已有?.content === 事件.content) {
      return { duplicate: true, path: 会话.消息路径 };
    }

    appendFileSync(会话.快照路径, `${JSON.stringify(事件)}\n`, { mode: 0o600 });
    chmodSync(会话.快照路径, 0o600);
    会话.消息表.set(事件.message_id, {
      ...事件,
      first_observed_at: 已有?.first_observed_at || 事件.observed_at,
      updated_at: 事件.observed_at,
    });
    if (!会话.定时器) {
      会话.定时器 = setTimeout(() => 刷新(会话), 400);
      会话.定时器.unref?.();
    }
    return { duplicate: false, path: 会话.消息路径 };
  }

  function 刷新全部() {
    for (const 会话 of 会话缓存.values()) {
      if (会话.定时器) 刷新(会话);
    }
  }

  return { 保存快照, 刷新全部 };
}
