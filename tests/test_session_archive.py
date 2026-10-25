import json
import tempfile
import unittest
from pathlib import Path

from ai_memory_manager.archive.session_archive import (
    会话目录,
    原子写入会话对象,
    安全名称,
    写入会话元数据,
)


class SessionArchiveTests(unittest.TestCase):
    def test_safe_name_normalizes_unicode_and_removes_path_separators(self):
        self.assertEqual(安全名称(" e\u0301/会话 "), "é_会话")

    def test_session_directory_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session_dir = 会话目录(
                "ChatGPT", "2026-10-26", "conversation-1", "测试会话", root
            )
            self.assertTrue(session_dir.is_dir())
            self.assertEqual(session_dir.stat().st_mode & 0o777, 0o700)

            raw_session = {"conversation_id": "conversation-1", "messages": ["原文"]}
            raw_path = 原子写入会话对象(session_dir / "session.json", raw_session)
            self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8")), raw_session)
            self.assertEqual(raw_path.stat().st_mode & 0o777, 0o600)

            metadata_path = 写入会话元数据(
                session_dir,
                来源="ChatGPT",
                会话编号="conversation-1",
                日期="2026-10-26",
                会话名称="测试会话",
                人设编号="persona-1",
                归档方式="official-export",
                文件列表=[raw_path.name],
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["schema_version"], 1)
            self.assertEqual(metadata["provider"], "ChatGPT")
            self.assertEqual(metadata["persona_id"], "persona-1")
            self.assertEqual(metadata["files"], ["session.json"])
            self.assertEqual(metadata_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
