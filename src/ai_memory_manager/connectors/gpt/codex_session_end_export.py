#!/usr/bin/env python3
"""通过 Codex SessionEnd 钩子原样归档本地会话转录。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


脚本路径 = Path(__file__).resolve()
源代码目录 = 脚本路径.parents[3]
sys.path.insert(0, str(源代码目录))

from ai_memory_manager.archive.session_archive import (
    会话目录,
    原子写入字节,
    写入会话元数据,
)


def _读取事件() -> dict[str, Any]:
    try:
        事件 = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as 错误:
        raise ValueError(f"无法读取 Codex 钩子输入：{错误}") from 错误
    if not isinstance(事件, dict):
        raise ValueError("Codex 钩子输入必须是 JSON 对象")
    return 事件


def _归档(事件: dict[str, Any]) -> Path:
    会话编号 = 事件.get("session_id")
    转录路径 = 事件.get("transcript_path")
    if not isinstance(会话编号, str) or not 会话编号.strip():
        raise ValueError("钩子输入缺少有效的 session_id")
    if not isinstance(转录路径, str) or not 转录路径.strip():
        raise ValueError("此 Codex 会话没有可用的 transcript_path")

    源文件 = Path(转录路径).expanduser()
    if not 源文件.is_file():
        raise FileNotFoundError(f"找不到会话转录文件：{源文件}")

    日期 = datetime.now().astimezone().date().isoformat()
    目标目录 = 会话目录("Codex", 日期, 会话编号)
    目标文件 = 原子写入字节(目标目录 / "session.jsonl", 源文件.read_bytes())

    写入会话元数据(
        目标目录,
        来源="Codex",
        会话编号=会话编号,
        日期=日期,
        归档方式="session-end-hook",
        文件列表=sorted(
            path.name
            for path in 目标目录.iterdir()
            if path.is_file() and path.name != "session.meta.json"
        ),
        其他字段={
            "cwd": 事件.get("cwd"),
            "model": 事件.get("model"),
            "reason": 事件.get("reason"),
            "transcript_path": str(源文件),
            "transcript_format": "opaque; copied byte-for-byte",
        },
    )
    return 目标文件


def main() -> int:
    try:
        目标文件 = _归档(_读取事件())
    except Exception as 错误:
        print(f"Codex 会话归档失败：{错误}", file=sys.stderr)
        return 1
    print(f"Codex 会话已归档：{目标文件}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
