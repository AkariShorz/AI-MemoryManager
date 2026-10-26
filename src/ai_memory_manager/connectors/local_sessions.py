"""Discover local coding-assistant sessions and copy selected transcripts into the archive.

Discovery is read-only. Each connector declares its own source and capabilities;
archiving copies the source bytes without interpreting the private transcript format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ai_memory_manager.archive.session_archive import (
    会话库目录,
    会话目录,
    原子写入字节,
    写入会话元数据,
)
from ai_memory_manager.connectors.dsh import dsh_sessions


@dataclass(frozen=True)
class ConnectorInfo:
    connector_id: str
    label: str
    source_path: Path
    installed: bool
    can_discover: bool
    can_archive: bool
    note: str


@dataclass(frozen=True)
class SessionPreview:
    connector_id: str
    provider: str
    session_id: str
    source_path: Path
    title: str
    updated_at: datetime
    size_bytes: int
    mtime_ns: int


_SOURCES = {
    "codex": ("Codex", "sessions"),
    "claude-code": ("Claude Code", "projects"),
    "dsh": ("DeepSeek", "sessions"),
}
_LABELS = {"codex": "Codex", "claude-code": "Claude Code", "dsh": "DeepSeek (DSH)"}
_CODEX_NAME = re.compile(r"^rollout-.*-([0-9a-fA-F-]{36})\.jsonl$")
_BOILERPLATE = ("# AGENTS.md", "<INSTRUCTIONS>", "<system-reminder>", "<system_instruction>")


def _dsh_home(home: Path | None = None) -> Path:
    """Resolve the DSH data root while still allowing callers to pass a fake home."""

    base = os.environ.get("DSH_HOME")
    if base:
        return Path(base).expanduser()
    return (Path(home) if home is not None else Path.home()).expanduser() / ".dsh"


def _source_root(connector_id: str, home: Path | None = None) -> Path:
    if connector_id not in _SOURCES:
        raise ValueError(f"未知连接器：{connector_id}")
    user_home = Path(home) if home is not None else Path.home()
    if connector_id == "codex":
        base = Path(os.environ.get("CODEX_HOME") or user_home / ".codex")
    elif connector_id == "dsh":
        base = _dsh_home(home)
    else:
        base = user_home / ".claude"
    return base.expanduser() / _SOURCES[connector_id][1]


def detect_local_connectors(home: Path | None = None) -> tuple[ConnectorInfo, ...]:
    """Report actual local read/archive capabilities without writing to any source."""

    result = []
    for connector_id in _SOURCES:
        root = _source_root(connector_id, home)
        installed = root.parent.is_dir()
        readable = root.is_dir() and os.access(root, os.R_OK | os.X_OK)
        note = "可发现并归档本机会话" if readable else (
            "已安装，但没有可读取的会话目录" if installed else "未发现本地客户端数据"
        )
        result.append(ConnectorInfo(connector_id, _LABELS[connector_id], root, installed, readable, readable, note))
    return tuple(result)


def _candidate_paths(connector_id: str, root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    if connector_id == "codex":
        candidates = root.rglob("rollout-*.jsonl")
    elif connector_id == "dsh":
        candidates = root.rglob(dsh_sessions.会话文件名)
    else:
        candidates = root.rglob("*.jsonl")
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        if connector_id == "codex":
            if not _CODEX_NAME.match(path.name):
                continue
        elif connector_id == "dsh":
            if not dsh_sessions.is_session_file(path):
                continue
        elif "subagents" in path.relative_to(root).parts:
            continue
        yield path


def _text_parts(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part["text"] for part in value
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
            and isinstance(part.get("text"), str)
        )
    return ""


def _clean_title(text: str) -> str:
    stripped = re.sub(
        r"<external_codex_apps_open_page>[\s\S]*?</external_codex_apps_open_page>",
        "", text, flags=re.IGNORECASE,
    ).strip()
    if not stripped or stripped.startswith(_BOILERPLATE):
        return ""
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    stripped = " ".join(stripped.split())
    return stripped[:80]


def _preview_content(connector_id: str, path: Path) -> tuple[str, str | None]:
    """Read only the beginning of a transcript for a display title and Codex ID."""

    title = ""
    embedded_id = None
    remaining = 512 * 1024
    try:
        with path.open("rb") as source:
            while remaining > 0:
                line = source.readline(min(remaining, 64 * 1024))
                if not line:
                    break
                remaining -= len(line)
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if connector_id == "codex":
                    if record.get("type") == "session_meta" and isinstance(payload, dict):
                        raw_id = payload.get("id") or payload.get("session_id")
                        if isinstance(raw_id, str):
                            embedded_id = raw_id
                    if record.get("type") == "event_msg" and isinstance(payload, dict):
                        if payload.get("type") == "user_message":
                            title = _clean_title(_text_parts(payload.get("message")))
                    elif record.get("type") == "response_item" and isinstance(payload, dict):
                        if payload.get("type") == "message" and payload.get("role") == "user":
                            title = _clean_title(_text_parts(payload.get("content")))
                elif record.get("type") == "user" and not record.get("isSidechain") and not record.get("isMeta"):
                    message = record.get("message")
                    if isinstance(message, dict):
                        title = _clean_title(_text_parts(message.get("content")))
                if title:
                    break
    except (OSError, PermissionError):
        pass
    return title, embedded_id


def discover_local_sessions(
    connector_id: str | None = None, *, home: Path | None = None
) -> list[SessionPreview]:
    """List source sessions newest first; malformed individual files are skipped."""

    connector_ids = (connector_id,) if connector_id is not None else tuple(_SOURCES)
    result = []
    for selected in connector_ids:
        root = _source_root(selected, home)
        provider = _SOURCES[selected][0]
        for path in _candidate_paths(selected, root):
            try:
                stat = path.stat()
                if stat.st_size == 0:
                    continue
                if selected == "dsh":
                    session_id = dsh_sessions.session_id(path)
                    if session_id is None:
                        continue
                    title = dsh_sessions.session_title(path, _dsh_home(home))
                    if not title and not dsh_sessions.has_messages(path):
                        continue
                    result.append(SessionPreview(
                        selected, provider, session_id, path,
                        title or session_id, datetime.fromtimestamp(stat.st_mtime).astimezone(),
                        stat.st_size, stat.st_mtime_ns,
                    ))
                    continue
                if selected == "codex":
                    match = _CODEX_NAME.match(path.name)
                    session_id = match.group(1) if match else path.stem
                else:
                    session_id = path.stem
                title, embedded_id = _preview_content(selected, path)
                if embedded_id and selected == "codex" and embedded_id != session_id:
                    continue
                result.append(SessionPreview(
                    selected, provider, session_id, path,
                    title or session_id, datetime.fromtimestamp(stat.st_mtime).astimezone(),
                    stat.st_size, stat.st_mtime_ns,
                ))
            except (OSError, OverflowError, ValueError):
                continue
    result.sort(key=lambda item: item.updated_at, reverse=True)
    return result


def archive_local_session(
    session: SessionPreview, output_dir: str | Path | None = None,
    persona_id: str | None = None, *, home: Path | None = None,
) -> Path:
    """Archive one selected source into Session_Lib.

    Codex and Claude Code transcripts are copied byte for byte. DSH sessions are
    stored as decompressed ``session.v4.jsonl`` because that is the format DSH
    itself exports; the source archive and its hash stay recorded in metadata.
    """

    root = _source_root(session.connector_id, home).resolve()
    source = session.source_path
    resolved_source = source.resolve()
    if source.is_symlink() or not source.is_file() or root not in resolved_source.parents:
        raise ValueError(f"会话来源不在连接器目录内：{source}")
    if session.connector_id == "codex":
        match = _CODEX_NAME.match(source.name)
        expected_id = match.group(1) if match else None
    elif session.connector_id == "dsh":
        expected_id = dsh_sessions.session_id(source)
    else:
        expected_id = source.stem if "subagents" not in resolved_source.relative_to(root).parts else None
    if expected_id != session.session_id:
        raise ValueError("会话编号与来源文件不匹配")

    before = source.stat()
    source_bytes = source.read_bytes()
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("来源会话正在变化，请稍后重试")
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    if session.connector_id == "dsh":
        try:
            content = dsh_sessions.decompress_session(source_bytes)
        except (OSError, ValueError) as error:
            raise OSError("来源会话暂时无法解压，可能仍在写入，请稍后重试") from error
        transcript_format = "dsh-session-v4-jsonl (decompressed from zstd)"
    else:
        content = source_bytes
        transcript_format = "opaque; copied byte-for-byte"
    digest = hashlib.sha256(content).hexdigest()
    day = session.updated_at.date().isoformat()
    output_root = Path(output_dir) if output_dir is not None else 会话库目录
    existing_metadata: dict[str, object] = {}
    existing_directory: Path | None = None
    for existing in output_root.rglob("session.meta.json"):
        try:
            metadata = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (isinstance(metadata, dict) and metadata.get("provider") == session.provider
                and metadata.get("conversation_id") == session.session_id):
            existing_metadata = metadata
            existing_directory = existing.parent
            day = str(metadata.get("date") or existing.parent.parent.name)
            break
    directory = existing_directory or 会话目录(session.provider, day, session.session_id, None, output_root)
    destination = directory / "session.jsonl"
    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        原子写入字节(destination, content)
    preserved = {
        key: value for key, value in existing_metadata.items()
        if key not in {"schema_version", "provider", "persona_id", "conversation_id", "title", "date", "archived_at", "archive_method", "files"}
    }
    preserved.update({
        "source_connector": session.connector_id,
        "source_path": str(source),
        "source_size": after.st_size,
        "source_mtime_ns": after.st_mtime_ns,
        "source_sha256": source_digest,
        "transcript_sha256": digest,
        "transcript_format": transcript_format,
    })
    写入会话元数据(
        directory, 来源=session.provider, 会话编号=session.session_id, 日期=day,
        会话名称=session.title if session.title != session.session_id else existing_metadata.get("title"),
        人设编号=persona_id if persona_id is not None else existing_metadata.get("persona_id"),
        归档方式="local-session-copy",
        文件列表=sorted(path.name for path in directory.iterdir() if path.is_file() and path.name != "session.meta.json"),
        其他字段=preserved,
    )
    return destination


def transcript_messages(path: str | Path, connector_id: str) -> list[tuple[str, str]]:
    """Extract display text from a copied transcript; never alter its raw file."""

    if connector_id not in _SOURCES:
        raise ValueError(f"未知连接器：{connector_id}")
    if connector_id == "dsh":
        return dsh_sessions.session_messages(path)
    messages = []
    event_fallback = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if connector_id == "codex":
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "response_item" and payload.get("type") == "message":
                    role = payload.get("role")
                    if role in {"user", "assistant"}:
                        content = _text_parts(payload.get("content")).strip()
                        if content:
                            messages.append((role, content))
                elif record.get("type") == "event_msg":
                    role = {"user_message": "user", "agent_message": "assistant"}.get(payload.get("type"))
                    if role and isinstance(payload.get("message"), str):
                        event_fallback.append((role, payload["message"].strip()))
            elif record.get("type") in {"user", "assistant"} and not record.get("isSidechain") and not record.get("isMeta"):
                message = record.get("message")
                if isinstance(message, dict):
                    content = _text_parts(message.get("content")).strip()
                    if content:
                        messages.append((record["type"], content))
    return messages or [item for item in event_fallback if item[1]]


def refresh_visible_codex_archives(archive: Path, *, home: Path | None = None) -> int:
    """Refresh raw copies only for Codex sessions already captured in this archive."""
    root = _source_root("codex", home)
    known = {}
    for path in _candidate_paths("codex", root):
        match = _CODEX_NAME.match(path.name)
        if match:
            known[match.group(1)] = path
    count = 0
    for meta_path in archive.rglob("session.meta.json"):
        if not (meta_path.parent / "messages.jsonl").is_file():
            continue
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if metadata.get("provider") != "Codex":
                continue
            session_id = metadata.get("conversation_id") or metadata.get("session_id") or meta_path.parent.name
            path = known.get(session_id)
            if path is None:
                continue
            stat = path.stat()
            if metadata.get("source_mtime_ns") == stat.st_mtime_ns and metadata.get("source_size") == stat.st_size:
                continue
            preview = SessionPreview("codex", "Codex", session_id, path,
                str(metadata.get("title") or session_id), datetime.fromtimestamp(stat.st_mtime).astimezone(),
                stat.st_size, stat.st_mtime_ns)
            archive_local_session(preview, archive, persona_id=metadata.get("persona_id"), home=home)
            count += 1
        except (OSError, ValueError, TypeError):
            continue
    return count
