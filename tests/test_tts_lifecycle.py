from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import msgpack
import pytest

import gideon


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wav", "pcm"])
async def test_playback_spawn_failure_clears_speaking_state(monkeypatch, mode):
    player = gideon.TTSPlayer(gideon.SignalBus())
    monkeypatch.setattr(gideon.subprocess, "Popen", MagicMock(side_effect=FileNotFoundError("fixture")))
    if mode == "wav":
        await player.play_wav(b"fixture")
    else:
        stream = gideon._FishStream("fixture")
        stream.chunk_queue.put_nowait(b"\0\0")
        await player.play_pcm_stream(stream)
    assert not player._is_speaking
    assert player._current_proc is None


@pytest.mark.asyncio
async def test_close_finishes_the_owned_playback_worker():
    player = gideon.TTSPlayer(gideon.SignalBus())
    player.start_worker()
    worker = player._worker_task
    await asyncio.sleep(0)
    try:
        await player.close()
        assert worker.done()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_wav_playback_reaps_process_and_closes_pipes(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    proc = MagicMock(returncode=None)

    def communicate(data, **kwargs):
        entered.set()
        release.wait(1)
        return None, b""

    def terminate():
        proc.returncode = -15
        release.set()

    proc.communicate.side_effect = communicate
    proc.terminate.side_effect = terminate
    monkeypatch.setattr(gideon.subprocess, "Popen", MagicMock(return_value=proc))
    monkeypatch.setattr(gideon, "_extract_audio_frames", lambda *a, **kw: ([], 0.04))
    player = gideon.TTSPlayer(gideon.SignalBus())
    task = asyncio.create_task(player.play_wav(b"fixture"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        proc.terminate.assert_called_once_with()
        proc.wait.assert_called()
        proc.stdin.close.assert_called()
        proc.stderr.close.assert_called()
        assert not player._is_speaking
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_stops_audio_before_waiting_for_socket_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_socket():
        entered.set()
        await release.wait()

    player = gideon.TTSPlayer(gideon.SignalBus())
    proc = MagicMock()
    player._current_proc = proc
    player._is_speaking = True
    player._warm_ws = SimpleNamespace(close=close_socket)
    task = asyncio.create_task(player.interrupt())
    try:
        await entered.wait()
        proc.terminate.assert_called_once_with()
        assert not player._is_speaking
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_interrupt_waits_for_owned_synthesis_cleanup():
    entered = asyncio.Event()
    cleaned = []

    async def synthesize(text):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    player = gideon.TTSPlayer(gideon.SignalBus())
    player.synthesize = synthesize
    player.queue_sentence("fixture sentence")
    await entered.wait()
    task = next(iter(player._current_synth_tasks))
    try:
        await player.interrupt()
        assert cleaned == [True]
        assert task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _BrokenAfterAudioWS:
    state = SimpleNamespace(name="OPEN")

    async def send(self, data):
        pass

    def __aiter__(self):
        self.count = 0
        return self

    async def __anext__(self):
        self.count += 1
        if self.count == 1:
            return msgpack.packb({"event": "audio", "audio": b"\0\1" * 20})
        raise ConnectionError("fixture stream interruption")

    async def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("reused", [False, True])
async def test_websocket_failure_after_audio_never_retries_speech(monkeypatch, reused):
    ws = _BrokenAfterAudioWS()
    cm = SimpleNamespace(__aenter__=AsyncMock(return_value=ws), __aexit__=AsyncMock())

    class Context:
        async def __aenter__(self):
            return await cm.__aenter__()

        async def __aexit__(self, *args):
            return await cm.__aexit__(*args)

    connect = MagicMock(return_value=Context())
    monkeypatch.setattr("websockets.connect", connect)
    player = gideon.TTSPlayer(gideon.SignalBus())
    player.synthesize = AsyncMock(return_value=b"duplicate-fixture-speech")
    if reused:
        player._warm_ws = ws
        player._warm_ws_ts = time.monotonic()
    stream = gideon._FishStream("fixture sentence")
    stream.task = asyncio.current_task()
    await player._run_ws_stream(stream)
    player.synthesize.assert_not_awaited()
    assert connect.call_count == (0 if reused else 1)
    assert stream.chunk_queue.qsize() == 2


@pytest.mark.asyncio
async def test_unconsumed_pcm_stream_applies_backpressure():
    class ManyAudioWS(_BrokenAfterAudioWS):
        async def __anext__(self):
            self.count += 1
            if self.count > 100:
                raise StopAsyncIteration
            return msgpack.packb({"event": "audio", "audio": b"\0\1" * 20})

    player = gideon.TTSPlayer(gideon.SignalBus())
    stream = gideon._FishStream("fixture")
    task = asyncio.create_task(player._ws_session(ManyAudioWS(), stream, msgpack))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert stream.chunk_queue.qsize() <= 64
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_synthesis_pipeline_limits_concurrent_requests():
    active = []

    async def synthesize(text):
        active.append(text)
        await asyncio.Event().wait()

    player = gideon.TTSPlayer(gideon.SignalBus())
    player.synthesize = synthesize
    for index in range(5):
        player.queue_sentence(f"fixture {index}")
    try:
        await asyncio.sleep(0)
        assert len(active) == 2
    finally:
        await player.interrupt()
        await asyncio.gather(*player._current_synth_tasks, return_exceptions=True)
