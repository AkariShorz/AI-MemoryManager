import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_memory_manager.memory import dsh_mcp_server as bridge
from ai_memory_manager.memory.lifecycle import list_memories
from ai_memory_manager.personas.global_persona import set_global_persona


class DshMemoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.personas = self.root / "personas"
        self.personas.mkdir()
        (self.personas / "demo.json").write_text(
            json.dumps({"id": "demo", "name": "示例角色", "system_prompt": "示例人设：简洁友好。"}), encoding="utf-8")
        self.home = self.root / "dsh"
        self.home.mkdir()
        patchers = [
            patch.object(bridge, "PERSONAS", self.personas),
            patch.object(bridge, "PERSONA_USAGE", self.root / "usage.json"),
            patch.dict(os.environ, {"DSH_HOME": str(self.home)}),
            patch("ai_memory_manager.memory.semantic.scores", return_value={}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.enable_persona("demo")

    def enable_persona(self, persona_id: str) -> None:
        set_global_persona(persona_id, "示例角色", "示例人设：简洁友好。", provider="deepseek")

    def call(self, method: str, params: dict | None = None, request_id: int = 1) -> dict:
        response = bridge._handle({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        assert response is not None
        return response

    def test_tools_list_exposes_both_memory_tools(self):
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["memory_search", "memory_update"])
        update = next(tool for tool in tools if tool["name"] == "memory_update")
        self.assertIn("DeepSeek", update["description"])
        self.assertEqual(update["inputSchema"]["required"], ["memory_type", "subject", "content"])

    def test_update_then_search_uses_global_persona_database(self):
        result = self.call("tools/call", {"name": "memory_update", "arguments": {
            "memory_type": "stable_preference", "subject": "饮食偏好", "content": "喜欢吃白米饭"}})["result"]
        self.assertFalse(result["isError"])
        self.assertIn("已自动写入", result["content"][0]["text"])

        db = self.personas / ".memory" / "demo.sqlite3"
        saved = list_memories(db)[0]
        self.assertEqual(saved["source"], "conversation:DeepSeek")
        self.assertEqual(saved["conversation_id"], bridge.CONVERSATION_ID)

        found = self.call("tools/call", {"name": "memory_search", "arguments": {"query": "白米饭"}})["result"]
        self.assertFalse(found["isError"])
        self.assertIn("喜欢吃白米饭", found["content"][0]["text"])

    def test_duplicate_and_invalid_calls_are_reported(self):
        arguments = {"memory_type": "project", "subject": "项目", "content": "在写记忆管理器"}
        for _ in range(2):
            text = self.call("tools/call", {"name": "memory_update", "arguments": arguments})["result"]
        self.assertIn("没有重复写入", text["content"][0]["text"])

        rejected = self.call("tools/call", {"name": "memory_update", "arguments": {
            "memory_type": "project", "subject": "项目", "content": "x", "action": "delete"}})["result"]
        self.assertTrue(rejected["isError"])
        self.assertIn("记忆未写入", rejected["content"][0]["text"])

    def test_missing_global_persona_is_reported_instead_of_guessing(self):
        (self.home / "AGENTS.md").unlink()
        result = self.call("tools/call", {"name": "memory_search", "arguments": {"query": "偏好"}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("全局人设未启用", result["content"][0]["text"])

    def test_env_override_pins_a_persona(self):
        (self.personas / "other.json").write_text(json.dumps({"id": "other"}), encoding="utf-8")
        with patch.dict(os.environ, {bridge.PERSONA_ENV: "other"}):
            text = self.call("tools/call", {"name": "memory_search", "arguments": {"query": "偏好"}})["result"]
            self.assertFalse(text["isError"])
            self.assertEqual(bridge._memory_db(), self.personas / ".memory" / "other.sqlite3")

    def test_stdin_protocol_loop_answers_initialize(self):
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}}) + "\n")
        stdout = io.StringIO()
        with patch("sys.stdin", stdin), redirect_stdout(stdout):
            self.assertEqual(bridge.main(), 0)
        response = json.loads(stdout.getvalue())
        self.assertEqual(response["result"]["serverInfo"]["name"], "ai-memory-manager-dsh")


if __name__ == "__main__":
    unittest.main()
