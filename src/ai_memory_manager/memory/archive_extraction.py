"""Extract durable user memories from persona-linked local conversation archives.

The archive is read-only. Ollama runs on loopback; the validated lifecycle owns
all writes, provenance and revision history.
"""

from __future__ import annotations

import hashlib
import base64
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ai_memory_manager.memory.lifecycle import (  # noqa: E402
    MEMORY_TYPES, MemoryCandidate, mark_message_ingested, message_was_ingested,
    process_conversation, retrieve,
)
from ai_memory_manager.summaries.summaries import _session_content  # noqa: E402

ARCHIVE = ROOT / "src/ai_memory_manager/archive/Session_Lib"
MODEL = os.environ.get("AI_MEMORY_OLLAMA_MODEL", "qwen3.5:9b")
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"


def archived_messages(directory: Path, provider: str) -> list[dict[str, str]]:
    """Prefer the captured message IDs; fall back to stable archive positions."""
    original = directory / "session.jsonl"
    if provider == "Codex" and original.is_file():
        from ai_memory_manager.connectors.local_sessions import _text_parts
        messages, fallback = [], []
        occurrences = {}
        for line in original.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            if record.get("type") == "response_item" and payload.get("type") == "message":
                role, content, target = payload.get("role"), _text_parts(payload.get("content")), messages
            elif record.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
                role = "user" if payload["type"] == "user_message" else "assistant"
                content, target = payload.get("message"), fallback
            else:
                continue
            if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()[:20]
            key = (role, digest, target is fallback)
            occurrences[key] = occurrences.get(key, 0) + 1
            target.append({"id": str(payload.get("id") or f"codex:{role}:{digest}:{occurrences[key]}"),
                "role": role, "content": content,
                "observed_at": record.get("timestamp")})
            if role == "user":
                images = []
                for part in payload.get("content", []) if isinstance(payload.get("content"), list) else []:
                    uri = part.get("image_url", "") if isinstance(part, dict) else ""
                    if isinstance(uri, str) and re.match(r"^data:image/(png|jpeg|webp);base64,", uri):
                        encoded = uri.split(",", 1)[1]
                        if len(encoded) <= 14_000_000:
                            try:
                                base64.b64decode(encoded, validate=True)
                                images.append(encoded)
                            except ValueError:
                                pass
                if images:
                    target[-1]["images"] = images[:3]
        if messages or fallback:
            return messages or fallback
    visible = directory / "messages.jsonl"
    if visible.is_file() and not (directory / "session.jsonl").is_file():
        messages = []
        for line in visible.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
                messages.append({"id": str(item.get("message_id") or len(messages)),
                                 "role": item["role"], "content": item["content"],
                                 "observed_at": item.get("first_observed_at") or item.get("observed_at")})
        return messages
    return [{"id": f"archive:{index}:{hashlib.sha256(content.encode()).hexdigest()[:16]}",
             "role": role, "content": content}
            for index, (role, content) in enumerate(_session_content(directory, provider))
            if role.casefold() in {"user", "human", "assistant", "ai"}]


def _ollama_extract(user_message: dict[str, str], recent: list[dict[str, str]],
                    existing: list[dict[str, Any]], observation_date: str,
                    *, model: str = MODEL) -> list[MemoryCandidate]:
    """Ask a local model for evidence-bound, self-contained facts from one user turn."""
    prior = [{"type": item["memory_type"], "subject": item["subject"],
              "content": item["content"]} for item in existing[:8]]
    clean = lambda message: {key: value for key, value in message.items() if key != "images"}
    images = list(user_message.get("images", []))
    if not images and any(word in user_message["content"] for word in ("课表", "图片", "照片", "这张", "这是", "我的", "这个")):
        images = next((item["images"] for item in reversed(recent) if item.get("images")), [])
    prompt = {
        "observation_date": observation_date,
        "recent_messages_for_reference_only": [{**clean(m), "content": m["content"][:3000]} for m in recent[-4:]
            if not m["content"].lstrip().startswith(("# AGENTS.md", "<INSTRUCTIONS>"))],
        "existing_memories_for_deduplication_and_subject_names": prior,
        "new_user_message_to_extract": clean(user_message),
    }
    schema = {"type": "object", "properties": {"memories": {"type": "array", "maxItems": 3,
        "items": {"type": "object", "properties": {
            "memory_type": {"type": "string", "enum": sorted(MEMORY_TYPES)},
            "subject": {"type": "string", "maxLength": 120}, "content": {"type": "string", "maxLength": 500},
            "confidence": {"type": "number"},
            "valid_until": {"type": ["string", "null"]},
        }, "required": ["memory_type", "subject", "content", "confidence", "valid_until"],
           "additionalProperties": False}}}, "required": ["memories"], "additionalProperties": False}
    system = (
        "你是本地长期记忆提取器。只从 new_user_message_to_extract 中提取用户明确表达、"
        "未来对话仍有用的偏好、身份、人设要求、项目事实、事件或有时限的安排。"
        "最近消息只用于理解指代；如果新消息明确确认前文图片或资料属于用户，可结合对应资料提取已确认的信息。"
        "旧记忆只用于去重和沿用主题名称，不能当作新事实来源。"
        "图片中模糊或截断内容不要猜测，没有年份时不要补年份，单周课表不能推定为每周固定安排。"
        "不要保存问句、一次性的操作命令、口令、密钥、个人隐私推断或助手未经用户确认的说法。"
        "没有值得保存的内容时返回空数组。每条内容要脱离当前聊天也能理解，沿用用户语言。"
        "最多提取3条，每条简明概括，不超过300字；课表只概括主要课程和适用周次，不逐格抄写。"
        "memory_type 按事实选择：长期偏好 stable_preference，AI角色设定 persona，"
        "已发生事件 episodic，项目事实 project，有明确期限的安排 schedule。主题名称也沿用用户语言。"
        "相对日期以 observation_date 为准；无法确定截止日期的安排不要选 schedule。"
        "已经过去的课表或安排作为历史资料，选 episodic；valid_until 仅用于尚未结束的 schedule，其他类型必须为 null。"
        "同主题的新陈述只在明确更新旧事实时提取。仅输出符合 JSON schema 的对象。"
    )
    request = urllib.request.Request(
        OLLAMA_CHAT,
        data=json.dumps({"model": model, "stream": False, "think": False, "format": schema,
                         "options": {"temperature": 0, "num_predict": 4096},
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": json.dumps(prompt, ensure_ascii=False),
                                       **({"images": images} if images else {})}]},
                        ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=120) as response:
            result = json.load(response)
    except (OSError, urllib.error.HTTPError) as error:
        raise RuntimeError(f"本机 Ollama 不可用或模型 {model} 无法响应：{error}") from error
    try:
        raw = json.loads(result["message"]["content"])["memories"]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("本机模型没有返回可解析的记忆候选") from error
    if not isinstance(raw, list) or len(raw) > 5:
        raise RuntimeError("本机模型返回了无效的记忆候选数量")
    candidates = []
    for item in raw:
        if not isinstance(item, dict) or item.get("confidence", 0) < 0.65:
            continue
        memory_type = item["memory_type"]
        valid_until = item.get("valid_until") if memory_type == "schedule" else None
        if valid_until and datetime.fromisoformat(valid_until.replace("Z", "+00:00")).date().isoformat() < observation_date:
            memory_type, valid_until = "episodic", None
        candidate = MemoryCandidate(
            memory_type=memory_type, subject=item["subject"], content=item["content"],
            confidence=item["confidence"], source="local_ollama",
            source_message_id=user_message["id"], valid_until=valid_until,
            extractor=f"ollama:{model}",
        ).validated()
        candidates.append(candidate)
    return candidates


def extract_archive(directory: Path, db_path: Path, *, model: str = MODEL,
                    before_commit=None, max_messages: int | None = None,
                    message_ids: set[str] | None = None, reprocess: bool = False) -> dict[str, int]:
    metadata = json.loads((directory / "session.meta.json").read_text(encoding="utf-8"))
    provider = str(metadata.get("provider") or directory.parent.parent.name)
    conversation_id = str(metadata.get("conversation_id") or metadata.get("session_id") or directory.name.rsplit("--", 1)[-1])
    observed = str(metadata.get("date") or directory.parent.name)
    date.fromisoformat(observed)
    messages = archived_messages(directory, provider)
    counts = {"processed": 0, "created": 0, "duplicate": 0}
    remaining = 0
    for index, message in enumerate(messages):
        if message_ids is not None and message["id"] not in message_ids:
            continue
        if message["role"].casefold() not in {"user", "human"} or not message["content"].strip():
            continue
        digest = hashlib.sha256(message["content"].encode("utf-8")).hexdigest()
        if not reprocess and message_was_ingested(db_path, provider, conversation_id, message["id"], digest):
            continue
        if max_messages is not None and counts["processed"] >= max_messages:
            remaining += 1
            continue
        if before_commit:
            before_commit()
        text = re.sub(r"<external_codex_apps_open_page>.*?</external_codex_apps_open_page>",
                      "", message["content"], flags=re.S).strip()
        if not text or text.startswith(("# AGENTS.md", "<INSTRUCTIONS>", "请在本对话接下来的回复中采用")):
            mark_message_ingested(db_path, provider, conversation_id, message["id"], digest)
            counts["processed"] += 1
            continue
        relevant = retrieve(db_path, message["content"], limit=8)
        observation_time = message.get("observed_at") or observed + "T00:00:00+00:00"
        observation_date = datetime.fromisoformat(observation_time.replace("Z", "+00:00")).date().isoformat()
        candidates = _ollama_extract(message, messages[max(0, index - 4):index],
                                     relevant, observation_date, model=model)
        if before_commit:
            before_commit()
        current = next((item for item in archived_messages(directory, provider) if item["id"] == message["id"]), None)
        if (current is None or current["content"] != message["content"]
                or current.get("images") != message.get("images")):
            raise RuntimeError("来源消息已修改或删除，将重新提取")
        # One message is the only accepted source for this extraction call.
        grounded = [replace(item, observed_at=observation_time, candidate_id=hashlib.sha256(
            (provider + "\0" + conversation_id + "\0" + message["id"] + "\0"
             + item.memory_type + "\0" + item.subject + "\0" + item.content
             + "\0" + str(item.valid_until) + "\0" + str(item.confidence)
             + "\0" + str(item.extractor)).encode()).hexdigest())
                    for item in candidates]
        results = process_conversation(db_path, [message], provider=provider,
                                       conversation_id=conversation_id, extractor=lambda _messages: grounded)
        mark_message_ingested(db_path, provider, conversation_id, message["id"], digest)
        counts["processed"] += 1
        for result in results:
            if result.status in counts:
                counts[result.status] += 1
    if max_messages is not None:
        counts["remaining"] = remaining
    return counts


def extract_persona_archives(persona_path: Path, *, archive_root: Path = ARCHIVE,
                             model: str = MODEL,
                             session_paths: list[Path] | None = None) -> dict[str, int]:
    persona_path = persona_path.expanduser().resolve()
    if persona_path.parent != (ROOT / "src/ai_memory_manager/personas").resolve():
        raise ValueError("人设档案必须位于应用的人设目录")
    persona = json.loads(persona_path.read_text(encoding="utf-8"))
    persona_id = persona.get("id", persona_path.stem)
    db_path = persona_path.parent / ".memory" / f"{persona_path.stem}.sqlite3"
    counts = {"sessions": 0, "processed": 0, "created": 0, "duplicate": 0}
    archive_root = archive_root.expanduser().resolve()
    directories = (sorted(path.parent for path in archive_root.rglob("session.meta.json")) if session_paths is None
                   else sorted({path.expanduser().resolve() for path in session_paths}))
    for directory in directories:
        if archive_root not in directory.resolve().parents:
            raise ValueError("所选会话必须位于本地会话库")
        if not directory.is_dir():
            continue
        meta_path = directory / "session.meta.json"
        if not meta_path.is_file():
            continue
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if session_paths is None and metadata.get("persona_id") != persona_id:
            continue
        result = extract_archive(directory, db_path, model=model)
        counts["sessions"] += 1
        for key in result:
            counts[key] += result[key]
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="使用本机模型从归档提取长期记忆")
    parser.add_argument("persona", type=Path)
    parser.add_argument("--session", action="append", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(extract_persona_archives(args.persona, session_paths=args.session), ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
