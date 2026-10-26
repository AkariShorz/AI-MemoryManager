import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_memory_manager.archive.session_archive import 写入会话元数据
from ai_memory_manager.memory.jobs import ArchiveQueue
from ai_memory_manager.memory.lifecycle import MemoryCandidate, list_memories, submit_candidate
from ai_memory_manager.memory.routing import resolve_persona
from ai_memory_manager.personas.global_persona import refresh_memory_policy, set_global_persona
from ai_memory_manager.memory.archive_extraction import archived_messages, _ollama_extract
from ai_memory_manager.connectors.local_sessions import refresh_visible_codex_archives


class AutomaticMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        self.personas = self.root / "personas"
        self.personas.mkdir()
        (self.personas / "sample.json").write_text(json.dumps({"id": "sample"}))
        self.usage = self.root / "usage.json"
        self.global_path = self.root / "AGENTS.md"
        set_global_persona("sample", "示例", "示例人设", self.global_path)
        self.session = self.archive / "Codex/2026-10-26/c1"
        self.session.mkdir(parents=True)
        (self.session / "messages.jsonl").write_text(json.dumps({
            "role": "user", "message_id": "u1", "content": "我爱吃西瓜", "observed_at": "2026-10-26T09:00:00Z",
        }, ensure_ascii=False) + "\n")
        写入会话元数据(self.session, 来源="Codex", 会话编号="c1", 日期="2026-10-26",
                    归档方式="test", 文件列表=["messages.jsonl"])
        self.queue = ArchiveQueue(self.archive, personas=self.personas, usage=self.usage, global_path=self.global_path)
        self.addCleanup(self.queue.close)
        patcher = patch("ai_memory_manager.memory.semantic.scores", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def proposal(self, message, *_args, **_kwargs):
        return [MemoryCandidate("stable_preference", "水果喜好", "用户喜欢吃西瓜。", "model",
                                source_message_id=message["id"])]

    def job(self):
        return self.queue.db.execute("SELECT * FROM jobs").fetchone()

    def test_archive_hook_enqueues_and_backup_writes_without_active_model_call(self):
        self.assertEqual(self.job()["status"], "pending")
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=self.proposal) as model:
            self.assertTrue(self.queue.step())
            self.queue.discover()
            self.assertFalse(self.queue.step())
            self.assertEqual(model.call_count, 1)
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual(len(list_memories(self.personas / ".memory/sample.sqlite3")), 1)

    def test_dual_channels_deduplicate_even_when_subject_labels_differ(self):
        db = self.personas / ".memory/sample.sqlite3"
        submit_candidate(db, MemoryCandidate("stable_preference", "西瓜", "用户喜欢吃西瓜。", "conversation:Codex"))
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=self.proposal):
            self.queue.step()
        self.assertEqual(len(list_memories(db)), 1)
        self.assertEqual(json.loads(self.job()["result"])["duplicate"], 1)

    def test_failed_job_is_durable_and_retries_without_marking_message_done(self):
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=RuntimeError("offline")):
            self.queue.step()
        self.assertEqual(self.job()["status"], "retry")
        self.assertEqual(self.job()["attempts"], 1)
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET next_retry=0")
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=self.proposal):
            self.queue.step()
        self.assertEqual(self.job()["status"], "done")
        self.assertEqual(self.job()["attempts"], 2)

    def test_cancel_during_inference_prevents_memory_resurrection(self):
        def cancelled(message, *args, **kwargs):
            self.queue.cancel(self.session)
            return self.proposal(message)
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=cancelled):
            self.queue.step()
        self.assertEqual(self.job()["status"], "cancelled")
        self.assertEqual(list_memories(self.personas / ".memory/sample.sqlite3"), [])
        self.queue.discover()
        self.assertFalse(self.queue.step())

    def test_missing_route_waits_then_resumes_and_disabled_binding_blocks_global(self):
        self.global_path.unlink()
        self.queue.step()
        self.assertEqual(self.job()["status"], "needs_persona")
        set_global_persona("sample", "示例", "示例", self.global_path)
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET next_retry=0")
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=self.proposal):
            self.queue.step()
        self.assertEqual(self.job()["status"], "done")
        self.usage.write_text(json.dumps({"bindings": [{"provider": "Codex", "conversation_id": "c1", "disabled": True}]}))
        self.assertIsNone(resolve_persona("Codex", "c1", personas=self.personas, usage=self.usage, global_path=self.global_path))

    def test_policy_upgrade_preserves_other_text_and_is_idempotent(self):
        text = self.global_path.read_text()
        self.global_path.write_text("Before\n" + text + "\nAfter\n")
        refresh_memory_policy(self.global_path)
        self.assertFalse(refresh_memory_policy(self.global_path))
        text = self.global_path.read_text()
        self.assertIn("Before", text)
        self.assertIn("After", text)
        self.assertEqual(text.count("BEGIN automatic memory policy"), 1)

    def test_older_archive_does_not_replace_newer_preference(self):
        db = self.personas / ".memory/sample.sqlite3"
        submit_candidate(db, MemoryCandidate("stable_preference", "水果喜好", "现在不吃西瓜", "conversation:Codex",
                                             observed_at="2026-10-27T00:00:00Z"))
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=self.proposal):
            self.queue.step()
        self.assertEqual(list_memories(db)[0]["content"], "现在不吃西瓜")

    def test_codex_attachment_and_observation_time_are_preserved_for_confirmation(self):
        (self.session / "session.jsonl").write_text(json.dumps({
            "type": "response_item", "timestamp": "2026-10-26T08:00:00Z", "payload": {
                "type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "这是我的课表"},
                    {"type": "input_image", "image_url": "data:image/png;base64,aGVsbG8="},
                    {"type": "input_image", "image_url": "https://example.invalid/private-image"},
                ]}}))
        messages = archived_messages(self.session, "Codex")
        self.assertEqual(messages[0]["images"], ["aGVsbG8="])
        self.assertEqual(messages[0]["observed_at"], "2026-10-26T08:00:00Z")

    def test_source_edit_during_inference_is_retried_without_writing_stale_fact(self):
        def edited(message, *_args, **_kwargs):
            (self.session / "messages.jsonl").write_text(json.dumps({
                "role": "user", "message_id": "u1", "content": "现在不吃西瓜"}) + "\n")
            return self.proposal(message)
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=edited):
            self.queue.step()
        self.assertEqual(self.job()["status"], "retry")
        self.assertEqual(list_memories(self.personas / ".memory/sample.sqlite3"), [])
        self.queue.discover()
        self.assertEqual(self.job()["status"], "pending")

    def test_appending_new_messages_does_not_discard_an_unchanged_fact(self):
        def appended(message, *_args, **_kwargs):
            with (self.session / "messages.jsonl").open("a") as stream:
                stream.write(json.dumps({"role": "assistant", "message_id": "a1", "content": "好的"}) + "\n")
            self.queue.enqueue(self.session)
            return self.proposal(message)
        with patch("ai_memory_manager.memory.archive_extraction._ollama_extract", side_effect=appended) as model:
            self.queue.step()
            self.queue.step()
            self.assertEqual(model.call_count, 1)
        self.assertEqual(len(list_memories(self.personas / ".memory/sample.sqlite3")), 1)
        self.assertEqual(self.job()["status"], "done")

    def test_historical_timetable_is_retrievable_without_expiry(self):
        for memory_type in ("episodic", "schedule"):
            result = {"message": {"content": json.dumps({"memories": [{
                "memory_type": memory_type, "subject": "第5周课表", "content": "用户确认这份课表属于自己",
                "confidence": 0.9, "valid_until": "2026-10-25T00:00:00Z"}]})}}
            with patch("ai_memory_manager.memory.archive_extraction.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps(result).encode())
                candidates = _ollama_extract({"id": "u1", "role": "user", "content": "这是我的课表"}, [], [], "2026-10-26")
            self.assertEqual(candidates[0].memory_type, "episodic")
            self.assertIsNone(candidates[0].valid_until)

    def test_visible_session_gets_new_raw_messages_without_modifying_source(self):
        session_id = "00000000-0000-0000-0000-000000000001"
        home = self.root / "home"
        source_dir = home / ".codex/sessions"
        source_dir.mkdir(parents=True)
        source = source_dir / f"rollout-2026-10-26-{session_id}.jsonl"
        source.write_text('{"type":"session_meta","payload":{"id":"' + session_id + '"}}\n')
        directory = self.archive / f"Codex/2026-10-26/{session_id}"
        directory.mkdir(parents=True)
        (directory / "messages.jsonl").write_text("")
        写入会话元数据(directory, 来源="Codex", 会话编号=session_id, 日期="2026-10-26",
                    人设编号="sample", 归档方式="visible-capture", 文件列表=["messages.jsonl"])
        with patch.dict(os.environ):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(refresh_visible_codex_archives(self.archive, home=home), 1)
            with source.open("a") as stream:
                stream.write('{"type":"event_msg","payload":{"type":"user_message","message":"我爱吃西瓜"}}\n')
            original = source.read_bytes()
            self.assertEqual(refresh_visible_codex_archives(self.archive, home=home), 1)
            self.assertEqual((directory / "session.jsonl").read_bytes(), original)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(refresh_visible_codex_archives(self.archive, home=home), 0)


if __name__ == "__main__":
    unittest.main()
