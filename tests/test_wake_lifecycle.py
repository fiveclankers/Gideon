import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from audio.wake import AudioListener, StreamingSileroVad, VadWhisperWakeEngine
from gideon import WakeWordListener


class Stream:
    def __init__(self, on_read=None, loud=False):
        self.n = 0
        self.closed = False
        self.on_read = on_read
        self.loud = loud

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size):
        self.n += 1
        if self.on_read:
            self.on_read(self.n)
        if self.loud:
            return (np.sin(np.arange(size) * 0.3) * 16000).astype(np.int16)[:, None], False
        return np.full((size, 1), self.n, dtype=np.int16), False


class Model:
    def __init__(self, detect=None):
        self.vad_n = 0
        self.clips = []
        self.detect = detect
        self.resets = 0

    def reset(self):
        self.resets += 1

    def vad_prob(self, chunk):
        self.vad_n += 1
        return 0.9 if (self.vad_n - 1) % 6 < 3 else 0.0

    def detect_wake(self, clip, require_prefix=False):
        self.clips.append((clip.copy(), require_prefix))
        if self.detect:
            return self.detect(clip, require_prefix)
        return "Hey Gideon", "Hey Gideon, pause music."


def setup_stream(monkeypatch, stream):
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=lambda **kw: stream))
    monkeypatch.setattr("audio.wake.play_sound", MagicMock())


@pytest.mark.asyncio
async def test_stream_error_clears_recording_state(monkeypatch):
    def fail(n):
        if n == 4:
            raise OSError("device lost")

    stream = Stream(fail, loud=True)
    setup_stream(monkeypatch, stream)
    listener = AudioListener()
    listener.wakeword_model = Model()
    with pytest.raises(OSError):
        await listener.listen_for_command(True, False)
    assert not listener.is_recording_command
    assert stream.closed


@pytest.mark.asyncio
async def test_cancelled_capture_closes_stream_and_clears_state(monkeypatch):
    task = asyncio.current_task()
    stream = Stream(lambda n: task.cancel() if n == 4 else None, loud=True)
    setup_stream(monkeypatch, stream)
    listener = AudioListener()
    listener.wakeword_model = Model()
    with pytest.raises(asyncio.CancelledError):
        await listener.listen_for_command(True, False)
    assert stream.closed
    assert not listener.is_recording_command


@pytest.mark.asyncio
async def test_stop_discards_partial_command(monkeypatch):
    listener = AudioListener()
    stream = Stream(lambda n: listener.stop() if n == 9 else None, loud=True)
    setup_stream(monkeypatch, stream)
    listener.wakeword_model = Model()
    result = await listener.listen_for_command(True, False)
    assert result is None
    assert not listener.is_recording_command


@pytest.mark.asyncio
async def test_wake_preroll_has_no_duplicate_onset_frame(monkeypatch):
    stream = Stream()
    setup_stream(monkeypatch, stream)
    listener = AudioListener(mic_gain=1.0)
    model = Model()
    listener.wakeword_model = model
    result = await listener.listen_for_command(False, False, enabled_checker=lambda: stream.n < 30)
    assert result is not None
    clip = model.clips[0][0]
    expected = np.repeat(np.arange(1, 7, dtype=np.float32) / 32768, 1280)
    np.testing.assert_array_equal(clip, expected)
    # Every frame through result collection must be returned exactly once.
    np.testing.assert_array_equal(result, np.repeat(np.arange(1, stream.n + 1), 1280))
    listener.close()


@pytest.mark.asyncio
async def test_tts_start_invalidates_idle_result_without_overlapping_workers(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    active = [0, 0]
    speaking = [False]

    def detect(clip, prefix):
        active[0] += 1
        active[1] = max(active)
        started.set()
        release.wait(2)
        active[0] -= 1
        return (None, "Gideon") if prefix else ("Gideon", "Gideon")

    def on_read(n):
        if n == 8:
            speaking[0] = True
        if n == 16:
            release.set()

    stream = Stream(on_read)
    setup_stream(monkeypatch, stream)
    barge = MagicMock()
    listener = AudioListener(on_barge_in=barge)
    listener.wakeword_model = Model(detect)
    try:
        await listener.listen_for_command(False, False, is_speaking_checker=lambda: speaking[0],
                                          enabled_checker=lambda: stream.n < 32)
    finally:
        release.set()
        if hasattr(listener, "close"):
            listener.close()
    assert started.is_set()
    assert active[1] == 1
    barge.assert_not_called()


@pytest.mark.asyncio
async def test_armed_hotmic_still_detects_prefixed_barge_in(monkeypatch):
    speaking = [True]
    stream = Stream()
    setup_stream(monkeypatch, stream)
    barge = MagicMock(side_effect=lambda: speaking.__setitem__(0, False))
    listener = AudioListener(on_barge_in=barge, post_tts_refractory_s=0)
    model = Model()
    listener.wakeword_model = model
    try:
        await listener.listen_for_command(True, True, is_speaking_checker=lambda: speaking[0],
                                          enabled_checker=lambda: stream.n < 40)
    finally:
        if hasattr(listener, "close"):
            listener.close()
    barge.assert_called_once_with()
    assert model.clips[0][1] is True


def test_silero_rejects_unsupported_sample_rate_before_loading():
    with pytest.raises(ValueError, match="sample rate"):
        StreamingSileroVad(sample_rate=44100)


def test_silero_context_does_not_retain_or_alias_input():
    vad = StreamingSileroVad.__new__(StreamingSileroVad)
    vad._ctx, vad._hop = 64, 512
    vad._sr_arr = np.array(16000, dtype=np.int64)
    vad._sess = SimpleNamespace(run=lambda *a: (np.array([[0.5]]), np.ones((2, 1, 128))))
    vad.reset()
    audio = np.ones(512, dtype=np.float32)
    vad.max_speech_prob(audio)
    audio[:] = 0
    np.testing.assert_array_equal(vad._context, np.ones(64))


def test_cuda_warmup_failure_uses_bounded_cpu_and_reports_actual_precision(monkeypatch):
    calls = []

    def create(*args, **kw):
        calls.append(kw)

        def transcribe(*a, **k):
            if kw["device"] == "cuda":
                raise RuntimeError("missing CUDA kernel")
            return iter(()), None

        return SimpleNamespace(transcribe=transcribe)

    monkeypatch.setattr("audio.wake._preload_cuda_libs_for_wake", lambda: None)
    monkeypatch.setattr("audio.wake.StreamingSileroVad", lambda **kw: Model())
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=create))
    engine = VadWhisperWakeEngine(device="cuda", compute_type="float16")
    assert engine.device == "cpu"
    assert engine.compute_type == "int8"
    assert calls[-1]["cpu_threads"] == 2
    assert len(calls) == 2


def test_stale_hotmic_timer_cannot_disarm_new_window(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, interval, callback):
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    clock = [100.0]
    monkeypatch.setattr("gideon.threading.Timer", Timer)
    monkeypatch.setattr("gideon.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("gideon.time.time", lambda: clock[0])
    listener = WakeWordListener(MagicMock(), SimpleNamespace(_is_speaking=False), MagicMock())
    listener.arm_hot_mic(1)
    old = timers[0]
    listener.arm_hot_mic(2)
    clock[0] += 3
    old.callback()
    assert listener._hot_mic_armed
    timers[1].callback()
    assert not listener._hot_mic_armed


def test_hotmic_expiry_rechecks_after_active_capture(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, interval, callback):
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    clock = [100.0]
    monkeypatch.setattr("gideon.threading.Timer", Timer)
    monkeypatch.setattr("gideon.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("gideon.time.time", lambda: clock[0])
    listener = WakeWordListener(MagicMock(), SimpleNamespace(_is_speaking=False), MagicMock())
    listener._listener_instance = SimpleNamespace(is_recording_command=True, hot_mic_timeout=0)
    listener.arm_hot_mic(1)
    clock[0] += 2
    timers[0].callback()
    assert len(timers) == 2
    listener._listener_instance.is_recording_command = False
    timers[1].callback()
    assert not listener._hot_mic_armed


def test_wake_thread_shutdown_closes_owned_listener(monkeypatch):
    host = WakeWordListener(MagicMock(), SimpleNamespace(_is_speaking=False), MagicMock())
    child = MagicMock()

    async def listen(**kw):
        host._running = False
        assert kw["enabled_checker"]() is False

    child.listen_for_command = listen
    monkeypatch.setattr("audio.wake.AudioListener", lambda **kw: child)
    host._running = True
    host._run_loop()
    child.close.assert_called_once_with()


def test_silero_hop_alignment_and_reset_for_80ms_chunks():
    seen = []
    vad = StreamingSileroVad.__new__(StreamingSileroVad)
    vad._ctx, vad._hop = 64, 512
    vad._sr_arr = np.array(16000)

    def run(names, inputs):
        seen.append(inputs["input"][0, 64:].copy())
        return np.array([[0.4]]), inputs["state"] + 1

    vad._sess = SimpleNamespace(run=run)
    vad.reset()
    audio = np.arange(2560, dtype=np.float32)
    vad.max_speech_prob(audio[:1280])
    assert len(vad._pending) == 256
    vad.max_speech_prob(audio[1280:])
    np.testing.assert_array_equal(np.concatenate(seen), audio)
    assert len(vad._pending) == 0
    vad.reset()
    assert not np.any(vad._state)
    assert not np.any(vad._context)


@pytest.mark.asyncio
async def test_live_probe_closes_listener_on_cancellation(monkeypatch):
    from scripts import test_wake_mic

    listener = MagicMock()

    async def listen():
        raise asyncio.CancelledError

    listener.listen_for_command = listen
    monkeypatch.setattr(test_wake_mic, "AudioListener", lambda **kw: listener)
    with pytest.raises(asyncio.CancelledError):
        await test_wake_mic._listen(SimpleNamespace(vad_threshold=0.35))
    listener.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_sustained_singing_burst_does_not_repeatedly_fire_wake_checks(monkeypatch):
    """Sustained vocal bursts (singing) without leading wake words only check once on initial peek
    and do not repeatedly trigger wake checks across the singing line until silence resets state.
    """
    detect_calls = []

    class SingingModel:
        def __init__(self):
            self.resets = 0
            self.chunk_idx = 0

        def reset(self):
            self.resets += 1

        def vad_prob(self, chunk):
            self.chunk_idx += 1
            # Chunks 1..35: continuous singing (voiced speech)
            # Chunks 36..40: silence gap (5 chunks < wake_threshold)
            # Chunks 41..55: user says "Hey Gideon, play music" (voiced speech)
            if self.chunk_idx <= 35:
                return 0.90
            elif self.chunk_idx <= 40:
                return 0.05
            else:
                return 0.90

        def detect_wake(self, clip, require_prefix=False):
            detect_calls.append((self.chunk_idx, clip.copy(), require_prefix))
            if self.chunk_idx <= 38:
                # User is singing lyrics, no wake word
                return None, "Wise men say only fools rush in"
            else:
                # User spoke true positive wake phrase
                return "Hey Gideon", "Hey Gideon, play music"

    stream = Stream()
    setup_stream(monkeypatch, stream)
    on_wake = MagicMock()
    listener = AudioListener(on_wake_detected=on_wake)
    model = SingingModel()
    listener.wakeword_model = model

    try:
        await listener.listen_for_command(
            is_hot_mic=False,
            is_speaking=False,
            is_speaking_checker=lambda: False,
            enabled_checker=lambda: stream.n <= 55,
        )
    finally:
        listener.close()

    # During the 35 continuous voiced chunks of singing, detect_wake should be called AT MOST ONCE (initial peek)
    singing_checks = [c for c in detect_calls if c[0] <= 38]
    assert len(singing_checks) == 1, f"Expected 1 peek check during singing, got {len(singing_checks)}: {singing_checks}"

    # After silence gap (chunks 36-40), the subsequent speech burst must trigger wake detection
    assert on_wake.called, "Wake word was not detected after silence reset"
    post_silence_checks = [c for c in detect_calls if c[0] > 38]
    assert len(post_silence_checks) >= 1, "Expected wake check after silence reset"

