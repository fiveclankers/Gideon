from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

from audio.wake import AudioListener
from gideon import SignalBus, WakeWordListener


class _FakeInputStream:
    def __init__(self, chunk_size: int):
        self.chunk_size = chunk_size
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _chunk_size):
        self.read_calls += 1
        return np.zeros((self.chunk_size, 1), dtype=np.int16), None


class _FakeWakeModel:
    def __init__(self):
        self.predict_calls = 0
        self.reset_calls = 0

    def predict(self, _audio_chunk):
        self.predict_calls += 1
        return {"hey_jarvis": 0.99}

    def reset(self):
        self.reset_calls += 1


class TestAudioWakeGating(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_gate_skips_wake_prediction_and_callbacks(self):
        """A disabled wake listener must not turn audio into an activation."""
        model = _FakeWakeModel()
        on_wake = MagicMock()
        on_barge_in = MagicMock()
        stream = _FakeInputStream(chunk_size=1280)
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )
        listener = AudioListener(
            on_wake_detected=on_wake,
            on_barge_in=on_barge_in,
        )
        listener.wakeword_model = model

        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            result = await listener.listen_for_command(
                is_hot_mic=False,
                is_speaking=False,
                is_speaking_checker=lambda: False,
                enabled_checker=lambda: False,
            )

        self.assertIsNone(result)
        self.assertEqual(model.predict_calls, 0)
        on_wake.assert_not_called()
        on_barge_in.assert_not_called()
        self.assertFalse(listener.is_recording_command)

    def test_disabling_wake_listener_stops_active_capture_and_hot_mic(self):
        bus = SignalBus.__new__(SignalBus)
        bus.current_state = "hotmic"
        bus.current_level = 0.5
        bus.clear_waveform = MagicMock()
        bus.set_state = MagicMock()
        wake_listener = WakeWordListener(
            bus=bus,
            tts=SimpleNamespace(_is_speaking=False),
            on_speech_captured=MagicMock(),
        )
        wake_listener._listener_instance = MagicMock()
        wake_listener.arm_hot_mic(timeout_s=10.0)

        wake_listener.set_enabled(False)

        self.assertFalse(wake_listener.enabled)
        self.assertFalse(wake_listener.is_hot_mic_active())
        wake_listener._listener_instance.stop.assert_called_once_with()
        bus.clear_waveform.assert_called()
        bus.set_state.assert_called_with("idle")

        wake_listener.arm_hot_mic(timeout_s=10.0)
        self.assertFalse(wake_listener.is_hot_mic_active())


if __name__ == "__main__":
    unittest.main(verbosity=2)
