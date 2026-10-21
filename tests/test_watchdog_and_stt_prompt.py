#!/usr/bin/env python3
"""
Unit and Integration Test Suite for:
1. Faster-Whisper / CTranslate2 STT Optimal Conditioning Prompt Tokenizer Representation.
2. Hardened Tool Execution Watchdog Loop (Zombie Tool & Collision Immunity) — LIVE engines.

The LIVE tests import `AntigravityEngine`/`VoiceListener` from `gideon` and
`OpenCodeEngine` from `opencode_engine` so that regressions in the running
implementation fail the suite, not just the specification simulation.
"""
from __future__ import annotations

import asyncio
import site
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

sys.path.insert(0, "/home/cat/gideon")

import numpy as np  # noqa: E402

import gideon as gideon_module  # noqa: E402
import opencode_engine as opencode_engine_module  # noqa: E402
from gideon import OPTIMAL_WHISPER_PROMPT as LIVE_WHISPER_PROMPT  # noqa: E402
from gideon import AntigravityEngine, SignalBus, VoiceListener  # noqa: E402
from gideon import clean_text_for_tts as live_clean_text_for_tts  # noqa: E402
from opencode_engine import OpenCodeEngine  # noqa: E402

# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------

OPTIMAL_WHISPER_PROMPT = (
    "Gideon, autonomous AI executive on Omarchy Linux with Hyprland and Wayland. "
    "Executing Antigravity, Quickshell, PipeWire, wpctl, pw-play, Pacman, Yay, "
    "systemctl, Fish Audio, Obsidian, WebGL, Barehands, Euphonium, and brass acoustics."
)


class MockTTSPlayer:
    """Mock TTS player tracking queued speech and playback state."""
    def __init__(self):
        self._is_speaking = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self.queued_sentences: list[str] = []

    def queue_sentence(self, text: str):
        self.queued_sentences.append(text)
        self._queue.put_nowait(text)

    async def wait_until_done(self):
        return None


class MockSignalBus:
    """Mock SignalBus tracking UI and companion states."""
    def __init__(self):
        self.current_state = "idle"
        self.current_tool = None
        self.current_detail = ""

    def set_state(self, state: str, detail: str = ""):
        self.current_state = state
        self.current_detail = detail


def make_live_bus() -> SignalBus:
    """A real SignalBus rooted in a throwaway temp signals dir (hermetic)."""
    return SignalBus(Path(tempfile.mkdtemp(prefix="gideon-test-signals-")))


class HardenedWatchdogSimulator:
    """Simulates the hardened _watchdog_loop logic for AntigravityEngine and OpenCodeEngine."""
    def __init__(self, bus: MockSignalBus, tts: MockTTSPlayer):
        self.bus = bus
        self.tts = tts
        self.is_turn_active = True
        self.current_tool: Optional[dict] = None
        self.turn_start_time = 0.0

    async def execute_watchdog_tick(self, elapsed_s: int) -> str:
        """Executes a single tick of the hardened watchdog logic.
        Returns outcome: 'break', 'suppressed_collision', or 'spoken'.
        """
        # Strict Status Gate: Exit if turn finished or tool is missing/no longer actively running
        if not self.is_turn_active or not self.current_tool or self.current_tool.get("status") != "running":
            return "break"

        # Acoustic Collision Guard: Never interrupt ongoing playback or active user speech
        if self.tts._is_speaking or self.bus.current_state in ("speaking", "listening", "hotmic"):
            return "suppressed_collision"

        t_label = self.current_tool.get("label") or "working on the solution"
        checkin_text = f"Still running {t_label}, {elapsed_s} seconds in."
        self.tts.queue_sentence(checkin_text)
        return "spoken"


# ---------------------------------------------------------------------------
# LIVE watchdog harness
# ---------------------------------------------------------------------------

class _ImmediateSleepAsyncio:
    """Proxy for the asyncio module whose sleep() yields instead of waiting 45s.

    Lets the tests drive the LIVE `_watchdog_loop` deterministically.
    """

    def __init__(self, real, delay_s: float = 0.005):
        self._real = real
        self._delay_s = delay_s

    async def sleep(self, _delay):
        await self._real.sleep(self._delay_s)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _run_live_watchdog(engine, module, timeout_s: float = 1.0) -> bool:
    """Runs a LIVE `engine._watchdog_loop()` until it breaks or emits a check-in.

    Returns True if the loop terminated by itself (strict status gate), False if
    it was still looping when the timeout elapsed (collision suppression).
    """
    spoke = asyncio.Event()
    tts = engine.tts
    original = tts.queue_sentence

    def _hook(text):
        original(text)
        # Deterministically stop the loop after the first check-in so the
        # assertions observe exactly one queued sentence.
        engine.is_turn_active = False
        spoke.set()

    tts.queue_sentence = _hook
    loop_task = asyncio.ensure_future(engine._watchdog_loop())
    spoke_task = asyncio.ensure_future(spoke.wait())
    try:
        with mock.patch.object(module, "asyncio", _ImmediateSleepAsyncio(asyncio)):
            await asyncio.wait(
                {loop_task, spoke_task}, timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED)
        broke_on_its_own = loop_task.done()
    finally:
        tts.queue_sentence = original
        loop_task.cancel()
        spoke_task.cancel()
        results = await asyncio.gather(loop_task, spoke_task, return_exceptions=True)
        first = results[0] if results else None
        if isinstance(first, BaseException) and not isinstance(first, asyncio.CancelledError):
            raise first
    return broke_on_its_own


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestSTTPromptTokenizer(unittest.TestCase):
    """Verifies that the optimal conditioning prompt tokenizes cleanly under Faster-Whisper."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, "/home/cat/gideon")
        from gideon import _preload_cuda_libs
        _preload_cuda_libs()

        import faster_whisper
        try:
            cls.model = faster_whisper.WhisperModel("medium.en", device="cuda", compute_type="int8")
        except Exception:
            cls.model = faster_whisper.WhisperModel("small.en", device="cpu", compute_type="int8")
        cls.tokenizer = cls.model.hf_tokenizer

    def test_token_count_within_whisper_budget(self):
        """Faster-Whisper limits initial_prompt to 224 tokens."""
        encoding = self.tokenizer.encode(OPTIMAL_WHISPER_PROMPT)
        token_count = len(encoding.ids)
        self.assertLess(token_count, 224, f"Prompt token count {token_count} exceeds 224 limit")
        self.assertEqual(token_count, 74, f"Expected 74 tokens, got {token_count}")

    def test_exact_roundtrip_decoding(self):
        """Verifies no byte fallback or unknown token mangling during tokenization."""
        encoding = self.tokenizer.encode(OPTIMAL_WHISPER_PROMPT)
        decoded = self.tokenizer.decode(encoding.ids).strip()
        self.assertEqual(decoded, OPTIMAL_WHISPER_PROMPT.strip())

    def test_domain_keywords_present(self):
        """Confirms presence of domain-critical keywords in prompt."""
        required_terms = [
            "Omarchy", "Hyprland", "Wayland", "Antigravity", "Quickshell",
            "PipeWire", "wpctl", "pw-play", "Pacman", "Yay", "Fish Audio",
            "Obsidian", "Barehands", "Euphonium"
        ]
        for term in required_terms:
            self.assertIn(term, OPTIMAL_WHISPER_PROMPT, f"Missing critical domain term: {term}")

    # --- LIVE gideon.OPTIMAL_WHISPER_PROMPT (FIX G5) -------------------------

    def test_live_prompt_matches_spec(self):
        """The live module-level constant must match the hardening specification."""
        self.assertEqual(LIVE_WHISPER_PROMPT, OPTIMAL_WHISPER_PROMPT)

    def test_live_prompt_token_budget_and_roundtrip(self):
        encoding = self.tokenizer.encode(LIVE_WHISPER_PROMPT)
        self.assertLess(len(encoding.ids), 224)
        self.assertEqual(
            self.tokenizer.decode(encoding.ids).strip(), LIVE_WHISPER_PROMPT.strip())


class TestLiveVoiceListenerTranscribe(unittest.TestCase):
    """FIX G3/G4/G5: drives the LIVE VoiceListener.transcribe_audio with a fake Whisper model."""

    class _FakeModel:
        def __init__(self, texts, exc: Optional[Exception] = None):
            self._texts = texts
            self._exc = exc
            self.calls: list[dict] = []

        def transcribe(self, audio, **kwargs):
            self.calls.append(kwargs)
            if self._exc is not None:
                raise self._exc
            return ([type("Seg", (), {"text": t})() for t in self._texts], None)

    @staticmethod
    def _pcm() -> bytes:
        return np.zeros(4000, dtype=np.int16).tobytes()

    def _listener(self, model):
        listener = object.__new__(VoiceListener)
        listener.device = "cpu"
        listener.compute_type = "int8"
        listener.model = model
        return listener

    def test_live_transcribe_uses_optimal_prompt_and_vad(self):
        fake = self._FakeModel(["hello world"])
        listener = self._listener(fake)

        text = listener.transcribe_audio(self._pcm())

        self.assertEqual(text, "hello world")
        self.assertEqual(len(fake.calls), 1)
        kwargs = fake.calls[0]
        self.assertEqual(kwargs["beam_size"], 1)
        self.assertEqual(kwargs["best_of"], 1)
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(kwargs["initial_prompt"], LIVE_WHISPER_PROMPT)
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(kwargs["vad_parameters"], {"min_silence_duration_ms": 300})

    def test_live_transcribe_short_buffer_returns_empty(self):
        fake = self._FakeModel(["should not be used"])
        listener = self._listener(fake)
        self.assertEqual(listener.transcribe_audio(b"\x00\x00"), "")
        self.assertEqual(fake.calls, [])

    def test_live_cpu_oom_fallback_loads_small_en_with_hardened_params(self):
        """FIX G3/G4: OOM recovery reloads small.en and applies best_of/prompt/VAD params."""
        oom = RuntimeError("CUDA out of memory")
        listener = self._listener(self._FakeModel([], exc=oom))
        recovered = self._FakeModel(["recovered"])

        with mock.patch("faster_whisper.WhisperModel", return_value=recovered) as ctor:
            text = listener.transcribe_audio(self._pcm())

        self.assertEqual(text, "recovered")
        # CPU fallback model is small.en (not medium.en).
        self.assertEqual(ctor.call_args[0][0], "small.en")
        self.assertEqual(ctor.call_args[1].get("device"), "cpu")

        kwargs = recovered.calls[0]
        self.assertEqual(kwargs["beam_size"], 1)
        self.assertEqual(kwargs["best_of"], 1)
        self.assertEqual(kwargs["initial_prompt"], LIVE_WHISPER_PROMPT)
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(kwargs["vad_parameters"], {"min_silence_duration_ms": 300})


class TestHardenedWatchdog(unittest.TestCase):
    """Verifies the hardened watchdog loop behavior across tool states and audio collisions."""

    def setUp(self):
        self.bus = MockSignalBus()
        self.tts = MockTTSPlayer()
        self.engine = HardenedWatchdogSimulator(self.bus, self.tts)

    def test_active_tool_speaks_checkin(self):
        """When a tool is actively running and the line is quiet, watchdog speaks a calm check-in."""
        self.engine.current_tool = {
            "name": "run_command",
            "status": "running",
            "label": "run_command: pacman -Syu"
        }
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "spoken")
        self.assertEqual(len(self.tts.queued_sentences), 1)
        self.assertEqual(
            self.tts.queued_sentences[0],
            "Still running run_command: pacman -Syu, 45 seconds in."
        )

    def test_zombie_tool_prevented(self):
        """When tool status is 'done', watchdog loop breaks immediately without speaking."""
        self.engine.current_tool = {
            "name": "run_command",
            "status": "done",
            "label": "run_command: pacman -Syu"
        }
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "break")
        self.assertEqual(len(self.tts.queued_sentences), 0)

    def test_inactive_turn_breaks(self):
        """When is_turn_active is False, watchdog breaks immediately."""
        self.engine.is_turn_active = False
        self.engine.current_tool = {"name": "run_command", "status": "running"}
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "break")
        self.assertEqual(len(self.tts.queued_sentences), 0)

    def test_no_tool_breaks(self):
        """When current_tool is None, watchdog breaks immediately."""
        self.engine.current_tool = None
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "break")
        self.assertEqual(len(self.tts.queued_sentences), 0)

    def test_collision_guard_tts_speaking(self):
        """When TTS is actively vocalizing, watchdog suppresses check-in."""
        self.engine.current_tool = {"name": "run_command", "status": "running", "label": "build"}
        self.tts._is_speaking = True
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "suppressed_collision")
        self.assertEqual(len(self.tts.queued_sentences), 0)

    def test_collision_guard_user_listening(self):
        """When user microphone is capturing speech, watchdog suppresses check-in."""
        self.engine.current_tool = {"name": "run_command", "status": "running", "label": "build"}
        self.bus.current_state = "listening"
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "suppressed_collision")
        self.assertEqual(len(self.tts.queued_sentences), 0)

    def test_collision_guard_user_hotmic(self):
        """When companion is in hotmic buffer zone, watchdog suppresses check-in."""
        self.engine.current_tool = {"name": "run_command", "status": "running", "label": "build"}
        self.bus.current_state = "hotmic"
        outcome = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome, "suppressed_collision")
        self.assertEqual(len(self.tts.queued_sentences), 0)

    def test_recovery_after_collision_clears(self):
        """Verifies that once collision clears and tool is still running, check-in proceeds."""
        self.engine.current_tool = {"name": "run_command", "status": "running", "label": "tests"}
        # Tick 1: user is speaking
        self.bus.current_state = "listening"
        outcome1 = asyncio.run(self.engine.execute_watchdog_tick(45))
        self.assertEqual(outcome1, "suppressed_collision")
        self.assertEqual(len(self.tts.queued_sentences), 0)

        # Tick 2: user finished, line quiet, tool still running
        self.bus.current_state = "toolcall"
        outcome2 = asyncio.run(self.engine.execute_watchdog_tick(75))
        self.assertEqual(outcome2, "spoken")
        self.assertEqual(len(self.tts.queued_sentences), 1)
        self.assertEqual(self.tts.queued_sentences[0], "Still running tests, 75 seconds in.")


class _LiveWatchdogMixin:
    """Shared LIVE watchdog assertions for AntigravityEngine and OpenCodeEngine."""

    module = None  # set by subclasses

    def _make_engine(self, current_tool=None, state="toolcall", turn_active=True,
                     elapsed_s=45, speaking=False, queued=False):
        bus = make_live_bus()
        bus.set_state(state)
        tts = MockTTSPlayer()
        tts._is_speaking = speaking
        if queued:
            tts._queue.put_nowait("backlog")
        engine = self.engine_cls(bus=bus, tts=tts)
        engine.is_turn_active = turn_active
        engine.turn_start_time = time.time() - elapsed_s
        engine.current_tool = current_tool
        return engine

    def test_live_watchdog_speaks_checkin(self):
        engine = self._make_engine(current_tool={
            "name": "run_command", "status": "running",
            "label": "run_command: pacman -Syu"})
        asyncio.run(_run_live_watchdog(engine, self.module))
        self.assertEqual(engine.tts.queued_sentences,
                         ["Still running run_command: pacman -Syu, 45 seconds in."])

    def test_live_watchdog_default_label_when_unlabeled(self):
        engine = self._make_engine(current_tool={"name": "tool", "status": "running"})
        asyncio.run(_run_live_watchdog(engine, self.module))
        self.assertEqual(engine.tts.queued_sentences,
                         ["Still running working on the solution, 45 seconds in."])

    def test_live_watchdog_breaks_on_done_tool(self):
        engine = self._make_engine(current_tool={
            "name": "run_command", "status": "done", "label": "x"})
        broke = asyncio.run(_run_live_watchdog(engine, self.module))
        self.assertTrue(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_breaks_when_no_tool(self):
        engine = self._make_engine(current_tool=None)
        broke = asyncio.run(_run_live_watchdog(engine, self.module))
        self.assertTrue(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_breaks_when_turn_inactive(self):
        engine = self._make_engine(
            current_tool={"name": "tool", "status": "running"}, turn_active=False)
        broke = asyncio.run(_run_live_watchdog(engine, self.module))
        self.assertTrue(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_suppressed_while_tts_speaking(self):
        engine = self._make_engine(
            current_tool={"name": "tool", "status": "running", "label": "build"},
            speaking=True)
        broke = asyncio.run(_run_live_watchdog(engine, self.module, timeout_s=0.3))
        self.assertFalse(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_suppressed_when_queue_not_empty(self):
        engine = self._make_engine(
            current_tool={"name": "tool", "status": "running", "label": "build"},
            queued=True)
        broke = asyncio.run(_run_live_watchdog(engine, self.module, timeout_s=0.3))
        self.assertFalse(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_suppressed_while_listening(self):
        engine = self._make_engine(
            current_tool={"name": "tool", "status": "running", "label": "build"},
            state="listening")
        broke = asyncio.run(_run_live_watchdog(engine, self.module, timeout_s=0.3))
        self.assertFalse(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_suppressed_while_hotmic(self):
        engine = self._make_engine(
            current_tool={"name": "tool", "status": "running", "label": "build"},
            state="hotmic")
        broke = asyncio.run(_run_live_watchdog(engine, self.module, timeout_s=0.3))
        self.assertFalse(broke)
        self.assertEqual(engine.tts.queued_sentences, [])

    def test_live_watchdog_suppressed_while_speaking_state(self):
        engine = self._make_engine(
            current_tool={"name": "tool", "status": "running", "label": "build"},
            state="speaking")
        broke = asyncio.run(_run_live_watchdog(engine, self.module, timeout_s=0.3))
        self.assertFalse(broke)
        self.assertEqual(engine.tts.queued_sentences, [])


class TestLiveAntigravityWatchdog(_LiveWatchdogMixin, unittest.TestCase):
    """FIX G1: exercises gideon.AntigravityEngine._watchdog_loop directly."""

    module = gideon_module
    engine_cls = AntigravityEngine


class TestLiveOpenCodeWatchdog(_LiveWatchdogMixin, unittest.TestCase):
    """FIX O4: exercises opencode_engine.OpenCodeEngine._watchdog_loop directly."""

    module = opencode_engine_module
    engine_cls = OpenCodeEngine


class TestLiveOpenCodeSpeakSentence(unittest.TestCase):
    """FIX O1/O2: live OpenCodeEngine sentence sanitization."""

    def setUp(self):
        self.bus = make_live_bus()
        self.tts = MockTTSPlayer()
        self.engine = OpenCodeEngine(bus=self.bus, tts=self.tts)

    def test_live_sentence_end_regex_matches_gideon(self):
        self.assertEqual(
            opencode_engine_module._SENTENCE_END.pattern,
            gideon_module._SENTENCE_END.pattern)
        self.assertEqual(
            opencode_engine_module._SENTENCE_END.flags,
            gideon_module._SENTENCE_END.flags)

    def test_live_speak_sentence_sanitizes(self):
        self.engine._speak_sentence("Config saved to /home/cat/gideon/gideon.py.")
        self.assertEqual(self.tts.queued_sentences, ["Config saved to gideon.py."])

    def test_live_speak_sentence_redacts_urls_and_markdown(self):
        self.engine._speak_sentence("Read **the docs** at https://github.com/x now.")
        self.assertEqual(self.tts.queued_sentences, ["Read the docs at the linked website now."])

    def test_live_speak_sentence_drops_planning_phrase(self):
        self.engine._speak_sentence("Let me check the system uptime.")
        self.assertEqual(self.tts.queued_sentences, [])

    def test_live_speak_sentence_drops_whitespace(self):
        self.engine._speak_sentence("   ")
        self.assertEqual(self.tts.queued_sentences, [])

    def test_live_speak_sentence_matches_sanitizer_output(self):
        raw = "### Status: **All systems** are `online` & running—ready for commands."
        self.engine._speak_sentence(raw)
        self.assertEqual(self.tts.queued_sentences, [live_clean_text_for_tts(raw)])


class TestLiveOpenCodeEventHandling(unittest.TestCase):
    """FIX O3/O4: live OpenCodeEngine.current_tool lifecycle via _handle_event."""

    def setUp(self):
        self.bus = make_live_bus()
        self.tts = MockTTSPlayer()
        self.engine = OpenCodeEngine(bus=self.bus, tts=self.tts)
        self.engine.session_id = "ses_test"

    def test_live_step_started_populates_dict_and_toolcall_state(self):
        self.engine._handle_event("session.step.started", {"agent": "build"})
        self.assertEqual(self.engine.current_tool,
                         {"name": "build", "label": "build", "status": "running"})
        self.assertEqual(self.bus.current_state, "toolcall")
        self.assertEqual(self.bus.current_detail, "build")

    def test_live_step_started_defaults_agent(self):
        self.engine._handle_event("session.step.started", {})
        self.assertEqual(self.engine.current_tool,
                         {"name": "tool", "label": "working on the solution", "status": "running"})

    def test_live_text_delta_marks_tool_done_and_thinking(self):
        self.engine._handle_event("session.step.started", {"agent": "build"})
        self.engine._handle_event("session.text.delta", {"delta": "Hello. "})
        self.assertEqual(self.engine.current_tool["status"], "done")
        self.assertEqual(self.bus.current_state, "thinking")
        self.assertIn("Hello.", self.engine._sentence_buffer)

    def test_live_send_prompt_and_interrupt_reset_tool(self):
        self.engine.current_tool = {"name": "x", "status": "running"}
        asyncio.run(self.engine.interrupt())
        self.assertIsNone(self.engine.current_tool)

        self.engine.current_tool = {"name": "x", "status": "running"}
        self.engine._client = None
        with self.assertRaises(RuntimeError):
            asyncio.run(self.engine.send_prompt("hi"))
        self.assertIsNone(self.engine.current_tool)

    def test_live_finish_turn_resets_tool(self):
        self.engine.current_tool = {"name": "x", "status": "running"}
        self.engine.is_turn_active = True
        asyncio.run(self.engine._finish_turn("test"))
        self.assertIsNone(self.engine.current_tool)
        self.assertFalse(self.engine.is_turn_active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
