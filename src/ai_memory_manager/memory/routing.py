"""Resolve memory ownership from explicit bindings or the enabled global persona."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_memory_manager.personas.global_persona import current_persona_id

ROOT = Path(__file__).resolve().parents[3]
PERSONAS = ROOT / "src/ai_memory_manager/personas"
USAGE = ROOT / "src/ai_memory_manager/sync/persona_usage.json"


def read_bindings(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if isinstance(value, dict):
        value = value.get("bindings", [])
    if not isinstance(value, list):
        raise ValueError("人设绑定记录必须为列表或 bindings 对象")
    return [item for item in value if isinstance(item, dict)]


def resolve_persona(provider: str, conversation_id: str, *, persona_id: str | None = None,
                    personas: Path = PERSONAS, usage: Path = USAGE,
                    global_path: Path | None = None) -> Path | None:
    """An explicit but missing/disabled binding never falls through to another persona."""
    if persona_id is None:
        bindings = read_bindings(usage)
        binding = next((item for item in reversed(bindings)
                        if item.get("provider") == provider
                        and item.get("conversation_id") == conversation_id), None)
        if binding is not None:
            if binding.get("disabled"):
                return None
            persona_id = binding.get("persona_id")
            if not persona_id:
                return None
        elif provider == "Codex":
            persona_id = current_persona_id(global_path)
    if not persona_id:
        return None
    for path in sorted(personas.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("id", path.stem) == persona_id:
            return path
    return None
