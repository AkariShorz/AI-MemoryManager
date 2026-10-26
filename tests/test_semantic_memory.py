import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_memory_manager.memory.lifecycle import MemoryCandidate, _connect, delete_memory, retrieve, submit_candidate
from ai_memory_manager.memory.semantic import EmbeddingUnavailable


class SemanticMemoryTests(unittest.TestCase):
    def test_semantic_match_cache_model_change_and_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "memory.sqlite3"
            car = submit_candidate(db, MemoryCandidate("project", "car", "electric vehicle", "test"))
            submit_candidate(db, MemoryCandidate("stable_preference", "drink", "tea", "test"))
            digest = ["version-one"]
            batches = []
            def request(path, payload=None):
                if path == "/api/tags":
                    return {"models": [{"name": "bge-m3:latest", "digest": digest[0]}]}
                batches.append(payload["input"])
                return {"embeddings": [[0, 1] if "tea" in text else [1, 0]
                                       for text in payload["input"]]}
            with patch("ai_memory_manager.memory.semantic._request", side_effect=request):
                first = retrieve(db, "automobile")
                self.assertEqual([row["id"] for row in first], [car.memory_id])
                self.assertEqual(first[0]["retrieval_mode"], "hybrid")
                retrieve(db, "automobile")
                self.assertEqual(len(batches), 3)  # documents once, query each time
                digest[0] = "version-two"
                retrieve(db, "automobile")
                self.assertEqual(len(batches), 5)  # model replacement rebuilds document vectors
                delete_memory(db, car.memory_id)
                connection = _connect(db)
                try:
                    self.assertEqual(connection.execute("SELECT count(*) FROM memory_vectors WHERE memory_id=?",
                                                        (car.memory_id,)).fetchone()[0], 0)
                finally:
                    connection.close()
                self.assertEqual(retrieve(db, "automobile"), [])

    def test_unavailable_model_falls_back_and_filters_apply_before_embedding(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "memory.sqlite3"
            submit_candidate(db, MemoryCandidate("project", "Python", "学习 Python", "test"))
            submit_candidate(db, MemoryCandidate("schedule", "旧日程", "Python 考试", "test",
                                                 valid_until="2020-01-01T00:00:00Z"))
            with patch("ai_memory_manager.memory.semantic._request", side_effect=EmbeddingUnavailable("offline")):
                found = retrieve(db, "Python")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["retrieval_mode"], "keyword_fallback")
            with patch("ai_memory_manager.memory.semantic._request") as request:
                self.assertEqual(retrieve(db, "Python", memory_types=["schedule"]), [])
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
