"""Application-owned automatic memory follow-up worker."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ai_memory_manager.memory.archive_extraction import ARCHIVE
from ai_memory_manager.memory.jobs import ArchiveQueue
from ai_memory_manager.connectors.local_sessions import refresh_visible_codex_archives


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    args.archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.archive / ".memory_worker.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        stop = False
        def request_stop(*_args):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        queue = ArchiveQueue(args.archive)
        capture = None
        try:
            queue.recover()
            scanned = 0.0
            while not stop:
                if args.parent_pid:
                    try:
                        os.kill(args.parent_pid, 0)
                    except ProcessLookupError:
                        break
                if args.capture and (capture is None or capture.poll() is not None):
                    try:
                        capture = subprocess.Popen(["node", str(ROOT / "src/ai_memory_manager/connectors/gpt/live_capture.mjs"),
                            "--output-dir", str(args.archive), "--parent-pid", str(os.getpid())],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except OSError as error:
                        print(f"实时归档尚未启动：{error}", file=sys.stderr, flush=True)
                if time.monotonic() - scanned >= 10:
                    if args.capture:
                        refresh_visible_codex_archives(args.archive)
                    queue.discover()
                    scanned = time.monotonic()
                if queue.step():
                    print(json.dumps({"memory_jobs": queue.stats()}, ensure_ascii=False), flush=True)
                    continue
                if args.once:
                    break
                time.sleep(1)
        finally:
            if capture is not None and capture.poll() is None:
                capture.terminate()
                try:
                    capture.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    capture.kill()
                    capture.wait()
            queue.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
