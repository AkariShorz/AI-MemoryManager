"""Durable archive follow-up queue, with leases and bounded retry backoff."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from ai_memory_manager.memory.routing import PERSONAS, USAGE, resolve_persona


def fingerprint(directory: Path) -> str:
    metadata = json.loads((directory / "session.meta.json").read_text(encoding="utf-8"))
    parts = [metadata.get(key) for key in ("provider", "conversation_id", "persona_id", "date")]
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "session.meta.json" and path.suffix in {".json", ".jsonl", ".txt", ".md"}:
            stat = path.stat()
            parts.append((path.name, stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class ArchiveQueue:
    def __init__(self, archive: Path, *, personas: Path = PERSONAS, usage: Path = USAGE,
                 global_path: Path | None = None):
        self.archive = archive.expanduser().resolve()
        self.personas, self.usage, self.global_path = personas, usage, global_path
        self.archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.archive / ".memory_jobs.sqlite3"
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            path TEXT PRIMARY KEY, version TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0, persona TEXT, error TEXT, result TEXT,
            updated_at REAL NOT NULL)""")
        self.db.commit()
        os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    def enqueue(self, directory: Path) -> None:
        directory = directory.resolve()
        if self.archive not in directory.parents or directory.is_symlink():
            raise ValueError("会话不在归档目录内")
        version = fingerprint(directory)
        with self.db:
            self.db.execute("""INSERT INTO jobs(path,version,status,updated_at) VALUES(?,?,'pending',?)
                ON CONFLICT(path) DO UPDATE SET version=excluded.version, status='pending', attempts=0,
                next_retry=0,lease_until=0,error=NULL,result=NULL,updated_at=excluded.updated_at
                WHERE jobs.version<>excluded.version""", (str(directory), version, time.time()))

    def discover(self) -> None:
        for meta in sorted(self.archive.rglob("session.meta.json")):
            try:
                self.enqueue(meta.parent)
            except (OSError, ValueError, TypeError):
                continue
        with self.db:
            for job in self.db.execute("SELECT path FROM jobs WHERE status NOT IN ('cancelled','missing')").fetchall():
                if not Path(job["path"]).is_dir():
                    self.db.execute("UPDATE jobs SET status='missing',error=NULL WHERE path=?", (job["path"],))

    def cancel(self, directory: Path) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='cancelled',error=NULL,result=NULL,updated_at=? WHERE path=?",
                            (time.time(), str(directory.resolve())))

    def recover(self) -> None:
        """Called only after the worker has acquired the exclusive process lock."""
        with self.db:
            self.db.execute("UPDATE jobs SET status='retry',lease_until=0,next_retry=0 WHERE status='running'")

    def stats(self) -> dict[str, int]:
        return {row[0]: row[1] for row in self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status")}

    def step(self, *, extractor=None) -> bool:
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.db.execute("""SELECT * FROM jobs WHERE
                (status IN ('pending','retry','needs_persona') AND next_retry<=?)
                OR (status='running' AND lease_until<?) ORDER BY updated_at DESC,path LIMIT 1""", (now, now)).fetchone()
            if not job:
                return False
            self.db.execute("UPDATE jobs SET status='running',attempts=attempts+1,lease_until=? WHERE path=?",
                            (now + 300, job["path"]))
        directory = Path(job["path"])
        try:
            metadata = json.loads((directory / "session.meta.json").read_text(encoding="utf-8"))
            metadata.setdefault("provider", directory.parent.parent.name)
            metadata.setdefault("conversation_id", metadata.get("session_id") or directory.name.rsplit("--", 1)[-1])
            persona = resolve_persona(metadata["provider"], metadata["conversation_id"],
                persona_id=metadata.get("persona_id"), personas=self.personas,
                usage=self.usage, global_path=self.global_path)
            if persona is None:
                with self.db:
                    self.db.execute("UPDATE jobs SET status='needs_persona',next_retry=?,error=? WHERE path=? AND version=?",
                        (now + 15, "等待可用的人设路由", job["path"], job["version"]))
                return True

            def guard():
                current = self.db.execute("SELECT status,version FROM jobs WHERE path=?", (job["path"],)).fetchone()
                if not current or current["status"] in {"cancelled", "missing"}:
                    raise RuntimeError("归档任务已取消")
                if not (directory / "session.meta.json").is_file():
                    raise RuntimeError("来源归档已删除")
                with self.db:
                    self.db.execute("UPDATE jobs SET lease_until=? WHERE path=?", (time.time() + 300, job["path"]))

            from ai_memory_manager.memory.archive_extraction import extract_archive
            guard()
            db_path = persona.parent / ".memory" / f"{persona.stem}.sqlite3"
            result = (extractor or extract_archive)(directory, db_path, before_commit=guard, max_messages=12)
            guard()
            with self.db:
                self.db.execute("""UPDATE jobs SET status=?,persona=?,error=NULL,result=?,updated_at=?
                    WHERE path=? AND version=? AND status='running'""",
                    ("pending" if result.get("remaining") else "done", persona.stem,
                     json.dumps(result), time.time(), job["path"], job["version"]))
        except Exception as error:
            with self.db:
                self.db.execute("""UPDATE jobs SET status='retry',error=?,next_retry=?,lease_until=0,updated_at=?
                    WHERE path=? AND version=? AND status='running'""",
                    (str(error)[:500], time.time() + min(300, 5 * 2 ** min(job["attempts"], 6)),
                     time.time(), job["path"], job["version"]))
        return True


def enqueue_archive(directory: Path) -> None:
    queue = ArchiveQueue(directory.parent.parent.parent)
    try:
        queue.enqueue(directory)
    finally:
        queue.close()
