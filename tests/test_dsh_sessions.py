import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from compression import zstd
except ImportError:  # pragma: no cover - 测试环境需要 zstd 才能构造样本
    zstd = None

from ai_memory_manager.connectors.local_sessions import (
    archive_local_session,
    detect_local_connectors,
    discover_local_sessions,
    transcript_messages,
)


def _events() -> list[dict]:
    return [
        {"type": "session", "version": 4, "id": "session-11111111-2222-3333-4444-555555555555",
         "createdAt": 1790000000000, "cwd": "/tmp/work", "agentPreset": "standard"},
        {"type": "user/message", "seq": 1, "time": 1790000000001,
         "data": {"content": [{"type": "text", "text": "帮我整理 DSH 会话"}],
                  "source": {"kind": "user"}, "role": "user", "id": "u1"}},
        {"type": "user/message", "seq": 2, "time": 1790000000002,
         "data": {"content": [{"type": "text", "text": "Current runtime context."}],
                  "source": {"kind": "runtime-context"}, "role": "user", "id": "ctx"}},
        {"type": "assistant/message", "seq": 3, "time": 1790000000003,
         "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "id": "a1", "content": [
             {"type": "reasoning", "text": "先看看有哪些会话。"},
             {"type": "tool-call", "id": "call1", "name": "bash", "arguments": "{}"},
             {"type": "text", "text": "已经整理好了。"},
         ]}}},
    ]


def _write_session(root: Path, identifier: str, events: list[dict], title: str = "") -> Path:
    directory = root / "sessions" / "--tmp-work--" / f"session-{identifier}"
    directory.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    (directory / "session.v4.jsonl.zstd").write_bytes(zstd.compress(text.encode("utf-8")))
    if title:
        cache = root / "storages" / "session_projcache" / "sessions"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"session-{identifier}.json").write_text(json.dumps({
            "version": 7,
            "record": {"rows": {"title": {"ver": 1, "seq": 3, "val": title}}},
        }, ensure_ascii=False), encoding="utf-8")
    return directory / "session.v4.jsonl.zstd"


@unittest.skipIf(zstd is None, "需要 Python 3.14 的 compression.zstd")
class DshSessionTests(unittest.TestCase):
    identifier = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "dsh"
        self.root.mkdir()
        self.source = _write_session(self.root, self.identifier, _events(), "接入 DSH 会话库")
        self.env = patch.dict(os.environ, {"DSH_HOME": str(self.root)}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def test_detect_and_discover_reads_title_and_skips_empty_sessions(self):
        empty = "99999999-8888-7777-6666-555555555555"
        directory = self.root / "sessions" / "--tmp-work--" / f"session-{empty}"
        directory.mkdir(parents=True)
        (directory / "session.v4.jsonl.zstd").write_bytes(zstd.compress(
            json.dumps({"type": "session", "version": 4, "id": f"session-{empty}"}).encode()
        ))

        connectors = {info.connector_id: info for info in detect_local_connectors()}
        self.assertTrue(connectors["dsh"].installed)
        self.assertTrue(connectors["dsh"].can_archive)

        broken = self.root / "sessions" / "--tmp-work--" / "session-00000000-0000-0000-0000-000000000000"
        broken.mkdir(parents=True)
        (broken / "session.v4.jsonl.zstd").write_bytes(b"not a zstd frame")

        previews = discover_local_sessions("dsh")
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].session_id, self.identifier)
        self.assertEqual(previews[0].provider, "DeepSeek")
        self.assertEqual(previews[0].title, "接入 DSH 会话库")

    def test_archive_writes_decompressed_transcript_and_metadata(self):
        preview = discover_local_sessions("dsh")[0]
        output = Path(self.temporary.name) / "archive"
        destination = archive_local_session(preview, output)
        self.assertEqual(destination.parent.parent.parent.name, "DeepSeek")
        self.assertEqual(destination.name, "session.jsonl")
        self.assertTrue(destination.read_text(encoding="utf-8").startswith('{"type": "session"'))

        metadata = json.loads((destination.parent / "session.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["provider"], "DeepSeek")
        self.assertEqual(metadata["conversation_id"], self.identifier)
        self.assertEqual(metadata["source_connector"], "dsh")
        self.assertEqual(metadata["transcript_format"], "dsh-session-v4-jsonl (decompressed from zstd)")
        self.assertEqual(metadata["files"], ["session.jsonl"])
        self.assertTrue(metadata["source_sha256"])
        self.assertTrue(metadata["transcript_sha256"])

        messages = transcript_messages(destination, "dsh")
        self.assertEqual(messages, [("user", "帮我整理 DSH 会话"), ("assistant", "已经整理好了。")])

    def test_reimport_is_idempotent_and_updates_changed_source(self):
        preview = discover_local_sessions("dsh")[0]
        output = Path(self.temporary.name) / "archive"
        first = archive_local_session(preview, output)
        before = first.stat().st_mtime_ns
        archive_local_session(preview, output)
        self.assertEqual(first.stat().st_mtime_ns, before)

        events = _events() + [{
            "type": "user/message", "seq": 4, "time": 1790000000004,
            "data": {"content": [{"type": "text", "text": "再补一句"}],
                     "source": {"kind": "user"}, "role": "user", "id": "u2"},
        }]
        _write_session(self.root, self.identifier, events, "接入 DSH 会话库")
        updated = discover_local_sessions("dsh")[0]
        self.assertNotEqual(updated.mtime_ns, preview.mtime_ns)
        destination = archive_local_session(updated, output)
        messages = transcript_messages(destination, "dsh")
        self.assertEqual([text for role, text in messages if role == "user"],
                         ["帮我整理 DSH 会话", "再补一句"])
        archived = list(output.rglob("session.meta.json"))
        self.assertEqual(len(archived), 1)


if __name__ == "__main__":
    unittest.main()
