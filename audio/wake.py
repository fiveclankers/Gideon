"""
Audio Capture, RMS Level Calculator, and openWakeWord Listener for GIDEON.
Handles:
- Live RMS audio calculation for the Quickshell UI overlay.
- Continuous Wake Word detection with acoustic barge-in during TTS playback.
- Hot Mic follow-up capture without repeating the wake word.
- VAD silence cutoff detection.
"""
import asyncio
import logging
import math
import time
from typing import Callable
import numpy as np
import scipy.signal as signal
from .sounds import play_sound

logger = logging.getLogger("gideon.wake")

# 2nd-order Butterworth bandpass filter (200Hz - 3800Hz) to eliminate fan and AC acoustic rumble
BP_B, BP_A = signal.butter(2, [200 / 8000, 3800 / 8000], btype="bandpass")

# Absolute ceiling for one continuous capture. The conversational budget
# (max_duration_s) slides forward on every active-speech chunk, so trailing
# monologues are not cut at a fixed wall-clock mark; this is only a safety
# valve against a stuck VAD.
ABSOLUTE_MAX_CAPTURE_S = 180.0


class AudioListener:
    def __init__(
        self,
        wake_word: str = "hey jarvis",
        wake_threshold: float = 0.20,
        sample_rate: int = 16000,
        chunk_size: int = 1280,  # 80ms chunks for openWakeWord
        on_rms_update: Callable[[float], None] | None = None,
        on_wake_detected: Callable[[], None] | None = None,
        on_barge_in: Callable[[], None] | None = None,
        hot_mic_timeout: float = 45.0,
        mic_gain: float = 1.8
    ):
        self.wake_word = wake_word
        self.wake_threshold = wake_threshold
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.on_rms_update = on_rms_update
        self.on_wake_detected = on_wake_detected
        self.on_barge_in = on_barge_in
        self.hot_mic_timeout = hot_mic_timeout
        self.mic_gain = max(1.0, min(4.0, mic_gain))

        self.wakeword_model = None
        self._is_listening: bool = False
        self._is_running: bool = True
        self._stream = None
        self._last_wake_time: float = 0.0
        self.is_recording_command: bool = False

    def stop(self) -> None:
        """Interrupt any active listening loop immediately."""
        self._is_running = False

    def load_wake_model(self) -> None:
        """Initialize openWakeWord engine with built-in Silero VAD gating."""
        try:
            from openwakeword.model import Model
            logger.info("Initializing openWakeWord model for 'hey jarvis' (vad_threshold=0.5)...")
            self.wakeword_model = Model(
                wakeword_models=["hey_jarvis_v0.1"],
                vad_threshold=0.5,
                inference_framework="onnx"
            )
            logger.info(f"openWakeWord loaded models: {list(self.wakeword_model.models.keys())}")
        except Exception as e:
            logger.warning(f"Could not load openWakeWord: {e}. Falling back to energy trigger.")
            self.wakeword_model = None

    def calculate_rms(self, audio_chunk: np.ndarray, ambient_floor: float = 0.010) -> float:
        """Calculate normalized RMS energy (0.0 to 1.0) above ambient noise floor."""
        if len(audio_chunk) == 0:
            return 0.0
        float_data = audio_chunk.astype(np.float32) / 32768.0
        # Filter low-frequency rumble before RMS calculation
        filtered = signal.lfilter(BP_B, BP_A, float_data)
        raw_rms = float(np.sqrt(np.mean(filtered**2)))
        net_rms = max(0.0, raw_rms - ambient_floor)
        norm_rms = min(1.0, max(0.0, net_rms * 14.0))
        return norm_rms

    async def listen_for_command(
        self,
        is_hot_mic: bool,
        is_speaking: bool,
        silence_timeout_s: float = 0.85,
        max_duration_s: float = 45.0,
        hot_mic_checker: Callable[[], bool] | None = None,
        is_speaking_checker: Callable[[], bool] | None = None
    ) -> np.ndarray | None:
        """
        Record and return spoken utterance buffer.
        - Emits activation chime on wake word.
        - Emits acknowledgment chime on command capture finish.
        - Bandpass filtered (200Hz-3800Hz) to prevent room fan rumble from tripping triggers.
        - Blanking window prevents wake-word tail from prematurely ending recording.
        - Supports dynamic hot_mic_checker to capture background confirmations seamlessly.
        """
        import sounddevice as sd

        if self.wakeword_model is None:
            self.load_wake_model()

        audio_frames = []
        is_recording = False
        speech_started = False
        speech_start_time = 0.0
        recording_start = 0.0
        silence_start = None
        loop_start = time.time()
        last_speech_time = time.time()
        hot_mic_timeout_s = self.hot_mic_timeout
        from collections import deque
        ambient_floor = 0.010
        hot_mic_consecutive_chunks = 0
        hot_mic_pending_frames = []
        pre_roll_buffer = deque(maxlen=5)
        silence_chunk_count = 0

        self._is_running = True
        try:
            stream_ctx = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                blocksize=self.chunk_size
            )
        except Exception as e:
            logger.warning(f"Audio device open failed ({e}); resetting PortAudio...")
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
            raise

        with stream_ctx as stream:
            logger.debug(f"Listening loop active (hot_mic={is_hot_mic}, speaking={is_speaking})")

            while self._is_running:
                data, _ = stream.read(self.chunk_size)
                audio_chunk = data.flatten()
                await asyncio.sleep(0)

                # Dynamically update speaking and hot-mic states from host daemon
                if is_speaking_checker:
                    is_speaking = is_speaking_checker()
                if hot_mic_checker and not is_hot_mic:
                    if hot_mic_checker():
                        is_hot_mic = True
                        loop_start = time.time()
                        logger.info("Hot mic armed dynamically mid-stream (confirmation gate active).")

                # Software mic pre-amp (config mic_gain, default 1.6x): applied in
                # float domain with hard clip, so quiet/far speech clears the
                # onset + openWakeWord gates without touching system volume.
                # Downstream (predict, frames, RMS) all use the gained chunk.
                float_chunk = audio_chunk.astype(np.float32) / 32768.0
                if self.mic_gain != 1.0:
                    float_chunk = np.clip(float_chunk * self.mic_gain, -1.0, 1.0)
                    audio_chunk = (float_chunk * 32767.0).astype(np.int16)

                # Filter audio to human voice frequency band (200Hz - 3800Hz)
                filtered_chunk = signal.lfilter(BP_B, BP_A, float_chunk)
                voice_rms = float(np.sqrt(np.mean(filtered_chunk**2)))

                # Track ambient noise floor adaptively during quiet moments & buffer pre-roll
                if not is_recording:
                    pre_roll_buffer.append(audio_chunk)
                    if voice_rms < 0.008:
                        ambient_floor = ambient_floor * 0.95 + voice_rms * 0.05

                # Update live normalized RMS energy for Quickshell UI breathing
                ui_rms = self.calculate_rms(audio_chunk, ambient_floor)
                if self.on_rms_update:
                    self.on_rms_update(ui_rms)

                # Dynamically scaled thresholds based on filtered voice band
                speech_threshold = max(0.006, ambient_floor * 1.8)
                silence_threshold = max(0.003, ambient_floor * 1.3)

                # 1. Hot Mic Ingestion: Wait for user to speak follow-up (reject isolated typing transients)
                if is_hot_mic and not speech_started:
                    # Acoustic Echo Immunity: Never ingest audio while TTS is actively vocalizing through speakers
                    if is_speaking:
                        hot_mic_consecutive_chunks = 0
                        hot_mic_pending_frames.clear()
                        loop_start = time.time()  # Give user full timeout after speech ends
                        continue

                    if time.time() - loop_start > hot_mic_timeout_s:
                        logger.debug("Hot mic window closed (timeout reached)")
                        return None
                    if voice_rms >= speech_threshold:
                        hot_mic_consecutive_chunks += 1
                        hot_mic_pending_frames.append(audio_chunk)
                        if hot_mic_consecutive_chunks >= 2:
                            logger.info("Speech detected during hot mic window. Locking audio capture open.")
                            speech_started = True
                            speech_start_time = time.time()
                            is_recording = True
                            self.is_recording_command = True
                            recording_start = time.time()
                            last_speech_time = recording_start
                            audio_frames.extend(hot_mic_pending_frames)
                            hot_mic_pending_frames.clear()
                            continue
                    else:
                        hot_mic_consecutive_chunks = 0
                        hot_mic_pending_frames.clear()

                # 2. Wake Word Inspection (when not hot-mic and not recording)
                if not is_hot_mic and not is_recording and self.wakeword_model is not None:
                    # Debounce refractory period (minimum 1.2s between wake word firings)
                    if time.time() - self._last_wake_time > 1.2:
                        predictions = self.wakeword_model.predict(audio_chunk)
                        for model_name, score in predictions.items():
                            if "jarvis" in model_name.lower():
                                if score >= self.wake_threshold:
                                    logger.info(f"Wake word detected ({model_name}: {score:.2f})")
                                    self._last_wake_time = time.time()
                                    self.wakeword_model.reset()

                                    if is_speaking:
                                        if self.on_barge_in:
                                            self.on_barge_in()
                                        return None

                                    if self.on_wake_detected:
                                        self.on_wake_detected()

                                    is_recording = True
                                    self.is_recording_command = True
                                    recording_start = time.time()
                                    last_speech_time = recording_start
                                    audio_frames.clear()
                                    speech_started = False
                                    silence_start = None

                                    # Include last 3 pre-roll chunks (~240ms) so seamless one-shot command onset is never clipped
                                    if len(pre_roll_buffer) >= 3:
                                        audio_frames.extend(list(pre_roll_buffer)[-3:])

                                    # If user already stopped speaking after wake word, play chime; otherwise stay silent to avoid stepping on contiguous speech
                                    if voice_rms < speech_threshold:
                                        play_sound("activate.wav")
                                    else:
                                        logger.info("Contiguous speech following wake word (one-shot mode).")
                                    break

                # 3. Speech Accumulation & Adaptive Trailing Silence Detection
                if is_recording:
                    audio_frames.append(audio_chunk)

                    # Guard against wake-word tail / chime tripping premature onset
                    if not speech_started:
                        if time.time() - recording_start < 0.15:
                            continue

                        if voice_rms >= speech_threshold:
                            speech_started = True
                            speech_start_time = time.time()
                            last_speech_time = speech_start_time
                            silence_chunk_count = 0
                            logger.debug("Command speech onset detected.")
                        elif time.time() - recording_start > 4.5:
                            logger.debug("No speech detected following wake word within 4.5s.")
                            self.is_recording_command = False
                            return None
                    else:
                        speech_duration = time.time() - speech_start_time
                        if voice_rms < silence_threshold:
                            silence_chunk_count += 1
                        else:
                            # Leaky reset: allow brief breath/noise without wiping out silence progression
                            last_speech_time = time.time()
                            silence_chunk_count = max(0, silence_chunk_count - 2)

                        # Require at least 0.50s of command speech AND ~700-800ms of sustained trailing silence
                        required_silence_chunks = int(max(6, silence_timeout_s / 0.080))
                        if speech_duration >= 0.50 and silence_chunk_count >= required_silence_chunks:
                            logger.info("Command speech finished (trailing silence detected).")
                            play_sound("acknowledged.wav")
                            break

                        # Sliding capture budget: the clock restarts on every
                        # active-speech chunk, so a trailing thought is never cut
                        # at a fixed wall-clock mark. ABSOLUTE_MAX_CAPTURE_S is a
                        # safety valve only; the real cutoff is trailing silence.
                        if (time.time() - last_speech_time > max_duration_s
                                or time.time() - recording_start > ABSOLUTE_MAX_CAPTURE_S):
                            logger.info("Max recording duration reached.")
                            play_sound("acknowledged.wav")
                            break

                await asyncio.sleep(0.001)

        self.is_recording_command = False
        if audio_frames:
            full_audio = np.concatenate(audio_frames)
            if len(full_audio) > self.sample_rate * 0.35:
                return full_audio
        return None
