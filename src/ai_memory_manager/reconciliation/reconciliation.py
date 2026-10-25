"""周期性核对实时捕获快照与去重后的消息视图。"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_memory_manager.archive.session_archive import 原子写入字节

默认会话库 = Path(__file__).resolve().parents[1] / "archive" / "Session_Lib"
默认间隔秒数 = 120


@dataclass(frozen=True)
class 对账结果:
    扫描会话数: int
    修复文件数: int
    忽略无效快照数: int


def _读取消息(path: Path) -> tuple[list[dict[str, Any]], int]:
    消息列表: list[dict[str, Any]] = []
    无效数 = 0
    try:
        行列表 = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return 消息列表, 1
    for 行 in 行列表:
        if not 行.strip():
            continue
        try:
            消息 = json.loads(行)
        except json.JSONDecodeError:
            无效数 += 1
            continue
        if not isinstance(消息, dict) or not 消息.get("message_id") or not isinstance(消息.get("content"), str):
            无效数 += 1
            continue
        消息列表.append(消息)
    return 消息列表, 无效数


def _时间戳(消息: dict[str, Any]) -> str:
    return str(消息.get("updated_at") or 消息.get("observed_at") or "")


def _合并快照(消息路径: Path, 快照路径: Path) -> tuple[bool, int]:
    当前消息, 当前无效数 = _读取消息(消息路径) if 消息路径.exists() else ([], 0)
    快照列表, 快照无效数 = _读取消息(快照路径)
    # 读取失败时不能把现有消息视图覆盖为空文件。
    if 快照无效数 and not 快照列表:
        return False, 快照无效数
    合并: dict[str, dict[str, Any]] = {}
    for 消息 in 当前消息:
        合并[str(消息["message_id"])] = 消息
    for 消息 in 快照列表:
        编号 = str(消息["message_id"])
        旧消息 = 合并.get(编号)
        if 旧消息 is None or _时间戳(消息) >= _时间戳(旧消息):
            最新 = dict(消息)
        else:
            最新 = dict(旧消息)
        首次观察 = (
            (旧消息 or {}).get("first_observed_at")
            or 消息.get("first_observed_at")
            or 消息.get("observed_at")
        )
        if 首次观察:
            最新["first_observed_at"] = 首次观察
        合并[编号] = 最新

    新内容 = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in 合并.values())
    try:
        原内容 = 消息路径.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        原内容 = ""
    修复 = 原内容 != 新内容
    if 修复:
        原子写入字节(消息路径, 新内容.encode("utf-8"))
    return 修复, 快照无效数 + 当前无效数


def 对账会话库(会话库: str | Path = 默认会话库) -> 对账结果:
    """从快照日志重建各会话的最新去重消息视图。"""
    根目录 = Path(会话库).expanduser().resolve()
    if not 根目录.is_dir():
        return 对账结果(0, 0, 0)
    扫描数 = 修复数 = 无效数 = 0
    for 快照路径 in sorted(根目录.rglob("snapshots.jsonl")):
        if not 快照路径.is_file():
            continue
        扫描数 += 1
        修复, 本次无效数 = _合并快照(快照路径.with_name("messages.jsonl"), 快照路径)
        修复数 += int(修复)
        无效数 += 本次无效数
    return 对账结果(扫描数, 修复数, 无效数)


def main() -> int:
    parser = argparse.ArgumentParser(description="核对实时捕获快照与归档消息视图")
    parser.add_argument("--archive-dir", default=str(默认会话库), help="会话库目录")
    parser.add_argument("--interval-seconds", type=int, default=默认间隔秒数, help="持续模式的对账间隔，默认 120 秒")
    parser.add_argument("--watch", action="store_true", help="持续运行并按间隔对账")
    args = parser.parse_args()
    if args.interval_seconds < 1:
        parser.error("--interval-seconds 必须大于 0")

    while True:
        result = 对账会话库(args.archive_dir)
        print(
            f"已扫描 {result.扫描会话数} 个会话，修复 {result.修复文件数} 个消息视图，"
            f"忽略 {result.忽略无效快照数} 条无效记录",
            flush=True,
        )
        if not args.watch:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
