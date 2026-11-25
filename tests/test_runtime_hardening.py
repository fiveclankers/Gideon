from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import decision_router
import gideon_shared
import reflex_router
import selected_context
from codex_engine import CodexEngine, _JsonRpcStdioTransport
from gideon_handler import GideonDispatcher
from handlers_study_passthrough import evaluate_risk_gate


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", "hundred " * 200], ids=["nan", "infinity", "overflow", "spoken-overflow"])
def test_spoken_number_rejects_nonfinite_values(value):
    assert gideon_shared.parse_number(value) is None


def test_sync_command_timeout_does_not_wait_for_descendant_pipe(tmp_path):
    pid_file = tmp_path / "sync-child-pid"
    program = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.8)']); "
        f"Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(5)"
    )
    started = time.monotonic()
    try:
        assert gideon_shared.run_cmd([sys.executable, "-c", program], timeout=0.15)[0] == 124
        assert time.monotonic() - started < 0.65
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass


def test_sync_command_timeout_kills_the_owned_process_group(monkeypatch):
    proc = MagicMock(pid=912345)
    proc.__enter__.return_value = proc
    proc.communicate.side_effect = subprocess.TimeoutExpired("fixture", 0.1)
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=proc))
    kill_group = MagicMock()
    monkeypatch.setattr(os, "killpg", kill_group)
    assert gideon_shared.run_cmd(["fixture-command"], timeout=0.1)[0] == 124
    kill_group.assert_called_once_with(proc.pid, 9)
    proc.wait.assert_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [gideon_shared.run_cmd_async, reflex_router._exec_raw])
async def test_command_cancellation_reaps_owned_child(monkeypatch, runner):
    entered = asyncio.Event()

    async def communicate():
        entered.set()
        await asyncio.Event().wait()

    proc = SimpleNamespace(communicate=communicate, returncode=None, kill=MagicMock(), wait=AsyncMock(return_value=-9))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    task = asyncio.create_task(runner(["fixture-command"], 10))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited()


@pytest.mark.asyncio
async def test_command_timeout_does_not_wait_for_descendant_pipe(tmp_path):
    pid_file = tmp_path / "child-pid"
    program = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(0.8)']); "
        f"Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(5)"
    )
    started = time.monotonic()
    try:
        result = await gideon_shared.run_cmd_async([sys.executable, "-c", program], timeout=0.15)
        assert result[0] == 124
        assert time.monotonic() - started < 0.65
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass


def test_clipboard_pipe_read_has_real_deadline_and_closes_pipe(monkeypatch):
    popen = subprocess.Popen
    proc = popen([sys.executable, "-c", "import time; print('fixture text',flush=True); time.sleep(0.35)"], stdout=subprocess.PIPE)
    monkeypatch.setattr(selected_context.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(selected_context, "PASTE_TIMEOUT_S", 0.04)
    started = time.monotonic()
    try:
        assert selected_context._read_paste_command([]) is None
        assert time.monotonic() - started < 0.25
        assert proc.stdout.closed
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def test_clipboard_eof_waits_for_successful_exit(monkeypatch):
    popen = subprocess.Popen
    proc = popen([sys.executable, "-c", "import os,time; os.write(1,b'fixture text'); os.close(1); time.sleep(0.03)"], stdout=subprocess.PIPE)
    monkeypatch.setattr(selected_context.subprocess, "Popen", lambda *a, **kw: proc)
    try:
        assert selected_context._read_paste_command([]) == "fixture text"
        assert proc.stdout.closed
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.5])
def test_risk_gate_rejects_invalid_probabilities(confidence):
    for tier in ("safe", "confirm", "dangerous"):
        assert evaluate_risk_gate("system_shutdown", confidence, tier)[0] is False


def test_decision_fallback_accepts_real_bge_numpy_probabilities():
    result = decision_router.GeminiDecisionDispatcher._as_probability_dict(
        np.array([0.7, 0.3]), {0: "conversation", 1: "media_play"}
    )
    assert result == {"conversation": 0.7, "media_play": 0.3}


@pytest.mark.parametrize("choice,confidence", [("unlisted_intent", 0.99), ("conversation", float("nan")), ("conversation", 1.5)])
def test_decision_rejects_out_of_contract_answer(choice, confidence):
    dispatcher = decision_router.GeminiDecisionDispatcher.__new__(decision_router.GeminiDecisionDispatcher)
    dispatcher.criteria = {"conversation": "Talk"}
    dispatcher._post = lambda payload: {"answers": {"intent": {"type": "choice", "choice": choice, "confidence": confidence}}}
    expected = ("conversation", 0.9, {"conversation": 0.9})
    dispatcher._fallback_predict = MagicMock(return_value=expected)
    assert dispatcher.predict("fixture utterance") == expected
    dispatcher._fallback_predict.assert_called_once_with("fixture utterance")


@pytest.mark.parametrize("choice,confidence,modal", [("not-a-candidate", 0.9, 0.1), ("read", float("nan"), 0.1), ("read", 0.9, float("inf"))])
def test_browser_decision_validates_candidate_and_probabilities(choice, confidence, modal):
    dispatcher = decision_router.GeminiDecisionDispatcher.__new__(decision_router.GeminiDecisionDispatcher)
    dispatcher._post = lambda payload: {"answers": {
        "candidate_action": {"type": "choice", "choice": choice, "confidence": confidence},
        "has_blocking_modal": {"noul": modal}, "goal_complete": {"noul": 0.0},
    }}
    with pytest.raises(decision_router.DecisionAPIError):
        dispatcher.decide_browser_step("fixture goal", "fixture DOM", {"read": "Read text"})


def test_invalid_retry_after_uses_normal_cooldown():
    dispatcher = decision_router.GeminiDecisionDispatcher.__new__(decision_router.GeminiDecisionDispatcher)
    dispatcher._key_lock = threading.Lock()
    dispatcher._rate_limit_strikes = {}
    dispatcher._cooldown_until = {}
    duration = dispatcher._record_rate_limit("fixture", SimpleNamespace(headers={"Retry-After": "NaN"}))
    assert duration == decision_router.DEFAULT_COOLDOWN_S


def test_api_error_does_not_include_untrusted_response_body():
    dispatcher = decision_router.GeminiDecisionDispatcher.__new__(decision_router.GeminiDecisionDispatcher)
    dispatcher._refresh_keys = lambda: None
    dispatcher._take_key = lambda attempted: "fixture-key" if not attempted else None
    dispatcher.api_keys = ["fixture-key"]
    dispatcher._client = SimpleNamespace(post=MagicMock(return_value=SimpleNamespace(status_code=400, text="fixture-sensitive-header")))
    with pytest.raises(decision_router.DecisionAPIError) as error:
        dispatcher._post({})
    assert "fixture-sensitive-header" not in str(error.value)


@pytest.mark.asyncio
async def test_dispatch_classification_runs_off_event_loop_thread():
    dispatcher = GideonDispatcher.__new__(GideonDispatcher)
    dispatcher.schema = {"conversation": {"risk_tier": "safe", "slots": {}}}
    threads = []

    def predict(text):
        threads.append(threading.get_ident())
        return "conversation", 0.99, np.array([0.99])

    dispatcher.predict = predict
    await dispatcher.dispatch("fixture utterance", dry_run=True)
    assert threads and threads[0] != threading.get_ident()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.TimeoutError, asyncio.CancelledError])
async def test_hypr_socket_is_closed_on_read_failure(monkeypatch, error):
    reader = SimpleNamespace(read=AsyncMock(side_effect=error))
    writer = SimpleNamespace(write=MagicMock(), drain=AsyncMock(), close=MagicMock(), wait_closed=AsyncMock())
    monkeypatch.setattr(reflex_router, "ensure_hyprland_env", lambda: "fixture-signature")
    monkeypatch.setattr("reflex_router.Path.exists", lambda self: True)
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(reader, writer)))
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await reflex_router.hypr_socket_dispatch("fixture")
    else:
        assert (await reflex_router.hypr_socket_dispatch("fixture"))[0] == 1
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited()


@pytest.mark.asyncio
async def test_cancelled_codex_request_drops_pending_future():
    transport = _JsonRpcStdioTransport([], ".")
    transport.proc = SimpleNamespace(returncode=None, stdin=object())
    transport._write_frame = AsyncMock()
    task = asyncio.create_task(transport.request("fixture-request"))
    await asyncio.sleep(0)
    assert len(transport._pending) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not transport._pending


@pytest.mark.asyncio
async def test_cancelled_codex_start_closes_partial_transport():
    transport = SimpleNamespace(start=AsyncMock(), request=AsyncMock(side_effect=asyncio.CancelledError), close=AsyncMock())
    engine = CodexEngine(SimpleNamespace(current_effort=""), SimpleNamespace())
    engine._make_transport = lambda: transport
    with pytest.raises(asyncio.CancelledError):
        await engine.start()
    transport.close.assert_awaited_once_with()
    assert engine._transport is None


@pytest.mark.asyncio
async def test_codex_ignores_malformed_response_id():
    transport = _JsonRpcStdioTransport([], ".")
    pending = asyncio.get_running_loop().create_future()
    transport._pending[1] = pending
    try:
        await transport._dispatch_message({"id": [], "result": {}})
        assert transport._pending[1] is pending
    finally:
        pending.cancel()


@pytest.mark.asyncio
async def test_codex_turn_started_ignores_foreign_thread():
    engine = CodexEngine(SimpleNamespace(), SimpleNamespace())
    engine.is_turn_active = True
    engine._thread_id = "current-thread"
    await engine._on_notification("turn/started", {"threadId": "old-thread", "turn": {"id": "old-turn"}})
    assert engine._active_turn_id is None
