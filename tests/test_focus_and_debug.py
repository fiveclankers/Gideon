"""Tests for Focus Mode, scrubbed Debug Bundle, Fish WS streaming, and bugfix regressions."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import msgpack  # noqa: E402

import focus_mode  # noqa: E402
import gideon  # noqa: E402
import gideon_debug  # noqa: E402
import gideon_shared  # noqa: E402
import reflex_router  # noqa: E402


class TestFocusMode(unittest.IsolatedAsyncioTestCase):
    async def test_grace_window_cooldown_and_allowlist(self):
        fake_now = [1000.0]
        window_state = {"class": "firefox", "title": "Cat video - YouTube"}
        tts = MagicMock()
        tts.speak = MagicMock()
        tts.play_sound = MagicMock()
        bus = MagicMock()
        bus.current_state = "idle"

        rules = focus_mode.FocusRules(
            poll_interval_s=20.0,
            grace_hits=2,
            cooldown_s=180.0,
            allowed_title_keywords=("mit ocw",),
        )
        mgr = focus_mode.FocusSessionManager(
            bus=bus,
            tts=tts,
            rules=rules,
            window_provider=lambda: window_state,
            clock=lambda: fake_now[0],
        )
        mgr.start(duration_minutes=30, goal="physics", reload_rules=False)
        mgr._cancel_task()  # Drive deterministically via check_once

        # Hit 1: still inside 2-hit grace period
        r1 = await mgr.check_once()
        self.assertEqual(r1["status"], "grace")
        tts.speak.assert_not_called()

        # Brief switch back to work resets consecutive streak
        window_state = {"class": "obsidian", "title": "Electrostatics Notes"}
        r_focus = await mgr.check_once()
        self.assertEqual(r_focus["status"], "focused")
        self.assertEqual(mgr.consecutive_hits, 0)

        # Back to YouTube: hit 1 grace, hit 2 triggers nudge
        window_state = {"class": "firefox", "title": "Cat video - YouTube"}
        self.assertEqual((await mgr.check_once())["status"], "grace")
        r2 = await mgr.check_once()
        self.assertEqual(r2["status"], "nudged")
        self.assertEqual(r2["label"], "YouTube")
        tts.play_sound.assert_called_once_with("activate.wav")
        tts.speak.assert_called_once()
        self.assertIn("physics", tts.speak.call_args.args[0])

        # Within 180s cooldown: no second nudge
        fake_now[0] += 60.0
        r3 = await mgr.check_once()
        self.assertEqual(r3["status"], "cooldown")
        self.assertEqual(tts.speak.call_count, 1)

        # Exempt educational YouTube title passes cleanly
        window_state = {"class": "firefox", "title": "MIT OCW 8.02 Lecture - YouTube"}
        self.assertEqual((await mgr.check_once())["status"], "focused")

        # After cooldown + 2 hits again -> second nudge
        fake_now[0] += 130.0
        window_state = {"class": "discord", "title": "Discord"}
        self.assertEqual((await mgr.check_once())["status"], "grace")
        r4 = await mgr.check_once()
        self.assertEqual(r4["status"], "nudged")
        self.assertEqual(r4["label"], "Discord")

        stopped = mgr.stop()
        self.assertFalse(stopped["active"])
        self.assertEqual(stopped["nudges_sent"], 2)

    async def test_tier0_focus_voice_commands(self):
        parsed_start = reflex_router.parse_command("start focus mode for 45 minutes on calculus")
        self.assertEqual(parsed_start["intent"], "focus_start")
        self.assertEqual(parsed_start["minutes"], 45.0)
        self.assertEqual(parsed_start["goal"], "calculus")

        parsed_hour = reflex_router.parse_command("start focus mode for an hour")
        self.assertEqual(parsed_hour["intent"], "focus_start")
        self.assertEqual(parsed_hour["minutes"], 60.0)

        parsed_stop = reflex_router.parse_command("stop focus mode")
        self.assertEqual(parsed_stop["intent"], "focus_stop")

        parsed_status = reflex_router.parse_command("focus status")
        self.assertEqual(parsed_status["intent"], "focus_status")


class TestScrubbedDebugBundle(unittest.TestCase):
    def test_scrub_sensitive_text_and_bundle_creation(self):
        raw = (
            "FISH_API_KEY=secret_fish_value_12345\n"
            "Authorization: Bearer super_secret_token_xyz\n"
            "email=private.user@example.com\n"
            "normal stack line: Time=12.4ms\n"
        )
        scrubbed = gideon_debug.scrub_sensitive_text(raw)
        self.assertNotIn("secret_fish_value_12345", scrubbed)
        self.assertNotIn("super_secret_token_xyz", scrubbed)
        self.assertNotIn("private.user@example.com", scrubbed)
        self.assertIn("FISH_API_KEY=[REDACTED]", scrubbed)
        self.assertIn("[REDACTED_TOKEN]", scrubbed)
        self.assertIn("[REDACTED_EMAIL]", scrubbed)
        self.assertIn("normal stack line: Time=12.4ms", scrubbed)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            env_file = tmp_dir / "gideon.env"
            env_file.write_text('FISH_API_KEY="my_super_secret_key_val"\n')
            agy_dir = tmp_dir / "agy_logs"
            agy_dir.mkdir()
            (agy_dir / "cli-20261115.log").write_text("Bearer secret_cli_tok\nline_ok\n")
            out_file = tmp_dir / "bundle.log"

            def _fake_run(argv, *, timeout=5.0):
                return f"ok:{argv[0]}"

            with patch.object(gideon_debug, "_run_text", side_effect=_fake_run):
                dest, summary = gideon_debug.collect_debug_bundle(
                    out_file,
                    env_file=env_file,
                    agy_log_dir=agy_dir,
                )

            self.assertEqual(dest, out_file)
            self.assertIn("Saved scrubbed debug bundle", summary)
            content = dest.read_text(encoding="utf-8")
            self.assertNotIn("my_super_secret_key_val", content)
            self.assertNotIn("secret_cli_tok", content)
            self.assertIn("FISH_API_KEY (<set, 23 chars>)", content)
            self.assertIn("cli-20261115.log", content)
            self.assertIn("line_ok", content)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)

        self.assertEqual(reflex_router.parse_command("create debug bundle")["intent"], "debug_bundle")


class TestSlice3BugfixesAndFishWS(unittest.IsolatedAsyncioTestCase):
    async def test_parse_number_hundred_and_slot_extractors(self):
        self.assertEqual(reflex_router._parse_number("one hundred"), 100.0)
        self.assertEqual(reflex_router._parse_number("hundred percent"), 100.0)

        vol = gideon_shared.extract_params("audio_volume_set", "set volume to 50%.")
        self.assertEqual(vol.get("level"), 50.0)

        wall = gideon_shared.extract_params("omarchy_wallpaper_set", "set wallpaper to ~/Pictures/bg.png.")
        self.assertEqual(wall.get("path"), "~/Pictures/bg.png")

        sprint = gideon_shared.extract_params("study_sprint_start", "start study sprint")
        self.assertIn("chapter", sprint)

    async def test_confirmation_preserves_hotmic_state(self):
        reflex_router.get_pending_confirmation_store().clear()
        bus = gideon.SignalBus()
        wake = MagicMock()
        wake._hot_mic_armed = False

        async def _arm(timeout_s=15.0):
            wake._hot_mic_armed = True
            bus.set_state("hotmic")

        bus.arm_hot_mic = _arm
        bus.wake_listener = wake

        fake_res = gideon_shared.HandlerResult(
            ok=True,
            message="Confirm reboot",
            needs_confirmation=True,
            data={"intent": "system_reboot", "risk_tier": "dangerous", "params": {}},
        )
        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value=fake_res)
        tts = MagicMock()
        tts.speak = MagicMock()
        tts.play_sound = MagicMock()

        with patch.object(reflex_router, "get_gideon_dispatcher", return_value=dispatcher):
            handled = await reflex_router.try_reflex("reboot computer", bus=bus, tts=tts)

        self.assertTrue(handled)
        self.assertEqual(bus.current_state, "hotmic")
        await asyncio.sleep(0.05)
        self.assertEqual(bus.current_state, "hotmic")

        # Playback finishing during armed confirmation restores hotmic rather than idle
        player = gideon.TTSPlayer(bus)
        bus.current_state = "speaking"
        player._finish_playback_state()
        self.assertEqual(bus.current_state, "hotmic")
        reflex_router.get_pending_confirmation_store().clear()

    async def test_classify_turn_directive_uses_cached_prediction(self):
        fake_disp = MagicMock()
        fake_disp.peek_cached_intent = MagicMock(return_value="hard_goal")
        fake_disp.predict = MagicMock()
        self.assertEqual(
            gideon.classify_turn_directive("refactor the audio loop", dispatcher=fake_disp),
            gideon.CODING_DIRECTIVE,
        )
        fake_disp.predict.assert_not_called()

    async def test_selected_context_keeps_clean_display_transcript(self):
        bus = gideon.SignalBus()
        tts = MagicMock()
        tts.interrupt = AsyncMock()
        engine = gideon.AntigravityEngine(bus, tts)
        engine._sync_auth_account = AsyncMock()
        engine._ensure_engine_alive = AsyncMock(return_value=True)
        engine._write_prompt = AsyncMock(return_value=True)

        await engine.send_prompt("HUGE SELECTED CONTEXT PROMPT", display_text="summarize this")
        self.assertEqual(bus.current_user, "summarize this")

    async def test_fish_live_ws_streaming_and_http_fallback(self):
        bus = gideon.SignalBus()
        player = gideon.TTSPlayer(bus)

        # Build valid 16-bit PCM sine frames (~80ms = 3528*2 bytes), split on an ODD byte boundary!
        samples = [int(12000 * ((i % 40) / 40.0 - 0.5)) for i in range(3600)]
        raw_pcm = struct.pack(f"<{len(samples)}h", *samples)
        split_pt = 1001  # Intentionally odd byte offset
        chunk1 = msgpack.packb({"event": "audio", "audio": raw_pcm[:split_pt]}, use_bin_type=True)
        chunk2 = msgpack.packb({"event": "audio", "audio": raw_pcm[split_pt:]}, use_bin_type=True)
        fin = msgpack.packb({"event": "finish", "reason": "stop"}, use_bin_type=True)

        sent_events = []

        class _FakeWS:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, data):
                sent_events.append(msgpack.unpackb(data, raw=False))

            def __aiter__(self):
                self._items = iter([chunk1, chunk2, fin])
                return self

            async def __anext__(self):
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration

        fake_proc = MagicMock(returncode=0)
        fake_proc.poll.side_effect = [None, 0]
        fake_proc.stdin = MagicMock()
        fake_proc.stderr = MagicMock()
        fake_proc.stderr.read.return_value = b""
        fake_proc.wait.return_value = 0

        stream = gideon._FishStream(text="Hello Gideon.")
        with patch.object(gideon, "FISH_API_KEY", "test_key"), \
             patch("websockets.connect", return_value=_FakeWS()), \
             patch("gideon.subprocess.Popen", return_value=fake_proc) as popen_mock:
            await asyncio.gather(player._run_ws_stream(stream), player.play_pcm_stream(stream))

        self.assertEqual(sent_events[0]["event"], "start")
        self.assertEqual(sent_events[0]["request"]["format"], "pcm")
        self.assertFalse(sent_events[0]["request"]["prosody"]["normalize_loudness"])
        self.assertEqual(sent_events[1]["event"], "text")
        self.assertEqual(sent_events[2]["event"], "flush")
        self.assertEqual(sent_events[3]["event"], "stop")
        cmd = popen_mock.call_args.args[0]
        self.assertIn("--raw", cmd)
        self.assertIn("44100", cmd)
        written = b"".join(c.args[0] for c in fake_proc.stdin.write.call_args_list)
        self.assertEqual(written, raw_pcm)

        # Fallback path when WS raises before yielding audio
        stream_fail = gideon._FishStream(text="Fallback test.")
        stream_fail.task = asyncio.current_task()
        with patch.object(gideon, "FISH_API_KEY", "test_key"), \
             patch("websockets.connect", side_effect=RuntimeError("ws down")), \
             patch.object(player, "synthesize", new=AsyncMock(return_value=b"RIFF_FALLBACK")), \
             patch.object(player, "play_wav", new=AsyncMock()) as play_wav_mock:
            await player._run_ws_stream(stream_fail)
            await player.play_pcm_stream(stream_fail)
        play_wav_mock.assert_awaited_once_with(b"RIFF_FALLBACK", text="Fallback test.")


if __name__ == "__main__":
    unittest.main()
