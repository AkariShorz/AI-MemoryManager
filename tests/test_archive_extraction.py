import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_memory_manager.memory.archive_extraction import extract_archive
from ai_memory_manager.memory.lifecycle import MemoryCandidate, list_memories, purge_conversation, retrieve
from ai_memory_manager.privacy.privacy import 删除会话


class ArchiveExtractionTests(unittest.TestCase):
    def setUp(self):
        mock_scores = patch("ai_memory_manager.memory.semantic.scores", return_value={})
        mock_scores.start()
        self.addCleanup(mock_scores.stop)

    def test_extract_once_retrieve_chinese_and_purge_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "archive" / "Codex" / "2026-10-26" / "conversation-1"
            session.mkdir(parents=True)
            (session / "session.meta.json").write_text(json.dumps({
                "provider": "Codex", "conversation_id": "conversation-1",
                "date": "2026-10-26", "persona_id": "demo",
            }), encoding="utf-8")
            (session / "messages.jsonl").write_text(json.dumps({
                "message_id": "user:1", "role": "user", "content": "我喜欢助手温柔地讲解数学。",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            db = root / "persona.sqlite3"
            def propose(message, _recent, _existing, _date, *, model):
                return [MemoryCandidate("stable_preference", "数学讲解风格",
                    "喜欢助手温柔地讲解数学", "local_ollama", 0.9,
                    source_message_id=message["id"])]
            with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=propose) as model:
                first = extract_archive(session, db)
                second = extract_archive(session, db)
            self.assertEqual(first, {"processed": 1, "created": 1, "duplicate": 0})
            self.assertEqual(second, {"processed": 0, "created": 0, "duplicate": 0})
            self.assertEqual(model.call_count, 1)
            found = retrieve(db, "助手数学讲解")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["source_message_id"], "user:1")
            self.assertEqual(purge_conversation(db, "Codex", "conversation-1"), 1)
            self.assertEqual(list_memories(db), [])
            self.assertEqual(retrieve(db, "助手数学讲解"), [])

    def test_privacy_deletion_cleans_derived_memory_without_touching_other_sources(self):
        from ai_memory_manager.memory.lifecycle import submit_candidate
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive"
            session = archive / "Codex" / "2026-10-26" / "c1"
            session.mkdir(parents=True)
            (session / "session.meta.json").write_text(json.dumps({
                "provider": "Codex", "conversation_id": "c1", "date": "2026-10-26",
            }), encoding="utf-8")
            memory_dir = root / "memories"
            db = memory_dir / "demo.sqlite3"
            for conversation, subject in (("c1", "删除来源"), ("c2", "保留来源")):
                submit_candidate(db, MemoryCandidate("project", subject, subject,
                    "conversation:Codex", conversation_id=conversation))
            删除会话("Codex", "c1", 会话库=archive, 索引路径=root / "index.sqlite3",
                     记忆目录=memory_dir, 确认=True)
            self.assertEqual([row["subject"] for row in list_memories(db)], ["保留来源"])


if __name__ == "__main__":
    unittest.main()
