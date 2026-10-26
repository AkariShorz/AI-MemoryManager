import tempfile
import unittest
from pathlib import Path

from ai_memory_manager.memory import dsh_mcp_config as config

ORIGINAL = """# dsh profile root — an empty entry list.
[]
"""

SAVED = """# Your patch layer for this dsh profile.
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    provider: deepseek-account
- id: ui-theme
  name: "@deepseek-ai/dsh-client-ui-theme"
  config:
    preference: dark
"""


class DshMcpConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cordis.patch.yml"

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_install_appends_once_and_remove_restores_original_bytes(self):
        self.write(SAVED)
        self.assertEqual(config.status(self.path), "absent")
        self.assertEqual(config.install(self.path), "installed")
        self.assertEqual(config.status(self.path), "installed")

        installed = self.path.read_text(encoding="utf-8")
        self.assertTrue(installed.startswith(SAVED))
        self.assertIn("- id: mcp-memory", installed)
        self.assertIn("transport: stdio", installed)

        before = installed
        self.assertEqual(config.install(self.path), "already-installed")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

        self.assertEqual(config.remove(self.path), "removed")
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAVED)
        self.assertEqual(config.remove(self.path), "absent")

    def test_remove_keeps_unrelated_entries_after_the_managed_block(self):
        self.write(SAVED)
        config.install(self.path)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n- id: ui-chat\n  name: \"@deepseek-ai/dsh-client-ui-chat\"\n  config:\n    transcriptView: standard\n")
        self.assertEqual(config.remove(self.path), "removed")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("mcp-memory", text)
        self.assertNotIn("dsh-mcp-client", text)
        self.assertIn("- id: ui-chat", text)
        self.assertIn("- id: ui-theme", text)

    def test_missing_profile_is_reported_without_creating_files(self):
        missing = Path(self.temp.name) / "nope.yml"
        self.assertEqual(config.status(missing), "profile-missing")
        self.assertEqual(config.install(missing), "profile-missing")
        self.assertEqual(config.remove(missing), "profile-missing")
        self.assertFalse(missing.exists())

    def test_backup_is_created_before_each_change(self):
        self.write(SAVED)
        config.install(self.path)
        backups = sorted(Path(self.temp.name).glob("cordis.patch.yml.bak-*-dsh-memory"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), SAVED)
        config.remove(self.path)
        self.assertEqual(len(sorted(Path(self.temp.name).glob("cordis.patch.yml.bak-*-dsh-memory"))), 2)

    def test_refuses_to_edit_an_unexpected_layout(self):
        self.write("- id: mcp-memory\n  name: '@deepseek-ai/dsh-mcp-client'\n")
        with self.assertRaises(ValueError):
            config.remove(self.path)
        self.assertIn("mcp-memory", self.path.read_text(encoding="utf-8"))

    def test_generated_block_is_valid_yaml_and_points_at_the_bridge(self):
        self.write(SAVED)
        config.install(self.path, python="/usr/bin/python3")
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            self.skipTest("PyYAML not installed")
        document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        entry = next(item for item in document if "insert" in item)["insert"][0]
        self.assertEqual(entry["id"], "mcp-memory")
        self.assertEqual(entry["config"]["command"], "/usr/bin/python3")
        self.assertEqual(entry["config"]["args"], [str(config.BRIDGE)])
        self.assertEqual(entry["config"]["env"]["PYTHONPATH"], str(config.SOURCE))


if __name__ == "__main__":
    unittest.main()
