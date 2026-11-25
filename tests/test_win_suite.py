import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import music_win
import reflex_win
import stt_win
import tts_win
import win_tools


class TestReflexWin(unittest.TestCase):
    def test_exact_tier0_hits(self):
        self.assertEqual(reflex_win.parse_tier0("bye bye")["intent"], "dismiss")
        self.assertEqual(reflex_win.parse_tier0("Bye-bye!")["intent"], "dismiss")
        self.assertEqual(reflex_win.parse_tier0("Hey Gideon, play something")["intent"], "music_play_local_shuffle")
        self.assertEqual(reflex_win.parse_tier0("play something")["intent"], "music_play_local_shuffle")
        self.assertEqual(reflex_win.parse_tier0("pause")["action"], "pause")
        self.assertEqual(reflex_win.parse_tier0("skip")["action"], "next")
        self.assertEqual(reflex_win.parse_tier0("mute")["action"], "mute")

    def test_varied_natural_phrases_bypass_tier0(self):
        bypass_cases = [
            "ask antigravity to check my disk space",
            "hey gideon can you ask antigravity about this bug",
            "is antigravity done yet?",
            "how much longer on that task?",
            "what is antigravity up to?",
            "play something moody and late night",
            "play arctic monkeys",
            "queue up three tracks for a chill evening",
            "open steam",
            "launch obsidian",
            "look at my screen and help me fix this",
            "turn the volume down a little bit",
        ]
        for phrase in bypass_cases:
            with self.subTest(phrase=phrase):
                self.assertIsNone(reflex_win.parse_tier0(phrase))


class TestMusicWin(unittest.TestCase):
    def test_linux_path_mapping_resolves_to_windows_flac(self):
        mapped = music_win.resolve_track_path("/home/cat/Music/Lossless/Arctic Monkeys/Favourite Worst Nightmare/12 505.flac")
        self.assertTrue(mapped.endswith(r"Arctic Monkeys\Favourite Worst Nightmare\12 505.flac"))
        self.assertTrue(Path(mapped).is_file())

    def test_library_loads_and_filters(self):
        _, tracks = music_win.load_library()
        self.assertGreaterEqual(len(tracks), 300)
        mood_hits = [t for t in tracks if music_win.track_matches(t, ["chill"], [], [], [], [])]
        self.assertGreater(len(mood_hits), 10)
        genre_hits = [t for t in tracks if music_win.track_matches(t, [], ["alternative"], [], [], [])]
        self.assertGreater(len(genre_hits), 5)

    def test_tastes_command(self):
        rc = music_win.main(["tastes", "--json"])
        self.assertEqual(rc, 0)

    def test_load_library_merges_unindexed_disk_flacs(self):
        _, tracks = music_win.load_library()
        self.assertTrue(any("09 in the pool" in (t.get("path") or "").lower() for t in tracks))

    def test_find_tracks_fuzzy_ranks_expected_hits(self):
        hits_kensuke = music_win.find_tracks("kensuke ushiho in the pool")
        self.assertTrue(hits_kensuke)
        self.assertIn("09 in the pool", (hits_kensuke[0].get("path") or "").lower())

        hits_elvis = music_win.find_tracks("elvis presley")
        self.assertTrue(hits_elvis)
        self.assertIn("elvis", (hits_elvis[0].get("artist") or "").lower())

    def test_find_mpv_bin_prefers_mpv_exe(self):
        mpv_bin = music_win.find_mpv_bin()
        self.assertIsNotNone(mpv_bin)
        self.assertTrue(mpv_bin.lower().endswith("mpv.exe"))


class TestWinTools(unittest.TestCase):
    def test_agy_status(self):
        res = win_tools.get_agy_status()
        self.assertTrue(res.get("ok"))
        self.assertIn("state", res)
        self.assertIn("spoken_summary", res)
        self.assertIsInstance(res.get("recent_activity"), list)

    def test_launch_dry_run(self):
        res = win_tools.launch_app("open obsidian", dry_run=True)
        self.assertTrue(res.get("ok"))
        self.assertIn("obsidian", res.get("target", "").lower())

    def test_screenshot_creates_valid_png(self):
        res = win_tools.take_screenshot()
        if not res.get("ok") and "Screen capture unavailable" in str(res.get("error", "")):
            self.skipTest(f"Interactive desktop GDI capture unavailable in this session: {res.get('error')}")
        self.assertTrue(res.get("ok"), msg=res)
        p = Path(res["path"])
        self.assertTrue(p.is_file())
        self.assertGreater(p.stat().st_size, 5000)
        self.assertEqual(p.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


class TestAckClipsAndTTSClean(unittest.TestCase):
    def test_ack_player_rotates_without_immediate_repeat(self):
        player = tts_win.AckPlayer()
        expected = {
            "listen_start": 30,
            "ask_agy": 15,
            "check_agy": 10,
            "bye_bye": 10,
            "music_play": 15,
        }
        for cat, min_count in expected.items():
            cat_dir = tts_win.ACKS_DIR / cat
            wavs = list(cat_dir.glob("*.wav"))
            self.assertGreaterEqual(len(wavs), min_count, f"Missing clips in {cat_dir}")
            first = player.pick_clip(cat)
            second = player.pick_clip(cat)
            self.assertNotEqual(first, second)
        self.assertTrue((tts_win.SOUNDS_DIR / "empty_soft.wav").is_file())
        self.assertTrue((tts_win.SOUNDS_DIR / "heard_soft.wav").is_file())
        self.assertTrue((tts_win.SOUNDS_DIR / "listen_soft.wav").is_file())

    def test_audio_ducker_lifecycle_safe(self):
        from ducking_win import AudioDucker

        ducker = AudioDucker(duck_level=0.15)
        try:
            ducker.duck()
            ducker.restore()
        finally:
            ducker.close()

    def test_pcm16_gain_boosts_and_clamps_cleanly(self):
        import struct

        pcm = struct.pack("<3h", 1000, -1000, 20000)
        boosted = struct.unpack("<3h", tts_win.apply_pcm16_gain(pcm, 2.5))
        self.assertEqual(boosted, (2500, -2500, 32767))

    def test_mpv_default_volume_100(self):
        self.assertGreaterEqual(music_win.MPV_VOLUME, 100)

    def test_hallucination_filter(self):
        self.assertTrue(stt_win.is_hallucination("[BLANK_AUDIO]"))
        self.assertTrue(stt_win.is_hallucination("Thank you."))
        self.assertFalse(stt_win.is_hallucination("Is Antigravity done yet?"))


class TestWindowsAgentsHotkeyAndGroq(unittest.TestCase):
    def test_sync_win_agents_md_clean_no_linux_rules(self):
        import tempfile
        import engines_win

        with tempfile.TemporaryDirectory() as tmp:
            out = engines_win.sync_win_agents_md(Path(tmp))
            self.assertTrue(out.is_file())
            txt = out.read_text(encoding="utf-8")
            self.assertIn("music_win.py", txt)
            self.assertIn("win_tools.py", txt)
            self.assertNotIn("{{PY_EXE}}", txt)
            self.assertNotIn("graphify", txt.lower())
            self.assertNotIn("hyprctl", txt.lower())

    def test_codex_ignores_commentary_phase_and_speaks_final_answer(self):
        import asyncio
        import engines_win

        class _Bus:
            def set_state(self, *a, **kw): pass

        class _TTS:
            def __init__(self):
                self.spoken = []
            def queue_sentence(self, s):
                self.spoken.append(s)

        tts = _TTS()
        eng = engines_win.WinCodexEngine(_Bus(), tts)

        async def _drive():
            await eng._on_notification("item/started", {"item": {"id": "c1", "type": "agentMessage", "phase": "commentary"}})
            await eng._on_notification("item/agentMessage/delta", {"itemId": "c1", "delta": "I will try 'in the pool' locally."})
            await eng._on_notification("item/completed", {"item": {"id": "c1", "type": "agentMessage", "phase": "commentary", "text": "I will try 'in the pool' locally."}})
            await eng._on_notification("item/started", {"item": {"id": "f1", "type": "agentMessage", "phase": "final_answer"}})
            await eng._on_notification("item/agentMessage/delta", {"itemId": "f1", "delta": "Playing in the pool by Kensuke Ushio."})
            await eng._on_notification("item/completed", {"item": {"id": "f1", "type": "agentMessage", "phase": "final_answer", "text": "Playing in the pool by Kensuke Ushio."}})

        asyncio.run(_drive())
        self.assertEqual(tts.spoken, ["Playing in the pool by Kensuke Ushio."])
        self.assertEqual(eng._reply, "Playing in the pool by Kensuke Ushio.")

    def test_resolve_codex_bin_prefers_native_exe(self):
        import engines_win

        resolved = engines_win.resolve_codex_bin()
        self.assertTrue(resolved.lower().endswith("codex.exe"))

    def test_win32_alt_space_hook_installs_and_stops(self):
        import gideon_win

        listener = gideon_win.WinHotkeyListener("alt+space", lambda: None, lambda _held: None)
        listener.start()
        try:
            self.assertIsNotNone(listener._hook_handle)
            self.assertNotEqual(listener._hook_handle, 0)
        finally:
            listener.stop()

    def test_groq_stt_active_when_env_loaded(self):
        import inspect
        import gideon_win

        gideon_win.load_env_file()
        stt = stt_win.WinSTT()
        self.assertEqual(stt.active_backend(), "groq")
        src = inspect.getsource(stt_win.WinSTT._transcribe_groq)
        self.assertIn('"whisper-large-v3"', src)
        self.assertNotIn("whisper-large-v3-turbo", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
