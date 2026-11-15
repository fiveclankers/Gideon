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

    async def test_wake_debounce_rejects_single_frame_spike(self):
        """A single-frame score spike must be rejected; sustained frames trigger wake."""
        scores = [0.85, 0.05, 0.90, 0.02]
        model = MagicMock()

        def _predict(_chunk):
            idx = model.predict.call_count - 1
            val = scores[idx] if idx < len(scores) else 0.0
            return {"hey_jarvis_v0.1": val}

        model.predict.side_effect = _predict
        on_wake = MagicMock()
        stream = _FakeInputStream(chunk_size=1280)
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )
        listener = AudioListener(
            wake_threshold=0.45,
            wake_patience_frames=2,
            on_wake_detected=on_wake,
        )
        listener.wakeword_model = model

        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch("audio.wake.play_sound"):
            result = await listener.listen_for_command(
                is_hot_mic=False,
                is_speaking=False,
                is_speaking_checker=lambda: False,
                enabled_checker=lambda: stream.read_calls <= len(scores),
            )

        self.assertIsNone(result)
        on_wake.assert_not_called()

    async def test_wake_debounce_accepts_sustained_frames(self):
        """Consecutive frames meeting wake_patience_frames trigger on_wake_detected."""
        scores = [0.55, 0.62]
        model = MagicMock()

        def _predict(_chunk):
            idx = model.predict.call_count - 1
            val = scores[idx] if idx < len(scores) else 0.0
            return {"hey_jarvis_v0.1": val}

        model.predict.side_effect = _predict
        stream = _FakeInputStream(chunk_size=1280)
        listener = AudioListener(
            wake_threshold=0.45,
            wake_patience_frames=2,
        )
        listener.wakeword_model = model

        def _on_wake():
            listener.stop()

        listener.on_wake_detected = MagicMock(side_effect=_on_wake)
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )

        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch("audio.wake.play_sound"):
            await listener.listen_for_command(
                is_hot_mic=False,
                is_speaking=False,
                is_speaking_checker=lambda: False,
                enabled_checker=lambda: True,
            )

        listener.on_wake_detected.assert_called_once_with()
        model.reset.assert_called_once_with()

    async def test_post_tts_refractory_period_blocks_echo_wake(self):
        """Immediately after TTS finishes speaking, wake predictions are suppressed during the refractory window."""
        speaking_states = [True, False, False]
        model = _FakeWakeModel()
        on_wake = MagicMock()
        stream = _FakeInputStream(chunk_size=1280)
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )
        listener = AudioListener(
            wake_threshold=0.45,
            wake_patience_frames=1,
            post_tts_refractory_s=0.60,
            on_wake_detected=on_wake,
        )
        listener.wakeword_model = model

        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch("audio.wake.play_sound"):
            await listener.listen_for_command(
                is_hot_mic=False,
                is_speaking=True,
                is_speaking_checker=lambda: speaking_states[min(stream.read_calls - 1, len(speaking_states) - 1)],
                enabled_checker=lambda: stream.read_calls <= len(speaking_states),
            )

        on_wake.assert_not_called()

    async def test_whisper_hallucination_filter_and_dismissal_ordering(self):
        """Verify hallucination filter drops silence artifacts, keeps real speech, and preserves 'bye bye' dismissal first."""
        import gideon as gideon_module

        # Must drop whole-utterance hallucinations and punctuation
        for dropped in (
            "you",
            "You.",
            "Thank you.",
            "thanks",
            " . ",
            "...",
            "Thanks for watching!",
            "Thank you for watching.",
            "Thank you, everyone.",
            "bye.",
            "Subtitles by the Amara.org community",
            "Subs by www.zeoranger.co.uk",
            "Please subscribe",
            "$0.00, $0.00, $1.00, $2.00, $1.00.",
        ):
            with self.subTest(dropped=dropped):
                self.assertTrue(gideon_module.is_whisper_hallucination(dropped))
                prepared = await gideon_module._prepare_user_prompt(dropped)
                self.assertIsNone(prepared)

        # Must keep genuine commands and questions
        for kept in (
            "thank you, open firefox",
            "you there?",
            "what is 2 plus 2",
            "play some jazz",
        ):
            with self.subTest(kept=kept):
                self.assertFalse(gideon_module.is_whisper_hallucination(kept))
                prepared = await gideon_module._prepare_user_prompt(kept)
                self.assertIsNotNone(prepared)

        # 'thank you bye bye' must NOT be treated as a hallucination drop; it must trigger dismissal reflex
        self.assertFalse(gideon_module.is_whisper_hallucination("thank you bye bye"))
        bus = MagicMock()
        bus.dismiss_hot_mic = MagicMock()
        tts = MagicMock()
        tts.interrupt = MagicMock(return_value=None)

        with patch("gideon.try_reflex") as mock_try_reflex:
            mock_try_reflex.return_value = True
            prepared = await gideon_module._prepare_user_prompt("thank you bye bye", bus=bus, tts=tts)
            self.assertIsNone(prepared)
            mock_try_reflex.assert_called_once_with("thank you bye bye", bus=bus, tts=tts)

    def test_segment_confidence_filtering(self):
        """Low-confidence faster-whisper segments (high no_speech_prob, low avg_logprob, high compression_ratio) are dropped."""
        from gideon import _filter_stt_segments

        segs = [
            SimpleNamespace(text=" Thank you. ", no_speech_prob=0.85, avg_logprob=-0.3, compression_ratio=1.1),
            SimpleNamespace(text=" garbled ", no_speech_prob=0.10, avg_logprob=-1.45, compression_ratio=1.1),
            SimpleNamespace(text=" loop loop loop ", no_speech_prob=0.10, avg_logprob=-0.3, compression_ratio=3.1),
            SimpleNamespace(text=" open firefox ", no_speech_prob=0.05, avg_logprob=-0.2, compression_ratio=1.0),
        ]
        self.assertEqual(_filter_stt_segments(segs), ["open firefox"])

    def test_hot_mic_not_rearmed_or_extended_after_dropped_capture(self):
        """A dropped capture must not re-arm or extend the hot-mic window, returning to idle or preserving remaining window."""
        bus = SignalBus.__new__(SignalBus)
        bus.current_state = "thinking"
        bus.current_level = 0.0
        bus.clear_waveform = MagicMock()
        bus.set_state = MagicMock()
        wake_listener = WakeWordListener(
            bus=bus,
            tts=SimpleNamespace(_is_speaking=False),
            on_speech_captured=MagicMock(),
        )

        # Case 1: When hot-mic is NOT active (e.g. false wake from idle), a dropped capture returns to idle without arming hot-mic.
        self.assertFalse(wake_listener.is_hot_mic_active())
        wake_listener.restore_state_after_drop()
        self.assertFalse(wake_listener.is_hot_mic_active())
        bus.set_state.assert_called_with("idle")

        # Case 2: When hot-mic IS active, a dropped capture returns orb to 'hotmic' without extending _hot_mic_until.
        wake_listener.arm_hot_mic(timeout_s=45.0)
        original_deadline = wake_listener._hot_mic_until
        bus.set_state.reset_mock()

        # Simulate elapsed time by shifting deadline slightly back
        wake_listener._hot_mic_until = original_deadline - 10.0
        shifted_deadline = wake_listener._hot_mic_until

        wake_listener.restore_state_after_drop()
        self.assertTrue(wake_listener.is_hot_mic_active())
        self.assertEqual(wake_listener._hot_mic_until, shifted_deadline)
        bus.set_state.assert_called_once_with("hotmic")
        wake_listener.disarm_hot_mic()


class TestMidStreamDisarmRegression(unittest.IsolatedAsyncioTestCase):
    async def test_mid_stream_disarm_stops_hot_mic_ingest(self):
        """Regression (2026-11-12 bug): after a mid-stream 'bye bye' disarm, the
        running capture must revert to wake-word watch within one chunk. Loud
        post-dismissal noise must NOT lock capture open or return a buffer."""
        loud = (np.ones((1280, 1), dtype=np.int16) * 12000)
        silent = np.zeros((1280, 1), dtype=np.int16)
        reads = {"n": 0}

        class _GateStream:
            chunk_size = 1280

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _chunk_size):
                reads["n"] += 1
                return (loud if reads["n"] >= 4 else silent), None

        checker_calls = {"n": 0}

        def hot_mic_checker():
            checker_calls["n"] += 1
            # Armed for chunks 1-3, then 'bye bye' disarms it mid-iteration.
            return checker_calls["n"] <= 3

        def enabled_checker():
            return reads["n"] < 40

        on_wake = MagicMock()
        on_barge_in = MagicMock()
        model = _FakeWakeModel()
        listener = AudioListener(
            wake_threshold=2.0,
            on_wake_detected=on_wake,
            on_barge_in=on_barge_in,
        )
        listener.wakeword_model = model

        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: _GateStream(),
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            result = await listener.listen_for_command(
                is_hot_mic=True,
                is_speaking=False,
                silence_timeout_s=1.0,
                max_duration_s=15.0,
                hot_mic_checker=hot_mic_checker,
                is_speaking_checker=lambda: False,
                enabled_checker=enabled_checker,
            )

        # The pre-fix latch would keep hot-mic ingest alive past the disarm and
        # return the loud post-dismissal noise as a captured buffer.
        self.assertIsNone(result)
        self.assertFalse(listener.is_recording_command)
        on_wake.assert_not_called()
        on_barge_in.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
