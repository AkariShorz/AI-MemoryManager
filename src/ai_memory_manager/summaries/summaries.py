"""从本地归档生成可回溯的每日摘录概要，不调用外部 AI 服务。"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from ai_memory_manager.archive.session_archive import 原子写入会话对象

默认会话库 = Path(__file__).resolve().parents[1] / "archive" / "Session_Lib"
概要目录名 = "summaries"
忽略目录 = {".git", "__pycache__", 概要目录名}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _text(item)))
    if isinstance(value, dict):
        # ChatGPT exports store text in content.parts; captured messages use content directly.
        for key in ("text", "content", "parts", "result"):
            if key in value:
                result = _text(value[key])
                if result:
                    return result
    return ""


def _messages(value: Any) -> Iterator[tuple[str, str]]:
    """Yield role/text pairs from common ChatGPT, Codex, and visible-capture shapes."""
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, dict):
            author = message.get("author")
            role = author.get("role", "unknown") if isinstance(author, dict) else "unknown"
            content = _text(message.get("content"))
            if content:
                yield str(role), content
        elif isinstance(value.get("content"), str):
            content = value["content"].strip()
            if content:
                yield str(value.get("role") or "unknown"), content
        for key, child in value.items():
            if key not in {"message", "content"}:
                yield from _messages(child)
    elif isinstance(value, list):
        for child in value:
            yield from _messages(child)


def _session_content(directory: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in {"session.meta.json", "snapshots.jsonl"}:
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            found.extend(_messages(_read_json(path)))
        elif suffix == ".jsonl":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    found.extend(_messages(json.loads(line)))
                except json.JSONDecodeError:
                    continue
        elif suffix in {".txt", ".md"}:
            try:
                found.extend(("unknown", line.strip()) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            except (OSError, UnicodeError):
                pass
    return found


def _brief(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _session_records(root: Path) -> Iterator[dict[str, Any]]:
    for directory in sorted(root.glob("*/*/*")):
        if not directory.is_dir() or any(part in 忽略目录 for part in directory.parts):
            continue
        metadata = _read_json(directory / "session.meta.json") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        relative = directory.relative_to(root)
        day = str(metadata.get("date") or (relative.parts[1] if len(relative.parts) > 1 else ""))
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        messages = _session_content(directory)
        if not messages:
            continue
        users = [_brief(text) for role, text in messages if role.lower() in {"user", "human"}]
        assistant = [_brief(text) for role, text in messages if role.lower() in {"assistant", "ai"}]
        yield {
            "provider": str(metadata.get("provider") or relative.parts[0]),
            "conversation_id": str(metadata.get("conversation_id") or directory.name.split("--")[-1]),
            "title": metadata.get("title") or (directory.name.rsplit("--", 1)[0] if "--" in directory.name else directory.name),
            "date": day,
            "persona_id": metadata.get("persona_id"),
            "session_path": relative.as_posix(),
            "files": [p.name for p in sorted(directory.iterdir()) if p.is_file() and p.name != "session.meta.json"],
            "message_count": len(messages),
            "user_highlights": users[:3],
            "assistant_highlights": assistant[-3:],
        }


def 生成概要(
    日期: str | None = None,
    *,
    会话库: str | Path = 默认会话库,
) -> tuple[Path, ...]:
    """扫描归档并按日期写出 JSON 概要；传日期时只生成该日。"""
    if 日期 is not None:
        try:
            if date.fromisoformat(日期).isoformat() != 日期:
                raise ValueError
        except ValueError as exc:
            raise ValueError("日期必须为 YYYY-MM-DD") from exc
    root = Path(会话库).expanduser().resolve()
    records = [record for record in _session_records(root) if 日期 is None or record["date"] == 日期]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["date"]].append(record)
    output_root = root / 概要目录名
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_root, 0o700)
    # Summary files are fully derived; remove stale days so deleted or moved
    # sessions cannot remain discoverable through an old daily index.
    if 日期 is None:
        keep = {f"{day}.json" for day in grouped}
        for stale in output_root.glob("*.json"):
            if stale.name not in keep:
                stale.unlink()
    elif 日期 not in grouped:
        (output_root / f"{日期}.json").unlink(missing_ok=True)
    written: list[Path] = []
    for day, sessions in sorted(grouped.items()):
        doc = {
            "schema_version": 1,
            "date": day,
            "generated_at": datetime.now().astimezone().isoformat(),
            "summary_type": "extractive",
            "session_count": len(sessions),
            "sessions": sessions,
        }
        written.append(原子写入会话对象(output_root / f"{day}.json", doc))
    return tuple(written)


def 读取概要(日期: str, *, 会话库: str | Path = 默认会话库) -> dict[str, Any] | None:
    """读取某日概要；概要不存在时返回 None。"""
    try:
        date.fromisoformat(日期)
    except ValueError as exc:
        raise ValueError("日期必须为 YYYY-MM-DD") from exc
    value = _read_json(Path(会话库).expanduser() / 概要目录名 / f"{日期}.json")
    return value if isinstance(value, dict) else None


def 列出概要(*, 会话库: str | Path = 默认会话库) -> tuple[str, ...]:
    """列出已有概要日期。"""
    directory = Path(会话库).expanduser() / 概要目录名
    return tuple(sorted(path.stem for path in directory.glob("*.json"))) if directory.is_dir() else ()


def 定位原始会话(日期: str, 会话编号: str, *, 会话库: str | Path = 默认会话库) -> tuple[Path, ...]:
    """由每日概要中的会话编号返回原始会话目录和文件路径。"""
    doc = 读取概要(日期, 会话库=会话库)
    if not doc:
        return ()
    root = Path(会话库).expanduser().resolve()
    for item in doc.get("sessions", []):
        if str(item.get("conversation_id")) != str(会话编号):
            continue
        directory = (root / item["session_path"]).resolve()
        if directory != root and root not in directory.parents:
            return ()
        files = []
        for name in item.get("files", []):
            candidate = (directory / name).resolve()
            if candidate == directory or directory in candidate.parents:
                files.append(candidate)
        return (directory, *files)
    return ()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并查看本地每日会话概要")
    parser.add_argument("action", choices=("generate", "list", "show", "locate"))
    parser.add_argument("value", nargs="?", help="日期 YYYY-MM-DD，或 locate 时填写会话编号")
    parser.add_argument("--date", dest="day", help="locate 时指定日期")
    parser.add_argument("--archive-dir", default=str(默认会话库))
    args = parser.parse_args()
    if args.action == "generate":
        for path in 生成概要(args.value, 会话库=args.archive_dir):
            print(path)
    elif args.action == "list":
        print("\n".join(列出概要(会话库=args.archive_dir)))
    elif args.action == "show":
        if not args.value:
            parser.error("show 需要日期 YYYY-MM-DD")
        doc = 读取概要(args.value, 会话库=args.archive_dir)
        if doc is None:
            print("未找到概要；先运行 generate")
        else:
            for session in doc["sessions"]:
                print(f"[{session['provider']} | {session['title']} | {session['conversation_id']}]")
                for text in session["user_highlights"]:
                    print(f"用户：{text}")
                for text in session["assistant_highlights"]:
                    print(f"助手：{text}")
    else:
        if not args.value or not args.day:
            parser.error("locate 需要会话编号和 --date")
        print("\n".join(str(path) for path in 定位原始会话(args.day, args.value, 会话库=args.archive_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
