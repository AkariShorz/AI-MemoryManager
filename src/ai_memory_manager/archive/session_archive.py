"""提供连接器共用的本地会话归档路径和安全写入函数。"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any


会话库目录 = Path(__file__).resolve().parent / "Session_Lib"


def 安全名称(值: str, 默认值: str = "未命名会话") -> str:
    """生成适用于文件和目录的名称，保留中文并清理路径分隔符。"""

    清理后 = "".join(
        字符 if 字符.isalnum() or 字符 in "-_." else "_"
        for 字符 in unicodedata.normalize("NFC", 值.strip())
    ).strip(". ")
    return 清理后[:100] or 默认值


def 会话日期(时间戳: Any) -> str:
    """将 Unix 时间戳转换为本机日期；无法识别时返回 unknown-date。"""

    try:
        return datetime.fromtimestamp(float(时间戳)).astimezone().date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown-date"


def 会话目录(
    来源: str,
    日期: str,
    会话编号: str,
    会话名称: str | None = None,
    输出根目录: str | Path | None = None,
) -> Path:
    """返回按来源、日期和会话标识组织的会话目录。"""

    根目录 = Path(输出根目录) if 输出根目录 is not None else 会话库目录
    安全来源 = 安全名称(来源, "Unknown")
    安全编号 = 安全名称(会话编号, "unknown-id")
    安全日期 = 安全名称(日期, "unknown-date")
    安全标题 = 安全名称(会话名称, "") if 会话名称 else ""
    目录名 = f"{安全标题}--{安全编号}" if 安全标题 else 安全编号
    目标目录 = 根目录 / 安全来源 / 安全日期 / 目录名
    目标目录.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(目标目录, 0o700)
    return 目标目录


def 原子写入字节(目标路径: str | Path, 内容: bytes, 权限: int = 0o600) -> Path:
    """在目标目录内写临时文件，再原子替换为完整文件。"""

    目标 = Path(目标路径)
    目标.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    文件描述符, 临时路径 = tempfile.mkstemp(prefix=f".{目标.name}-", suffix=".tmp", dir=目标.parent)
    try:
        with os.fdopen(文件描述符, "wb") as 文件:
            文件.write(内容)
        os.chmod(临时路径, 权限)
        os.replace(临时路径, 目标)
    except Exception:
        try:
            os.unlink(临时路径)
        except FileNotFoundError:
            pass
        raise
    return 目标


def 原子写入会话对象(目标路径: str | Path, 内容: Any) -> Path:
    """以 UTF-8 JSON 保存未简化的会话对象。"""

    编码后 = (json.dumps(内容, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return 原子写入字节(目标路径, 编码后)


def 写入会话元数据(
    目录: str | Path,
    *,
    来源: str,
    会话编号: str,
    日期: str,
    会话名称: str | None = None,
    人设编号: str | None = None,
    归档方式: str,
    文件列表: list[str] | None = None,
    其他字段: dict[str, Any] | None = None,
) -> Path:
    """写入统一的会话索引元数据，不改动原始会话文件。"""

    元数据: dict[str, Any] = {
        "schema_version": 1,
        "provider": 来源,
        "persona_id": 人设编号,
        "conversation_id": 会话编号,
        "title": 会话名称,
        "date": 日期,
        "archived_at": datetime.now().astimezone().isoformat(),
        "archive_method": 归档方式,
        "files": 文件列表 or [],
    }
    if 其他字段:
        元数据.update(其他字段)
    return 原子写入会话对象(Path(目录) / "session.meta.json", 元数据)
