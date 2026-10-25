import json
import tempfile
import unittest
from pathlib import Path

from ai_memory_manager.archive.session_archive import 写入会话元数据
from ai_memory_manager.privacy.privacy import 删除会话
from ai_memory_manager.summaries.summaries import 生成概要, 列出概要, 定位原始会话, 读取概要


class SummaryTests(unittest.TestCase):
    def _session(self, root: Path, conversation_id: str, user: str, assistant: str) -> Path:
        directory = root / "ChatGPT" / "2026-10-26" / conversation_id
        directory.mkdir(parents=True)
        raw_path = directory / "session.json"
        raw_path.write_text(
            json.dumps({"messages": [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ]}, ensure_ascii=False),
            encoding="utf-8",
        )
        写入会话元数据(
            directory, 来源="ChatGPT", 会话编号=conversation_id,
            日期="2026-10-26", 会话名称="测试会话", 归档方式="test",
            文件列表=[raw_path.name],
        )
        return directory

    def test_generate_read_and_locate_original_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            session = self._session(root, "conversation-1", "用户问题内容", "助手回答内容")

            generated = 生成概要(会话库=root)
            summary = 读取概要("2026-10-26", 会话库=root)

            self.assertEqual(len(generated), 1)
            self.assertEqual(summary["summary_type"], "extractive")
            self.assertEqual(summary["sessions"][0]["user_highlights"], ["用户问题内容"])
            self.assertEqual(summary["sessions"][0]["assistant_highlights"], ["助手回答内容"])
            self.assertEqual(定位原始会话("2026-10-26", "conversation-1", 会话库=root),
                (session, session / "session.json"))
            self.assertEqual(列出概要(会话库=root), ("2026-10-26",))

    def test_deleting_session_rebuilds_and_removes_stale_summary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            self._session(root, "conversation-1", "用户问题内容", "助手回答内容")
            生成概要(会话库=root)

            删除会话("ChatGPT", "conversation-1", 会话库=root, 确认=True)

            self.assertIsNone(读取概要("2026-10-26", 会话库=root))
            self.assertEqual(列出概要(会话库=root), ())


if __name__ == "__main__":
    unittest.main()
