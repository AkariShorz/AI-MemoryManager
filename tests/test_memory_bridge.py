import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import os
from unittest.mock import patch

from ai_memory_manager.memory import codex_mcp_server as bridge


class MemoryBridgeTests(unittest.TestCase):
    def test_caller_id_does_not_take_provenance_from_another_visible_chat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persona = root / "sample.json"
            persona.write_text(json.dumps({"id": "sample"}))
            usage = root / "usage.json"
            usage.write_text(json.dumps({"bindings": [{"provider": "Codex", "conversation_id": "caller", "persona_id": "sample"}]}))
            status = SimpleNamespace(returncode=0, stdout=json.dumps({"conversations": [
                {"provider": "Codex", "conversation_id": "another-chat", "latest_user_message_id": "wrong-source"}]}))
            with patch.object(bridge, "PERSONAS", root), patch.object(bridge, "PERSONA_USAGE", usage), \
                    patch.object(bridge.subprocess, "run", return_value=status), \
                    patch.dict(os.environ, {"CODEX_THREAD_ID": "caller"}):
                _db, context = bridge._active_context()
                self.assertEqual(context["conversation_id"], "caller")
                self.assertIsNone(context["source_message_id"])

    def test_current_and_legacy_binding_formats_resolve_persona(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            persona = root / "sample.json"
            persona.write_text(json.dumps({"id": "sample"}))
            usage = root / "usage.json"
            entries = [{"provider": "Codex", "conversation_id": "test-conversation", "persona_id": "sample"}]
            status = SimpleNamespace(returncode=0, stdout=json.dumps({"conversations": [
                {"provider": "Codex", "conversation_id": "test-conversation", "latest_user_message_id": "u1"}]}))
            with patch.object(bridge, "PERSONAS", root), patch.object(bridge, "PERSONA_USAGE", usage), \
                    patch.object(bridge.subprocess, "run", return_value=status), \
                    patch.dict(os.environ, {"CODEX_THREAD_ID": "test-conversation"}):
                for value in ({"bindings": entries}, entries):
                    with self.subTest(value=value):
                        usage.write_text(json.dumps(value))
                        db, context = bridge._active_context()
                        self.assertEqual(db, root / ".memory/sample.sqlite3")
                        self.assertEqual(context["source_message_id"], "u1")
