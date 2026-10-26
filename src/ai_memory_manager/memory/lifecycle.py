"""Validated, model-independent lifecycle for derived memories.

Extractors may suggest candidates, but only this module validates and commits them.
Conversation archives remain the source of truth and are never edited here.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MEMORY_TYPES = {"stable_preference", "persona", "episodic", "project", "schedule", "inferred"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _time(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 string") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    subject: str
    content: str
    source: str
    confidence: float = 1.0
    conversation_id: str | None = None
    source_message_id: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    extractor: str | None = None
    candidate_id: str | None = None
    observed_at: str | None = None

    def validated(self) -> "MemoryCandidate":
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"memory_type must be one of {sorted(MEMORY_TYPES)}")
        for field in ("subject", "content", "source"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} cannot be empty")
            if len(value) > (2000 if field == "content" else 500):
                raise ValueError(f"{field} is too long")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        start, end = _time(self.valid_from, "valid_from"), _time(self.valid_until, "valid_until")
        observed = _time(self.observed_at, "observed_at")
        if self.memory_type == "schedule" and end is None:
            raise ValueError("schedule memories require valid_until")
        if start and end and start >= end:
            raise ValueError("valid_from must be earlier than valid_until")
        if self.candidate_id and (len(self.candidate_id) > 100 or not self.candidate_id.strip()):
            raise ValueError("candidate_id must be a non-empty string of at most 100 characters")
        return MemoryCandidate(**{**asdict(self), "subject": self.subject.strip(), "content": self.content.strip(),
            "source": self.source.strip(), "valid_from": start, "valid_until": end,
            "observed_at": observed,
            "candidate_id": self.candidate_id or str(uuid.uuid4())})


@dataclass(frozen=True)
class Submission:
    status: str
    candidate_id: str
    memory_id: str | None = None
    conflicts: tuple[str, ...] = ()


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS memories (
      id TEXT PRIMARY KEY, memory_type TEXT NOT NULL, subject TEXT NOT NULL,
      content TEXT NOT NULL, confidence REAL NOT NULL, source TEXT NOT NULL,
      conversation_id TEXT, source_message_id TEXT, extractor TEXT,
      valid_from TEXT, valid_until TEXT, status TEXT NOT NULL,
      superseded_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, observed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS memories_lookup ON memories(status, memory_type, subject);
    CREATE TABLE IF NOT EXISTS revisions (
      id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, action TEXT NOT NULL,
      snapshot TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL,
      FOREIGN KEY(memory_id) REFERENCES memories(id)
    );
    CREATE TABLE IF NOT EXISTS candidates (
      id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
      submitted_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT
    );
    CREATE TABLE IF NOT EXISTS ingested_messages (
      provider TEXT NOT NULL, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
      content_hash TEXT NOT NULL, processed_at TEXT NOT NULL,
      PRIMARY KEY(provider, conversation_id, message_id)
    );
    CREATE TABLE IF NOT EXISTS memory_vectors (
      memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
      model TEXT NOT NULL, content_hash TEXT NOT NULL, vector TEXT NOT NULL
    );
    """)
    if "observed_at" not in {row[1] for row in db.execute("PRAGMA table_info(memories)")}:
        db.execute("ALTER TABLE memories ADD COLUMN observed_at TEXT")
    os.chmod(path, 0o600)
    return db


def _snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)


def _revision(db: sqlite3.Connection, memory_id: str, action: str, snapshot: dict[str, Any], actor: str) -> None:
    db.execute("INSERT INTO revisions(memory_id,action,snapshot,at,actor) VALUES(?,?,?,?,?)",
               (memory_id, action, json.dumps(snapshot, ensure_ascii=False), _now(), actor))


def _same(a: str, b: str) -> bool:
    norm = lambda s: " ".join(unicodedata.normalize("NFKC", s).casefold().split())
    return norm(a) == norm(b)


MEMORY_UPDATE_TOOL = {
    "name": "memory_update",
    "description": (
        "For a Codex conversation with a persona imported by AI-MemoryManager, decide whether a "
        "durable user preference, persona fact, event, project, or schedule should be remembered. "
        "Call only when useful for future conversations; skip transient or ambiguous information. "
        "The app writes automatically and keeps a revision when a subject changes. This tool cannot delete memories."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
            "subject": {"type": "string", "maxLength": 500},
            "content": {"type": "string", "maxLength": 2000},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "valid_from": {"type": ["string", "null"], "format": "date-time"},
            "valid_until": {"type": ["string", "null"], "format": "date-time"},
        },
        "required": ["memory_type", "subject", "content"],
        "additionalProperties": False,
    },
}

MEMORY_SEARCH_TOOL = {
    "name": "memory_search",
    "description": (
        "Before every reply, search the current persona's relevant long-term memories using a concise query based on the current user message. "
        "Use relevant persona details, communication preferences, interaction patterns, and background naturally to make the conversation feel personal; do not force unrelated memories into the reply. "
        "Results are historical context and may be stale; the user's latest message wins."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def submit_candidate(db_path: str | Path, candidate: MemoryCandidate, *, actor: str = "model") -> Submission:
    """Apply a validated model decision; same-key conflicts become revisions automatically."""
    item = candidate.validated()
    payload = json.dumps(asdict(item), ensure_ascii=False)
    db = _connect(db_path)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload,status FROM candidates WHERE id=?", (item.candidate_id,)).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise ValueError("candidate_id was already used for different content")
                memory = db.execute("""SELECT id FROM memories WHERE memory_type=? AND subject=? AND content=?
                    AND source=? AND conversation_id IS ? AND source_message_id IS ? ORDER BY created_at DESC LIMIT 1""",
                    (item.memory_type, item.subject, item.content, item.source, item.conversation_id, item.source_message_id)).fetchone()
                return Submission("duplicate" if prior["status"] == "duplicate" else "created",
                                  item.candidate_id, memory["id"] if memory else None)
            duplicate = db.execute("""SELECT id,content FROM memories WHERE status='active' AND memory_type=?""",
                                   (item.memory_type,)).fetchall()
            duplicate = [r["id"] for r in duplicate if _same(r["content"], item.content)]
            if duplicate:
                existing_candidate = db.execute("SELECT id FROM candidates WHERE id=?", (item.candidate_id,)).fetchone()
                if not existing_candidate:
                    db.execute("INSERT INTO candidates VALUES(?,?,'duplicate',?, ?, ?)",
                               (item.candidate_id, payload, _now(), _now(), "duplicate"))
                return Submission("duplicate", item.candidate_id, duplicate[0])
            conflicts = db.execute("""SELECT id,observed_at,created_at FROM memories WHERE status='active' AND memory_type=? AND subject=?""",
                                   (item.memory_type, item.subject)).fetchall()
            newer = [r for r in conflicts if item.observed_at and (r["observed_at"] or r["created_at"]) > item.observed_at]
            if newer:
                db.execute("INSERT INTO candidates VALUES(?,?,'rejected',?,?,?)",
                    (item.candidate_id, payload, _now(), _now(), "older_source"))
                return Submission("outdated", item.candidate_id, newer[0]["id"])
            conflict_ids = tuple(r["id"] for r in conflicts)
            db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?)", (item.candidate_id, payload, "accepted", _now(), None, None))
            return _apply_candidate(db, item, actor=actor, conflicts=conflict_ids)
    finally:
        db.close()


def process_conversation(db_path: str | Path, messages: Iterable[dict[str, Any]], *,
                         provider: str, conversation_id: str,
                         extractor: Any) -> list[Submission]:
    """Run a pluggable extractor and submit only candidates grounded in archived user messages.

    Expected message fields are ``id`` or ``message_id``, ``role`` and arbitrary
    content. The extractor receives an immutable tuple of message mappings and
    returns ``MemoryCandidate`` objects. It cannot choose provenance identifiers.
    """
    if not provider.strip() or not conversation_id.strip():
        raise ValueError("provider and conversation_id cannot be empty")
    archived = tuple(dict(message) for message in messages)
    user_ids = {str(m.get("id") or m.get("message_id")) for m in archived
                if str(m.get("role", "")).casefold() in {"user", "human"}
                and (m.get("id") or m.get("message_id"))}
    if not callable(extractor):
        raise ValueError("extractor must be callable")
    proposed = extractor(archived)
    if proposed is None:
        return []
    candidates = list(proposed)
    grounded: list[MemoryCandidate] = []
    for candidate in candidates:
        if not isinstance(candidate, MemoryCandidate):
            raise ValueError("extractor must return MemoryCandidate objects")
        item = candidate.validated()
        if not item.source_message_id or item.source_message_id not in user_ids:
            raise ValueError("candidate source_message_id must refer to an archived user message")
        grounded.append(replace(item, source=f"conversation:{provider.strip()}",
                                conversation_id=conversation_id.strip()))
    # Validate the whole batch before any writes so malformed model output cannot
    # leave a partially processed conversation in storage.
    return [submit_candidate(db_path, item) for item in grounded]


def _apply_candidate(db: sqlite3.Connection, item: MemoryCandidate, *, actor: str, conflicts: Iterable[str]) -> Submission:
    memory_id = str(uuid.uuid4())
    now = _now()
    row = {"id": memory_id, "memory_type": item.memory_type, "subject": item.subject, "content": item.content,
           "confidence": float(item.confidence), "source": item.source, "conversation_id": item.conversation_id,
           "source_message_id": item.source_message_id, "extractor": item.extractor, "valid_from": item.valid_from,
           "valid_until": item.valid_until, "status": "active", "superseded_by": None,
           "created_at": now, "updated_at": now, "observed_at": item.observed_at}
    with db:
        for old_id in conflicts:
            old = db.execute("SELECT * FROM memories WHERE id=? AND status='active'", (old_id,)).fetchone()
            if old:
                before = _snapshot(old)
                db.execute("UPDATE memories SET status='superseded',superseded_by=?,updated_at=? WHERE id=?", (memory_id, now, old_id))
                before["status"], before["superseded_by"], before["updated_at"] = "superseded", memory_id, now
                _revision(db, old_id, "superseded", before, actor)
        db.execute("INSERT INTO memories(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        _revision(db, memory_id, "created", row, actor)
        db.execute("UPDATE candidates SET status='accepted',resolved_at=?,resolution=? WHERE id=?",
                   (now, f"automatic:{actor}", item.candidate_id))
    return Submission("created", item.candidate_id, memory_id)


def resolve_candidate(db_path: str | Path, candidate_id: str, *, approve: bool, actor: str = "user") -> Submission:
    """Explicitly accept/reject a pending suggestion. Approval supersedes conflicts atomically."""
    if not actor.strip():
        raise ValueError("actor cannot be empty")
    db = _connect(db_path)
    try:
        record = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not record:
            raise KeyError(candidate_id)
        if record["status"] != "pending":
            raise ValueError(f"candidate is not pending (status={record['status']})")
        item = MemoryCandidate(**json.loads(record["payload"])).validated()
        if not approve:
            with db:
                db.execute("UPDATE candidates SET status='rejected',resolved_at=?,resolution=? WHERE id=?", (_now(), actor, candidate_id))
            return Submission("rejected", candidate_id)
        conflicts = [r["id"] for r in db.execute("SELECT id FROM memories WHERE status='active' AND memory_type=? AND subject=?",
                                                  (item.memory_type, item.subject))]
        return _apply_candidate(db, item, actor=actor, conflicts=conflicts)
    finally:
        db.close()


def apply_model_memory_update(db_path: str | Path, arguments: dict[str, Any], *,
                              provider: str, conversation_id: str,
                              source_message_id: str | None = None) -> Submission:
    """Validated tool-call target for an AI deciding to remember a conversation fact.

    The model supplies memory content only. Conversation provenance is injected by
    the trusted connector, and this operation cannot delete or run arbitrary SQL.
    """
    if not isinstance(arguments, dict):
        raise ValueError("memory update arguments must be an object")
    allowed = {"memory_type", "subject", "content", "confidence", "valid_from", "valid_until"}
    unknown = set(arguments) - allowed
    if unknown:
        raise ValueError(f"unsupported memory update fields: {', '.join(sorted(unknown))}")
    if not provider.strip() or not conversation_id.strip():
        raise ValueError("trusted conversation provenance is required")
    candidate = MemoryCandidate(
        memory_type=arguments.get("memory_type"), subject=arguments.get("subject"),
        content=arguments.get("content"), source=f"conversation:{provider.strip()}",
        confidence=arguments.get("confidence", 1.0), conversation_id=conversation_id.strip(),
        source_message_id=source_message_id,
        valid_from=arguments.get("valid_from"), valid_until=arguments.get("valid_until"),
        extractor="model_tool_call",
        observed_at=_now(),
    )
    return submit_candidate(db_path, candidate, actor="model")


def delete_memory(db_path: str | Path, memory_id: str, *, actor: str = "user") -> None:
    """Permanently remove one memory and its content-bearing history; user-only."""
    if actor != "user":
        raise PermissionError("only the user may delete memories")
    db = _connect(db_path)
    try:
        db.execute("PRAGMA secure_delete=ON")
        with db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise KeyError(memory_id)
            identity = {key: row[key] for key in (
                "memory_type", "subject", "content", "source", "conversation_id", "source_message_id")}
            for candidate in db.execute("SELECT id,payload FROM candidates").fetchall():
                payload = json.loads(candidate["payload"])
                if all(payload.get(key) == value for key, value in identity.items()):
                    db.execute("DELETE FROM candidates WHERE id=?", (candidate["id"],))
            db.execute("DELETE FROM revisions WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()


def purge_conversation(db_path: str | Path, provider: str, conversation_id: str,
                       *, actor: str = "user") -> int:
    """Remove derived memories and ingestion markers when a source session is erased."""
    if actor != "user":
        raise PermissionError("only the user may purge conversation memories")
    if not Path(db_path).is_file():
        return 0
    db = _connect(db_path)
    try:
        db.execute("PRAGMA secure_delete=ON")
        source = f"conversation:{provider}"
        with db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM memories WHERE source=? AND conversation_id=?", (source, conversation_id))]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(f"DELETE FROM revisions WHERE memory_id IN ({placeholders})", ids)
                db.execute(f"UPDATE memories SET superseded_by=NULL WHERE superseded_by IN ({placeholders})", ids)
                db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
            for row in db.execute("SELECT id,payload FROM candidates").fetchall():
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                if payload.get("source") == source and payload.get("conversation_id") == conversation_id:
                    db.execute("DELETE FROM candidates WHERE id=?", (row["id"],))
            db.execute("DELETE FROM ingested_messages WHERE provider=? AND conversation_id=?",
                       (provider, conversation_id))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(ids)
    finally:
        db.close()


def expire_due(db_path: str | Path, *, now: str | None = None) -> int:
    """Expire memories whose valid_until has passed, preserving their revision records."""
    cutoff = _time(now, "now") if now else _now()
    db = _connect(db_path)
    changed = 0
    try:
        with db:
            for row in db.execute("SELECT * FROM memories WHERE status='active' AND valid_until IS NOT NULL AND valid_until<=?", (cutoff,)).fetchall():
                before = _snapshot(row)
                db.execute("UPDATE memories SET status='expired',updated_at=? WHERE id=?", (cutoff, row["id"]))
                before["status"], before["updated_at"] = "expired", cutoff
                _revision(db, row["id"], "expired", before, "lifecycle")
                changed += 1
        return changed
    finally:
        db.close()


def retrieve(db_path: str | Path, query: str, *, limit: int = 12, at: str | None = None,
             memory_types: Iterable[str] | None = None, semantic: bool = True,
             semantic_threshold: float = 0.45) -> list[dict[str, Any]]:
    """Combine local semantic similarity with phrase, word and Han-bigram matches."""
    if not isinstance(query, str) or not query.strip():
        return []
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not isinstance(semantic_threshold, (int, float)) or not math.isfinite(semantic_threshold) or not 0 <= semantic_threshold <= 1:
        raise ValueError("semantic_threshold must be between 0 and 1")
    instant = _time(at, "at") if at else _now()
    normalized_query = unicodedata.normalize("NFKC", query).casefold().strip()
    words = {word for word in re.findall(r"[a-z0-9_]+", normalized_query) if len(word) > 1}
    han_runs = re.findall(r"[\u3400-\u9fff]+", normalized_query)
    han_pairs = {run[i:i + 2] for run in han_runs for i in range(len(run) - 1)}
    han_single = {run for run in han_runs if len(run) == 1}
    db = _connect(db_path)
    try:
        params: list[Any] = [instant, instant]
        sql = "SELECT * FROM memories WHERE status='active' AND (valid_from IS NULL OR valid_from<=?) AND (valid_until IS NULL OR valid_until>?)"
        types = tuple(memory_types or ())
        if types:
            if set(types) - MEMORY_TYPES:
                raise ValueError("unknown memory type")
            sql += " AND memory_type IN (" + ",".join("?" for _ in types) + ")"
            params.extend(types)
        rows = db.execute(sql, params).fetchall()
        from ai_memory_manager.memory.semantic import EmbeddingUnavailable, scores
        similarities = {}
        mode = "keyword"
        if semantic:
            try:
                similarities = scores(db, rows, query)
                mode = "hybrid"
            except EmbeddingUnavailable:
                mode = "keyword_fallback"
        ranked = []
        for row in rows:
            subject = unicodedata.normalize("NFKC", row["subject"]).casefold()
            content = unicodedata.normalize("NFKC", row["content"]).casefold()
            text = f"{subject} {content}"
            score = 0.0
            if normalized_query in text:
                score += 8.0
            score += sum(3.0 if word in subject else 1.5 for word in words if word in text)
            score += sum(2.0 if pair in subject else 1.0 for pair in han_pairs if pair in text)
            score += sum(0.5 for char in han_single if char in text)
            similarity = similarities.get(row["id"], 0.0)
            keyword_score = score
            if mode == "hybrid":
                score = 0.7 * max(0.0, similarity) + 0.3 * (score / (score + 4.0))
            if keyword_score or similarity >= semantic_threshold:
                result = _snapshot(row)
                result["retrieval_score"] = round(score, 3)
                result["semantic_score"] = round(similarity, 4)
                result["retrieval_mode"] = mode
                ranked.append(result)
        return sorted(ranked, key=lambda x: (x["retrieval_score"], x["confidence"], x["updated_at"]), reverse=True)[:limit]
    finally:
        db.close()


def history(db_path: str | Path, memory_id: str) -> list[dict[str, Any]]:
    db = _connect(db_path)
    try:
        return [{**dict(row), "snapshot": json.loads(row["snapshot"])} for row in db.execute(
            "SELECT * FROM revisions WHERE memory_id=? ORDER BY id", (memory_id,))]
    finally:
        db.close()


def message_was_ingested(db_path: str | Path, provider: str, conversation_id: str,
                         message_id: str, content_hash: str) -> bool:
    db = _connect(db_path)
    try:
        row = db.execute("""SELECT content_hash FROM ingested_messages
            WHERE provider=? AND conversation_id=? AND message_id=?""",
            (provider, conversation_id, message_id)).fetchone()
        return bool(row and row["content_hash"] == content_hash)
    finally:
        db.close()


def mark_message_ingested(db_path: str | Path, provider: str, conversation_id: str,
                          message_id: str, content_hash: str) -> None:
    db = _connect(db_path)
    try:
        with db:
            db.execute("""INSERT INTO ingested_messages VALUES(?,?,?,?,?)
                ON CONFLICT(provider, conversation_id, message_id) DO UPDATE SET
                content_hash=excluded.content_hash, processed_at=excluded.processed_at""",
                (provider, conversation_id, message_id, content_hash, _now()))
    finally:
        db.close()


def list_memories(db_path: str | Path, *, status: str | None = "active", limit: int = 200) -> list[dict[str, Any]]:
    """List a persona's memories for the local management interface."""
    if status not in {None, "active", "superseded", "expired"}:
        raise ValueError("unknown memory status")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    db = _connect(db_path)
    try:
        if status is None:
            rows = db.execute("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM memories WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
        return [_snapshot(row) for row in rows]
    finally:
        db.close()


def list_candidates(db_path: str | Path, *, status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
    """List candidates awaiting review or their recorded resolution."""
    if status not in {"pending", "accepted", "rejected", "duplicate"}:
        raise ValueError("unknown candidate status")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    db = _connect(db_path)
    try:
        rows = db.execute("SELECT * FROM candidates WHERE status=? ORDER BY submitted_at DESC LIMIT ?", (status, limit)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
    finally:
        db.close()
