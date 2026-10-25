/**
 * Frontend lifecycle adapter for the OpenAI connection watcher.
 *
 * Import startServices() when the frontend starts and call the returned stop()
 * during shutdown. The watcher is owned by the frontend process and receives
 * the same termination signal when the frontend exits.
 */

import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const 此目录 = dirname(fileURLToPath(import.meta.url));
const 监测脚本 = resolve(此目录, 'openai_connection_watcher.mjs');

/** Start background services owned by the frontend. */
export function startServices({
  watcher = true,
  port = 9222,
  pollMs = 2000,
  outputDir,
  env = process.env,
  onMessage = (message) => console.log(`[service] ${message}`),
  onError = (error) => console.error(`[service] ${error.message}`),
} = {}) {
  let child = null;
  let stopping = false;
  let stopPromise = null;

  if (watcher) {
    const args = [监测脚本, '--port', String(port), '--poll-ms', String(pollMs)];
    if (outputDir) args.push('--output-dir', resolve(outputDir));
    child = spawn(process.execPath, args, { env, stdio: ['ignore', 'pipe', 'pipe'] });
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => chunk.split(/\r?\n/).filter(Boolean).forEach(onMessage));
    child.stderr.on('data', (chunk) => chunk.split(/\r?\n/).filter(Boolean).forEach((line) => onError(new Error(line))));
    child.on('error', onError);
    child.on('exit', (code, signal) => {
      if (!stopping && (code !== 0 || signal)) {
        onError(new Error(`OpenAI 连接监测器意外退出（${signal || code}）`));
      }
      child = null;
    });
  }

  function stop() {
    if (stopPromise) return stopPromise;
    stopping = true;
    if (!child) return Promise.resolve();
    const runningChild = child;
    stopPromise = new Promise((resolveStop) => {
      const timer = setTimeout(() => {
        if (runningChild.exitCode === null && runningChild.signalCode === null) {
          runningChild.kill('SIGKILL');
        }
      }, 4000);
      timer.unref();
      runningChild.once('exit', () => {
        clearTimeout(timer);
        resolveStop();
      });
      runningChild.kill('SIGTERM');
    });
    return stopPromise;
  }

  const stopOnSignal = () => { void stop(); };
  process.once('SIGINT', stopOnSignal);
  process.once('SIGTERM', stopOnSignal);
  process.once('exit', () => {
    if (child && child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
  });

  return {
    stop: async () => {
      process.off('SIGINT', stopOnSignal);
      process.off('SIGTERM', stopOnSignal);
      await stop();
    },
  };
}
