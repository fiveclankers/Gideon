from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# Make the repo root importable when pytest is invoked without ``python -m``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp_antigravity_engine import (  # noqa: E402
    CANCELLING,
    IDLE,
    READY,
    ACPAntigravityEngine,
    DEFAULT_AGY_PROFILE_DIR,
    DEFAULT_AGY_APP_DATA_DIR,
    CURATED_VOICE_SKILLS,
    EXCLUDED_SKILLS,
    ensure_gideon_agy_home,
)
import acp_antigravity_engine as engine_mod  # noqa: E402

_FAKE_AGENT = '''\
import json
import sys
import time

MODE = "basic"
LOG = None
for arg in sys.argv[1:]:
    if arg.startswith("--mode="):
        MODE = arg.split("=", 1)[1]
    elif arg.startswith("--log="):
        LOG = arg.split("=", 1)[1]


def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\\n")
    sys.stdout.flush()


def log(msg):
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg, separators=(",", ":")) + "\\n")


def read_msg():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except ValueError:
        return {}


def respond(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})
    log({"sent_response": {"id": rid, "result": result}})


def chunk(sid, text):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid,
                     "update": {"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": text}}}})


def tool_update(sid, update):
    payload = {"sessionId": sid, "update": update}
    send({"jsonrpc": "2.0", "method": "session/update", "params": payload})


CONFIG = {
    "configOptions": [
        {"id": "thought_level", "name": "Thinking", "type": "select",
         "currentValue": "medium",
         "options": [
             {"value": "low", "name": "Low"},
             {"value": "medium", "name": "Medium"},
             {"value": "high", "name": "High"},
         ]},
        {"id": "model", "name": "Model", "type": "select",
         "currentValue": "gemini-4-hydrogen",
         "options": [
             {"value": "gemini-4-hydrogen", "name": "Hydrogen"},
             {"value": "gemini-3.8-flash-low", "name": "Flash Low"},
             {"value": "gemini-3.8-flash-high", "name": "Flash High"},
             {"value": "custom-model", "name": "Custom"},
         ]},
    ]
}

prompt_id = None
sid = None

while True:
    msg = read_msg()
    if msg is None:
        break
    log(msg)

    if "method" in msg and "id" in msg:
        method = msg["method"]
        rid = msg["id"]
        params = msg.get("params") or {}
        if method == "initialize":
            respond(rid, {
                "protocolVersion": 1,
                "agentInfo": {"name": "fake-agy", "version": "0.0.0"},
                "agentCapabilities": {
                    "loadSession": False,
                    "sessionCapabilities": {"resume": True, "close": True},
                },
            })
        elif method == "session/new":
            sid = "sess-1"
            respond(rid, {"sessionId": sid, "configOptions": CONFIG["configOptions"]})
        elif method == "session/resume":
            sid = params.get("sessionId") or "sess-1"
            respond(rid, {"sessionId": sid, "configOptions": CONFIG["configOptions"]})
        elif method == "session/set_config_option":
            cid = params.get("configId")
            val = params.get("value")
            for opt in CONFIG["configOptions"]:
                if opt["id"] == cid:
                    opt["currentValue"] = val
            respond(rid, {"configOptions": CONFIG["configOptions"]})
        elif method == "session/close":
            respond(rid, {})
        elif method == "session/prompt":
            prompt_id = rid
            if MODE == "chunks":
                chunk(sid, "Hello there. How")
                chunk(sid, " are you today?")
                respond(rid, {"stopReason": "end_turn"})
                prompt_id = None
            elif MODE == "thoughts":
                tool_update(sid, {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Secret reasoning. Do not speak."},
                })
                tool_update(sid, {
                    "sessionUpdate": "tool_call", "toolCallId": "t1",
                    "title": "run_shell", "kind": "execute",
                    "status": "in_progress", "rawInput": {"CommandLine": "ls -la"},
                })
                tool_update(sid, {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1", "status": "completed",
                })
                chunk(sid, "All done now.")
                respond(rid, {"stopReason": "end_turn"})
                prompt_id = None
            elif MODE == "tools_hold":
                tool_update(sid, {
                    "sessionUpdate": "tool_call", "toolCallId": "t1",
                    "title": "run_shell", "kind": "execute",
                    "status": "in_progress", "rawInput": {"CommandLine": "ls -la"},
                })
                tool_update(sid, {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1", "rawOutput": {"stdout": "ok"},
                })
            elif MODE == "slow_cancel":
                chunk(sid, "First part is spoken. ")
            elif MODE == "permission":
                send({"jsonrpc": "2.0", "id": 900,
                      "method": "session/request_permission",
                      "params": {
                          "sessionId": sid,
                          "toolCall": {"toolCallId": "p1", "title": "run_shell"},
                          "options": [
                              {"optionId": "opt-reject", "name": "Reject",
                                "kind": "reject_once"},
                              {"optionId": "opt-allow", "name": "Allow",
                                "kind": "allow_once"},
                          ],
                      }})
            elif MODE == "permission_cancel":
                pass
            elif MODE == "die":
                chunk(sid, "About to die.")
                sys.exit(1)
        else:
            respond(rid, {})

    elif "method" in msg:
        if msg["method"] == "session/cancel" and prompt_id is not None:
            if MODE == "slow_cancel":
                chunk(sid, "This must not be spoken.")
                time.sleep(0.4)
                respond(prompt_id, {"stopReason": "cancelled"})
                prompt_id = None
            elif MODE == "permission_cancel":
                send({"jsonrpc": "2.0", "id": 901,
                      "method": "session/request_permission",
                      "params": {
                          "sessionId": sid,
                          "toolCall": {"toolCallId": "p2", "title": "run_shell"},
                          "options": [
                              {"optionId": "opt-allow", "name": "Allow",
                                "kind": "allow_once"},
                          ],
                      }})
            else:
                respond(prompt_id, {"stopReason": "cancelled"})
                prompt_id = None

    else:
        rid = msg.get("id")
        if rid in (900, 901) and prompt_id is not None:
            respond(prompt_id, {"stopReason": "end_turn"})
            prompt_id = None
'''


class _Bus:
    """Lightweight SignalBus stand-in that records calls."""

    def __init__(self) -> None:
        self.current_state = "idle"
        self.current_detail = ""
        self.current_reply = ""
        self.current_user = ""
        self.current_tool = None
        self.current_effort = "medium"
        self.states: list[tuple[str, str]] = []
        self.user_transcripts: list[str] = []
        self.transcript_clears = 0

    def set_state(self, state: str, detail: str = "") -> None:
        self.current_state = state
        self.current_detail = detail
        self.states.append((state, detail))

    def set_user_transcript(self, text: str) -> None:
        self.current_user = text
        self.current_reply = ""
        self.user_transcripts.append(text)

    def clear_transcript(self) -> None:
        self.current_user = ""
        self.current_reply = ""
        self.transcript_clears += 1


class _TTS:
    """TTSPlayer stand-in with the attributes the engine touches."""

    def __init__(self) -> None:
        self._is_speaking = False
        self._queue: queue.Queue = queue.Queue()
        self.spoken: list[str] = []
        self.interrupts = 0
        self.waits = 0
        self.prewarms = 0
        self.drained = asyncio.Event()

    async def prewarm_ws(self) -> None:
        self.prewarms += 1

    def queue_sentence(self, sentence: str) -> None:
        self.spoken.append(sentence)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def wait_until_done(self) -> None:
        self.waits += 1
        self.drained.set()


class _Wake:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def arm_hot_mic(self, timeout_s: float) -> None:
        self.calls.append(timeout_s)


def _clean_text_for_tts(raw: str):
    text = " ".join(str(raw or "").split()).strip()
    return text or None


def _gideon_stub() -> types.ModuleType:
    module = types.ModuleType("gideon")
    module.clean_text_for_tts = _clean_text_for_tts
    module.HOT_MIC_WINDOW_S = 45.0
    return module


class _TestEngine(ACPAntigravityEngine):
    """Engine whose argv is a fake ACP agent instead of ``agy acp``."""

    def __init__(self, *args, argv: list[str], **kwargs) -> None:
        self._test_argv = list(argv)
        super().__init__(*args, **kwargs)

    def _build_argv(self) -> list[str]:
        return list(self._test_argv)


class ACPAntigravityEngineTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.agent_path = Path(cls._tmpdir.name) / "fake_agy.py"
        cls.agent_path.write_text(_FAKE_AGENT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    async def asyncSetUp(self) -> None:
        self._gideon_patch = mock.patch.dict(sys.modules, {"gideon": _gideon_stub()})
        self._gideon_patch.start()
        self.addCleanup(self._gideon_patch.stop)
        self.bus = _Bus()
        self.tts = _TTS()
        self.wake = _Wake()
        self._engines: list[ACPAntigravityEngine] = []

    async def asyncTearDown(self) -> None:
        for engine in self._engines:
            try:
                await asyncio.wait_for(engine.close(), timeout=15)
            except BaseException:  # noqa: BLE001 - cleanup must never mask a failure
                pass

    def _log_path(self) -> Path:
        return Path(self._tmpdir.name) / f"log_{self._testMethodName}_{len(self._engines)}.jsonl"

    async def _start(self, mode: str, **kwargs) -> ACPAntigravityEngine:
        log_path = self._log_path()
        argv = [
            sys.executable,
            "-u",
            str(self.agent_path),
            f"--mode={mode}",
            f"--log={log_path}",
        ]
        engine = _TestEngine(
            self.bus,
            self.tts,
            preamble="TEST PREAMBLE",
            cwd=self._tmpdir.name,
            argv=argv,
            **kwargs,
        )
        engine.wake_listener = self.wake
        self._engines.append(engine)
        self.log_path = log_path
        await asyncio.wait_for(engine.start(), timeout=20)
        self.assertEqual(engine._conn_state, READY)
        return engine

    def _read_log(self) -> list[dict]:
        path = getattr(self, "log_path", None)
        if path is None or not path.exists():
            return []
        messages: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except ValueError:
                pass
        return messages

    async def _wait_for(self, predicate, timeout: float = 10.0) -> None:
        async def _poll() -> None:
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_poll(), timeout=timeout)

    # ------------------------------------------------------------------ tests

    def test_build_argv_defaults_and_env_flags(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            engine = ACPAntigravityEngine(self.bus, self.tts, preamble="P", cwd=self._tmpdir.name)
            argv = engine._build_argv()
            self.assertEqual(argv[:2], ["agy", "acp"])
            self.assertIn("--gemini-dir", argv)
            self.assertEqual(argv[argv.index("--gemini-dir") + 1], str(DEFAULT_AGY_PROFILE_DIR))
            self.assertIn("--app-data-dir", argv)
            self.assertEqual(argv[argv.index("--app-data-dir") + 1], DEFAULT_AGY_APP_DATA_DIR)
            self.assertIn("--include-default-customizations=false", argv)
            self.assertNotIn("--dangerously-skip-permissions", argv)

        with mock.patch.dict(os.environ, {"GIDEON_AGY_DEFAULT_CUSTOMIZATIONS": "1"}):
            self.assertNotIn("--include-default-customizations=false", engine._build_argv())

        with mock.patch.dict(os.environ, {"GIDEON_AGY_ACP_SKIP_PERMISSIONS": "1"}):
            self.assertIn("--dangerously-skip-permissions", engine._build_argv())

        with mock.patch.dict(os.environ, {
            "GIDEON_AGY_GEMINI_DIR": "/custom/gemini",
            "GIDEON_AGY_APP_DATA_DIR": "/custom/appdata",
        }):
            custom_argv = engine._build_argv()
            self.assertIn("--gemini-dir", custom_argv)
            self.assertEqual(custom_argv[custom_argv.index("--gemini-dir") + 1], "/custom/gemini")
            self.assertIn("--app-data-dir", custom_argv)
            self.assertEqual(custom_argv[custom_argv.index("--app-data-dir") + 1], "/custom/appdata")

    def test_ensure_gideon_agy_home(self) -> None:
        test_profile = Path(self._tmpdir.name) / "test_agy_home"
        test_source_gemini = Path(self._tmpdir.name) / "source_gemini"
        test_source_gemini.mkdir(parents=True, exist_ok=True)
        (test_source_gemini / "oauth_creds.json").write_text('{"token": "dummy"}', encoding="utf-8")
        (test_source_gemini / "installation_id").write_text("inst-123", encoding="utf-8")

        test_skills_src = Path(self._tmpdir.name) / "skills_src"
        test_skills_src.mkdir(parents=True, exist_ok=True)
        (test_skills_src / "gideon-doctor").mkdir()
        (test_skills_src / "gideon-music").mkdir()
        (test_skills_src / "frontend-design").mkdir()

        # Run ensure_gideon_agy_home
        res_dir = ensure_gideon_agy_home(
            test_profile,
            app_data_dir="test-cli",
            source_gemini_dir=test_source_gemini,
            curated_skills=("gideon-doctor", "gideon-music"),
            search_dirs=(test_skills_src,),
        )
        self.assertEqual(res_dir, test_profile.resolve())
        self.assertTrue((test_profile / "settings.json").is_file())
        self.assertTrue((test_profile / "test-cli" / "settings.json").is_file())

        # Verify auth files symlinked
        self.assertTrue((test_profile / "oauth_creds.json").exists())
        self.assertTrue((test_profile / "installation_id").exists())

        # Verify curated skills symlinked
        self.assertTrue((test_profile / "skills" / "gideon-doctor").exists())
        self.assertTrue((test_profile / "skills" / "gideon-music").exists())
        # Excluded skills must NOT be symlinked
        self.assertFalse((test_profile / "skills" / "frontend-design").exists())

        # Put an excluded skill in skills to test pruning
        (test_profile / "skills" / "impeccable").mkdir()
        self.assertTrue((test_profile / "skills" / "impeccable").exists())
        # Re-run ensure_gideon_agy_home to verify idempotence and pruning
        ensure_gideon_agy_home(
            test_profile,
            app_data_dir="test-cli",
            source_gemini_dir=test_source_gemini,
            curated_skills=("gideon-doctor", "gideon-music"),
            search_dirs=(test_skills_src,),
        )
        self.assertFalse((test_profile / "skills" / "impeccable").exists())

    async def test_start_initializes_then_creates_session_with_preamble(self) -> None:
        await self._start("basic")
        log = self._read_log()
        methods = [m.get("method") for m in log if "method" in m]
        self.assertIn("initialize", methods)
        self.assertIn("session/new", methods)
        self.assertLess(methods.index("initialize"), methods.index("session/new"))

        init = next(m for m in log if m.get("method") == "initialize")
        self.assertEqual(init["params"]["protocolVersion"], 1)
        caps = init["params"]["clientCapabilities"]
        self.assertIs(caps["fs"]["readTextFile"], False)
        self.assertIs(caps["fs"]["writeTextFile"], False)
        self.assertIs(caps["terminal"], False)

        new = next(m for m in log if m.get("method") == "session/new")
        params = new["params"]
        self.assertEqual(
            params["_meta"]["antigravity"]["systemPrompt"]["append"],
            "TEST PREAMBLE",
        )
        self.assertEqual(params["mcpServers"], [])
        self.assertTrue(Path(params["cwd"]).is_absolute())

    async def test_model_selection_via_config_option(self) -> None:
        with mock.patch.dict(os.environ, {"GIDEON_AGY_MODEL": "custom-model"}):
            await self._start("basic")
            log = self._read_log()
            model_configs = [
                m for m in log
                if m.get("method") == "session/set_config_option"
                and m.get("params", {}).get("configId") == "model"
            ]
            self.assertEqual(len(model_configs), 1)
            self.assertEqual(model_configs[0]["params"]["value"], "custom-model")

    async def test_prewarm_ws_and_display_text_parity(self) -> None:
        engine = await self._start("chunks")
        await asyncio.wait_for(
            engine.send_prompt("Hidden system prompt content", display_text="User visible question"),
            timeout=5,
        )
        self.assertGreaterEqual(self.tts.prewarms, 1)
        self.assertEqual(self.bus.current_user, "User visible question")
        self.assertIn("User visible question", self.bus.user_transcripts)
        await self._wait_for(lambda: self.wake.calls)

    async def test_directive_classifier_parity(self) -> None:
        def classifier(text: str) -> str:
            return "[Directive: Terse Spoken Response]"

        engine = await self._start("chunks", directive_classifier=classifier)
        await asyncio.wait_for(
            engine.send_prompt("What is the time?", display_text="What is the time?"),
            timeout=5,
        )
        log = self._read_log()
        prompt_call = next(m for m in log if m.get("method") == "session/prompt")
        prompt_text = prompt_call["params"]["prompt"][0]["text"]
        self.assertIn("[Directive: Terse Spoken Response]", prompt_text)
        self.assertIn("User: What is the time?", prompt_text)

    async def test_agent_message_chunks_are_split_and_queued_in_order(self) -> None:
        engine = await self._start("chunks")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: self.wake.calls)
        self.assertEqual(self.tts.spoken, ["Hello there.", "How are you today?"])

    async def test_thought_and_tool_output_are_not_spoken(self) -> None:
        engine = await self._start("thoughts")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: self.wake.calls)
        self.assertEqual(self.tts.spoken, ["All done now."])
        self.assertFalse(any("Secret" in line for line in self.tts.spoken))

    async def test_cancel_is_a_notification_and_settles_on_prompt_response(
        self,
    ) -> None:
        engine = await self._start("slow_cancel")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: "First part is spoken." in self.tts.spoken)

        interrupt_task = asyncio.create_task(engine.interrupt())
        await asyncio.sleep(0.15)
        self.assertFalse(interrupt_task.done())
        self.assertEqual(engine._turn_state, CANCELLING)
        self.assertFalse(engine.is_turn_active)

        acknowledged = await asyncio.wait_for(interrupt_task, timeout=10)
        self.assertTrue(acknowledged)
        self.assertEqual(engine._turn_state, IDLE)

        log = self._read_log()
        cancel_index = next(
            index
            for index, message in enumerate(log)
            if message.get("method") == "session/cancel"
        )
        cancel = log[cancel_index]
        self.assertNotIn("id", cancel)
        self.assertEqual(cancel["params"]["sessionId"], "sess-1")

        terminal_index = next(
            index
            for index, message in enumerate(log)
            if (message.get("sent_response") or {}).get("result", {}).get(
                "stopReason"
            )
            == "cancelled"
        )
        self.assertLess(cancel_index, terminal_index)

    async def test_chunk_after_cancel_is_not_spoken(self) -> None:
        engine = await self._start("slow_cancel")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: "First part is spoken." in self.tts.spoken)
        await asyncio.wait_for(engine.interrupt(), timeout=10)
        self.assertNotIn("This must not be spoken.", self.tts.spoken)

    async def test_set_effort_high_is_sent_and_max_is_rejected(self) -> None:
        engine = await self._start("basic")
        self.assertEqual(engine.effort, "medium")
        self.assertEqual(self.bus.current_effort, "medium")

        self.assertTrue(await asyncio.wait_for(engine.set_effort("high"), timeout=5))
        log = self._read_log()
        configs = [m for m in log if m.get("method") == "session/set_config_option" and m.get("params", {}).get("configId") == "thought_level"]
        # One at startup (medium), one for the explicit switch to high.
        self.assertEqual(
            [c["params"]["value"] for c in configs], ["medium", "high"]
        )
        self.assertEqual(configs[-1]["params"]["configId"], "thought_level")
        self.assertEqual(engine.effort, "high")
        self.assertEqual(self.bus.current_effort, "high")

        self.assertFalse(await asyncio.wait_for(engine.set_effort("max"), timeout=5))
        log = self._read_log()
        configs = [m for m in log if m.get("method") == "session/set_config_option" and m.get("params", {}).get("configId") == "thought_level"]
        self.assertEqual(len(configs), 2)
        self.assertEqual(engine.effort, "high")
        self.assertEqual(self.bus.current_effort, "high")
        self.assertTrue(
            any("headless" in line for line in self.tts.spoken),
            f"expected a headless-engine notice, got {self.tts.spoken}",
        )

    async def test_tool_records_merge_and_watchdog_sees_running(self) -> None:
        engine = await self._start("tools_hold")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: engine.current_tool is not None)
        await self._wait_for(lambda: engine._tools.get("t1", {}).get("rawOutput"))

        record = engine._tools["t1"]
        self.assertEqual(record["title"], "run_shell")
        self.assertEqual(record["rawInput"], {"CommandLine": "ls -la"})
        self.assertEqual(record["status"], "running")
        self.assertEqual(engine.current_tool["status"], "running")
        self.assertEqual(engine.current_tool["label"], "run_shell: ls -la")
        self.assertEqual(engine.current_tool["target"], "shell")

        # Drive the watchdog directly at a short interval: it must see the
        # running tool and speak a check-in.
        engine._stop_watchdog()
        with mock.patch.object(engine_mod, "WATCHDOG_INTERVAL_S", 0.01):
            watchdog = asyncio.create_task(engine._watchdog_loop())
            try:
                await self._wait_for(
                    lambda: any("Still running" in s for s in self.tts.spoken)
                )
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
        self.assertTrue(
            any(
                line.startswith("Still running run_shell: ls -la,")
                for line in self.tts.spoken
            ),
            self.tts.spoken,
        )

        self.assertTrue(await asyncio.wait_for(engine.interrupt(), timeout=10))

    async def test_terminal_result_flushes_tail_and_rearms_hot_mic(self) -> None:
        engine = await self._start("chunks")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: self.wake.calls)
        self.assertEqual(self.tts.spoken, ["Hello there.", "How are you today?"])
        self.assertEqual(self.wake.calls, [45.0])
        self.assertGreaterEqual(self.tts.waits, 1)
        self.assertEqual(engine._turn_state, IDLE)
        self.assertFalse(engine.is_turn_active)

    async def test_permission_request_answered_once_with_advertised_option(
        self,
    ) -> None:
        engine = await self._start("permission")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(lambda: self.wake.calls)
        log = self._read_log()
        answers = [
            m for m in log if "method" not in m and m.get("id") == 900
        ]
        self.assertEqual(len(answers), 1)
        self.assertEqual(
            answers[0]["result"],
            {"outcome": {"outcome": "selected", "optionId": "opt-allow"}},
        )

    async def test_permission_during_cancelled_turn_is_answered_cancelled(self) -> None:
        engine = await self._start("permission_cancel")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        self.assertTrue(await asyncio.wait_for(engine.interrupt(), timeout=10))
        await self._wait_for(
            lambda: any(
                "method" not in m and m.get("id") == 901
                for m in self._read_log()
            )
        )
        log = self._read_log()
        answers = [m for m in log if "method" not in m and m.get("id") == 901]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["result"], {"outcome": {"outcome": "cancelled"}})

    async def test_eof_mid_prompt_settles_and_recovers(self) -> None:
        engine = await self._start("die")
        await asyncio.wait_for(engine.send_prompt("hi"), timeout=5)
        await self._wait_for(
            lambda: engine._conn_state == READY and engine._turn_state == IDLE,
            timeout=15,
        )
        self.assertFalse(engine.is_turn_active)
        self.assertIsNone(engine._prompt_record)
        prompts = [m for m in self._read_log() if m.get("method") == "session/prompt"]
        self.assertEqual(len(prompts), 1)


if __name__ == "__main__":
    unittest.main()
