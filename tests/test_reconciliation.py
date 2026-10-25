import json
import tempfile
import unittest
from pathlib import Path

from ai_memory_manager.reconciliation.reconciliation import 对账会话库


class ReconciliationTests(unittest.TestCase):
    def test_rebuilds_latest_message_view_from_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = Path(temporary_directory) / "Codex" / "2026-10-26" / "session-1"
            session.mkdir(parents=True)
            snapshots = session / "snapshots.jsonl"
            messages = session / "messages.jsonl"
            snapshots.write_text(
                "\n".join(
                    json.dumps(event, ensure_ascii=False)
                    for event in (
                        {
                            "message_id": "message-1",
                            "content": "初始内容",
                            "observed_at": "2026-10-26T10:00:00+08:00",
                        },
                        {
                            "message_id": "message-1",
                            "content": "更新内容",
                            "observed_at": "2026-10-26T10:01:00+08:00",
                        },
                        {
                            "message_id": "message-2",
                            "content": "另一条消息",
                            "observed_at": "2026-10-26T10:02:00+08:00",
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            result = 对账会话库(temporary_directory)
            repaired = [json.loads(line) for line in messages.read_text(encoding="utf-8").splitlines()]

            self.assertEqual((result.扫描会话数, result.修复文件数), (1, 1))
            self.assertEqual(len(repaired), 2)
            self.assertEqual(repaired[0]["content"], "更新内容")
            self.assertEqual(repaired[0]["first_observed_at"], "2026-10-26T10:00:00+08:00")
            self.assertEqual(repaired[1]["message_id"], "message-2")

    def test_valid_view_is_left_unchanged_and_unreadable_snapshot_is_not_applied(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = Path(temporary_directory) / "ChatGPT" / "2026-10-26" / "session-1"
            session.mkdir(parents=True)
            messages = session / "messages.jsonl"
            snapshots = session / "snapshots.jsonl"
            event = {"message_id": "message-1", "content": "保留内容"}
            valid_line = json.dumps(event, ensure_ascii=False) + "\n"
            messages.write_text(valid_line, encoding="utf-8")
            snapshots.write_text(valid_line, encoding="utf-8")

            first = 对账会话库(temporary_directory)
            self.assertEqual(first.修复文件数, 0)

            snapshots.write_text("not-json\n", encoding="utf-8")
            second = 对账会话库(temporary_directory)
            self.assertEqual(second.修复文件数, 0)
            self.assertEqual(second.忽略无效快照数, 1)
            self.assertEqual(messages.read_text(encoding="utf-8"), valid_line)


if __name__ == "__main__":
    unittest.main()
