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

    async def test_try_reflex_engine_switch_supports_codex_agy_and_rejects_opencode(self):
        mock_set_engine = AsyncMock(return_value=True)
        with patch.object(self.tts, "speak") as mock_speak:
            handled = await try_reflex("switch to opencode", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
            self.assertTrue(handled)
            mock_set_engine.assert_not_awaited()
            mock_speak.assert_called_once_with("Only Codex and Antigravity engines are supported.")

        mock_set_engine.reset_mock()
        with patch.object(self.tts, "speak") as mock_speak:
            handled = await try_reflex("switch to antigravity", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
            self.assertTrue(handled)
            mock_set_engine.assert_awaited_once_with("agy")
            mock_speak.assert_called_once_with("Switched engine to Antigravity.")

        mock_set_engine.reset_mock()
        with patch.object(self.tts, "speak") as mock_speak:
            handled = await try_reflex("switch to codex", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
            self.assertTrue(handled)
            mock_set_engine.assert_awaited_once_with("codex")
            mock_speak.assert_called_once_with("Switched engine to Codex.")

    async def test_non_reflex_query_returns_false(self):
        mock_set_engine = AsyncMock(return_value=True)
        handled = await try_reflex("explain how quantum computing works", bus=self.bus, tts=self.tts, set_engine=mock_set_engine)
        self.assertFalse(handled)

    async def test_bye_bye_utterance_never_invokes_llm_or_bge(self):
        engine = MagicMock()
        engine.send_prompt = AsyncMock()
        engine.interrupt = AsyncMock()
        self.bus.dismiss_hot_mic = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch = AsyncMock()

        utterances = (
            "you thank you bye bye",
            "blah blah okay bye-bye",
            "Bye, bye.",
            "byebye",
            "bye bye for now",
            "with selected text: explain this bug and bye bye",
            "look at my clipboard okay bye bye",
            "fix the script why does it fail bye bye",
        )
        with (
            patch("reflex_router.get_gideon_dispatcher", return_value=mock_dispatcher),
            patch("gideon.prepare_prompt") as mock_prep,
            patch.object(self.tts, "interrupt", new_callable=AsyncMock) as mock_tts_interrupt,
        ):
            for text in utterances:
                with self.subTest(text=text):
                    self.bus.dismiss_hot_mic.reset_mock()
                    self.bus.set_state.reset_mock()
                    mock_tts_interrupt.reset_mock()

                    prepared = await gideon_module._prepare_user_prompt(text, bus=self.bus, tts=self.tts)
                    self.assertIsNone(prepared)
                    mock_prep.assert_not_called()

                    from reflex_router import dispatch
                    delegated = await dispatch(text, engine, bus=self.bus, tts=self.tts)
                    self.assertTrue(delegated)

                    engine.send_prompt.assert_not_awaited()
                    engine.interrupt.assert_not_awaited()
                    mock_dispatcher.dispatch.assert_not_awaited()
                    self.bus.dismiss_hot_mic.assert_called()
                    mock_tts_interrupt.assert_awaited()
                    self.bus.set_state.assert_called_with("idle")


if __name__ == "__main__":
    unittest.main()
