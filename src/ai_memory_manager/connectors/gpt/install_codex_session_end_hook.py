#!/usr/bin/env python3
"""安全安装或移除 AI-MemoryManager 的 Codex SessionEnd 钩子。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


配置路径 = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
导出脚本 = Path(__file__).with_name("codex_session_end_export.py").resolve()
命令 = f'python3 "{导出脚本}"'


def _读取配置() -> dict[str, Any]:
    if not 配置路径.exists():
        return {"hooks": {}}
    try:
        配置 = json.loads(配置路径.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as 错误:
        raise ValueError(f"无法读取 {配置路径}：{错误}") from 错误
    if not isinstance(配置, dict):
        raise ValueError("hooks.json 顶层必须为 JSON 对象")
    if not isinstance(配置.get("hooks", {}), dict):
        raise ValueError("hooks.json 的 hooks 字段必须为对象")
    return 配置


def _是本项目处理器(处理器: Any) -> bool:
    return isinstance(处理器, dict) and 导出脚本.name in str(处理器.get("command", ""))


def _移除旧处理器(分组列表: list[Any]) -> list[Any]:
    新分组列表: list[Any] = []
    for 分组 in 分组列表:
        if not isinstance(分组, dict) or not isinstance(分组.get("hooks"), list):
            新分组列表.append(分组)
            continue
        其余处理器 = [处理器 for 处理器 in 分组["hooks"] if not _是本项目处理器(处理器)]
        if 其余处理器:
            分组 = {**分组, "hooks": 其余处理器}
            新分组列表.append(分组)
        elif any(键 != "hooks" for 键 in 分组):
            分组 = {键: 值 for 键, 值 in 分组.items() if 键 != "hooks"}
            新分组列表.append(分组)
    return 新分组列表


def _写入配置(配置: dict[str, Any]) -> None:
    配置路径.parent.mkdir(parents=True, exist_ok=True)
    文件描述符, 临时路径 = tempfile.mkstemp(prefix=".hooks-", suffix=".tmp", dir=配置路径.parent)
    try:
        with os.fdopen(文件描述符, "w", encoding="utf-8") as 文件:
            json.dump(配置, 文件, ensure_ascii=False, indent=2)
            文件.write("\n")
        os.chmod(临时路径, 0o600)
        os.replace(临时路径, 配置路径)
    except Exception:
        try:
            os.unlink(临时路径)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    操作 = sys.argv[1] if len(sys.argv) > 1 else "install"
    if 操作 not in {"install", "uninstall"}:
        print("用法：python3 install_codex_session_end_hook.py [install|uninstall]", file=sys.stderr)
        return 2
    try:
        配置 = _读取配置()
        钩子配置 = 配置.setdefault("hooks", {})
        现有分组 = 钩子配置.get("SessionEnd", [])
        if not isinstance(现有分组, list):
            raise ValueError("hooks.json 中的 SessionEnd 必须为数组")
        保留分组 = _移除旧处理器(现有分组)
        if 操作 == "install":
            保留分组.append({
                "hooks": [{"type": "command", "command": 命令, "timeout": 300}]
            })
        if 保留分组:
            钩子配置["SessionEnd"] = 保留分组
        else:
            钩子配置.pop("SessionEnd", None)
        if not 钩子配置:
            配置.pop("hooks", None)
        _写入配置(配置)
    except (OSError, ValueError) as 错误:
        print(f"Codex 钩子配置失败：{错误}", file=sys.stderr)
        return 1

    if 操作 == "install":
        print(f"已写入 {配置路径}。重启 Codex 后审阅并信任该钩子即可启用。")
    else:
        print(f"已从 {配置路径} 移除本项目的 SessionEnd 钩子。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
