from __future__ import annotations

import asyncio
import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import codex_engine as ce  # noqa: E402
import gideon as gideon_mod  # noqa: E402
from codex_engine import (  # noqa: E402
    CODEX_EFFORT_LEVELS,
    CodexEngine,
    _format_tool_meta,
    _unwrap_shell_command,
    ensure_gideon_codex_home,
)


class _Bus:
    def __init__(self) -> None:
        self.current_state = "idle"
        self.current_detail = ""
        self.current_reply = ""
        self.current_user = ""
        self.current_tool = None
        self.current_effort = "medium"
        self.engine_name = "codex"
        self.state_history: list[tuple[str, str]] = []

    def set_state(self, state: str, detail: str = "") -> None:
        self.current_state = state
        self.current_detail = detail
        self.state_history.append((state, detail))

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


class _FakeTransport:
    def __init__(self) -> None:
        self.is_alive = True
        self.proc = None
        self.requests: list[tuple[str, dict]] = []
        self.turn_counter = 0
        self.started = False
        self.closed = False
        self.on_notification = None

    async def start(self) -> None:
        self.started = True
        self.is_alive = True

    async def request(self, method: str, params=None, timeout: float = 20.0):
        self.requests.append((method, dict(params or {})))
        if method == "initialize":
            return {"userAgent": "codex-stub"}
        if method == "thread/start":
            return {"thread": {"id": "thr_test_1"}}
        if method == "turn/start":
            self.turn_counter += 1
            return {"turn": {"id": f"turn_{self.turn_counter}", "status": "inProgress"}}
        if method == "turn/interrupt":
            return {}
        return {}

    async def close(self) -> None:
        self.is_alive = False
        self.closed = True


class _StubCodexEngine(CodexEngine):
    def __init__(self, *args, transport: _FakeTransport, **kwargs) -> None:
        self._stub_transport = transport
        super().__init__(*args, **kwargs)

    def _make_transport(self):
        self._stub_transport.on_notification = self._on_notification
        return self._stub_transport


class TestCodexHomeAndFormatting(unittest.TestCase):
    def test_isolated_codex_home_writes_lean_config_without_ticktick(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src_codex"
            dst = root / "gideon_codex"
            src.mkdir()
            (src / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
            (src / "models_cache.json").write_text("{}", encoding="utf-8")

            home = ensure_gideon_codex_home(
                dst,
                cwd="/home/cat/gideon",
                model="gpt-6-luna",
                effort="xhigh",
                source_codex_dir=src,
            )
            self.assertEqual(home, dst)
            self.assertTrue((dst / "auth.json").exists())
            self.assertTrue((dst / "models_cache.json").exists())

            cfg = (dst / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-6-luna"', cfg)
            self.assertIn('model_reasoning_effort = "xhigh"', cfg)
            self.assertIn('model_reasoning_summary = "none"', cfg)
            self.assertIn('model_verbosity = "low"', cfg)
            self.assertIn('web_search = "live"', cfg)
            self.assertIn("hooks = false", cfg)
            self.assertIn("apps = false", cfg)
            self.assertIn("[mcp_servers.second-brain]", cfg)
            self.assertIn("[mcp_servers.agent-browser]", cfg)
            self.assertNotIn("ticktick", cfg.lower())
            self.assertNotIn("node_repl", cfg.lower())

    def test_unwrap_and_format_tool_meta(self):
        self.assertEqual(
            _unwrap_shell_command("/usr/bin/bash -lc '~/.local/bin/music status --json'"),
            "~/.local/bin/music status --json",
        )
        shell_meta = _format_tool_meta({
            "type": "commandExecution",
            "command": "/usr/bin/bash -lc 'echo hello'",
        })
        self.assertEqual(shell_meta["target"], "shell")
        self.assertIn("echo hello", shell_meta["label"])

        sb_meta = _format_tool_meta({
            "type": "mcpToolCall",
            "server": "second-brain",
            "tool": "search_second_brain",
            "arguments": {"query": "physics backlog"},
        })
        self.assertEqual(sb_meta["target"], "core")
        self.assertIn("physics backlog", sb_meta["label"])

        browser_meta = _format_tool_meta({
            "type": "mcpToolCall",
            "server": "agent-browser",
            "tool": "browser_open",
            "arguments": {"url": "https://example.com"},
        })
        self.assertEqual(browser_meta["target"], "web")

        ws_meta = _format_tool_meta({"type": "webSearch", "query": "Python 3.15"})
        self.assertEqual(ws_meta["target"], "web")
        self.assertIn("Python 3.15", ws_meta["label"])

        self.assertIsNone(_format_tool_meta({"type": "reasoning"}))
        self.assertIsNone(_format_tool_meta({"type": "userMessage"}))

    def test_resolve_codex_bin_finds_mise_binary_under_stripped_systemd_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            mise_bin = fake_home / ".local" / "share" / "mise" / "installs" / "codex" / "latest" / "bin" / "codex"
            mise_bin.parent.mkdir(parents=True)
            mise_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            mise_bin.chmod(0o755)
            with (
                patch.dict("os.environ", {"PATH": "/nonexistent_bin_dir"}),
                patch("codex_engine.Path.home", return_value=fake_home),
            ):
                self.assertEqual(ce._resolve_codex_bin("codex"), str(mise_bin))
                env = ce._build_codex_env(fake_home / "codex_home")
                self.assertIn(str(mise_bin.parent), env["PATH"].split(":"))

    def test_clean_text_preserves_multidigit_numeric_answers(self):
        self.assertEqual(gideon_mod.clean_text_for_tts("210"), "210")
        self.assertEqual(gideon_mod.clean_text_for_tts("210."), "210.")
        self.assertIsNone(gideon_mod.clean_text_for_tts("12"))


class TestCodexEngineBehavior(unittest.IsolatedAsyncioTestCase):
    async def test_factory_defaults_to_codex_xhigh_and_keeps_agy_fallback(self):
        bus = _Bus()
        tts = _TTS()
        codex_eng = gideon_mod._make_engine("codex", bus, tts)
        self.assertIsInstance(codex_eng, CodexEngine)
        self.assertEqual(codex_eng.name, "codex")
        self.assertEqual(codex_eng.effort, "xhigh")
        self.assertIn("never read `SKILL.md`", codex_eng.preamble)
        self.assertIn("NEVER read `codemap.md`", codex_eng.preamble)
        self.assertNotIn("invoke_subagent", codex_eng.preamble)

        agy_eng = gideon_mod._make_engine("agy", bus, tts)
        self.assertIsInstance(agy_eng, gideon_mod.AntigravityEngine)
        self.assertEqual(agy_eng.name, "agy")

    async def test_streaming_speech_tool_status_and_reasoning_isolation(self):
        bus = _Bus()
        tts = _TTS()
        transport = _FakeTransport()
        engine = _StubCodexEngine(
            bus,
            tts,
            transport=transport,
            preamble=gideon_mod.CODEX_VOICE_PREAMBLE,
            cwd="/home/cat/gideon",
            clean_text_fn=gideon_mod.clean_text_for_tts,
            sentence_end_re=gideon_mod._SENTENCE_END,
        )
        await engine.start()
        self.assertEqual(engine.conversation_id, "thr_test_1")

        init_req, thread_req = transport.requests
        self.assertEqual(init_req[0], "initialize")
        self.assertEqual(thread_req[0], "thread/start")
        self.assertEqual(thread_req[1]["developerInstructions"], gideon_mod.CODEX_VOICE_PREAMBLE)

        await engine.send_prompt("Check Second Brain and tell me Dr. Smith's note.")
        tid = "thr_test_1"
        turn_id = "turn_1"

        await engine._on_notification(
            "item/started",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "rs_1", "type": "reasoning"}},
        )
        await engine._on_notification(
            "item/reasoning/textDelta",
            {"threadId": tid, "turnId": turn_id, "delta": "Secret thought that must never be spoken."},
        )
        await engine._on_notification(
            "item/started",
            {
                "threadId": tid,
                "turnId": turn_id,
                "item": {
                    "id": "mcp_1",
                    "type": "mcpToolCall",
                    "server": "second-brain",
                    "tool": "search_second_brain",
                    "arguments": {"query": "Dr. Smith"},
                },
            },
        )
        self.assertEqual(bus.current_state, "toolcall")
        await engine._on_notification(
            "item/completed",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "mcp_1", "type": "mcpToolCall"}},
        )
        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "msg_1", "delta": "Dr. Smith left a note e.g. review optics. "},
        )
        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "msg_1", "delta": "See [this doc](https://example.com) tomorrow!"},
        )
        await engine._on_notification(
            "turn/completed",
            {"threadId": tid, "turn": {"id": turn_id, "status": "completed"}},
        )
        if engine._finish_task:
            await engine._finish_task

        self.assertEqual(
            tts.spoken,
            ["Dr. Smith left a note e.g. review optics.", "See this doc tomorrow!"],
        )
        self.assertFalse(engine.is_turn_active)
        self.assertEqual(bus.current_state, "idle")
        await engine.close()

    async def test_multi_item_agent_messages_flush_without_glued_sentences(self):
        bus = _Bus()
        tts = _TTS()
        transport = _FakeTransport()
        engine = _StubCodexEngine(
            bus,
            tts,
            transport=transport,
            preamble=gideon_mod.CODEX_VOICE_PREAMBLE,
            cwd="/home/cat/gideon",
            clean_text_fn=gideon_mod.clean_text_for_tts,
            sentence_end_re=gideon_mod._SENTENCE_END,
        )
        await engine.start()
        await engine.send_prompt("Check open tabs.")
        tid, turn_id = "thr_test_1", "turn_1"

        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "msg_pre", "delta": "Checking your tabs now."},
        )
        await engine._on_notification(
            "item/completed",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "msg_pre", "type": "agentMessage"}},
        )
        # Pre-tool acknowledgment should already be flushed to TTS before tool finishes.
        self.assertEqual(tts.spoken, ["Checking your tabs now."])

        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "msg_post", "delta": "You have two tabs open."},
        )
        await engine._on_notification(
            "item/completed",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "msg_post", "type": "agentMessage"}},
        )
        await engine._on_notification(
            "turn/completed",
            {"threadId": tid, "turnId": turn_id, "turn": {"id": turn_id, "status": "completed"}},
        )
        if engine._finish_task:
            await engine._finish_task

        self.assertEqual(tts.spoken, ["Checking your tabs now.", "You have two tabs open."])
        self.assertEqual(bus.current_reply, "Checking your tabs now. You have two tabs open.")
        await engine.close()

    async def test_commentary_phase_is_spoken_without_polluting_final_answer(self):
        bus = _Bus()
        tts = _TTS()
        transport = _FakeTransport()
        engine = _StubCodexEngine(
            bus,
            tts,
            transport=transport,
            preamble=gideon_mod.CODEX_VOICE_PREAMBLE,
            cwd="/home/cat/gideon",
            clean_text_fn=gideon_mod.clean_text_for_tts,
            sentence_end_re=gideon_mod._SENTENCE_END,
        )
        await engine.start()
        await engine.send_prompt("Inspect Downloads.")
        tid, turn_id = "thr_test_1", "turn_1"

        await engine._on_notification(
            "item/started",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "msg_c", "type": "agentMessage", "phase": "commentary"}},
        )
        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "msg_c", "delta": "I’m checking the Downloads folder now."},
        )
        await engine._on_notification(
            "item/completed",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "msg_c", "type": "agentMessage", "phase": "commentary", "text": "I’m checking the Downloads folder now."}},
        )
        self.assertEqual(tts.spoken, ["I’m checking the Downloads folder now."])
        self.assertEqual(bus.current_reply, "")

        await engine._on_notification(
            "item/started",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "msg_f", "type": "agentMessage", "phase": "final_answer"}},
        )
        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": tid, "turnId": turn_id, "itemId": "msg_f", "delta": "Only the x86_64 AppImage is needed."},
        )
        await engine._on_notification(
            "item/completed",
            {"threadId": tid, "turnId": turn_id, "item": {"id": "msg_f", "type": "agentMessage", "phase": "final_answer"}},
        )
        await engine._on_notification(
            "turn/completed",
            {"threadId": tid, "turnId": turn_id, "turn": {"id": turn_id, "status": "completed"}},
        )
        if engine._finish_task:
            await engine._finish_task

        self.assertEqual(tts.spoken, ["I’m checking the Downloads folder now.", "Only the x86_64 AppImage is needed."])
        self.assertEqual(bus.current_reply, "Only the x86_64 AppImage is needed.")
        self.assertEqual(transport.requests[-1][1]["summary"], "none")
        await engine.close()

    async def test_interrupt_stops_stale_turn_and_set_effort_never_respawns(self):
        bus = _Bus()
        tts = _TTS()
        transport = _FakeTransport()
        engine = _StubCodexEngine(
            bus,
            tts,
            transport=transport,
            preamble=gideon_mod.CODEX_VOICE_PREAMBLE,
            cwd="/home/cat/gideon",
            clean_text_fn=gideon_mod.clean_text_for_tts,
            sentence_end_re=gideon_mod._SENTENCE_END,
        )
        await engine.start()
        await engine.send_prompt("First prompt")
        ok = await engine.interrupt()
        self.assertTrue(ok)
        self.assertEqual(transport.requests[-1][0], "turn/interrupt")
        self.assertEqual(transport.requests[-1][1], {"threadId": "thr_test_1", "turnId": "turn_1"})

        # Switching effort updates subsequent turns with 0 process respawns.
        start_count_before = len([m for m, _ in transport.requests if m == "thread/start"])
        for level in CODEX_EFFORT_LEVELS:
            self.assertTrue(await engine.set_effort(level))
        self.assertFalse(await engine.set_effort("invalid-level"))
        await engine.set_effort("high")
        self.assertEqual(bus.current_effort, "high")
        start_count_after = len([m for m, _ in transport.requests if m == "thread/start"])
        self.assertEqual(start_count_before, start_count_after)

        await engine.send_prompt("Second prompt")
        # Late deltas or completion from cancelled turn_1 must be dropped.
        await engine._on_notification(
            "item/agentMessage/delta",
            {"threadId": "thr_test_1", "turnId": "turn_1", "itemId": "old", "delta": "Stale text."},
        )
        await engine._on_notification(
            "turn/completed",
            {"threadId": "thr_test_1", "turn": {"id": "turn_1", "status": "completed"}},
        )
        self.assertEqual(tts.spoken, [])
        self.assertTrue(engine.is_turn_active)
        self.assertEqual(transport.requests[-1][0], "turn/start")
        self.assertEqual(transport.requests[-1][1]["effort"], "high")
        await engine.close()


if __name__ == "__main__":
    unittest.main()
