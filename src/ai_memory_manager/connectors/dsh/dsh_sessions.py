"""读取 DeepSeek Harness（DSH）本机会话，并提取可显示的用户与助手文本。

DSH 把会话保存在 ``$DSH_HOME/sessions/<工作区>/session-<编号>/session.v4.jsonl.zstd``，
内容是 zstd 压缩的 JSONL 事件流；会话标题和令牌用量另存于
``$DSH_HOME/storages/session_projcache/sessions/<编号>.json``。本模块只读来源文件，
归档写入由共用的会话归档流程负责。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

try:  # Python 3.14 起提供标准库 zstd 解压
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - 旧版 Python 走 zstd 命令
    _zstd = None

会话文件名 = "session.v4.jsonl.zstd"
_SESSION_DIR = re.compile(r"^session-([0-9a-fA-F-]{36})$")
_WHITESPACE = re.compile(r"\s+")


def dsh_home(home: Path | None = None) -> Path:
    """返回 DSH 数据根目录；可用 ``DSH_HOME`` 覆盖。"""

    if home is not None:
        return Path(home).expanduser()
    return Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()


def sessions_root(home: Path | None = None) -> Path:
    return dsh_home(home) / "sessions"


def cache_root(home: Path | None = None) -> Path:
    return dsh_home(home) / "storages" / "session_projcache" / "sessions"


def is_session_file(path: str | Path) -> bool:
    return Path(path).name == 会话文件名 and session_id(path) is not None


def session_id(path: str | Path) -> str | None:
    """从 ``session-<UUID>`` 目录名取出会话编号。"""

    match = _SESSION_DIR.match(Path(path).parent.name)
    return match.group(1) if match else None


def decompress_session(raw: bytes) -> bytes:
    """解压会话字节；没有 zstd 支持时退回本机 ``zstd`` 命令。"""

    if _zstd is not None:
        try:
            return _zstd.decompress(raw)
        except _zstd.ZstdError as error:
            raise ValueError(f"zstd 解压失败：{error}") from error
    try:
        result = subprocess.run(
            ["zstd", "-dc"], input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"无法调用 zstd 解压：{error}") from error
    if result.returncode != 0:
        raise ValueError(f"zstd 解压失败：{result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def load_session_bytes(path: str | Path) -> bytes:
    """读取会话字节。

    来源目录中的 ``session.v4.jsonl.zstd`` 需要解压；归档里的
    ``session.jsonl`` 已经是明文，直接读取，便于预览和概要复用。
    """

    data = Path(path).read_bytes()
    if Path(path).suffix == ".zstd":
        return decompress_session(data)
    return data


def iter_events(text: str | bytes) -> Iterator[dict[str, Any]]:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _text_parts(content: object) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def _clean_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", value).strip()[:80]


def _cached_title(session: str, home: Path | None = None) -> str:
    path = cache_root(home) / f"session-{session}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    rows = value.get("record", {}).get("rows", {}) if isinstance(value, dict) else {}
    row = rows.get("title") if isinstance(rows, dict) else None
    title = _clean_title(row.get("val")) if isinstance(row, dict) else ""
    if title:
        return title
    # 会话刚开始、标题尚未生成时使用第一条用户输入。
    title_input = rows.get("titleInput") if isinstance(rows, dict) else None
    first = title_input.get("first") if isinstance(title_input, dict) else None
    return _clean_title(first.get("text")) if isinstance(first, dict) else ""


def session_title(path: str | Path, home: Path | None = None) -> str:
    """优先读取 DSH 缓存中的标题，缺失时扫描事件流。"""

    identifier = session_id(path)
    if identifier:
        title = _cached_title(identifier, home)
        if title:
            return title
    try:
        text = load_session_bytes(path)
    except (OSError, ValueError):
        return ""
    first_user = ""
    for event in iter_events(text):
        kind = event.get("type")
        if kind == "session/title":
            title = _clean_title((event.get("data") or {}).get("title"))
            if title:
                return title
        elif kind == "user/message" and not first_user:
            data = event.get("data") or {}
            if (data.get("source") or {}).get("kind") == "user":
                first_user = _clean_title(_text_parts(data.get("content")))
    return first_user


def has_messages(path: str | Path) -> bool:
    try:
        text = load_session_bytes(path)
    except (OSError, ValueError):
        return False
    return any(kind in {"user/message", "assistant/message"} for kind in
               (event.get("type") for event in iter_events(text)))


def session_messages(path: str | Path) -> list[tuple[str, str]]:
    """提取可显示的用户与助手文本，跳过推理、工具调用和运行时注入内容。"""

    messages: list[tuple[str, str]] = []
    seen: set[str] = set()
    for event in iter_events(load_session_bytes(path)):
        kind = event.get("type")
        if kind == "user/message":
            data = event.get("data") or {}
            if (data.get("source") or {}).get("kind") != "user":
                continue
            identifier = str(data.get("id") or "")
            if identifier:
                if identifier in seen:
                    continue
                seen.add(identifier)
            content = _text_parts(data.get("content")).strip()
            if content:
                messages.append(("user", content))
        elif kind == "assistant/message":
            data = event.get("data") or {}
            message = data.get("message") or {}
            identifier = str(message.get("id") or "")
            if identifier:
                if identifier in seen:
                    continue
                seen.add(identifier)
            content = _text_parts(message.get("content")).strip()
            if content:
                messages.append(("assistant", content))
    return messages
