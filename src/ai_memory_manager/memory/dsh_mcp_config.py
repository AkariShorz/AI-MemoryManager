"""Keep the DSH profile's MCP memory entry in sync with the global persona toggle.

The desktop app calls :func:`install` when a DeepSeek global persona is enabled and
:func:`remove` when it is disabled, so users never edit the DSH profile by hand.

The profile file is user-maintained YAML containing ``!!js`` expressions, which
plain YAML loaders cannot round-trip. This module therefore edits text: it only
appends or excises the single ``- insert:`` item whose nested id is ``mcp-memory``,
leaving every other line byte-for-byte untouched, and it backs up the original
before each change.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BRIDGE = ROOT / "src" / "ai_memory_manager" / "memory" / "dsh_mcp_server.py"
SOURCE = ROOT / "src"
ENTRY_ID = "mcp-memory"
SERVER_NAME = "aimem"
TOOL_TIMEOUT_MS = 30000

COMMENT = "# ── 知忆长期记忆工具（AI-MemoryManager）自动管理；删除此块即可取消接入 ──"


def profile_path() -> Path:
    home = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")).expanduser()
    return home / "profiles" / "web" / "cordis.patch.yml"


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def entry_block(python: str | None = None) -> str:
    return "\n".join((
        COMMENT,
        "- insert:",
        f"    - id: {ENTRY_ID}",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        f"        serverName: {SERVER_NAME}",
        "        transport: stdio",
        f"        command: {_quote(python or sys.executable or 'python3')}",
        f"        args: [{_quote(BRIDGE)}]",
        "        env:",
        f"          PYTHONPATH: {_quote(SOURCE)}",
        f"        toolCallTimeoutMs: {TOOL_TIMEOUT_MS}",
    ))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}-dsh-memory")
    counter = 2
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{stamp}-{counter}-dsh-memory")
        counter += 1
    target.write_bytes(path.read_bytes())
    return target


def _write(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _entry_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == f"- id: {ENTRY_ID}":
            return index
    return None


def status(path: Path | None = None) -> str:
    """Return ``installed``, ``absent``, or ``profile-missing`` (no file to edit)."""
    target = path or profile_path()
    if not target.exists():
        return "profile-missing"
    return "installed" if _entry_index(_read(target).splitlines(keepends=True)) is not None else "absent"


def install(path: Path | None = None, *, python: str | None = None) -> str:
    """Append the MCP entry once. Returns ``installed``, ``already-installed``, or ``profile-missing``."""
    target = path or profile_path()
    if not target.exists():
        return "profile-missing"
    text = _read(target)
    if _entry_index(text.splitlines(keepends=True)) is not None:
        return "already-installed"
    if not BRIDGE.exists():
        raise FileNotFoundError(f"找不到记忆桥接脚本：{BRIDGE}")
    separator = "" if not text.strip() else ("\n" if text.endswith("\n") else "\n\n")
    updated = text + separator + entry_block(python) + "\n"
    _backup(target)
    _write(target, updated)
    return "installed"


def _item_span(lines: list[str], entry: int) -> tuple[int, int]:
    """Return the half-open line span of the ``- insert:`` item that owns *entry*."""
    start = None
    for index in range(entry, -1, -1):
        line = lines[index]
        if line[:1] not in {" ", "\t", "\n", "#", ""} and line.startswith("- "):
            start = index
            break
    if start is None or lines[start].strip() != "- insert:":
        raise ValueError("DSH 配置中的 mcp-memory 条目不在预期的 - insert: 列表中，已停止自动修改")
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    end = len(lines)
    for index in range(entry + 1, len(lines)):
        line = lines[index]
        if line[:1] not in {" ", "\t", "\n", ""} and line.startswith("- "):
            end = index
            break
    return start, end


def remove(path: Path | None = None) -> str:
    """Excise the managed MCP entry. Returns ``removed``, ``absent``, or ``profile-missing``."""
    target = path or profile_path()
    if not target.exists():
        return "profile-missing"
    text = _read(target)
    lines = text.splitlines(keepends=True)
    entry = _entry_index(lines)
    if entry is None:
        return "absent"
    start, end = _item_span(lines, entry)
    updated = "".join(lines[:start] + lines[end:])
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated = updated.rstrip("\n") + "\n" if updated.strip() else ""
    _backup(target)
    _write(target, updated)
    return "removed"
