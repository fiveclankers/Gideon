"""Engine selection, persona delivery, and barge-in routing.

Covers the promotion of the ACP engine to GIDEON's Antigravity backend:

* ``codex`` is the default brain backend;
* ``agy`` builds ``ACPAntigravityEngine`` (Antigravity ACP);
* ``agy-headless`` is the legacy NDJSON fallback (``AntigravityEngine``);
* the persona travels as ACP ``_meta`` and never as a prepended user message;
* an acoustic barge-in cancels the live engine turn via ``bus.engine.interrupt()``.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp_antigravity_engine import ACPAntigravityEngine  # noqa: E402
from codex_engine import CodexEngine  # noqa: E402
from gideon import (  # noqa: E402
    GIDEON_VOICE_PREAMBLE,
    HERE,
    AntigravityEngine,
    WakeWordListener,
    _make_engine,
)


class _Bus:
    """SignalBus stand-in with the surface the engines touch."""

    def __init__(self) -> None:
        self.current_state = "idle"
        self.current_detail = ""
        self.current_reply = ""
        self.current_user = ""
        self.current_tool = None
        self.current_effort = "xhigh"
        self.engine = None
        self.engine_name = "codex"
        self.states: list[tuple[str, str]] = []

    def set_state(self, state: str, detail: str = "") -> None:
        self.current_state = state
        self.current_detail = detail
        self.states.append((state, detail))

    def set_user_transcript(self, text: str) -> None:
        self.current_user = text
        self.current_reply = ""

    def clear_transcript(self) -> None:
        self.current_user = ""
        self.current_reply = ""


class _TTS:
    def __init__(self) -> None:
        self._is_speaking = False
        self._queue: queue.Queue = queue.Queue()
        self.spoken: list[str] = []
        self.interrupts = 0

    def queue_sentence(self, sentence: str) -> None:
        self.spoken.append(sentence)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def wait_until_done(self) -> None:
        return None


class _StubTransport:
    """In-process ACP transport: records frames, answers with fixed results."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []
        self.is_closed = False
        self._notification_handlers: dict = {}
        self._request_handlers: dict = {}

    def on_notification(self, method, handler) -> None:
        self._notification_handlers[method] = handler

    def on_request(self, method, handler) -> None:
        self._request_handlers[method] = handler

    async def start(self) -> None:
        return None

    async def request(self, method, params=None, timeout=None) -> dict:
        self.requests.append((method, params or {}))
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentInfo": {"name": "stub"},
                "agentCapabilities": {},
            }
        if method == "session/new":
            return {"sessionId": "sess-stub", "configOptions": []}
        if method == "session/prompt":
            return {"stopReason": "end_turn"}
        return {}

    async def notify(self, method, params=None) -> None:
        self.notifications.append((method, params or {}))

    async def close(self) -> None:
        self.is_closed = True

    # helpers
    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def params_for(self, method: str) -> list[dict]:
        return [params for name, params in self.requests if name == method]


class _StubEngine(ACPAntigravityEngine):
    """ACP engine wired to :class:`_StubTransport` instead of a real process."""

    def __init__(self, *args, transport: _StubTransport, **kwargs) -> None:
        self._stub_transport = transport
        super().__init__(*args, **kwargs)

    def _make_transport(self) -> _StubTransport:
        return self._stub_transport


class EngineFactoryTest(unittest.IsolatedAsyncioTestCase):
    """``_make_engine`` dispatch and engine identity."""

    def setUp(self) -> None:
        self.bus = _Bus()
        self.tts = _TTS()

    async def test_codex_is_default_backend(self) -> None:
        engine = _make_engine("codex", self.bus, self.tts)
        self.assertIsInstance(engine, CodexEngine)
        self.assertEqual(engine.name, "codex")
        self.assertEqual(engine.effort, "xhigh")

    async def test_agy_backend_selection(self) -> None:
        engine = _make_engine("agy", self.bus, self.tts)
        self.assertIsInstance(engine, ACPAntigravityEngine)
        self.assertEqual(engine.name, "agy")
        self.assertEqual(engine.effort, "medium")
        self.assertIs(engine.preamble, GIDEON_VOICE_PREAMBLE)
        self.assertEqual(engine.cwd, str(HERE))

    async def test_agy_headless_is_the_legacy_engine(self) -> None:
        engine = _make_engine("agy-headless", self.bus, self.tts)
        self.assertIsInstance(engine, AntigravityEngine)
        self.assertNotIsInstance(engine, ACPAntigravityEngine)

    async def test_unknown_name_falls_back_to_codex(self) -> None:
        with self.assertLogs("gideon", level="WARNING") as logs:
            engine = _make_engine("garbage", self.bus, self.tts)
        self.assertIsInstance(engine, CodexEngine)
        self.assertEqual(engine.name, "codex")
        self.assertTrue(any("garbage" in line for line in logs.output))

    async def test_engine_identifiers(self) -> None:
        self.assertEqual(CodexEngine.name, "codex")
        self.assertEqual(ACPAntigravityEngine.name, "agy")
        self.assertEqual(AntigravityEngine.name, "agy-headless")


class PersonaDeliveryTest(unittest.IsolatedAsyncioTestCase):
    """The persona must ride ACP metadata, never the first user message."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.bus = _Bus()
        self.tts = _TTS()
        self.transport = _StubTransport()
        self.engine = _StubEngine(
            self.bus,
            self.tts,
            preamble=GIDEON_VOICE_PREAMBLE,
            cwd=self._tmpdir.name,
            transport=self.transport,
        )
        self.addAsyncCleanup(self._close_engine)

    async def _close_engine(self) -> None:
        try:
            await asyncio.wait_for(self.engine.close(), timeout=10)
        except BaseException:  # noqa: BLE001 - cleanup must not mask failures
            pass

    async def _wait_for(self, predicate, timeout: float = 5.0) -> None:
        async def _poll() -> None:
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_poll(), timeout=timeout)

    async def test_persona_travels_as_session_meta_not_as_user_text(self) -> None:
        await self.engine.start()

        session_params = self.transport.params_for("session/new")
        self.assertEqual(len(session_params), 1)
        meta = session_params[0]["_meta"]["antigravity"]
        self.assertEqual(meta["systemPrompt"]["append"], GIDEON_VOICE_PREAMBLE)

        user_text = "What's the weather in Bangalore?"
        await self.engine.send_prompt(user_text)
        await self._wait_for(lambda: "session/prompt" in self.transport.methods())

        prompt_params = self.transport.params_for("session/prompt")[0]
        self.assertEqual(prompt_params["prompt"], [{"type": "text", "text": user_text}])
        self.assertNotIn(GIDEON_VOICE_PREAMBLE, prompt_params["prompt"][0]["text"])
        self.assertFalse(prompt_params["prompt"][0]["text"].startswith("[Directive"))


class BargeInTest(unittest.IsolatedAsyncioTestCase):
    """Acoustic barge-in must cancel the turn via bus.engine.interrupt(), not only audio."""

    class _Engine:
        def __init__(self, active: bool) -> None:
            self.is_turn_active = active
            self.interrupts = 0
            self.done = asyncio.Event()

        async def interrupt(self, hard: bool = False) -> bool:
            self.interrupts += 1
            self.done.set()
            return True

    class _RecordingTTS:
        def __init__(self) -> None:
            self.interrupts = 0
            self.done = asyncio.Event()

        async def interrupt(self) -> None:
            self.interrupts += 1
            self.done.set()

    async def test_live_turn_interrupts_engine_via_bus(self) -> None:
        bus = _Bus()
        engine = self._Engine(active=True)
        bus.engine = engine
        tts = self._RecordingTTS()
        listener = WakeWordListener(
            bus=bus,
            tts=tts,
            on_speech_captured=lambda pcm: None,
            main_loop=asyncio.get_running_loop(),
        )
        listener._handle_barge_in()
        await asyncio.wait_for(engine.done.wait(), timeout=2)
        self.assertEqual(engine.interrupts, 1)
        self.assertEqual(tts.interrupts, 0)

    async def test_fallback_interrupts_tts_when_no_engine(self) -> None:
        bus = _Bus()
        bus.engine = None
        tts = self._RecordingTTS()
        listener = WakeWordListener(
            bus=bus,
            tts=tts,
            on_speech_captured=lambda pcm: None,
            main_loop=asyncio.get_running_loop(),
        )
        listener._handle_barge_in()
        await asyncio.wait_for(tts.done.wait(), timeout=2)
        self.assertEqual(tts.interrupts, 1)

    async def test_no_loop_is_a_noop(self) -> None:
        bus = _Bus()
        engine = self._Engine(active=True)
        bus.engine = engine
        tts = self._RecordingTTS()
        listener = WakeWordListener(
            bus=bus,
            tts=tts,
            on_speech_captured=lambda pcm: None,
            main_loop=None,
        )
        listener._handle_barge_in()
        await asyncio.sleep(0)
        self.assertEqual(engine.interrupts, 0)
        self.assertEqual(tts.interrupts, 0)


if __name__ == "__main__":
    unittest.main()
