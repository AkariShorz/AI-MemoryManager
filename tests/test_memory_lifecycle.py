import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from ai_memory_manager.memory.lifecycle import (
    MemoryCandidate, apply_model_memory_update, delete_memory, expire_due, history, list_memories,
    process_conversation, retrieve, submit_candidate,
)


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        mock_scores = patch("ai_memory_manager.memory.semantic.scores", return_value={})
        mock_scores.start()
        self.addCleanup(mock_scores.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "memory.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, content, **kwargs):
        return MemoryCandidate("stable_preference", "交流风格", content,
                               source="user_statement", conversation_id="conv-1",
                               source_message_id="msg-1", **kwargs)

    def test_create_dedupe_retrieve_and_keep_provenance(self):
        first = submit_candidate(self.db, self.candidate("喜欢简短温和的表达"))
        duplicate = submit_candidate(self.db, self.candidate("喜欢简短温和的表达"))
        self.assertEqual(first.status, "created")
        self.assertEqual(duplicate.status, "duplicate")
        found = retrieve(self.db, "简短温和")
        self.assertEqual(found[0]["id"], first.memory_id)
        self.assertEqual(found[0]["source_message_id"], "msg-1")
        self.assertEqual(len(history(self.db, first.memory_id)), 1)

    def test_conflict_is_superseded_automatically_with_revision_history(self):
        original = submit_candidate(self.db, self.candidate("喜欢软软的表达"))
        candidate = submit_candidate(self.db, self.candidate("希望技术回复简洁直接"))
        self.assertEqual(candidate.status, "created")
        self.assertEqual(retrieve(self.db, "交流风格")[0]["id"], candidate.memory_id)
        self.assertEqual(list_memories(self.db, status="superseded")[0]["id"], original.memory_id)
        result = candidate
        self.assertEqual(result.status, "created")
        self.assertEqual(retrieve(self.db, "交流风格")[0]["id"], result.memory_id)
        self.assertEqual([r["action"] for r in history(self.db, original.memory_id)], ["created", "superseded"])

    def test_model_decides_when_to_write_and_tool_context_supplies_provenance(self):
        proposal = submit_candidate(self.db, MemoryCandidate("inferred", "偏好", "可能偏爱深色主题", "model", 0.99))
        self.assertEqual(proposal.status, "created")
        updated = apply_model_memory_update(self.db, {
            "memory_type": "stable_preference", "subject": "交流风格", "content": "喜欢自然温和的表达",
            "confidence": 0.9,
        }, provider="Codex", conversation_id="active-conversation", source_message_id="user-message")
        memory = next(item for item in list_memories(self.db) if item["id"] == updated.memory_id)
        self.assertEqual(memory["source"], "conversation:Codex")
        self.assertEqual(memory["conversation_id"], "active-conversation")
        self.assertEqual(memory["source_message_id"], "user-message")
        with self.assertRaises(ValueError):
            apply_model_memory_update(self.db, {"action": "delete", "memory_id": updated.memory_id},
                                      provider="Codex", conversation_id="active-conversation")

    def test_only_user_can_delete_and_delete_scrubs_content_history(self):
        result = submit_candidate(self.db, self.candidate("不要保留这条"))
        with self.assertRaises(PermissionError):
            delete_memory(self.db, result.memory_id, actor="model")
        delete_memory(self.db, result.memory_id, actor="user")
        self.assertEqual(list_memories(self.db, status=None), [])
        self.assertEqual(history(self.db, result.memory_id), [])

    def test_expiration_and_validation(self):
        result = submit_candidate(self.db, MemoryCandidate("schedule", "考试", "10月考试", "user_statement",
            valid_until="2026-10-25T00:00:00Z"))
        self.assertEqual(expire_due(self.db, now="2026-10-26T00:00:00Z"), 1)
        self.assertEqual(retrieve(self.db, "考试"), [])
        self.assertEqual(history(self.db, result.memory_id)[-1]["action"], "expired")
        with self.assertRaises(ValueError):
            submit_candidate(self.db, MemoryCandidate("unknown", "x", "y", "source"))
        with self.assertRaises(ValueError):
            submit_candidate(self.db, MemoryCandidate("schedule", "x", "y", "source", valid_from="2026-10-27", valid_until="2026-10-26"))

    def test_conversation_pipeline_overrides_provenance_and_rejects_unmapped_source(self):
        messages = [{"id": "u1", "role": "user", "content": "喜欢简短温和的表达"},
                    {"id": "a1", "role": "assistant", "content": "记下啦"}]
        candidate = MemoryCandidate("stable_preference", "交流风格", "喜欢软软的表达", "fabricated",
                                    source_message_id="u1")
        result = process_conversation(self.db, messages, provider="ChatGPT", conversation_id="c1",
                                      extractor=lambda _: [candidate])[0]
        memory = retrieve(self.db, "软软的表达")[0]
        self.assertEqual(result.status, "created")
        self.assertEqual(memory["source"], "conversation:ChatGPT")
        self.assertEqual(memory["conversation_id"], "c1")
        self.assertEqual(memory["source_message_id"], "u1")
        with self.assertRaises(ValueError):
            process_conversation(self.db, messages, provider="ChatGPT", conversation_id="c2",
                extractor=lambda _: [MemoryCandidate("stable_preference", "x", "y", "model", source_message_id="a1")])


if __name__ == "__main__":
    unittest.main()
