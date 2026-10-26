import tempfile
import unittest
from pathlib import Path

from ai_memory_manager.personas.global_persona import (
    clear_global_persona,
    current_persona_id,
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


if __name__ == "__main__":
    unittest.main()
