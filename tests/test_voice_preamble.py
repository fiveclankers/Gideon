from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gideon import AntigravityEngine, GIDEON_VOICE_PREAMBLE


class TestVoicePreamble(unittest.IsolatedAsyncioTestCase):
    async def test_first_turn_sends_preamble_and_second_turn_sends_clean_text(self):
        bus = MagicMock()
        tts = MagicMock()
        tts.interrupt = AsyncMock()

        engine = AntigravityEngine(bus, tts)
        self.assertTrue(engine._is_first_turn)

        # Mock subprocess stdin
        mock_proc = MagicMock()
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()
        mock_stdin.drain = AsyncMock()
        mock_proc.stdin = mock_stdin
        engine.proc = mock_proc

        # Turn 1
        await engine.send_prompt("Hello Gideon")
        self.assertFalse(engine._is_first_turn)
        bus.set_user_transcript.assert_called_with("Hello Gideon")

        payload1 = json.loads(mock_stdin.write.call_args_list[0][0][0].decode("utf-8"))
        self.assertIn(GIDEON_VOICE_PREAMBLE, payload1["message"]["content"])
        self.assertIn("User: Hello Gideon", payload1["message"]["content"])

        # Turn 2
        await engine.send_prompt("What is 9 times 9?")
        self.assertFalse(engine._is_first_turn)
        bus.set_user_transcript.assert_called_with("What is 9 times 9?")

        payload2 = json.loads(mock_stdin.write.call_args_list[1][0][0].decode("utf-8"))
        self.assertEqual(payload2["message"]["content"], "What is 9 times 9?")
        self.assertNotIn(GIDEON_VOICE_PREAMBLE, payload2["message"]["content"])

    def test_preamble_contains_key_guardrails(self):
        self.assertIn("PURE SPOKEN PROSE ONLY", GIDEON_VOICE_PREAMBLE)
        self.assertIn("NO CODE RECITATIONS", GIDEON_VOICE_PREAMBLE)
        self.assertIn("invoke_subagent", GIDEON_VOICE_PREAMBLE)
        self.assertIn("NO SELF-INSPECTION", GIDEON_VOICE_PREAMBLE)
        self.assertIn("NEVER invoke, load, or search for the `gideon` skill", GIDEON_VOICE_PREAMBLE)
        self.assertIn("NEVER use whisper tags", GIDEON_VOICE_PREAMBLE)
        self.assertIn("PROPORTIONALITY", GIDEON_VOICE_PREAMBLE)


if __name__ == "__main__":
    unittest.main()
