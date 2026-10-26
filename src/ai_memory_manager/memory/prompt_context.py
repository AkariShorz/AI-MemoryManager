"""Build a bounded prompt section from one persona's active local memories."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ai_memory_manager.memory.lifecycle import expire_due, list_memories  # noqa: E402


def build_context(persona_path: str | Path, *, max_chars: int = 7000) -> str:
    persona = Path(persona_path).expanduser().resolve()
    db_path = persona.parent / ".memory" / f"{persona.stem}.sqlite3"
    expire_due(db_path)
    now = datetime.now(timezone.utc)
    rows = list_memories(db_path, status="active", limit=200)
    lines = []
    for row in rows:
        if row.get("valid_from"):
            start = datetime.fromisoformat(row["valid_from"].replace("Z", "+00:00"))
            if start > now:
                continue
        if row.get("valid_until"):
            end = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00"))
            if end <= now:
                continue
        line = f"- [{row['memory_type']}] {row['subject']}：{row['content']}"
        if sum(len(item) + 1 for item in lines) + len(line) > max_chars:
            break
        lines.append(line)
    context = "\n".join(lines)
    print(json.dumps({"context": context}, ensure_ascii=False))
    return context


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: prompt_context.py PERSONA_JSON")
    build_context(sys.argv[1])
