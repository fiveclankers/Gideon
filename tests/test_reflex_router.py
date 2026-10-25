from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import reflex_router as router


class TestReflexParsing(unittest.TestCase):
    def test_mute_and_unmute(self):
        cases = {
            "mute": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
            "mute audio": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
            "mute volume": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
            "silence audio": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
            "unmute": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            "unmute audio": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            "unmute volume": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
        }
        for text, argv in cases.items():
            with self.subTest(text=text):
                self.assertEqual(router.parse_command(text)["argv"], argv)

    def test_volume_adjustments_and_bounds(self):
        cases = {
            "volume up": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"],
            "turn volume up": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"],
            "increase volume": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"],
            "increase volume by 20%": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "20%+"],
            "volume down": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%-"],
            "turn volume down": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%-"],
            "decrease volume by 5%": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
            "lower volume": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "10%-"],
            "set volume to 0%": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0%"],
            "set volume to 100%": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "100%"],
            "volume 42%": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "42%"],
            "increase volume by ten percent": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"],
            "decrease volume by five percent": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
            "set volume to fifty percent": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "50%"],
        }
        for text, argv in cases.items():
            with self.subTest(text=text):
                self.assertEqual(router.parse_command(text)["argv"], argv)
        for text in ("set volume to 101%", "set volume to -1%", "increase volume by 0%", "decrease volume by 101%"):
            with self.subTest(text=text):
                self.assertIsNone(router.parse_command(text))

    def test_all_media_phrases(self):
        expected = {
            "pause": "pause", "pause music": "pause", "pause media": "pause", "pause playback": "pause", "pause spotify": "pause",
            "resume": "play", "resume music": "play", "resume media": "play", "resume playback": "play", "resume spotify": "play",
            "stop music": "stop", "stop media": "stop", "stop playback": "stop", "stop spotify": "stop",
            "next track": "next", "skip track": "next", "next song": "next", "previous track": "previous", "previous song": "previous",
        }
        for phrase, action in expected.items():
            with self.subTest(phrase=phrase):
                parsed = router.parse_command(phrase)
                self.assertEqual(parsed["intent"], "media_control")
                self.assertEqual(parsed["action"], action)

    def test_generic_music_starts_shuffled_library(self):
        for phrase in (
            "play music", "play some music", "play something", "play anything",
            "play whatever", "play me something", "play a random song",
            "put on some music", "shuffle my music", "play my library",
            "Can you play some music?", "just play something",
        ):
            with self.subTest(phrase=phrase):
                parsed = router.parse_command(phrase)
                self.assertEqual(parsed["intent"], "music_play")
                self.assertEqual(parsed["kind"], "any")

    def test_specific_music_requests_are_not_generic(self):
        for phrase in ("play some jazz", "play radiohead", "play music by Radiohead", "resume", "play the video"):
            with self.subTest(phrase=phrase):
                self.assertFalse(router.is_generic_music_request(phrase))

    def test_local_music_play_phrases(self):
        cases = {
            "play chill music": ("mood", "chill"),
            "play some jazz": ("genre", "jazz"),
            "play hip hop": ("genre", "hip-hop"),
            "play late night music": ("mood", "late-night"),
            "play my Focus playlist": ("playlist", "Focus"),
            "play the playlist Chill": ("playlist", "Chill"),
            "play music by Arctic Monkeys": ("artist", "Arctic Monkeys"),
            "play something by Radiohead": ("artist", "Radiohead"),
            "play a song by Radiohead": ("artist", "Radiohead"),
            "play some radiohead": ("search", "radiohead"),
            "play radiohead": ("search", "radiohead"),
            "Can you play some Radiohead?": ("search", "Radiohead"),
            "I want to listen to some Radiohead.": ("search", "Radiohead"),
        }
        for phrase, (kind, value) in cases.items():
            with self.subTest(phrase=phrase):
                parsed = router.parse_command(phrase)
                self.assertEqual(parsed["intent"], "music_play")
                self.assertEqual(parsed["kind"], kind)
                self.assertEqual(parsed["value"], value)
        self.assertEqual(
            router.parse_command("play my Focus playlist")["summary"],
            "play playlist Focus",
        )

    def test_conversational_play_guard(self):
        conversational = (
            "That play seems nice.",
            "The play was good.",
            "I liked that play.",
            "What did you think of the play?",
            "We saw a play last night.",
            "I really enjoyed that play.",
            "That sounds like a nice play.",
            "I was thinking about what you said earlier. That play seems nice, but I'm not sure.",
        )
        for phrase in conversational:
            with self.subTest(phrase=phrase):
                self.assertIsNone(router.parse_command(phrase))
                self.assertTrue(router._looks_conversational(phrase))
        for phrase in ("play some radiohead", "Can you play some Radiohead?", "play chill music", "I want you to keep playing music"):
            with self.subTest(phrase=phrase):
                self.assertFalse(router._looks_conversational(phrase))

    def test_workspace_and_bounds(self):
        for workspace in (1, 3, 10):
            self.assertEqual(router.parse_command(f"workspace {workspace}")["argv"], ["hyprctl", "dispatch", "workspace", str(workspace)])
        self.assertEqual(router.parse_command("switch to workspace 2")["argv"][-1], "2")
        self.assertEqual(router.parse_command("go to workspace 9")["argv"][-1], "9")
        self.assertEqual(router.parse_command("switch to workspace two")["argv"], ["hyprctl", "dispatch", "workspace", "2"])
        self.assertEqual(router.parse_command("go to workspace three")["argv"], ["hyprctl", "dispatch", "workspace", "3"])
        self.assertEqual(router.parse_command("workspace four")["argv"], ["hyprctl", "dispatch", "workspace", "4"])
        self.assertEqual(router.parse_command("switch to workspace ten")["argv"], ["hyprctl", "dispatch", "workspace", "10"])
        self.assertEqual(router.parse_command("Switch back to Workspace ONE.")["argv"], ["hyprctl", "dispatch", "workspace", "1"])
        self.assertEqual(router.parse_command("switch back to workspace 2")["argv"], ["hyprctl", "dispatch", "workspace", "2"])
        self.assertEqual(router.parse_command("switch to the first workspace")["argv"], ["hyprctl", "dispatch", "workspace", "1"])
        self.assertEqual(router.parse_command("go to previous workspace")["argv"], ["hyprctl", "dispatch", "workspace", "previous"])
        self.assertEqual(router.parse_command("back to workspace 2")["argv"], ["hyprctl", "dispatch", "workspace", "2"])
        for text in ("workspace 0", "workspace 11", "switch to workspace 99"):
            with self.subTest(text=text):
                self.assertIsNone(router.parse_command(text))

    def test_engine_switch_phrases(self):
        expected = {
            "switch to opencode": "opencode", "use opencode engine": "opencode", "engine opencode": "opencode",
            "switch to antigravity": "agy", "switch to agy": "agy", "use antigravity engine": "agy", "engine agy": "agy",
        }
        for phrase, engine in expected.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(router.parse_command(phrase)["engine"], engine)

    def test_engine_switch_combinatorics(self):
        phrase_to_engine = {
            # bare verb, preposition optional
            "switch to opencode": "opencode",
            "change to opencode": "opencode",
            "activate opencode": "opencode",
            "enable the opencode engine": "opencode",
            "use opencode": "opencode",
            "use the opencode engine": "opencode",
            "start using opencode": "opencode",
            # persistence tails
            "start using opencode from now on": "opencode",
            "migrate to opencode as the default engine": "opencode",
            "make opencode my engine": "opencode",
            "make opencode the engine": "opencode",
            "set opencode as the default": "opencode",
            "change to antigravity from now on": "agy",
            # preposition variants and pronoun
            "hop over to opencode": "opencode",
            "switch me over to open code": "opencode",
            "switch to open-code": "opencode",
            "switch me to anti-gravity": "agy",
            # noun-before-target ordering
            "change your engine to opencode": "opencode",
            "move the brain to opencode": "opencode",
            "switch my cognitive brain to opencode": "opencode",
            "change engine over to opencode": "opencode",
            "please use the opencode backend": "opencode",
            # politeness wrappers
            "can you switch to opencode": "opencode",
            "could you switch to antigravity": "agy",
            "would you mind switching to opencode": "opencode",
            "please switch to agy": "agy",
            # antigravity family, trailing role noun
            "switch to antigravity engine": "agy",
            "switch to the agy backend": "agy",
            # trailing "please" (base value doesn't strip it the way music does)
            "switch opencode please": "opencode",
        }
        for phrase, engine in phrase_to_engine.items():
            with self.subTest(phrase=phrase):
                expected_summary = "switched to OpenCode" if engine == "opencode" else "switched to Antigravity"
                self.assertEqual(
                    router.parse_command(phrase),
                    {"intent": "engine_switch", "engine": engine, "summary": expected_summary},
                )

    def test_engine_switch_negative_controls(self):
        # The music and note chains must win over the engine grammar.
        self.assertEqual(router.parse_command("play opencode music")["intent"], "music_play")
        self.assertEqual(router.parse_command("note this down: opencode")["intent"], "flash_note")
        for phrase in (
            "open code review of gideon.py",
            "open the code",
            "who made opencode",
            "ok switch engine to opencode",
            "make me a sandwich",
            "make opencode default",
            "set opencode default",
            "switch the engine",
            "switch to the opencode runtime soon",
        ):
            with self.subTest(phrase=phrase):
                result = router.parse_command(phrase)
                self.assertTrue(
                    result is None or result.get("intent") != "engine_switch",
                    f"{phrase!r} must not route to engine_switch, got {result!r}",
                )

    def test_flash_note_prefixes(self):
        for prefix in ("note down:", "note this down:", "flash note:", "take a note:", "quick memo:", "add note:", "jot this down:"):
            with self.subTest(prefix=prefix):
                result = router.parse_command(prefix + " Remember the result.")
                self.assertEqual(result, {"intent": "flash_note", "body": "Remember the result."})
        self.assertIsNone(router.parse_command("note down:"))

    def test_coding_and_debugging_guardrails(self):
        prompts = (
            "fix the bug where pipewire drops audio", "write a script to mute audio", "explain how to mute audio",
            "why does volume down fail", "what is the next track command", "debug this code: pause music",
            "How can I switch to workspace 4?", "Please fix volume up",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(router.parse_command(prompt))

    def test_ensure_hyprland_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hypr_dir = Path(tmpdir) / "hypr" / "test_sig_12345"
            hypr_dir.mkdir(parents=True)
            sock = hypr_dir / ".socket.sock"
            sock.touch()
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}, clear=False):
                os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
                sig = router.ensure_hyprland_env()
                self.assertEqual(sig, "test_sig_12345")
                self.assertEqual(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), "test_sig_12345")

    def test_parser_latency(self):
        start = time.perf_counter()
        for _ in range(1000):
            router.parse_command("increase volume by 15%")
        per_call_ms = (time.perf_counter() - start) * 1000 / 1000
        self.assertLess(per_call_ms, 5)


class TestWindowControlParsing(unittest.TestCase):
    def test_window_control_phrases(self):
        expected = {
            "focus window left": ["hyprctl", "dispatch", "movefocus", "l"],
            "focus window right": ["hyprctl", "dispatch", "movefocus", "r"],
            "focus window up": ["hyprctl", "dispatch", "movefocus", "u"],
            "focus window down": ["hyprctl", "dispatch", "movefocus", "d"],
            "toggle fullscreen": ["hyprctl", "dispatch", "fullscreen", "1"],
            "close active window": ["hyprctl", "dispatch", "killactive"],
        }
        for text, argv in expected.items():
            with self.subTest(text=text):
                result = router.parse_command(text)
                self.assertEqual(result["intent"], "system_action")
                self.assertEqual(result["argv"], argv)

    def test_window_controls_case_insensitive_and_trailing_punctuation(self):
        self.assertEqual(router.parse_command("Focus Window Left")["argv"], ["hyprctl", "dispatch", "movefocus", "l"])
        self.assertEqual(router.parse_command("TOGGLE FULLSCREEN")["argv"], ["hyprctl", "dispatch", "fullscreen", "1"])
        self.assertEqual(router.parse_command("Close Active Window!")["argv"], ["hyprctl", "dispatch", "killactive"])
        self.assertEqual(router.parse_command("focus window down.")["argv"], ["hyprctl", "dispatch", "movefocus", "d"])

    def test_window_control_negatives(self):
        for text in ("focus window", "focus window middle", "movefocus left", "close window", "toggle fullscreen now", "killactive"):
            with self.subTest(text=text):
                self.assertIsNone(router.parse_command(text))

    def test_window_control_guardrail_fall_throughs(self):
        prompts = (
            "how do I focus window left", "why did toggle fullscreen fail", "fix bug: close active window",
            "write a script to focus window up", "what is toggle fullscreen", "explain focus window right",
            "code review focus window down",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(router.parse_command(prompt))


class TestGraphQueryParsing(unittest.TestCase):
    def test_query_graph_for_trigger(self):
        self.assertEqual(
            router.parse_command("query graph for reflex router"),
            {"intent": "graph_query", "query": "reflex router", "summary": "graph query: reflex router"},
        )

    def test_graphify_search_trigger(self):
        self.assertEqual(
            router.parse_command("graphify search audio output"),
            {"intent": "graph_query", "query": "audio output", "summary": "graph query: audio output"},
        )

    def test_case_insensitive_and_trailing_punctuation(self):
        self.assertEqual(router.parse_command("Query Graph For Audio Bus")["query"], "audio bus")
        self.assertEqual(router.parse_command("GRAPHIFY SEARCH Pyproject")["query"], "pyproject")
        self.assertEqual(router.parse_command("query graph for reflex router.")["query"], "reflex router")

    def test_topic_length_bounds(self):
        ok_topic = "a" * 160
        too_long = "a" * 161
        self.assertEqual(router.parse_command("query graph for " + ok_topic)["query"], ok_topic)
        self.assertIsNone(router.parse_command("query graph for " + too_long))
        self.assertIsNone(router.parse_command("graphify search " + too_long))
        self.assertIsNone(router.parse_command("query graph for"))
        self.assertIsNone(router.parse_command("graphify search"))

    def test_newlines_and_control_chars_rejected(self):
        for text in ("query graph for foo\nbar", "graphify search foo\nbar", "query graph for foo\tbar", "graphify search foo\x00bar"):
            with self.subTest(text=text):
                self.assertIsNone(router.parse_command(text))

    def test_graph_query_guardrail_fall_throughs(self):
        prompts = (
            "how do I query graph for audio", "why graphify search volume fails", "fix bug: query graph for reflex router",
            "write a script to graphify search bus", "what is query graph for", "explain graphify search engine",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(router.parse_command(prompt))


class TestAudioOutputSwitcher(unittest.TestCase):
    SINK_KEYS = ("GIDEON_HEADPHONES_SINK", "GIDEON_SPEAKERS_SINK")

    def test_switcher_phrases_default_sink(self):
        saved = {key: os.environ.pop(key, None) for key in self.SINK_KEYS}
        try:
            self.assertEqual(
                router.parse_command("switch audio to headphones")["argv"],
                ["wpctl", "set-default", "@DEFAULT_AUDIO_SINK@"],
            )
            self.assertEqual(
                router.parse_command("switch audio to speakers")["argv"],
                ["wpctl", "set-default", "@DEFAULT_AUDIO_SINK@"],
            )
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_switcher_env_sink_overrides(self):
        with patch.dict(os.environ, {"GIDEON_HEADPHONES_SINK": "alsa_output.usb_headphone", "GIDEON_SPEAKERS_SINK": "alsa_output.pci_speakers"}):
            self.assertEqual(
                router.parse_command("switch audio to headphones")["argv"],
                ["wpctl", "set-default", "alsa_output.usb_headphone"],
            )
            self.assertEqual(
                router.parse_command("switch audio to speakers")["argv"],
                ["wpctl", "set-default", "alsa_output.pci_speakers"],
            )

    def test_switcher_case_insensitive_and_trailing_punctuation(self):
        self.assertEqual(router.parse_command("Switch Audio To Headphones")["argv"][0], "wpctl")
        self.assertEqual(router.parse_command("switch audio to speakers.")["argv"][0], "wpctl")

    def test_switcher_negatives(self):
        for text in ("switch audio", "switch audio to microphone", "audio to headphones", "switch to headphones"):
            with self.subTest(text=text):
                self.assertIsNone(router.parse_command(text))

    def test_switcher_guardrail_fall_throughs(self):
        for prompt in ("how do I switch audio to headphones", "write a script to switch audio to speakers", "why switch audio to headphones fails"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(router.parse_command(prompt))


class TestByeByeDismissal(unittest.TestCase):
    def test_accepts_polite_mutations(self):
        for text in (
            "bye bye",
            "Bye-bye.",
            "bye bye bye",
            "ok, thank you, bye bye",
            "alright bye bye",
            "okay bye bye.",
            "Hey Jarvis, bye bye",
            "Hey Jarvis bye-bye for now",
            "thanks so much, bye bye",
            "yeah ok bye bye",
            "we're done, bye bye",
            "bye bye Gideon",
            "bye bye to you",
            "all right, bye bye",
        ):
            with self.subTest(text=text):
                self.assertEqual(router.parse_command(text), {"intent": "dismiss", "summary": "back to idle"})

    def test_rejects_bare_bye_by_and_embedded_mentions(self):
        for text in (
            "bye", "by", "goodbye", "by the way", "buy buy", "by bye",
            "I said bye bye to him", "bye bye or see you later",
            "say bye bye now", "the song is bye bye bye",
        ):
            with self.subTest(text=text):
                self.assertIsNone(router.parse_command(text))

    def test_music_request_containing_bye_bye_is_not_a_dismissal(self):
        for text in ("play bye bye bye", "put on bye bye bye by NSYNC"):
            with self.subTest(text=text):
                action = router.parse_command(text)
                self.assertIsNotNone(action)
                self.assertNotEqual(action.get("intent"), "dismiss")

    def test_dismissal_never_reaches_the_neural_dispatcher(self):
        async def run():
            dispatcher = AsyncMock()
            with patch.object(router, "get_gideon_dispatcher", return_value=dispatcher):
                handled = await router.try_reflex("ok, thank you, bye bye")
            self.assertTrue(handled)
            dispatcher.dispatch.assert_not_awaited()

        asyncio.run(run())

    def test_dismissal_disarms_hot_mic_and_goes_idle_silently(self):
        async def run():
            disarm = AsyncMock()
            bus = type("Bus", (), {"set_state": AsyncMock(), "dismiss_hot_mic": disarm})()
            tts = type("TTS", (), {"play_sound": AsyncMock(), "speak": AsyncMock(), "interrupt": AsyncMock()})()
            self.assertTrue(await router.try_reflex("bye bye", bus=bus, tts=tts))
            disarm.assert_awaited_once_with()
            bus.set_state.assert_awaited_once_with("idle")
            tts.interrupt.assert_awaited_once_with()
            tts.play_sound.assert_not_awaited()
            tts.speak.assert_not_awaited()

        asyncio.run(run())

    def test_dismissal_without_bus_or_tts_is_safe(self):
        async def run():
            self.assertTrue(await router.try_reflex("bye bye"))

        asyncio.run(run())


class TestNewIntentLatency(unittest.TestCase):
    def test_parser_latency_stays_under_5ms(self):
        phrases = (
            "focus window left", "toggle fullscreen", "close active window",
            "query graph for reflex router", "graphify search audio", "switch audio to speakers",
        )
        start = time.perf_counter()
        for _ in range(250):
            for phrase in phrases:
                router.parse_command(phrase)
        per_call_ms = (time.perf_counter() - start) * 1000 / (250 * len(phrases))
        self.assertLess(per_call_ms, 5)


class TestReflexExecution(unittest.IsolatedAsyncioTestCase):
    async def test_flash_note_writes_timestamped_bullet(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "ledger.md"
            class FixedDateTime(RealDateTime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 1, 2, 3, 4)
            with patch.dict(os.environ, {"GIDEON_NOTES_FILE": str(target)}):
                with patch.object(router, "datetime", FixedDateTime):
                    handled = await router.try_reflex("flash note: keep this", tts=None)
            self.assertTrue(handled)
            self.assertEqual(target.read_text(), "- **[2026-01-02 03:04]** keep this\n")

    async def test_engine_switch_callback_ack_and_idle(self):
        callback = AsyncMock()
        tts = type("TTS", (), {"play_sound": AsyncMock(), "speak": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        self.assertTrue(await router.try_reflex("switch to opencode", bus=bus, tts=tts, set_engine=callback))
        callback.assert_awaited_once_with("opencode")
        tts.play_sound.assert_awaited_once_with("acknowledged.wav")
        tts.speak.assert_awaited_once_with("Switched to OpenCode.")
        bus.set_state.assert_awaited_once_with("idle")

    def _t1_dispatcher(self, engine):
        res = SimpleNamespace(ok=True, message="Switched engines.", data={"intent": "engine_switch", "params": {"engine": engine}})
        dispatcher = AsyncMock()
        dispatcher.dispatch = AsyncMock(return_value=res)
        return dispatcher

    async def test_tier1_engine_switch_normalizes_anti_gravity_alias(self):
        dispatcher = self._t1_dispatcher("antigravity")
        set_engine = AsyncMock(return_value=True)
        tts = type("TTS", (), {"play_sound": AsyncMock(), "speak": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        # "soon" keeps this off the Tier-0 grammar so the BGE path runs.
        with patch.object(router, "get_gideon_dispatcher", return_value=dispatcher):
            self.assertTrue(await router.try_reflex("switch to antigravity soon", bus=bus, tts=tts, set_engine=set_engine))
        set_engine.assert_awaited_once_with("agy")
        tts.play_sound.assert_awaited_once_with("acknowledged.wav")

    async def test_tier1_engine_switch_rejects_unknown_engine(self):
        dispatcher = self._t1_dispatcher("lyra")
        set_engine = AsyncMock(return_value=True)
        tts = type("TTS", (), {"play_sound": AsyncMock(), "speak": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        with patch.object(router, "get_gideon_dispatcher", return_value=dispatcher):
            self.assertFalse(await router.try_reflex("switch to lyra", bus=bus, tts=tts, set_engine=set_engine))
        set_engine.assert_not_awaited()
        tts.play_sound.assert_not_awaited()
        tts.speak.assert_not_awaited()

    async def test_tier1_engine_switch_failure_is_not_acked(self):
        dispatcher = self._t1_dispatcher("opencode")
        set_engine = AsyncMock(return_value=False)
        tts = type("TTS", (), {"play_sound": AsyncMock(), "speak": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        with patch.object(router, "get_gideon_dispatcher", return_value=dispatcher):
            self.assertFalse(await router.try_reflex("switch from opencode", bus=bus, tts=tts, set_engine=set_engine))
        set_engine.assert_awaited_once_with("opencode")
        tts.play_sound.assert_not_awaited()
        tts.speak.assert_not_awaited()

    async def test_command_uses_fixed_runner_and_feedback(self):
        tts = type("TTS", (), {"play_sound": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(0, ""))) as run:
            self.assertTrue(await router.try_reflex("mute audio", bus=bus, tts=tts))
        run.assert_awaited_once_with(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
        tts.play_sound.assert_awaited_once_with("acknowledged.wav")
        bus.set_state.assert_awaited_once_with("idle")

    async def test_media_command_uses_local_first_helper(self):
        from gideon_shared import HandlerResult
        import handlers_system

        tts = type("TTS", (), {"play_sound": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        with patch.object(
            handlers_system,
            "run_media_action",
            new=AsyncMock(return_value=HandlerResult(ok=True, message="ok")),
        ) as action:
            self.assertTrue(await router.try_reflex("pause music", bus=bus, tts=tts))
        action.assert_awaited_once_with("pause")
        tts.play_sound.assert_awaited_once_with("acknowledged.wav")
        bus.set_state.assert_awaited_once_with("idle")

    async def test_media_command_failure_returns_false(self):
        from gideon_shared import HandlerResult
        import handlers_system

        with patch.object(
            handlers_system,
            "run_media_action",
            new=AsyncMock(return_value=HandlerResult(ok=False, error="no player")),
        ):
            self.assertFalse(await router.try_reflex("pause music"))

    async def test_conversational_play_bypasses_dispatcher(self):
        dispatcher = AsyncMock()
        with patch.object(router, "get_gideon_dispatcher", return_value=dispatcher):
            self.assertFalse(await router.try_reflex("That play seems nice."))
        dispatcher.dispatch.assert_not_awaited()

    async def test_music_play_uses_fixed_runner(self):
        with patch.object(
            router, "run_fixed", new=AsyncMock(return_value=(0, ""))
        ) as run:
            self.assertTrue(
                await router.try_reflex("play chill music", bus=None, tts=None)
            )
        run.assert_awaited_once_with(
            [router.MUSIC_BIN, "play", "--mood", "chill"],
            timeout_s=6.0,
        )

    async def test_generic_music_play_shuffles_library(self):
        with patch.object(
            router, "run_fixed", new=AsyncMock(return_value=(0, ""))
        ) as run:
            self.assertTrue(
                await router.try_reflex("play some music", bus=None, tts=None)
            )
        run.assert_awaited_once_with(
            [router.MUSIC_BIN, "play", "--shuffle", "--limit", "50"],
            timeout_s=6.0,
        )

    async def test_music_play_failure_returns_false(self):
        with patch.object(
            router, "run_fixed", new=AsyncMock(return_value=(1, "unknown mood"))
        ):
            self.assertFalse(await router.try_reflex("play chill music"))

    async def test_dispatch_delegates_unmatched_prompt(self):
        engine = type("Engine", (), {"send_prompt": AsyncMock()})()
        self.assertFalse(await router.dispatch("Please explain volume control", engine))
        engine.send_prompt.assert_awaited_once_with("Please explain volume control")


class TestWindowAndAudioExecution(unittest.IsolatedAsyncioTestCase):
    async def test_window_control_runs_fixed_argv(self):
        tts = type("TTS", (), {"play_sound": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(0, ""))) as run:
            self.assertTrue(await router.try_reflex("focus window left", bus=bus, tts=tts))
        run.assert_awaited_once_with(["hyprctl", "dispatch", "movefocus", "l"])
        tts.play_sound.assert_awaited_once_with("acknowledged.wav")
        bus.set_state.assert_awaited_once_with("idle")

    async def test_window_control_failure_returns_false(self):
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(1, "dispatch error"))):
            self.assertFalse(await router.try_reflex("toggle fullscreen"))

    async def test_audio_switcher_runs_wpctl_set_default(self):
        tts = type("TTS", (), {"play_sound": AsyncMock()})()
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        with patch.dict(os.environ, {"GIDEON_HEADPHONES_SINK": "sink.headphones"}):
            with patch.object(router, "run_fixed", new=AsyncMock(return_value=(0, ""))) as run:
                self.assertTrue(await router.try_reflex("switch audio to headphones", bus=bus, tts=tts))
        run.assert_awaited_once_with(["wpctl", "set-default", "sink.headphones"])
        tts.play_sound.assert_awaited_once_with("acknowledged.wav")
        bus.set_state.assert_awaited_once_with("idle")

    async def test_audio_switcher_failure_returns_false(self):
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(127, "wpctl not found"))):
            self.assertFalse(await router.try_reflex("switch audio to speakers"))


class TestGraphQueryExecution(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _drain_graph_tasks():
        tasks = list(router._GRAPH_TASKS)
        await asyncio.gather(*tasks)
        return tasks

    async def test_try_reflex_returns_immediately_and_schedules_background(self):
        release = asyncio.Event()

        async def blocking_run(argv, timeout_s=1.5):
            await release.wait()
            return 0, "NODE only.node"

        with patch.object(router, "run_fixed", new=blocking_run):
            self.assertTrue(await router.try_reflex("query graph for latency check"))
        tasks = list(router._GRAPH_TASKS)
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0].done())
        release.set()
        await asyncio.gather(*tasks)

    async def test_graph_query_success_extracts_three_nodes_and_reports(self):
        output = "header\nNODE reflex_router.py\nNODE audio_pipeline.py\nNODE ui_bus.py\nNODE extra_should_drop.py"
        tts = type("TTS", (), {"speak": AsyncMock()})()
        bus = type("Bus", (), {"broadcast_ui": AsyncMock()})()
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(0, output))) as run:
            self.assertTrue(await router.try_reflex("graphify search reflex router", bus=bus, tts=tts))
            await self._drain_graph_tasks()
        run.assert_awaited_once_with(["graphify", "query", "reflex router"], timeout_s=10.0)
        tts.speak.assert_awaited_once_with("Graph query for reflex router: found 3 nodes. reflex_router.py.")
        bus.broadcast_ui.assert_awaited_once_with({
            "event": "graph_query_result", "query": "reflex router",
            "nodes": ["reflex_router.py", "audio_pipeline.py", "ui_bus.py"], "status": "ok",
        })

    async def test_graph_query_empty_results_reported(self):
        tts = type("TTS", (), {"speak": AsyncMock()})()
        bus = type("Bus", (), {"broadcast_ui": AsyncMock()})()
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(0, "no graph here"))):
            self.assertTrue(await router.try_reflex("query graph for missing topic", bus=bus, tts=tts))
            await self._drain_graph_tasks()
        tts.speak.assert_awaited_once_with("No graph results found for missing topic.")
        bus.broadcast_ui.assert_awaited_once_with({
            "event": "graph_query_result", "query": "missing topic", "nodes": [], "status": "empty",
        })

    async def test_graph_query_command_failure_reports_empty(self):
        tts = type("TTS", (), {"speak": AsyncMock()})()
        bus = type("Bus", (), {"broadcast_ui": AsyncMock()})()
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(1, "graphify crashed"))):
            self.assertTrue(await router.try_reflex("graphify search broken", bus=bus, tts=tts))
            await self._drain_graph_tasks()
        tts.speak.assert_awaited_once_with("No graph results found for broken.")
        bus.broadcast_ui.assert_awaited_once_with({
            "event": "graph_query_result", "query": "broken", "nodes": [], "status": "empty",
        })

    async def test_graph_query_without_tts_or_bus_still_succeeds(self):
        with patch.object(router, "run_fixed", new=AsyncMock(return_value=(0, "NODE solo.node"))):
            self.assertTrue(await router.try_reflex("query graph for bare run"))
            await self._drain_graph_tasks()

    async def test_graph_query_runner_exception_is_contained(self):
        async def exploding_run(argv, timeout_s=1.5):
            raise RuntimeError("subprocess exploded")

        with patch.object(router, "run_fixed", new=exploding_run):
            self.assertTrue(await router.try_reflex("query graph for resilience"))
            await self._drain_graph_tasks()


class TestDismissReflex(unittest.TestCase):
    def test_bye_bye_mutations_parse_as_dismiss(self):
        for text in (
            "bye bye", "Bye-bye.", "okay bye bye", "alright, bye bye",
            "ok, thank you, bye bye", "Hey Jarvis, bye bye",
            "yeah ok bye bye", "we're done, bye bye", "bye bye Gideon",
        ):
            with self.subTest(text=text):
                action = router.parse_command(text)
                self.assertIsNotNone(action)
                self.assertEqual(action["intent"], "dismiss")

    def test_bare_bye_and_by_are_not_dismissals(self):
        for text in ("bye", "by", "goodbye", "by the way", "buy buy", "by bye"):
            with self.subTest(text=text):
                action = router.parse_command(text)
                self.assertNotEqual((action or {}).get("intent"), "dismiss")

    def test_bye_bye_embedded_in_sentence_is_not_dismissal(self):
        for text in (
            "I said bye bye to him", "bye bye or see you later",
            "play bye bye bye", "the song is bye bye bye",
        ):
            with self.subTest(text=text):
                action = router.parse_command(text)
                self.assertNotEqual((action or {}).get("intent"), "dismiss")


class TestDismissExecution(unittest.IsolatedAsyncioTestCase):
    async def test_dismiss_disarms_hot_mic_and_goes_idle_without_ack(self):
        bus = type("Bus", (), {"set_state": AsyncMock(), "dismiss_hot_mic": AsyncMock()})()
        tts = type("TTS", (), {"play_sound": AsyncMock(), "speak": AsyncMock(), "interrupt": AsyncMock()})()
        self.assertTrue(await router.try_reflex("ok, thank you, bye bye", bus=bus, tts=tts))
        bus.dismiss_hot_mic.assert_awaited_once_with()
        tts.interrupt.assert_awaited_once_with()
        bus.set_state.assert_awaited_once_with("idle")
        tts.play_sound.assert_not_awaited()
        tts.speak.assert_not_awaited()

    async def test_dismiss_without_hook_or_tts_still_succeeds(self):
        bus = type("Bus", (), {"set_state": AsyncMock()})()
        self.assertTrue(await router.try_reflex("bye bye", bus=bus))
        bus.set_state.assert_awaited_once_with("idle")

    async def test_dismiss_skips_neural_dispatcher(self):
        dispatcher = AsyncMock()
        with patch.object(router, "get_gideon_dispatcher", return_value=dispatcher):
            self.assertTrue(await router.try_reflex("bye bye"))
        dispatcher.dispatch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
