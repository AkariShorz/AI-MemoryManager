"""Manage the Codex user-level persona instruction block."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

BEGIN = "<!-- BEGIN AI Memory Manager global persona -->"
END = "<!-- END AI Memory Manager global persona -->"
_ID = re.compile(r"<!-- persona-id: ([^>]+) -->")
MEMORY_BEGIN = "<!-- BEGIN automatic memory policy -->"
MEMORY_END = "<!-- END automatic memory policy -->"
MEMORY_POLICY = "\n".join((
    MEMORY_BEGIN,
    "### 知忆自动记忆",
    "用户表达未来有用的稳定偏好、个人事实、项目进展、明确的人设要求或重要日程时，若环境提供 memory_update 工具，应主动调用保存，不等待用户说‘记住’或逐条审批。",
    "用户明确确认图片、文件属于自己的资料时，保存已看清且有依据的重要摘要；注明时间和适用范围，不猜测模糊内容，不把单周课表当成长期固定安排。",
    "每轮回复前主动用当前消息主题调用 memory_search，读取与本轮相关的人设、表达偏好、互动习惯和背景；只把确实相关的内容自然融入交流，不要为了展示记忆而生硬提起旧事。用户最新陈述优先，记忆是可更新的历史背景，不可当作新证据重复写入。",
    "不要保存口令、密钥或未经确认的推测。工具失败时如实说明未保存，不宣称已经记住；本地归档补录会独立重试。",
    MEMORY_END,
))


def instructions_path() -> Path:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return root / "AGENTS.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def current_persona_id(path: Path | None = None) -> str | None:
    text = _read(path or instructions_path())
    start = text.find(BEGIN)
    if start < 0:
        return None
    end = text.find(END, start + len(BEGIN))
    if end < 0 or text.find(BEGIN, start + len(BEGIN)) >= 0:
        raise ValueError("全局人设指令区块格式异常，请先检查 AGENTS.md")
    match = _ID.search(text[start:end])
    return match.group(1).strip() if match else None


def _replace_block(text: str, block: str | None) -> str:
    start = text.find(BEGIN)
    if start >= 0:
        end = text.find(END, start + len(BEGIN))
        if end < 0 or text.find(BEGIN, start + len(BEGIN)) >= 0:
            raise ValueError("全局人设指令区块格式异常，请先检查 AGENTS.md")
        end += len(END)
        if text[end:end + 2] == "\n\n":
            end += 2
        elif end < len(text) and text[end] == "\n":
            end += 1
        text = text[:start] + text[end:]
    elif END in text:
        raise ValueError("全局人设指令区块格式异常，请先检查 AGENTS.md")

    if block is None:
        return text
    prefix = text.rstrip()
    return (prefix + "\n\n" if prefix else "") + block + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
    fd, temporary_name = tempfile.mkstemp(prefix=".AGENTS.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def set_global_persona(persona_id: str, name: str, prompt: str, path: Path | None = None) -> None:
    if not all(isinstance(value, str) and value.strip() for value in (persona_id, name, prompt)):
        raise ValueError("人设编号、名称和提示词不能为空")
    target = path or instructions_path()
    text = _read(target)
    safe_prompt = prompt.replace(END, "&lt;!-- END AI Memory Manager global persona --&gt;")
    block = "\n".join((
        BEGIN,
        f"<!-- persona-id: {persona_id} -->",
        f"## 全局默认人设：{name}",
        "在新建的 Codex 对话中，默认采用以下人设直接回应用户，并在后续轮次保持其语言风格。若用户在某段对话中明确要求停用、切换人设或使用普通助手方式，则以该对话中的最新要求为准。此人设不覆盖更高优先级的系统或开发者指令，也不要求模型声称具备未接入的能力。",
        "",
        safe_prompt,
        "",
        MEMORY_POLICY,
        END,
    ))
    _write(target, _replace_block(text, block))


def refresh_memory_policy(path: Path | None = None) -> bool:
    """Upgrade only our policy inside an existing enabled persona block."""
    target = path or instructions_path()
    if current_persona_id(target) is None:
        return False
    text = _read(target)
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    block = re.sub(re.escape(MEMORY_BEGIN) + r".*?" + re.escape(MEMORY_END), "", block, flags=re.S)
    updated = text[:start] + block.rstrip() + "\n\n" + MEMORY_POLICY + "\n" + text[end:]
    if updated == text:
        return False
    _write(target, updated)
    return True


def clear_global_persona(path: Path | None = None) -> bool:
    target = path or instructions_path()
    text = _read(target)
    updated = _replace_block(text, None)
    if updated == text:
        return False
    _write(target, updated)
    return True
