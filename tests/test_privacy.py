import json
import tempfile
import unittest
from pathlib import Path

from ai_memory_manager.archive.session_archive import 写入会话元数据
from ai_memory_manager.privacy.privacy import 删除会话, 遗忘内容, 预览遗忘
from ai_memory_manager.search.search import 建立索引, 搜索


class PrivacyTests(unittest.TestCase):
    def _session(self, root: Path, provider: str, conversation_id: str, text: str) -> Path:
        session_dir = root / provider / "2026-10-26" / conversation_id
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(
            json.dumps({"conversation_id": conversation_id, "messages": [{"text": text}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        写入会话元数据(
            session_dir,
            来源=provider,
            会话编号=conversation_id,
            日期="2026-10-26",
            归档方式="test",
            文件列表=["session.json"],
        )
        return session_dir

    def test_forget_previews_then_updates_archive_and_search_index(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            index = Path(temporary_directory) / "search.sqlite3"
            session = self._session(root, "ChatGPT", "conversation-1", "这是一段秘密内容")
            建立索引(root, index)
            self.assertEqual(len(搜索("秘密内容", 会话库=root, 索引路径=index)), 1)

            preview = 预览遗忘("秘密内容", 会话库=root)
            self.assertEqual((preview.文件数, preview.命中数), (1, 1))
            self.assertIn("秘密内容", (session / "session.json").read_text(encoding="utf-8"))

            result = 遗忘内容("秘密内容", 会话库=root, 索引路径=index, 确认=True)
            self.assertTrue(result.已执行)
            self.assertIn("[已遗忘]", (session / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(搜索("秘密内容", 会话库=root, 索引路径=index), [])

    def test_delete_session_removes_archive_and_search_index(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            index = Path(temporary_directory) / "search.sqlite3"
            session = self._session(root, "ChatGPT", "conversation-1", "需要彻底删除的会话")
            建立索引(root, index)
            self.assertEqual(len(搜索("彻底删除", 会话库=root, 索引路径=index)), 1)

            self.assertEqual(删除会话("ChatGPT", "conversation-1", 会话库=root, 索引路径=index), (session,))
            self.assertTrue(session.exists())
            self.assertEqual(删除会话("ChatGPT", "conversation-1", 会话库=root, 索引路径=index, 确认=True), (session,))
            self.assertFalse(session.exists())
            self.assertEqual(搜索("彻底删除", 会话库=root, 索引路径=index), [])


if __name__ == "__main__":
    unittest.main()
