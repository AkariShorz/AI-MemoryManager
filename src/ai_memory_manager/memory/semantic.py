"""Local Ollama embeddings and persona-scoped SQLite vector cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
import urllib.request
from typing import Any


class EmbeddingUnavailable(RuntimeError):
    pass


def _request(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:11434" + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=30 if payload is not None else 3) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise EmbeddingUnavailable("本机嵌入模型不可用") from error


def _vector(value: Any) -> list[float]:
    if not isinstance(value, list) or not value:
        raise EmbeddingUnavailable("嵌入模型返回了空向量")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise EmbeddingUnavailable("嵌入模型返回了无效向量")
    norm = math.sqrt(sum(x * x for x in value))
    if not norm:
        raise EmbeddingUnavailable("嵌入模型返回了零向量")
    return [x / norm for x in value]


def scores(db: sqlite3.Connection, rows: list, query: str) -> dict[str, float]:
    """Lazily index eligible memories; model digest invalidates replaced models."""
    if not rows:
        return {}
    model = os.environ.get("AI_MEMORY_EMBED_MODEL", "bge-m3")
    models = _request("/api/tags").get("models", [])
    installed = next((m for m in models if m.get("name") in {model, model + ":latest"}), None)
    if not installed or not installed.get("digest"):
        raise EmbeddingUnavailable(f"本机尚未安装嵌入模型 {model}")
    key = model + ":" + installed["digest"]
    vectors = {}
    missing = []
    for row in rows:
        text = row["subject"] + "\n" + row["content"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        cached = db.execute("SELECT vector FROM memory_vectors WHERE memory_id=? AND model=? AND content_hash=?",
                            (row["id"], key, digest)).fetchone()
        if cached:
            try:
                vectors[row["id"]] = _vector(json.loads(cached[0]))
                continue
            except (ValueError, EmbeddingUnavailable):
                pass
        missing.append((row["id"], digest, text))
    for start in range(0, len(missing), 16):
        batch = missing[start:start + 16]
        result = _request("/api/embed", {"model": model, "input": [x[2] for x in batch], "truncate": False})
        raw = result.get("embeddings", [])
        if len(raw) != len(batch):
            raise EmbeddingUnavailable("嵌入模型返回的向量数量不匹配")
        normalized = [_vector(v) for v in raw]
        with db:
            for (memory_id, digest, _text), vector in zip(batch, normalized):
                db.execute("""INSERT INTO memory_vectors VALUES(?,?,?,?)
                    ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model,
                    content_hash=excluded.content_hash, vector=excluded.vector""",
                    (memory_id, key, digest, json.dumps(vector)))
                vectors[memory_id] = vector
    result = _request("/api/embed", {"model": model, "input": [query], "truncate": False})
    raw = result.get("embeddings", [])
    if len(raw) != 1:
        raise EmbeddingUnavailable("嵌入模型没有返回查询向量")
    query_vector = _vector(raw[0])
    if any(len(v) != len(query_vector) for v in vectors.values()):
        raise EmbeddingUnavailable("记忆和查询的向量维度不匹配")
    return {memory_id: sum(a * b for a, b in zip(vector, query_vector))
            for memory_id, vector in vectors.items()}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from ai_memory_manager.memory.lifecycle import retrieve
    if len(sys.argv) != 3:
        raise SystemExit("usage: semantic.py DATABASE QUERY")
    print(json.dumps(retrieve(sys.argv[1], sys.argv[2], limit=100), ensure_ascii=False))
