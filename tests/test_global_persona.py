import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_memory_manager.personas.global_persona import (
    clear_global_persona,
    current_persona_id,
    instructions_path,
    set_global_persona,
)


class GlobalPersonaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "AGENTS.md"

    def tearDown(self):
        self.temp.cleanup()

    def test_set_replaces_persona_and_preserves_other_instructions(self):
        self.path.write_text("User instructions\n\nKeep this paragraph.\n", encoding="utf-8")
        set_global_persona("demo", "示例角色", "Speak gently.", self.path)
        set_global_persona("other", "Other", "Use concise answers.", self.path)

        result = self.path.read_text(encoding="utf-8")
        self.assertEqual(current_persona_id(self.path), "other")
        self.assertIn("Keep this paragraph.", result)
        self.assertIn("Use concise answers.", result)
        self.assertNotIn("Speak gently.", result)
        self.assertEqual(result.count("BEGIN AI Memory Manager global persona"), 1)

    def test_clear_removes_only_managed_block(self):
        self.path.write_text("Before\n", encoding="utf-8")
        set_global_persona("demo", "示例角色", "Speak gently.", self.path)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\nAfter\n")

        self.assertTrue(clear_global_persona(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "Before\n\nAfter\n")
        self.assertFalse(clear_global_persona(self.path))
        self.assertIsNone(current_persona_id(self.path))

    def test_malformed_block_is_rejected_without_overwriting(self):
        original = "User instructions\n" + "<!-- BEGIN AI Memory Manager global persona -->\n"
        self.path.write_text(original, encoding="utf-8")

        with self.assertRaises(ValueError):
            set_global_persona("demo", "示例角色", "Speak gently.", self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_deepseek_persona_uses_dsh_agents_file(self):
        dsh_home = Path(self.temp.name) / "dsh"
        with patch.dict(os.environ, {"DSH_HOME": str(dsh_home)}):
            self.assertEqual(instructions_path("deepseek"), dsh_home / "AGENTS.md")
            set_global_persona("demo", "示例角色", "Speak gently.", provider="deepseek")
            text = (dsh_home / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("新建的 DeepSeek 对话", text)
            self.assertEqual(current_persona_id(provider="deepseek"), "demo")
            self.assertIsNone(current_persona_id(self.path, provider="codex"))
            self.assertTrue(clear_global_persona(provider="deepseek"))
            self.assertIsNone(current_persona_id(provider="deepseek"))

    def test_deepseek_global_persona_routes_archived_sessions(self):
        from ai_memory_manager.memory.routing import resolve_persona

        dsh_home = Path(self.temp.name) / "dsh"
        personas = Path(self.temp.name) / "personas"
        personas.mkdir()
        (personas / "demo.json").write_text(json.dumps({"id": "demo"}), encoding="utf-8")
        usage = Path(self.temp.name) / "usage.json"
        with patch.dict(os.environ, {"DSH_HOME": str(dsh_home)}):
            set_global_persona("demo", "示例角色", "Speak gently.", provider="deepseek")
            found = resolve_persona("DeepSeek", "conversation-1", personas=personas, usage=usage,
                                    deepseek_global_path=dsh_home / "AGENTS.md")
            self.assertEqual(found, personas / "demo.json")
            self.assertIsNone(resolve_persona("Codex", "conversation-1", personas=personas, usage=usage,
                                              global_path=self.path))


if __name__ == "__main__":
    unittest.main()
