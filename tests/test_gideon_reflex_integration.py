#!/usr/bin/env python3
"""
Integration Unit Tests for GIDEON Reflex Router Integration & Audio Decoupling.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/home/cat/gideon")

import gideon as gideon_module
from reflex_router import parse_command, try_reflex


class TestGideonReflexIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = MagicMock()
        self.tts = gideon_module.TTSPlayer(self.bus)

    def test_tts_player_has_play_sound_and_speak(self):
        self.assertTrue(hasattr(self.tts, "play_sound"))
        self.assertTrue(hasattr(self.tts, "speak"))
        with patch("audio.sounds.play_sound") as mock_ps:
            self.tts.play_sound("acknowledged.wav")
            mock_ps.assert_called_once_with("acknowledged.wav")

    async def test_try_reflex_audio_action_integration(self):
        mock_set_engine = AsyncMock(return_value=True)
        with patch("reflex_router.run_fixed", new_callable=AsyncMock) as mock_rf:
            mock_rf.return_value = (0, "ok")
            handled = await try_reflex("mute audio", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
            self.assertTrue(handled)
            mock_rf.assert_called_once_with(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
            self.bus.set_state.assert_called_with("idle")

    async def test_try_reflex_engine_switch_integration(self):
        mock_set_engine = AsyncMock(return_value=True)
        with patch.object(self.tts, "speak") as mock_speak:
            handled = await try_reflex("switch to opencode", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
            self.assertTrue(handled)
            mock_set_engine.assert_called_once_with("opencode")
            mock_speak.assert_called_once_with("Switched to OpenCode.")

    async def test_non_reflex_query_returns_false(self):
        mock_set_engine = AsyncMock(return_value=True)
        handled = await try_reflex("explain how quantum computing works", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
