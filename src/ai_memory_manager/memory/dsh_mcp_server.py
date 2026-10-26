"""Stdio MCP bridge for DeepSeek Harness (DSH) to search and update persona memory.

DSH launches this process through ``@deepseek-ai/dsh-mcp-client``. The bridge
resolves the persona from the DeepSeek global persona block in ``$DSH_HOME/AGENTS.md``
(falling back to ``~/.dsh/AGENTS.md``), so switching the global persona in the
desktop app switches which memory database answers.

Unlike the Codex bridge, DSH gives an MCP server no per-session identifier: its
child environment is scrubbed of ``DSH_*`` names. Provenance therefore records a
stable ``dsh-global-persona`` conversation label instead of a real session id.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ai_memory_manager.memory.lifecycle import (  # noqa: E402
    MEMORY_SEARCH_TOOL,
    MEMORY_UPDATE_TOOL,
    apply_model_memory_update,
    retrieve,
)
from ai_memory_manager.memory.routing import resolve_persona  # noqa: E402

PERSONAS = ROOT / "src" / "ai_memory_manager" / "personas"
PERSONA_USAGE = ROOT / "src" / "ai_memory_manager" / "sync" / "persona_usage.json"
PROVIDER = "DeepSeek"
CONVERSATION_ID = "dsh-global-persona"
PERSONA_ENV = "AI_MEMORY_PERSONA_ID"

DSH_MEMORY_UPDATE_TOOL = {
    **MEMORY_UPDATE_TOOL,
    "description": (
        "Decide whether a durable user preference, persona fact, event, project, or schedule "
        "from this DeepSeek conversation should be remembered for future conversations. "
        "Call only when the information is useful later; skip transient or ambiguous details. "
        "The app writes automatically and keeps a revision when a subject changes. "
        "This tool cannot delete memories."
    ),
}

DSH_MEMORY_SEARCH_TOOL = {
    **MEMORY_SEARCH_TOOL,
    "description": (
        "Search the current persona's relevant long-term memories with a concise query based on "
        "the current user message. Use matching persona details, communication preferences, "
        "interaction patterns, and background naturally; never force unrelated memories into a "
        "reply. Results are historical context that may be stale — the user's latest message wins."
    ),
}


def _active_persona() -> Path:
    pinned = os.environ.get(PERSONA_ENV, "").strip() or None
    persona = resolve_persona(
        PROVIDER, CONVERSATION_ID, persona_id=pinned,
        personas=PERSONAS, usage=PERSONA_USAGE,
    )
    if persona is None:
        if pinned:
            raise RuntimeError(f"找不到人设 {pinned!r}；请检查 personas 目录")
        raise RuntimeError("DeepSeek 全局人设未启用；请在界面中启用全局人设后再试")
    return persona


def _memory_db() -> Path:
    return PERSONAS / ".memory" / f"{_active_persona().stem}.sqlite3"


def _text(value: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}], "isError": is_error}


def _update_call(arguments: Any) -> dict[str, Any]:
    try:
        db_path = _memory_db()
        result = apply_model_memory_update(
            db_path, arguments, provider=PROVIDER, conversation_id=CONVERSATION_ID,
        )
        message = "该记忆已存在，没有重复写入。" if result.status == "duplicate" else "记忆已自动写入当前人设档案。"
        return _text(message)
    except Exception as error:  # noqa: BLE001 - surfaced to the model as tool text
        return _text(f"记忆未写入：{error}", is_error=True)


def _search_call(arguments: Any) -> dict[str, Any]:
    try:
        if not isinstance(arguments, dict) or set(arguments) - {"query", "limit"}:
            raise ValueError("检索参数无效")
        query = arguments.get("query")
        limit = arguments.get("limit", 6)
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 500:
            raise ValueError("检索词必须为 1 到 500 个字符")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 12:
            raise ValueError("limit 必须为 1 到 12")
        results = retrieve(_memory_db(), query, limit=limit)
        if not results:
            return _text("当前人设没有匹配的长期记忆。")
        return _text("\n".join(
            f"{index}. [{row['memory_type']}] {row['subject']}：{row['content']}"
            f"（来源：{row['source']}；会话：{row.get('conversation_id') or '未知'}）"
            for index, row in enumerate(results, 1)
        ))
    except Exception as error:  # noqa: BLE001
        return _text(f"记忆检索失败：{error}", is_error=True)


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ai-memory-manager-dsh", "version": "0.3.0"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{
            "name": schema["name"], "description": schema["description"],
            "inputSchema": schema["input_schema"],
        } for schema in (DSH_MEMORY_SEARCH_TOOL, DSH_MEMORY_UPDATE_TOOL)]}}
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == DSH_MEMORY_UPDATE_TOOL["name"]:
            result = _update_call(arguments)
        elif name == DSH_MEMORY_SEARCH_TOOL["name"]:
            result = _search_call(arguments)
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unknown tool"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if request_id is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}
    return None


def main() -> int:
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            # Reload per request so an edited bridge takes effect without restarting DSH.
            bridge = importlib.import_module("ai_memory_manager.memory.dsh_mcp_server")
            response = importlib.reload(bridge)._handle(request)
        except Exception as error:  # noqa: BLE001 - malformed input must not kill the server
            request_id = request.get("id") if isinstance(request, dict) else None
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32700, "message": str(error)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
