"""读取并归档用户提供的 ChatGPT 数据导出文件。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_memory_manager.archive.session_archive import (
    会话日期,
    会话目录,
    原子写入会话对象,
    安全名称,
)


class ChatGPTExportError(ValueError):
    """输入文件不是受支持的 ChatGPT 导出文件时抛出的异常。"""


@dataclass(frozen=True)
class MessageNode:
    """会话消息映射中的节点；raw 保留完整源对象。"""

    node_id: str
    raw: Mapping[str, Any]

    @property
    def message(self) -> Mapping[str, Any] | None:
        value = self.raw.get("message")
        return value if isinstance(value, Mapping) else None


@dataclass(frozen=True)
class Conversation:
    """会话及其在导出文件中的完整 JSON 对象。"""

    conversation_id: str
    raw: Mapping[str, Any]

    def message_nodes(self) -> Iterator[MessageNode]:
        """按源文件顺序返回所有映射节点，包括空节点。"""

        mapping = self.raw.get("mapping")
        if not isinstance(mapping, Mapping):
            return
        for node_id, node in mapping.items():
            if isinstance(node, Mapping):
                yield MessageNode(node_id=str(node_id), raw=node)


def read_export(path: str | Path) -> list[Conversation]:
    """读取 ChatGPT 的 conversations.json 文件或 ZIP 导出包，不改动输入。"""

    source = Path(path)
    try:
        payload = _read_zip(source) if zipfile.is_zipfile(source) else source.read_bytes()
    except OSError as exc:
        raise ChatGPTExportError(f"无法读取导出文件：{source}") from exc

    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatGPTExportError("导出内容不是有效的 UTF-8 JSON") from exc
    if not isinstance(decoded, list):
        raise ChatGPTExportError("conversations.json 顶层必须是数组")

    conversations: list[Conversation] = []
    for index, raw in enumerate(decoded):
        if not isinstance(raw, Mapping):
            raise ChatGPTExportError(f"第 {index} 个会话不是对象")
        conversation_id = raw.get("conversation_id") or raw.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ChatGPTExportError(f"第 {index} 个会话缺少有效的会话编号")
        conversations.append(Conversation(conversation_id, raw))
    return conversations


def iter_conversations(path: str | Path) -> Iterator[Conversation]:
    """逐个返回导出文件中的会话对象。"""

    yield from read_export(path)


def _read_zip(path: Path) -> bytes:
    try:
        with zipfile.ZipFile(path) as archive:
            matches = [
                member
                for member in archive.infolist()
                if not member.is_dir() and Path(member.filename).name == "conversations.json"
            ]
            if len(matches) != 1:
                raise ChatGPTExportError(
                    "ZIP 导出包必须且只能包含一个 conversations.json"
                )
            return archive.read(matches[0])
    except (OSError, zipfile.BadZipFile) as exc:
        raise ChatGPTExportError(f"无法读取 ZIP 导出包：{path}") from exc


def archive_export(path: str | Path, output_dir: str | Path | None = None) -> list[Path]:
    """把导出包中的每个会话原对象分别写入 Session_Lib。"""

    archived: list[Path] = []
    for conversation in read_export(path):
        raw = conversation.raw
        title_value = raw.get("title")
        title = title_value.strip() if isinstance(title_value, str) else ""
        title = title or conversation.conversation_id
        date = 会话日期(raw.get("create_time"))
        directory = 会话目录(
            "ChatGPT", date, conversation.conversation_id, title, output_dir
        )
        archived.append(
            原子写入会话对象(directory / f"{安全名称(title)}.json", raw)
        )
    return archived


def watch_export_directory(
    directory: str | Path,
    output_dir: str | Path | None = None,
    poll_seconds: float = 3.0,
) -> None:
    """监视用户指定目录，文件连续两次保持不变后自动导入。"""

    watch_dir = Path(directory).expanduser().resolve()
    if not watch_dir.is_dir():
        raise NotADirectoryError(f"监视目录不存在：{watch_dir}")
    observed: dict[Path, tuple[int, int, int]] = {}
    imported: dict[Path, tuple[int, int]] = {}
    print(f"正在监视 ChatGPT 导出目录：{watch_dir}", file=sys.stderr)

    while True:
        for candidate in sorted(watch_dir.iterdir()):
            if not candidate.is_file() or candidate.suffix.lower() not in {".zip", ".json"}:
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            version = (stat.st_size, stat.st_mtime_ns)
            previous = observed.get(candidate)
            if previous is None or previous[:2] != version:
                observed[candidate] = (*version, 0)
                continue
            stable_count = previous[2] + 1
            observed[candidate] = (*version, stable_count)
            if stable_count < 1 or imported.get(candidate) == version:
                continue
            try:
                paths = archive_export(candidate, output_dir)
            except (ChatGPTExportError, OSError) as error:
                print(f"跳过 {candidate.name}：{error}", file=sys.stderr)
                imported[candidate] = version
                continue
            imported[candidate] = version
            print(f"已归档 {len(paths)} 个会话：{candidate}", file=sys.stderr)
        time.sleep(max(0.5, poll_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="导入并归档 ChatGPT 数据导出包")
    parser.add_argument("source", nargs="?", help="ZIP 文件或 conversations.json")
    parser.add_argument("--output-dir", help="Session_Lib 根目录")
    parser.add_argument("--watch-dir", help="监视指定目录并自动归档新导出文件")
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="监视轮询间隔")
    args = parser.parse_args()
    if args.watch_dir:
        if args.source:
            parser.error("--watch-dir 与 source 不能同时使用")
        try:
            watch_export_directory(args.watch_dir, args.output_dir, args.poll_seconds)
        except (OSError, ValueError) as error:
            parser.exit(1, f"导出目录监视失败：{error}\n")
        return 0
    if not args.source:
        parser.error("请提供 ZIP/conversations.json，或指定 --watch-dir")
    try:
        written = archive_export(args.source, args.output_dir)
    except (ChatGPTExportError, OSError) as error:
        parser.exit(1, f"导入失败：{error}\n")
    print(f"已归档 {len(written)} 个会话：")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
