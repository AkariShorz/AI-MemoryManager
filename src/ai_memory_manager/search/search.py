"""基于 SQLite FTS5 的本地归档全文索引与搜索。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

默认会话库 = Path(__file__).resolve().parents[1] / "archive" / "Session_Lib"
默认索引 = Path(__file__).resolve().parent / "search-index.sqlite3"
可检索扩展名 = {".json", ".jsonl", ".txt", ".md"}
忽略目录 = {".git", "__pycache__", "summaries"}


@dataclass(frozen=True)
class 搜索结果:
    来源: str
    会话编号: str
    标题: str | None
    日期: str | None
    人设编号: str | None
    文件路径: str
    摘要: str


def _数据库连接(索引路径: str | Path) -> sqlite3.Connection:
    路径 = Path(索引路径)
    路径.parent.mkdir(parents=True, exist_ok=True)
    数据库 = sqlite3.connect(路径)
    数据库.execute("PRAGMA journal_mode=WAL")
    数据库.execute("""CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, provider TEXT NOT NULL,
        conversation_id TEXT NOT NULL, title TEXT, date TEXT, persona_id TEXT,
        content TEXT NOT NULL, modified_ns INTEGER NOT NULL, size INTEGER NOT NULL,
        metadata_mtime_ns INTEGER NOT NULL DEFAULT 0
    )""")
    if "metadata_mtime_ns" not in {列[1] for 列 in 数据库.execute("PRAGMA table_info(documents)")}:
        数据库.execute("ALTER TABLE documents ADD COLUMN metadata_mtime_ns INTEGER NOT NULL DEFAULT 0")
    数据库.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
        content, title, provider, tokenize='unicode61'
    )""")
    return 数据库


def _读取文本(路径: Path) -> str:
    try:
        return 路径.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _提取文本(值: Any) -> Iterator[str]:
    """递归提取 JSON 中的自然语言字符串，忽略常见内部标识字段。"""
    忽略键 = {"id", "conversation_id", "message_id", "parent_id", "user_id", "create_time", "update_time", "timestamp"}
    if isinstance(值, dict):
        for 键, 内容 in 值.items():
            if 键.lower() in 忽略键:
                continue
            yield from _提取文本(内容)
    elif isinstance(值, list):
        for 内容 in 值:
            yield from _提取文本(内容)
    elif isinstance(值, str):
        yield 值


def _索引正文(正文: str) -> str:
    """让 unicode61 能检索连续汉字中的任意子串。"""
    return re.sub(r"(?<=[\u3400-\u9fff])(?=[\u3400-\u9fff])", " ", 正文)


def _文件正文(路径: Path) -> str:
    原文 = _读取文本(路径)
    if not 原文:
        return ""
    if 路径.suffix.lower() == ".jsonl":
        片段: list[str] = []
        for 行 in 原文.splitlines():
            try:
                片段.extend(_提取文本(json.loads(行)))
            except json.JSONDecodeError:
                片段.append(行)
        return "\n".join(片段)
    if 路径.suffix.lower() == ".json":
        try:
            return "\n".join(_提取文本(json.loads(原文)))
        except json.JSONDecodeError:
            return 原文
    return 原文


def _会话信息(目录: Path, 根目录: Path) -> dict[str, Any]:
    清单 = 目录 / "session.meta.json"
    元数据: dict[str, Any] = {}
    if 清单.is_file():
        try:
            元数据 = json.loads(_读取文本(清单))
        except json.JSONDecodeError:
            pass
    相对 = 目录.relative_to(根目录)
    编号 = str(元数据.get("conversation_id") or 目录.name.split("--")[-1])
    return {
        "provider": str(元数据.get("provider") or (相对.parts[0] if len(相对.parts) > 1 else "Unknown")),
        "conversation_id": 编号,
        "title": 元数据.get("title") or (目录.name.rsplit("--", 1)[0] if "--" in 目录.name else None),
        "date": 元数据.get("date") or (相对.parts[1] if len(相对.parts) > 2 else None),
        "persona_id": 元数据.get("persona_id"),
    }


def 建立索引(
    会话库: str | Path = 默认会话库,
    索引路径: str | Path = 默认索引,
) -> int:
    """增量扫描归档目录；返回新增或更新的文档数，并清理已删除文件的索引。"""
    根目录 = Path(会话库).expanduser().resolve()
    根目录.mkdir(parents=True, exist_ok=True)
    数据库 = _数据库连接(索引路径)
    已见路径: set[str] = set()
    更新数 = 0
    try:
        for 路径 in 根目录.rglob("*"):
            if not 路径.is_file() or 路径.name == "session.meta.json" or 路径.suffix.lower() not in 可检索扩展名:
                continue
            if any(部件 in 忽略目录 for 部件 in 路径.parts):
                continue
            绝对路径 = str(路径.resolve())
            已见路径.add(绝对路径)
            状态 = 路径.stat()
            清单 = 路径.parent / "session.meta.json"
            清单时间 = 清单.stat().st_mtime_ns if 清单.is_file() else 0
            现有 = 数据库.execute("SELECT id, modified_ns, size, metadata_mtime_ns FROM documents WHERE path=?", (绝对路径,)).fetchone()
            if 现有 and 现有[1] == 状态.st_mtime_ns and 现有[2] == 状态.st_size and 现有[3] == 清单时间:
                continue
            会话目录 = 路径.parent
            信息 = _会话信息(会话目录, 根目录)
            正文 = _文件正文(路径)
            if not 正文.strip():
                if 现有:
                    数据库.execute("DELETE FROM documents_fts WHERE rowid=?", (现有[0],))
                    数据库.execute("DELETE FROM documents WHERE id=?", (现有[0],))
                continue
            if 现有:
                文档编号 = 现有[0]
                数据库.execute("DELETE FROM documents_fts WHERE rowid=?", (文档编号,))
                数据库.execute("""UPDATE documents SET provider=?, conversation_id=?, title=?, date=?, persona_id=?, content=?, modified_ns=?, size=?, metadata_mtime_ns=? WHERE id=?""",
                    (信息["provider"], 信息["conversation_id"], 信息["title"], 信息["date"], 信息["persona_id"], 正文, 状态.st_mtime_ns, 状态.st_size, 清单时间, 文档编号))
            else:
                游标 = 数据库.execute("""INSERT INTO documents(path, provider, conversation_id, title, date, persona_id, content, modified_ns, size, metadata_mtime_ns)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (绝对路径, 信息["provider"], 信息["conversation_id"], 信息["title"], 信息["date"], 信息["persona_id"], 正文, 状态.st_mtime_ns, 状态.st_size, 清单时间))
                文档编号 = 游标.lastrowid
            数据库.execute("INSERT INTO documents_fts(rowid, content, title, provider) VALUES (?, ?, ?, ?)", (文档编号, _索引正文(正文), _索引正文(信息["title"] or ""), 信息["provider"]))
            更新数 += 1
        if 已见路径:
            占位符 = ",".join("?" for _ in 已见路径)
            旧编号 = [行[0] for 行 in 数据库.execute(f"SELECT id FROM documents WHERE path NOT IN ({占位符})", tuple(已见路径))]
        else:
            旧编号 = [行[0] for 行 in 数据库.execute("SELECT id FROM documents")]
        for 文档编号 in 旧编号:
            数据库.execute("DELETE FROM documents_fts WHERE rowid=?", (文档编号,))
            数据库.execute("DELETE FROM documents WHERE id=?", (文档编号,))
        数据库.commit()
        return 更新数
    finally:
        数据库.close()


def 搜索(
    查询: str,
    *,
    会话库: str | Path = 默认会话库,
    索引路径: str | Path = 默认索引,
    来源: str | None = None,
    人设编号: str | None = None,
    日期起: str | None = None,
    日期止: str | None = None,
    数量: int = 20,
) -> list[搜索结果]:
    """在已建立的索引中搜索；中文按 Unicode 分词，可直接搜索连续汉字片段。"""
    文字 = 查询.strip()
    if not 文字:
        return []
    if 数量 < 1:
        return []
    # 将用户输入转成短语，避免 FTS 运算符和标点被当作查询语法。
    短语 = re.sub(r'"', '""', 文字)
    数据库 = _数据库连接(索引路径)
    try:
        条件 = ["documents_fts MATCH ?"]
        参数: list[Any] = [f'"{_索引正文(短语)}"']
        if 来源:
            条件.append("d.provider = ?"); 参数.append(来源)
        if 人设编号:
            条件.append("d.persona_id = ?"); 参数.append(人设编号)
        if 日期起:
            条件.append("d.date >= ?"); 参数.append(日期起)
        if 日期止:
            条件.append("d.date <= ?"); 参数.append(日期止)
        参数.append(min(数量, 500))
        结果 = 数据库.execute(f"""SELECT d.provider, d.conversation_id, d.title, d.date, d.persona_id, d.path,
            d.content
            FROM documents_fts JOIN documents d ON d.id=documents_fts.rowid
            WHERE {' AND '.join(条件)} ORDER BY rank LIMIT ?""", 参数).fetchall()
        命中结果: list[搜索结果] = []
        for 行 in 结果:
            正文 = 行[6]
            位置 = 正文.casefold().find(文字.casefold())
            if 位置 >= 0:
                起点 = max(0, 位置 - 70)
                终点 = min(len(正文), 位置 + len(文字) + 110)
                摘要 = ("…" if 起点 else "") + 正文[起点:终点] + ("…" if 终点 < len(正文) else "")
            else:
                摘要 = 正文[:180]
            命中结果.append(搜索结果(*行[:6], 摘要))
        return 命中结果
    except sqlite3.OperationalError as 错误:
        if "fts5" in str(错误).lower() or "no such table" in str(错误).lower():
            raise RuntimeError("当前 SQLite 未启用 FTS5；请使用支持 FTS5 的 Python/SQLite。") from 错误
        raise ValueError(f"无效的搜索表达式：{错误}") from 错误
    finally:
        数据库.close()


def main() -> int:
    """命令行入口：首次执行自动增量建索引。"""
    import argparse

    解析器 = argparse.ArgumentParser(description="搜索本地 AI 会话归档")
    解析器.add_argument("query", help="要搜索的关键词或短语")
    解析器.add_argument("--archive-dir", default=str(默认会话库), help="会话库目录")
    解析器.add_argument("--index", default=str(默认索引), help="SQLite 索引文件")
    解析器.add_argument("--provider", help="按来源筛选")
    解析器.add_argument("--persona-id", help="按人设编号筛选")
    解析器.add_argument("--date-from", help="起始日期（含，YYYY-MM-DD）")
    解析器.add_argument("--date-to", help="结束日期（含，YYYY-MM-DD）")
    解析器.add_argument("--limit", type=int, default=20, help="最多返回条数（1-500）")
    参数 = 解析器.parse_args()
    try:
        更新数 = 建立索引(参数.archive_dir, 参数.index)
        命中 = 搜索(参数.query, 会话库=参数.archive_dir, 索引路径=参数.index,
            来源=参数.provider, 人设编号=参数.persona_id,
            日期起=参数.date_from, 日期止=参数.date_to, 数量=参数.limit)
    except (OSError, ValueError, RuntimeError) as 错误:
        解析器.error(str(错误))
    print(f"索引新增或更新 {更新数} 个文件；命中 {len(命中)} 条")
    for 结果 in 命中:
        print(f"\n[{结果.来源} | {结果.日期 or '日期未知'} | {结果.标题 or 结果.会话编号} | persona={结果.人设编号 or '无'}]")
        print(结果.摘要)
        print(结果.文件路径)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
