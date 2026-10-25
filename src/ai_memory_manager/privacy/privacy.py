"""对本地会话归档执行可预览的遗忘和删除操作。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_memory_manager.search.search import 默认索引, 可检索扩展名, 建立索引

默认会话库 = Path(__file__).resolve().parents[1] / "archive" / "Session_Lib"
忽略目录 = {".git", "__pycache__"}


@dataclass(frozen=True)
class 遗忘预览:
    文件数: int
    命中数: int
    文件: tuple[str, ...]
    已执行: bool = False


def _压缩索引(索引路径: str | Path) -> None:
    """清理 SQLite 索引空闲页和 WAL 中可能残留的旧正文。"""
    路径 = Path(索引路径).expanduser()
    if not 路径.is_file():
        return
    数据库 = sqlite3.connect(路径)
    try:
        数据库.execute("PRAGMA secure_delete=ON")
        数据库.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        数据库.execute("VACUUM")
        数据库.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        数据库.close()


def _有效归档文件(根目录: Path):
    for 路径 in 根目录.rglob("*"):
        if 路径.is_file() and 路径.suffix.lower() in 可检索扩展名 and not any(
            部件 in 忽略目录 for 部件 in 路径.relative_to(根目录).parts
        ):
            yield 路径


def _字符串数量(值: Any, 目标: str) -> int:
    if isinstance(值, str):
        return 值.count(目标)
    if isinstance(值, dict):
        return sum(_字符串数量(子项, 目标) for 子项 in 值.values())
    if isinstance(值, list):
        return sum(_字符串数量(子项, 目标) for 子项 in 值)
    return 0


def _替换值(值: Any, 目标: str) -> Any:
    if isinstance(值, str):
        return 值.replace(目标, "[已遗忘]")
    if isinstance(值, dict):
        return {键: _替换值(子项, 目标) for 键, 子项 in 值.items()}
    if isinstance(值, list):
        return [_替换值(子项, 目标) for 子项 in 值]
    return 值


def _遗忘文件(路径: Path, 目标: str) -> int:
    原文 = 路径.read_text(encoding="utf-8")
    命中数 = 0
    后缀 = 路径.suffix.lower()
    if 后缀 == ".jsonl":
        新行 = []
        for 行 in 原文.splitlines(keepends=True):
            try:
                对象 = json.loads(行)
            except json.JSONDecodeError:
                数 = 行.count(目标)
                命中数 += 数
                新行.append(行.replace(目标, "[已遗忘]"))
            else:
                命中数 += _字符串数量(对象, 目标)
                新行.append(json.dumps(_替换值(对象, 目标), ensure_ascii=False) + ("\n" if 行.endswith("\n") else ""))
        新文 = "".join(新行)
    elif 后缀 == ".json":
        try:
            对象 = json.loads(原文)
        except json.JSONDecodeError:
            命中数 = 原文.count(目标)
            新文 = 原文.replace(目标, "[已遗忘]")
        else:
            命中数 = _字符串数量(对象, 目标)
            新文 = json.dumps(_替换值(对象, 目标), ensure_ascii=False, indent=2) + "\n"
    else:
        命中数 = 原文.count(目标)
        新文 = 原文.replace(目标, "[已遗忘]")
    if 命中数:
        from ai_memory_manager.archive.session_archive import 原子写入字节

        原子写入字节(路径, 新文.encode("utf-8"))
    return 命中数


def 预览遗忘(
    内容: str,
    *,
    会话库: str | Path = 默认会话库,
) -> 遗忘预览:
    """统计归档中精确文本命中，不修改任何文件。"""
    if not 内容:
        raise ValueError("遗忘内容不能为空")
    根目录 = Path(会话库).expanduser().resolve()
    命中文件: list[str] = []
    命中数 = 0
    for 路径 in _有效归档文件(根目录):
        原文 = 路径.read_text(encoding="utf-8")
        # 预览以编码后的文件字节查找 JSON 字符串可见文本，并同时计数解码后的结构字段。
        数 = 原文.count(内容)
        if 路径.suffix.lower() in {".json", ".jsonl"}:
            try:
                if 路径.suffix.lower() == ".json":
                    数 = _字符串数量(json.loads(原文), 内容)
                else:
                    数 = sum(_字符串数量(json.loads(行), 内容) for 行 in 原文.splitlines() if 行.strip())
            except json.JSONDecodeError:
                pass
        if 数:
            命中文件.append(str(路径))
            命中数 += 数
    return 遗忘预览(len(命中文件), 命中数, tuple(命中文件))


def 遗忘内容(
    内容: str,
    *,
    会话库: str | Path = 默认会话库,
    索引路径: str | Path = 默认索引,
    确认: bool = False,
) -> 遗忘预览:
    """精确替换归档内文本，并重建受影响的全文索引；需显式确认。"""
    预览 = 预览遗忘(内容, 会话库=会话库)
    if not 确认 or not 预览.命中数:
        return 预览
    for 文件 in 预览.文件:
        _遗忘文件(Path(文件), 内容)
    建立索引(会话库, 索引路径)
    _压缩索引(索引路径)
    return 遗忘预览(预览.文件数, 预览.命中数, 预览.文件, True)


def 删除会话(
    来源: str,
    会话编号: str,
    *,
    会话库: str | Path = 默认会话库,
    索引路径: str | Path = 默认索引,
    确认: bool = False,
) -> tuple[Path, ...]:
    """按清单内的来源和会话编号匹配并删除会话目录。"""
    if not 来源 or not 会话编号:
        raise ValueError("来源和会话编号都必须提供")
    根目录 = Path(会话库).expanduser().resolve()
    匹配目录: list[Path] = []
    for 清单 in 根目录.rglob("session.meta.json"):
        if any(部件 in 忽略目录 for 部件 in 清单.relative_to(根目录).parts):
            continue
        try:
            元数据 = json.loads(清单.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if 元数据.get("provider") == 来源 and str(元数据.get("conversation_id")) == str(会话编号):
            匹配目录.append(清单.parent)
    if 确认:
        for 目录 in 匹配目录:
            shutil.rmtree(目录)
        if 匹配目录:
            建立索引(会话库, 索引路径)
            _压缩索引(索引路径)
    return tuple(匹配目录)


def main() -> int:
    解析器 = argparse.ArgumentParser(description="预览或执行本地会话遗忘与删除")
    子命令 = 解析器.add_subparsers(dest="操作", required=True)
    遗忘 = 子命令.add_parser("forget", help="按精确文本遗忘")
    遗忘.add_argument("text")
    遗忘.add_argument("--archive-dir", default=str(默认会话库))
    遗忘.add_argument("--index", default=str(默认索引))
    遗忘.add_argument("--apply", action="store_true", help="确认执行")
    删除 = 子命令.add_parser("delete-session", help="删除一个完整会话")
    删除.add_argument("provider")
    删除.add_argument("conversation_id")
    删除.add_argument("--archive-dir", default=str(默认会话库))
    删除.add_argument("--index", default=str(默认索引))
    删除.add_argument("--apply", action="store_true", help="确认执行")
    参数 = 解析器.parse_args()
    if 参数.操作 == "forget":
        预览 = 遗忘内容(参数.text, 会话库=参数.archive_dir, 索引路径=参数.index, 确认=参数.apply)
        print(f"命中 {预览.命中数} 处，涉及 {预览.文件数} 个文件")
        if 参数.apply and 预览.已执行:
            print("已遗忘并更新搜索索引")
        elif not 参数.apply:
            print("仅预览；添加 --apply 执行")
        else:
            print("没有匹配内容，无需执行")
    else:
        匹配 = 删除会话(参数.provider, 参数.conversation_id, 会话库=参数.archive_dir, 索引路径=参数.index, 确认=参数.apply)
        print(f"匹配 {len(匹配)} 个会话目录")
        for 路径 in 匹配:
            print(路径)
        if 参数.apply and 匹配:
            print("已删除会话并清理搜索索引")
        elif not 参数.apply:
            print("仅预览；添加 --apply 执行")
        else:
            print("没有匹配会话，无需执行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
