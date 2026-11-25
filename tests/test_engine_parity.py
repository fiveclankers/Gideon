"""Speech parity between the ACP engine and the legacy headless engine.

Both backends must produce byte-identical spoken output for the same streamed
text. ACP delivers ``session/update`` ``agent_message_chunk`` events; the legacy
engine delivers ``step_update`` ``agent_response`` events. The sentence
splitter, abbreviation protection, cross-chunk stitching, tail flush, and
watchdog cadence are all expected to match.
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acp_antigravity_engine as acp_mod  # noqa: E402
import gideon as gideon_mod  # noqa: E402
from acp_antigravity_engine import ACPAntigravityEngine, _PromptRecord  # noqa: E402
from gideon import AntigravityEngine  # noqa: E402

# Neutral event script replayed into both engines' native dialects. "thought"
# is reasoning the user must never hear.
EVENTS: list[tuple[str, str]] = [
    ("text", "Sure thing. Here is the answer"),
    ("text", " you asked for. That is e.g. a good"),
    ("text", " sign. Dr. Smith said so"),
    ("thought", "I must not speak this reasoning aloud."),
    ("text", " and the word is spl"),
    ("text", "it across chunks."),
]

EXPECTED_SPEECH = [
    "Sure thing.",
    "Here is the answer you asked for.",
    "That is e.g. a good sign.",
    "Dr. Smith said so and the word is split across chunks.",
]


class _Bus:
    def __init__(self) -> None:
        self.current_state = "idle"
        self.current_detail = ""
        self.current_reply = ""
        self.current_user = ""
        self.current_tool = None
        self.current_effort = "low"

    def set_state(self, state: str, detail: str = "") -> None:
        self.current_state = state
        self.current_detail = detail

    def set_user_transcript(self, text: str) -> None:
        self.current_user = text
        self.current_reply = ""

    def clear_transcript(self) -> None:
        self.current_user = ""
        self.current_reply = ""


class _RecordingTTS:
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


def _acp_event(kind: str, text: str) -> dict:
    update = (
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
        if kind == "text"
        else {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": text}}
    )
    return {"update": update}


def _legacy_event(kind: str, text: str) -> dict:
    step_type = "agent_response" if kind == "text" else "agent_thought"
    return {
        "event": "step_update",
        "step_update": {"step_type": step_type, "text_delta": text},
    }


class EngineParityTest(unittest.IsolatedAsyncioTestCase):
    async def _run_acp(self) -> list[str]:
        tts = _RecordingTTS()
        engine = ACPAntigravityEngine(
            _Bus(), tts, preamble="PARITY PREAMBLE", cwd=str(Path.cwd())
        )
        # Stand in for an active turn so chunks are considered audible.
        engine._turn_generation = 0
        engine.is_turn_active = True
        engine._prompt_record = _PromptRecord(generation=0, text="parity")

        for kind, text in EVENTS:
            await engine._on_session_update(_acp_event(kind, text))
        engine._flush_tail()  # ACP flushes the tail on the prompt's terminal
        return list(tts.spoken)

    async def _run_legacy(self) -> list[str]:
        tts = _RecordingTTS()
        engine = AntigravityEngine(_Bus(), tts)
        engine.proc = MagicMock()
        engine.proc.returncode = None
        engine.proc.wait = AsyncMock(return_value=0)
        engine.proc.stdin.drain = AsyncMock()
        engine.proc.stdout = asyncio.StreamReader()

        engine._turn_generation = 0
        engine.is_turn_active = True
        engine._pending_turns.append(0)

        reader = asyncio.create_task(engine._read_loop())
        try:
            for kind, text in EVENTS:
                line = json.dumps(_legacy_event(kind, text)) + "\n"
                engine.proc.stdout.feed_data(line.encode())
            # The terminal result event flushes the tail.
            engine.proc.stdout.feed_data(b'{"event":"result"}\n')
            for _ in range(20):
                await asyncio.sleep(0)
                if engine.sentence_buffer == "" and not engine.is_turn_active:
                    break
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        return list(tts.spoken)

    async def test_same_deltas_produce_identical_sentences(self) -> None:
        acp_spoken = await self._run_acp()
        legacy_spoken = await self._run_legacy()
        self.assertEqual(acp_spoken, legacy_spoken)
        self.assertEqual(acp_spoken, EXPECTED_SPEECH)

    async def test_abbreviations_are_not_split(self) -> None:
        spoken = await self._run_acp()
        self.assertIn("That is e.g. a good sign.", spoken)
        self.assertTrue(any(s.startswith("Dr. Smith") for s in spoken))
        self.assertFalse(any(s.endswith("e.g.") for s in spoken))
        self.assertFalse(any(s == "Dr." for s in spoken))

    async def test_thought_chunks_stay_silent(self) -> None:
        spoken = await self._run_acp()
        self.assertFalse(any("reasoning aloud" in s for s in spoken))
        spoken_legacy = await self._run_legacy()
        self.assertFalse(any("reasoning aloud" in s for s in spoken_legacy))

    async def test_mid_word_boundary_is_stitched(self) -> None:
        spoken = await self._run_acp()
        self.assertTrue(any("split across chunks." in s for s in spoken))
        self.assertFalse(any("spl it" in s for s in spoken))

    async def test_watchdog_cadence_matches(self) -> None:
        self.assertEqual(acp_mod.WATCHDOG_INTERVAL_S, 45.0)
        self.assertEqual(gideon_mod.WATCHDOG_INTERVAL_S, 45.0)
        self.assertEqual(acp_mod.WATCHDOG_INTERVAL_S, gideon_mod.WATCHDOG_INTERVAL_S)


if __name__ == "__main__":
    unittest.main()
