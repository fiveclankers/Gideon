from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import unittest
from math import gcd
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
from scipy.signal import resample_poly

from audio.wake import AudioListener, VadWhisperWakeEngine, _extract_command_remainder
from gideon import SignalBus, WakeWordListener


class _FakeInputStream:
    def __init__(self, chunk_size: int, sample_value: int = 0):
        self.chunk_size = chunk_size
        self.sample_value = sample_value
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _chunk_size):
        self.read_calls += 1
        return np.full((self.chunk_size, 1), self.sample_value, dtype=np.int16), None


class _FakeVadWhisperModel:
    def __init__(self, vad_probs=None, detect_result=("Hey Gideon", "Hey Gideon.")):
        self.vad_probs = list(vad_probs) if vad_probs is not None else [0.0]
        self.detect_result = detect_result
        self.vad_calls = 0
        self.detect_calls = 0
        self.reset_calls = 0
        self.received_vad_chunks: list[np.ndarray] = []
        self.received_detect_args: list[tuple[np.ndarray, bool]] = []

    def vad_prob(self, raw_float: np.ndarray) -> float:
        idx = self.vad_calls
        self.vad_calls += 1
        self.received_vad_chunks.append(raw_float.copy())
        if idx < len(self.vad_probs):
            return self.vad_probs[idx]
        return self.vad_probs[-1] if self.vad_probs else 0.0

    def detect_wake(self, float_audio: np.ndarray, require_prefix: bool = False) -> tuple[str | None, str]:
        self.detect_calls += 1
        self.received_detect_args.append((float_audio.copy(), require_prefix))
        return self.detect_result

    def reset(self) -> None:
        self.reset_calls += 1


class TestAudioWakeGating(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_gate_skips_wake_prediction_and_callbacks(self):
        """A disabled wake listener must not turn audio into an activation."""
        model = _FakeVadWhisperModel(vad_probs=[0.95] * 5)
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
        self.assertEqual(model.vad_calls, 0)
        self.assertEqual(model.detect_calls, 0)
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

    async def test_vad_burst_triggers_whisper_wake_and_resets_state(self):
        """A voiced VAD burst followed by a brief pause triggers Whisper wake detection and on_wake_detected."""
        # 4 voiced chunks -> 3 silent chunks triggers detect_wake; subsequent iterations observe task completion.
        model = _FakeVadWhisperModel(
            vad_probs=[0.90, 0.92, 0.91, 0.88, 0.05, 0.04, 0.03, 0.01, 0.01, 0.01],
            detect_result=("Hey Gideon", "Hey Gideon."),
        )
        stream = _FakeInputStream(chunk_size=1280)
        listener = AudioListener(wake_threshold=0.35)
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
                enabled_checker=lambda: stream.read_calls <= 30,
            )

        listener.on_wake_detected.assert_called_once_with()
        self.assertEqual(model.detect_calls, 1)
        self.assertGreaterEqual(model.reset_calls, 1)

    async def test_continuous_voiced_singing_does_not_retrigger_wake_peeks(self):
        """A continuous voiced speech burst (singing) triggers at most one initial peek (~1.3s in).
        If the initial peek is not a wake word, subsequent continuous singing chunks do NOT re-trigger
        detect_wake until at least 3 silence chunks occur.
        """
        # 30 voiced chunks continuously, then 3 silent chunks, then 4 voiced chunks + silence with wake word.
        vad_probs = [0.9] * 30 + [0.0] * 3 + [0.9] * 4 + [0.0] * 10
        detect_results = [
            (None, "I can't help falling in love with you"),  # initial peek of song fails
            ("Hey Gideon", "Hey Gideon."),                    # second phrase after silence succeeds
        ]
        detect_call_count = [0]

        def _detect(clip, prefix=False):
            res = detect_results[min(detect_call_count[0], len(detect_results) - 1)]
            detect_call_count[0] += 1
            return res

        model = _FakeVadWhisperModel(vad_probs=vad_probs)
        model.detect_wake = MagicMock(side_effect=_detect)
        stream = _FakeInputStream(chunk_size=1280)
        listener = AudioListener(wake_threshold=0.35)

        def _on_wake():
            listener.stop()

        listener.on_wake_detected = MagicMock(side_effect=_on_wake)
        listener.wakeword_model = model

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
                enabled_checker=lambda: stream.read_calls <= len(vad_probs),
            )

        # In 30 continuous voiced chunks, detect_wake should have been called EXACTLY ONCE (the initial peek).
        # It should NOT be called every 1.1s across the 30 chunks!
        # Then, after the 3 silence chunks, the new phrase (4 voiced + 3 silence) triggered the 2nd detect_wake.
        self.assertEqual(model.detect_wake.call_count, 2)
        listener.on_wake_detected.assert_called_once_with()

    async def test_complete_one_shot_command_returns_buffer_without_chime(self):
        """A complete one-shot utterance ('Hey Gideon, play some jazz') returns immediately with acknowledged.wav."""
        model = _FakeVadWhisperModel(
            vad_probs=[0.90, 0.92, 0.91, 0.88, 0.85, 0.05, 0.04, 0.03, 0.01, 0.01],
            detect_result=("Hey Gideon", "Hey Gideon, play some jazz."),
        )
        stream = _FakeInputStream(chunk_size=1280)
        on_wake = MagicMock()
        listener = AudioListener(wake_threshold=0.35, on_wake_detected=on_wake)
        listener.wakeword_model = model
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )

        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch("audio.wake.play_sound") as play_snd:
            result = await listener.listen_for_command(
                is_hot_mic=False,
                is_speaking=False,
                is_speaking_checker=lambda: False,
                enabled_checker=lambda: stream.read_calls <= 30,
            )

        self.assertIsNotNone(result)
        on_wake.assert_called_once_with()
        play_snd.assert_called_with("acknowledged.wav")

    async def test_vad_receives_raw_unclipped_float32_audio(self):
        """Silero VAD must receive raw unclipped float32 audio even when mic_gain > 1.0."""
        model = _FakeVadWhisperModel(vad_probs=[0.0])
        stream = _FakeInputStream(chunk_size=1280, sample_value=24576)
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )
        listener = AudioListener(mic_gain=2.0)
        listener.wakeword_model = model

        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            await listener.listen_for_command(
                is_hot_mic=False,
                is_speaking=False,
                is_speaking_checker=lambda: False,
                enabled_checker=lambda: stream.read_calls < 2,
            )

        self.assertGreaterEqual(len(model.received_vad_chunks), 1)
        chunk = model.received_vad_chunks[0]
        self.assertEqual(chunk.dtype, np.float32)
        np.testing.assert_allclose(chunk, 24576.0 / 32768.0, atol=1e-5)

    async def test_post_tts_refractory_period_blocks_echo_wake(self):
        """Immediately after TTS finishes speaking, wake predictions are suppressed during the refractory window."""
        speaking_states = [True, False, False, False, False, False, False, False]
        model = _FakeVadWhisperModel(
            vad_probs=[0.0, 0.95, 0.95, 0.95, 0.05, 0.05, 0.05, 0.05],
            detect_result=("Hey Gideon", "Hey Gideon."),
        )
        on_wake = MagicMock()
        on_barge_in = MagicMock()
        stream = _FakeInputStream(chunk_size=1280)
        sounddevice = SimpleNamespace(
            InputStream=lambda **kwargs: stream,
            _terminate=MagicMock(),
            _initialize=MagicMock(),
        )
        listener = AudioListener(
            post_tts_refractory_s=0.60,
            on_wake_detected=on_wake,
            on_barge_in=on_barge_in,
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
        self.assertEqual(model.detect_calls, 0)
        self.assertGreaterEqual(model.reset_calls, 1)

    def test_extract_command_remainder(self):
        self.assertEqual(_extract_command_remainder("Hey Gideon."), "")
        self.assertEqual(_extract_command_remainder("Hey Gideon, play some jazz."), "play some jazz")
        self.assertEqual(_extract_command_remainder("Okay Gideon turn up the volume!"), "turn up the volume")

    async def test_whisper_hallucination_filter_and_dismissal_ordering(self):
        """Verify hallucination filter drops silence artifacts, keeps real speech, and preserves 'bye bye' dismissal first."""
        import gideon as gideon_module

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

        self.assertFalse(wake_listener.is_hot_mic_active())
        wake_listener.restore_state_after_drop()
        self.assertFalse(wake_listener.is_hot_mic_active())
        bus.set_state.assert_called_with("idle")

        wake_listener.arm_hot_mic(timeout_s=45.0)
        original_deadline = wake_listener._hot_mic_until
        bus.set_state.reset_mock()

        wake_listener._hot_mic_until = original_deadline - 10.0
        shifted_deadline = wake_listener._hot_mic_until

        wake_listener.restore_state_after_drop()
        self.assertTrue(wake_listener.is_hot_mic_active())
        self.assertEqual(wake_listener._hot_mic_until, shifted_deadline)
        bus.set_state.assert_called_once_with("hotmic")
        wake_listener.disarm_hot_mic()


class TestWakeWordPrefixAndConfidenceGating(unittest.TestCase):
    def setUp(self):
        self.engine = VadWhisperWakeEngine.__new__(VadWhisperWakeEngine)
        self.engine.sample_rate = 16000
        self.engine._whisper_lock = threading.Lock()
        self.engine.initial_prompt = ""
        self.dummy_audio = np.zeros(8000, dtype=np.float32)

    def test_bare_or_mid_sentence_gideon_rejected(self):
        """Bare 'Gideon' or mid-sentence 'Gideon' without a leading wake greeting prefix is rejected."""
        rejected_phrases = (
            "Gideon",
            "Gideon.",
            "gideon",
            "Thank you Gideon.",
            "I saw Gideon yesterday",
            "The book is about Gideon",
            "What about Elvis Presley? Thank you Gideon.",
            "Singing along with Gideon",
            "can't help falling in love with Gideon",
            "Wise men say only fools rush in for Gideon",
        )
        for phrase in rejected_phrases:
            for require_prefix in (False, True):
                with self.subTest(phrase=phrase, require_prefix=require_prefix):
                    seg = SimpleNamespace(
                        text=phrase,
                        no_speech_prob=0.01,
                        avg_logprob=-0.15,
                        compression_ratio=1.0,
                    )
                    self.engine._whisper = SimpleNamespace(
                        transcribe=lambda *args, **kwargs: ([seg], None)
                    )
                    matched, transcript = self.engine.detect_wake(self.dummy_audio, require_prefix=require_prefix)
                    self.assertIsNone(
                        matched,
                        f"Expected {phrase!r} (require_prefix={require_prefix}) to be rejected, but matched {matched!r}",
                    )

    def test_true_positive_wake_greetings_accepted(self):
        """Leading wake greeting prefixes ('Hey Gideon', 'Hi Gideon', 'Okay Gideon', one-shot commands) are accepted."""
        accepted_phrases = (
            ("Hey Gideon", "Hey Gideon"),
            ("Hey Gideon.", "Hey Gideon"),
            ("Hi Gideon", "Hi Gideon"),
            ("Okay Gideon", "Okay Gideon"),
            ("Ok Gideon", "Ok Gideon"),
            ("Hey Gideon, play some jazz", "Hey Gideon"),
            ("Hi Gideon, what time is it?", "Hi Gideon"),
            ("Okay Gideon, turn off lights", "Okay Gideon"),
            ("Uh, hey Gideon", "hey Gideon"),
            ("Um, hi Gideon, pause music", "hi Gideon"),
        )
        for phrase, _expected in accepted_phrases:
            with self.subTest(phrase=phrase):
                seg = SimpleNamespace(
                    text=phrase,
                    no_speech_prob=0.01,
                    avg_logprob=-0.15,
                    compression_ratio=1.0,
                )
                self.engine._whisper = SimpleNamespace(
                    transcribe=lambda *args, **kwargs: ([seg], None)
                )
                matched, transcript = self.engine.detect_wake(self.dummy_audio, require_prefix=False)
                self.assertIsNotNone(
                    matched,
                    f"Expected {phrase!r} to match, but got None (transcript={transcript!r})",
                )
                self.assertEqual(transcript, phrase)

    def test_low_confidence_whisper_hallucinations_rejected_by_detect_wake(self):
        """Whisper hallucination segments during singing/music (high no_speech_prob, low avg_logprob, high compression_ratio) are rejected."""
        hallucination_segments = [
            # Low avg_logprob (< -0.85)
            SimpleNamespace(text="Hey Gideon", no_speech_prob=0.10, avg_logprob=-1.25, compression_ratio=1.0),
            SimpleNamespace(text="Hey Gideon", no_speech_prob=0.10, avg_logprob=-0.95, compression_ratio=1.0),
            # High no_speech_prob (>= 0.55)
            SimpleNamespace(text="Hey Gideon", no_speech_prob=0.70, avg_logprob=-0.20, compression_ratio=1.0),
            SimpleNamespace(text="Hey Gideon", no_speech_prob=0.60, avg_logprob=-0.20, compression_ratio=1.0),
            # High compression_ratio (> 2.2)
            SimpleNamespace(text="Hey Gideon", no_speech_prob=0.10, avg_logprob=-0.20, compression_ratio=2.6),
            SimpleNamespace(text="Hey Gideon", no_speech_prob=0.10, avg_logprob=-0.20, compression_ratio=2.3),
        ]
        for seg in hallucination_segments:
            with self.subTest(seg=seg):
                self.engine._whisper = SimpleNamespace(
                    transcribe=lambda *args, **kwargs: ([seg], None)
                )
                matched, transcript = self.engine.detect_wake(self.dummy_audio)
                self.assertIsNone(
                    matched,
                    f"Expected hallucination {seg} to be rejected, but matched {matched!r}",
                )
                self.assertEqual(transcript, "")

        # High confidence segment must pass
        clean_seg = SimpleNamespace(text="Hey Gideon", no_speech_prob=0.05, avg_logprob=-0.20, compression_ratio=1.0)
        self.engine._whisper = SimpleNamespace(
            transcribe=lambda *args, **kwargs: ([clean_seg], None)
        )
        matched, transcript = self.engine.detect_wake(self.dummy_audio)
        self.assertIsNotNone(matched)
        self.assertEqual(transcript, "Hey Gideon")


class TestVadWhisperIntegration(unittest.TestCase):
    def test_real_silero_whisper_detects_hey_gideon_and_rejects_noise(self):
        """Real end-to-end check on local Silero VAD + Faster-Whisper base:
        - Must detect synthesized 'Hey Gideon', 'Hi Gideon', and 'Okay Gideon'.
        - Must NOT false-trigger on silence, noise, or unrelated speech.
        """
        try:
            import espeakng_loader
        except ImportError:
            self.skipTest("espeakng_loader not installed")

        engine = VadWhisperWakeEngine()

        # Silence & white noise must not fire Silero VAD or wake regex
        rng = np.random.default_rng(123)
        for label, clip in (
            ("silence", np.zeros(24000, dtype=np.float32)),
            ("room_noise", rng.normal(0, 0.004, 24000).astype(np.float32)),
        ):
            engine.reset()
            max_p = max(engine.vad_prob(clip[i : i + 1280]) for i in range(0, len(clip), 1280))
            self.assertLess(max_p, engine.vad_threshold, f"VAD tripped on {label}: {max_p}")

        # Synthesize real audio with espeak-ng
        lib = ctypes.CDLL(espeakng_loader.get_library_path())
        cb_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
        collected: list[np.ndarray] = []

        @cb_t
        def _cb(wav, numsamples, _events):
            if numsamples > 0 and wav:
                collected.append(np.ctypeslib.as_array(wav, shape=(numsamples,)).copy())
            return 0

        sr = lib.espeak_Initialize(0x0002, 500, espeakng_loader.get_data_path().encode(), 0)
        lib.espeak_SetSynthCallback(_cb)

        def _synth(text: str) -> np.ndarray:
            collected.clear()
            lib.espeak_SetVoiceByName(b"en-us")
            raw = text.encode("utf-8")
            lib.espeak_Synth(raw, len(raw) + 1, 0, 0, 0, 1, None, None)
            pcm = np.concatenate(collected).astype(np.float32) / 32768.0
            g = gcd(sr, 16000)
            return resample_poly(pcm, 16000 // g, sr // g).astype(np.float32)

        for phrase in ("Hey Gideon", "Hi Gideon", "Okay Gideon"):
            audio = _synth(phrase)
            engine.reset()
            max_p = max(engine.vad_prob(audio[i : i + 1280]) for i in range(0, len(audio), 1280))
            self.assertGreaterEqual(max_p, engine.vad_threshold, f"VAD missed {phrase!r}")
            matched, transcript = engine.detect_wake(audio)
            self.assertIsNotNone(matched, f"Missed {phrase!r}; heard {transcript!r}")

        # Unrelated speech must NOT match
        unrelated = _synth("What is the weather like in London tomorrow morning")
        matched, transcript = engine.detect_wake(unrelated)
        self.assertIsNone(matched, f"False positive on unrelated speech: {transcript!r}")


class TestMidStreamDisarmRegression(unittest.IsolatedAsyncioTestCase):
    async def test_mid_stream_disarm_stops_hot_mic_ingest(self):
        """Regression (2026-11-12 bug): after a mid-stream 'bye bye' disarm, the
        running capture must revert to wake-word watch within one chunk. Loud
        post-dismissal noise must NOT lock capture open or return a buffer."""
        loud = np.ones((1280, 1), dtype=np.int16) * 12000
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
            return checker_calls["n"] <= 3

        def enabled_checker():
            return reads["n"] < 40

        on_wake = MagicMock()
        on_barge_in = MagicMock()
        model = _FakeVadWhisperModel(vad_probs=[0.0])
        listener = AudioListener(
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

        self.assertIsNone(result)
        self.assertFalse(listener.is_recording_command)
        on_wake.assert_not_called()
        on_barge_in.assert_not_called()
        self.assertGreaterEqual(model.reset_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
