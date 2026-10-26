"""Stdio MCP bridge for Codex to search and update the active persona's memory."""

from __future__ import annotations

import json
import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ai_memory_manager.memory.lifecycle import (
    MEMORY_SEARCH_TOOL, MEMORY_UPDATE_TOOL, apply_model_memory_update, retrieve,
)
from ai_memory_manager.memory.routing import resolve_persona

PERSONAS = ROOT / "src" / "ai_memory_manager" / "personas"
PERSONA_USAGE = ROOT / "src" / "ai_memory_manager" / "sync" / "persona_usage.json"
PERSONA_SENDER = ROOT / "src" / "ai_memory_manager" / "connectors" / "gpt" / "persona_send.mjs"


def _active_context() -> tuple[Path, dict[str, Any]]:
    caller_id = os.environ.get("CODEX_THREAD_ID")
    conversation = None
    try:
        result = subprocess.run(
            ["node", str(PERSONA_SENDER), "status"], capture_output=True, text=True, timeout=10, check=False,
        )
        status = json.loads(result.stdout)
        conversation = next((item for item in status.get("conversations", [])
                             if isinstance(item, dict) and item.get("provider") == "Codex"
                             and (not caller_id or item.get("conversation_id") == caller_id)), None)
    except (ValueError, OSError, AttributeError, subprocess.TimeoutExpired):
        pass
    conversation_id = caller_id or (conversation or {}).get("conversation_id")
    if not conversation_id:
        raise RuntimeError("未取得调用会话编号；归档补录将在会话可用后重试")
    persona_path = resolve_persona("Codex", conversation_id, personas=PERSONAS, usage=PERSONA_USAGE)
    if persona_path is None:
        raise RuntimeError("没有可用的人设路由；请启用全局人设或设置此会话的人设")
    return PERSONAS / ".memory" / f"{persona_path.stem}.sqlite3", {
        "provider": "Codex", "conversation_id": conversation_id,
        "source_message_id": (conversation or {}).get("latest_user_message_id"),
    }


def _tool_call(arguments: Any) -> dict[str, Any]:
    try:
        db_path, context = _active_context()
        result = apply_model_memory_update(
            db_path, arguments, provider=context["provider"],
            conversation_id=context["conversation_id"],
            source_message_id=context["source_message_id"],
        )
        if result.status == "duplicate":
            message = "该记忆已存在，没有重复写入。"
        else:
            message = "记忆已自动写入当前人设档案。"
        return {"content": [{"type": "text", "text": message}], "isError": False}
    except Exception as error:
        return {"content": [{"type": "text", "text": f"记忆未写入：{error}"}], "isError": True}


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
        db_path, _context = _active_context()
        results = retrieve(db_path, query, limit=limit)
        if not results:
            text = "当前人设没有匹配的长期记忆。"
        else:
            text = "\n".join(
                f"{index}. [{row['memory_type']}] {row['subject']}：{row['content']}"
                f"（来源：{row['source']}；会话：{row.get('conversation_id') or '未知'}）"
                for index, row in enumerate(results, 1)
            )
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as error:
        return {"content": [{"type": "text", "text": f"记忆检索失败：{error}"}], "isError": True}


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized" or method == "notifications/cancelled":
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ai-memory-manager", "version": "0.2.0"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{
            "name": schema["name"], "description": schema["description"],
            "inputSchema": schema["input_schema"],
        } for schema in (MEMORY_SEARCH_TOOL, MEMORY_UPDATE_TOOL)]}}
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        if name == MEMORY_UPDATE_TOOL["name"]:
            result = _tool_call(params.get("arguments", {}))
        elif name == MEMORY_SEARCH_TOOL["name"]:
            result = _search_call(params.get("arguments", {}))
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
            # Refresh dispatch code for each request so a fixed bridge need not
            # remain stale for the entire desktop application's lifetime.
            bridge = importlib.import_module("ai_memory_manager.memory.codex_mcp_server")
            response = importlib.reload(bridge)._handle(request)
        except Exception as error:
            request_id = request.get("id") if isinstance(request, dict) else None
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32700, "message": str(error)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
