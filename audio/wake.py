"""
Audio Capture, RMS Level Calculator, and Silero VAD + Faster-Whisper Wake Listener for GIDEON.
Handles:
- Live RMS audio calculation for the Quickshell UI overlay.
- Continuous ultra-low-CPU Silero VAD ONNX gating (~0.8 ms per 80 ms chunk on 1 CPU core).
- GPU-accelerated Faster-Whisper wake-word spotting (~160 ms on CUDA, 0% GPU when silent).
- Seamless two-step ("Hey Gideon" -> chime -> command) AND one-shot ("Hey Gideon, ...") capture.
- Acoustic barge-in during TTS playback.
- Hot Mic follow-up capture without repeating the wake word.
- Adaptive trailing silence cutoff detection.
"""
import asyncio
import ctypes
import logging
import os
import re
import site
import threading
import time
from collections import deque
from concurrent.futures import CancelledError as FutureCancelledError, ThreadPoolExecutor
from pathlib import Path
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
WAKE_CHECK_TIMEOUT_S = 5.0

# Tunable wake-word & hot-mic capture guards (override via environment variables)
DEFAULT_WAKE_THRESHOLD = float(os.environ.get("GIDEON_WAKE_THRESHOLD", "0.35"))
DEFAULT_WAKE_MODEL = os.environ.get("GIDEON_WAKE_WHISPER_MODEL", "base").strip() or "base"
DEFAULT_WAKE_PROMPT = os.environ.get("GIDEON_WAKE_PROMPT", "").strip()
WAKE_REFRACTORY_S = float(os.environ.get("GIDEON_WAKE_REFRACTORY_S", "1.2"))
POST_TTS_REFRACTORY_S = float(os.environ.get("GIDEON_POST_TTS_REFRACTORY_S", "0.60"))
HOT_MIC_ONSET_CHUNKS = int(os.environ.get("GIDEON_HOT_MIC_ONSET_CHUNKS", "3"))
SPEECH_THRESHOLD_FLOOR = float(os.environ.get("GIDEON_SPEECH_THRESHOLD_FLOOR", "0.008"))

# Wake phrase matchers:
# - Both idle and barge-in require an explicit wake prefix (hey, hi, okay, ok, yo, hello, wake up, listen)
#   before Gideon, anchored near the start of the transcript (allowing only minor leading filler like uh, um, oh, well, punctuation).
# - Barge-in during TTS playback also allows stop/wait/hold on interrupt prefixes.
_FILLER_PREFIX = r"^\s*(?:[^\w\s]*\s*)*(?:(?:uh|um|oh|well|ah)\b\s*[,.\s-]*)*"
_WAKE_NAME = r"gid(?:e|i|a)?(?:o|a|u)?n(?:'s|s)?\b"
_IDLE_PREFIXES = r"(?:hey|hi|okay|ok|yo|hello|wake\s+up|listen)"
_BARGE_IN_PREFIXES = r"(?:hey|hi|okay|ok|yo|hello|wake\s+up|listen|stop|wait|hold\s+on)"

_WAKE_IDLE_RE = re.compile(
    rf"{_FILLER_PREFIX}(?:{_IDLE_PREFIXES})\s*[,.\s-]+{_WAKE_NAME}",
    re.I,
)
_WAKE_BARGE_IN_RE = re.compile(
    rf"{_FILLER_PREFIX}(?:{_BARGE_IN_PREFIXES})\s*[,.\s-]+{_WAKE_NAME}",
    re.I,
)


def _preload_cuda_libs_for_wake() -> None:
    """Preload CUDA 12 shared libs (cublas/cudnn) under RTLD_GLOBAL for ctranslate2/faster-whisper."""
    roots = list(site.getsitepackages())
    roots.append(
        os.environ.get(
            "GIDEON_CUDA12_DIR",
            os.path.expanduser("~/.local/share/gideon/cuda12"),
        )
    )
    for root_dir in roots:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        nvidia_dir = os.path.join(root_dir, "nvidia")
        search_root = nvidia_dir if os.path.isdir(nvidia_dir) else root_dir
        found: list[str] = []
        for walk_root, _dirs, files in os.walk(search_root):
            for name in files:
                if name.startswith(("libcublas.so.12", "libcublasLt.so.12", "libcudart.so.12")):
                    found.append(os.path.join(walk_root, name))
                elif name.startswith("libcudnn") and ".so.9" in name:
                    found.append(os.path.join(walk_root, name))
        for lib in sorted(found, key=lambda p: (os.path.basename(p).startswith("libcudnn.so"), p)):
            try:
                ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def _resolve_silero_vad_onnx() -> Path:
    """Locate the locally cached silero_vad.onnx model file."""
    override = os.environ.get("GIDEON_SILERO_VAD_PATH", "").strip()
    if override and Path(override).is_file():
        return Path(override)

    hub = Path.home() / ".cache" / "huggingface" / "hub" / "models--istupakov--silero-vad-onnx" / "snapshots"
    if hub.is_dir():
        for snap in sorted(hub.iterdir(), reverse=True):
            candidate = snap / "silero_vad.onnx"
            if candidate.is_file():
                return candidate

    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download("istupakov/silero-vad-onnx", "silero_vad.onnx", token=False))


def _extract_command_remainder(transcript: str) -> str:
    """Return any substantive command text spoken after the wake phrase."""
    match = _WAKE_IDLE_RE.search(transcript) or _WAKE_BARGE_IN_RE.search(transcript)
    if not match:
        return ""
    tail = transcript[match.end() :].strip(" ,.:;!?-\"'")
    return tail


class StreamingSileroVad:
    """Lightweight frame-by-frame Silero VAD ONNX runner (~0.8 ms per 80 ms chunk on 1 CPU thread)."""

    def __init__(self, model_path: str | Path | None = None, sample_rate: int = 16000):
        if sample_rate not in (8000, 16000):
            raise ValueError("Silero sample rate must be 8000 or 16000 Hz")
        import onnxruntime as ort

        resolved = Path(model_path) if model_path else _resolve_silero_vad_onnx()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._sess = ort.InferenceSession(
            str(resolved),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.sample_rate = int(sample_rate)
        self._hop = 512 if self.sample_rate == 16000 else 256
        self._ctx = 64 if self.sample_rate == 16000 else 32
        self._sr_arr = np.array(self.sample_rate, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self._ctx, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)

    def max_speech_prob(self, float_chunk: np.ndarray) -> float:
        """Return max speech probability across all 512-sample Silero frames in this chunk."""
        float_chunk = np.asarray(float_chunk, dtype=np.float32).reshape(-1)
        if len(float_chunk) == 0:
            return 0.0
        buf = (
            np.concatenate([self._pending, float_chunk.astype(np.float32, copy=False)])
            if len(self._pending)
            else float_chunk.astype(np.float32, copy=False)
        )
        max_p = 0.0
        pos = 0
        hop = self._hop
        while pos + hop <= len(buf):
            frame = buf[pos : pos + hop]
            inp = np.concatenate([self._context, frame])[None, :]
            out, self._state = self._sess.run(
                ["output", "stateN"],
                {"input": inp, "state": self._state, "sr": self._sr_arr},
            )
            self._context = frame[-self._ctx :].copy()
            prob = float(out[0, 0])
            if prob > max_p:
                max_p = prob
            pos += hop
        self._pending = buf[pos:].copy()
        return max_p


class VadWhisperWakeEngine:
    """Two-stage local wake engine:
    1. Continuous Silero VAD ONNX on CPU (~0.8 ms/chunk; 0% GPU while silent).
    2. Faster-Whisper (`base` INT8 on CUDA, ~160 ms) on detected speech bursts.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_WAKE_MODEL,
        vad_threshold: float = DEFAULT_WAKE_THRESHOLD,
        initial_prompt: str = DEFAULT_WAKE_PROMPT,
        sample_rate: int = 16000,
        device: str | None = None,
        compute_type: str = "int8",
    ):
        if sample_rate != 16000:
            raise ValueError("Whisper wake sample rate must be 16000 Hz")
        _preload_cuda_libs_for_wake()
        import faster_whisper
        from huggingface_hub.errors import LocalEntryNotFoundError

        self.sample_rate = int(sample_rate)
        self.vad_threshold = float(vad_threshold)
        self.initial_prompt = initial_prompt
        self.model_name = model_name
        self._whisper_lock = threading.Lock()
        self._vad_lock = threading.Lock()

        self.vad = StreamingSileroVad(sample_rate=self.sample_rate)

        # Resolve device: prefer CUDA (GTX 1650 sits idle while Parakeet runs on CPU)
        if device is None:
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cuda"

        self.device = device
        self.compute_type = compute_type
        self._whisper = None
        try:
            try:
                self._whisper = faster_whisper.WhisperModel(
                    model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=2,
                    num_workers=1,
                    use_auth_token=False,
                    local_files_only=True,
                )
            except LocalEntryNotFoundError:
                self._whisper = faster_whisper.WhisperModel(
                    model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=2,
                    num_workers=1,
                    use_auth_token=False,
                )
            # Warm up CUDA kernels immediately so missing cublas/cudnn fails over to CPU now
            # and the very first spoken "Hey Gideon" runs in ~160 ms.
            list(
                self._whisper.transcribe(
                    np.zeros(self.sample_rate, dtype=np.float32),
                    beam_size=1,
                    best_of=1,
                    language="en",
                    condition_on_previous_text=False,
                    vad_filter=False,
                )[0]
            )
        except Exception as cuda_exc:
            if self.device == "cuda":
                logger.warning("Wake Whisper CUDA init/warmup failed (%s); falling back to CPU int8.", cuda_exc)
                self.device = "cpu"
                self.compute_type = "int8"
                self._whisper = None
                self._whisper = faster_whisper.WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=2,
                    num_workers=1,
                    use_auth_token=False,
                )
                list(self._whisper.transcribe(
                    np.zeros(self.sample_rate, dtype=np.float32), beam_size=1, best_of=1,
                    language="en", condition_on_previous_text=False, vad_filter=False,
                )[0])
            else:
                raise

    def vad_prob(self, raw_float: np.ndarray) -> float:
        """Return max Silero speech probability for this audio chunk."""
        with self._vad_lock:
            return self.vad.max_speech_prob(raw_float)

    def reset(self) -> None:
        """Reset streaming Silero VAD RNN state."""
        with self._vad_lock:
            self.vad.reset()

    @staticmethod
    def _normalize_for_whisper(float_audio: np.ndarray) -> np.ndarray:
        """Boost quiet mic captures into Whisper's sweet spot without hard-clipping loud speech."""
        float_audio = np.ascontiguousarray(float_audio, dtype=np.float32).reshape(-1)
        if len(float_audio) == 0:
            return float_audio
        rms = float(np.sqrt(np.mean(float_audio**2)))
        if rms > 0.0025:
            gain = min(6.0, max(1.0, 0.08 / rms))
            if gain > 1.05:
                return np.tanh(float_audio * gain).astype(np.float32)
        return np.ascontiguousarray(float_audio, dtype=np.float32)

    def detect_wake(self, float_audio: np.ndarray, require_prefix: bool = False) -> tuple[str | None, str]:
        """Transcribe a short speech burst and return (matched_wake_phrase, full_transcript)."""
        if self._whisper is None or len(float_audio) < int(self.sample_rate * 0.25):
            return None, ""
        norm = self._normalize_for_whisper(float_audio)
        # Segment iteration executes native inference; keep it inside the lock.
        with self._whisper_lock:
            transcribe_kwargs = {
                "beam_size": 1,
                "best_of": 1,
                "language": "en",
                "initial_prompt": self.initial_prompt or None,
                "condition_on_previous_text": False,
                "vad_filter": False,
                "no_speech_threshold": 0.55,
            }
            try:
                segments, _ = self._whisper.transcribe(
                    norm,
                    hotwords="Gideon",
                    **transcribe_kwargs,
                )
            except (TypeError, ValueError):
                segments, _ = self._whisper.transcribe(
                    norm,
                    **transcribe_kwargs,
                )

            kept = []
            for s in segments:
                text = s.text.strip() if getattr(s, "text", None) else ""
                if not text:
                    continue
                nsp = getattr(s, "no_speech_prob", None)
                if nsp is not None and float(nsp) >= 0.55:
                    continue
                alp = getattr(s, "avg_logprob", None)
                if alp is not None and float(alp) < -0.85:
                    continue
                cr = getattr(s, "compression_ratio", None)
                if cr is not None and float(cr) > 2.2:
                    continue
                kept.append(text)
        transcript = " ".join(kept).strip()
        if not transcript:
            return None, ""

        pattern = _WAKE_BARGE_IN_RE if require_prefix else _WAKE_IDLE_RE
        match = pattern.search(transcript)
        if match:
            return match.group(0).strip(), transcript
        return None, transcript


class AudioListener:
    def __init__(
        self,
        wake_word: str = "hey gideon",
        wake_threshold: float = DEFAULT_WAKE_THRESHOLD,
        sample_rate: int = 16000,
        chunk_size: int = 1280,  # 80ms chunks
        on_rms_update: Callable[[float], None] | None = None,
        on_wake_detected: Callable[[], None] | None = None,
        on_barge_in: Callable[[], None] | None = None,
        hot_mic_timeout: float = 45.0,
        mic_gain: float = 1.6,
        post_tts_refractory_s: float = POST_TTS_REFRACTORY_S,
        wake_model_name: str = DEFAULT_WAKE_MODEL,
    ):
        if sample_rate != 16000 or chunk_size <= 0:
            raise ValueError("AudioListener requires 16000 Hz and a positive chunk size")
        self.wake_word = wake_word
        self.wake_threshold = float(wake_threshold)
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.on_rms_update = on_rms_update
        self.on_wake_detected = on_wake_detected
        self.on_barge_in = on_barge_in
        self.hot_mic_timeout = hot_mic_timeout
        self.mic_gain = max(1.0, min(4.0, mic_gain))
        self.post_tts_refractory_s = max(0.0, float(post_tts_refractory_s))
        self.wake_model_name = wake_model_name

        self.wakeword_model = None
        self._is_listening: bool = False
        self._is_running: bool = True
        self._stream = None
        self._last_wake_time: float = 0.0
        self._last_tts_end_time: float = 0.0
        self.is_recording_command: bool = False
        self._wake_executor = None
        self._wake_future = None
        self._closed = False

    def stop(self) -> None:
        """Interrupt any active listening loop immediately."""
        self._is_running = False

    def close(self) -> None:
        """Release the owned worker without waiting on uncancellable native inference."""
        self.stop()
        self._closed = True
        if self._wake_future is not None:
            self._wake_future.cancel()
        if self._wake_executor is not None:
            self._wake_executor.shutdown(wait=False, cancel_futures=True)
            self._wake_executor = None

    def load_wake_model(self) -> None:
        """Initialize Silero VAD + Faster-Whisper wake engine."""
        try:
            logger.info(
                "Initializing Silero VAD + Faster-Whisper (%s) wake engine for '%s' (vad_threshold=%.2f)...",
                self.wake_model_name,
                self.wake_word,
                self.wake_threshold,
            )
            self.wakeword_model = VadWhisperWakeEngine(
                model_name=self.wake_model_name,
                vad_threshold=self.wake_threshold,
                sample_rate=self.sample_rate,
            )
            logger.info(
                "Wake engine loaded: Silero VAD (CPU) + Faster-Whisper '%s' (%s %s).",
                self.wakeword_model.model_name,
                self.wakeword_model.device,
                self.wakeword_model.compute_type,
            )
        except Exception as e:
            logger.warning("Could not load Silero+Whisper wake engine (%s). Wake listening disabled.", e)
            self.wakeword_model = None

    def calculate_rms(self, audio_chunk: np.ndarray, ambient_floor: float = 0.010) -> float:
        """Calculate normalized RMS energy (0.0 to 1.0) above ambient noise floor."""
        if len(audio_chunk) == 0:
            return 0.0
        float_data = audio_chunk.astype(np.float32).reshape(-1) / 32768.0
        # Filter low-frequency rumble before RMS calculation
        filtered = signal.lfilter(BP_B, BP_A, float_data)
        raw_rms = float(np.sqrt(np.mean(filtered**2)))
        net_rms = max(0.0, raw_rms - ambient_floor)
        norm_rms = min(1.0, max(0.0, net_rms * 14.0))
        return norm_rms

    async def listen_for_command(
        self,
        is_hot_mic: bool = False,
        is_speaking: bool = False,
        silence_timeout_s: float = 0.85,
        max_duration_s: float = 45.0,
        hot_mic_checker: Callable[[], bool] | None = None,
        is_speaking_checker: Callable[[], bool] | None = None,
        enabled_checker: Callable[[], bool] | None = None,
    ) -> np.ndarray | None:
        """
        Record and return spoken utterance buffer.
        - Continuous Silero VAD ONNX gates GPU Whisper wake checks (0% GPU while silent).
        - Non-blocking async wake checks keep draining PortAudio with zero dropped syllables.
        - Supports two-step ("Hey Gideon" -> chime -> command), fast one-shot
          ("Hey Gideon, pause music"), and continuous one-shot commands.
        - Bandpass filtered (200Hz-3800Hz) to prevent room fan rumble from tripping triggers.
        - Supports dynamic hot_mic_checker and enabled_checker.
        """
        import sounddevice as sd

        if self._closed or (enabled_checker and not enabled_checker()):
            self.is_recording_command = False
            return None

        if self.wakeword_model is None:
            self.load_wake_model()

        audio_frames: list[np.ndarray] = []
        is_recording = False
        speech_started = False
        speech_start_time = 0.0
        recording_start = 0.0
        loop_start = time.monotonic()
        last_speech_time = loop_start
        hot_mic_timeout_s = self.hot_mic_timeout
        ambient_floor = 0.010
        hot_mic_consecutive_chunks = 0
        hot_mic_pending_frames: list[np.ndarray] = []
        was_speaking = bool(is_speaking)
        chunk_s = self.chunk_size / self.sample_rate
        pre_roll_buffer: deque[np.ndarray] = deque(maxlen=max(5, HOT_MIC_ONSET_CHUNKS + 2))
        filter_state = np.zeros(max(len(BP_A), len(BP_B)) - 1)
        silence_chunk_count = 0

        # Wake-word VAD segmentation + non-blocking Whisper check state
        wake_pre_roll: deque[np.ndarray] = deque(maxlen=4)  # ~320ms raw float32 pre-roll
        wake_speech_chunks: list[np.ndarray] = []
        wake_speech_audio: list[np.ndarray] = []
        wake_in_speech = False
        wake_voiced_chunks = 0
        wake_silence_chunks = 0
        wake_chunks_since_peek = 0
        wake_peek_done = False
        wake_check_task = self._wake_future
        wake_check_meta: dict = {}
        pending_post_check_frames = deque(maxlen=max(1, int(WAKE_CHECK_TIMEOUT_S / chunk_s)))

        def _reset_wake_buffers() -> None:
            nonlocal wake_in_speech, wake_voiced_chunks, wake_silence_chunks, wake_chunks_since_peek, wake_peek_done
            wake_speech_chunks.clear()
            wake_speech_audio.clear()
            wake_pre_roll.clear()
            pre_roll_buffer.clear()
            wake_in_speech = False
            wake_voiced_chunks = 0
            wake_silence_chunks = 0
            wake_chunks_since_peek = 0
            wake_peek_done = False
            pending_post_check_frames.clear()

        def _cancel_wake_task() -> None:
            nonlocal wake_check_task
            if wake_check_task is not None:
                wake_check_task.cancel()
                # Cancelling a Future cannot stop a running CTranslate2 call.
                # Retain that worker slot until it actually completes.
                if wake_check_task.done():
                    try:
                        wake_check_task.result()
                    except FutureCancelledError:
                        pass
                    except Exception as exc:
                        logger.debug("Discarded wake check failed: %s", exc)
                    wake_check_task = None
            self._wake_future = wake_check_task
            wake_check_meta.clear()
            pending_post_check_frames.clear()

        self._is_running = True
        if self.wakeword_model is not None:
            self.wakeword_model.reset()
        try:
            stream_ctx = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.chunk_size,
            )
        except Exception as e:
            # Global PortAudio teardown invalidates other live streams,
            # including hold-to-talk's CFFI callback. Let the caller retry.
            logger.warning(f"Audio device open failed ({e}); retrying without global teardown")
            raise

        try:
            with stream_ctx as stream:
                logger.debug(f"Listening loop active (hot_mic={is_hot_mic}, speaking={is_speaking})")

                while self._is_running:
                    data, _ = stream.read(self.chunk_size)
                    raw_audio_chunk = data.flatten()
                    await asyncio.sleep(0)
                    if not self._is_running or self._closed:
                        return None
                    now = time.monotonic()
                    current_frame_buffered = False

                    # Dynamically update speaking and hot-mic states from host daemon
                    if is_speaking_checker:
                        is_speaking = is_speaking_checker()
                    if was_speaking != bool(is_speaking):
                        if not is_speaking:
                            self._last_tts_end_time = now
                        _cancel_wake_task()
                        _reset_wake_buffers()
                        if self.wakeword_model is not None:
                            self.wakeword_model.reset()
                    was_speaking = bool(is_speaking)

                    if enabled_checker and not enabled_checker():
                        logger.debug("Wake listener disabled mid-stream; closing audio capture.")
                        self.is_recording_command = False
                        return None
                    if hot_mic_checker:
                        # Bidirectional sync with the shared window: adopt a live arm
                        # mid-stream, and DEMOTE back to wake-word watch when the
                        # window is disarmed mid-iteration (e.g. "bye bye").
                        armed_now = hot_mic_checker()
                        if armed_now and not is_hot_mic and not is_recording:
                            is_hot_mic = True
                            loop_start = now
                            hot_mic_timeout_s = self.hot_mic_timeout
                            _cancel_wake_task()
                            _reset_wake_buffers()
                            if self.wakeword_model is not None:
                                self.wakeword_model.reset()
                            logger.info("Hot mic adopted mid-stream (window live; disarm observed).")
                        elif is_hot_mic and not armed_now and not is_recording:
                            is_hot_mic = False
                            speech_started = False
                            hot_mic_consecutive_chunks = 0
                            hot_mic_pending_frames.clear()
                            _cancel_wake_task()
                            _reset_wake_buffers()
                            if self.wakeword_model is not None:
                                self.wakeword_model.reset()
                            logger.info("Hot mic disarmed mid-stream; reverting to wake-word watch.")

                    # Raw unclipped float32 stream for Silero VAD & Whisper wake checks
                    raw_float = raw_audio_chunk.astype(np.float32) / 32768.0

                    # Soft-limited pre-amp for quiet/far-field RMS VAD and captured speech
                    if self.mic_gain != 1.0:
                        float_chunk = np.tanh(raw_float * self.mic_gain).astype(np.float32)
                        audio_chunk = (float_chunk * 32767.0).astype(np.int16)
                    else:
                        float_chunk = raw_float
                        audio_chunk = raw_audio_chunk

                    # Filter audio to human voice frequency band (200Hz - 3800Hz)
                    filtered_chunk, filter_state = signal.lfilter(BP_B, BP_A, float_chunk, zi=filter_state)
                    voice_rms = float(np.sqrt(np.mean(filtered_chunk**2)))

                    # Track ambient noise floor adaptively during quiet moments & buffer pre-roll
                    if not is_recording:
                        pre_roll_buffer.append(audio_chunk)
                        wake_pre_roll.append(raw_float)
                        if voice_rms < 0.008:
                            ambient_floor = ambient_floor * 0.95 + voice_rms * 0.05

                    # Update live normalized RMS energy for Quickshell UI breathing
                    ui_rms = self.calculate_rms(audio_chunk, ambient_floor)
                    if self.on_rms_update:
                        self.on_rms_update(ui_rms)

                    # Dynamically scaled thresholds based on filtered voice band
                    speech_threshold = max(SPEECH_THRESHOLD_FLOOR, ambient_floor * 1.8)
                    silence_threshold = max(0.003, ambient_floor * 1.3)

                    in_post_tts_cooldown = (
                        not is_speaking
                        and self._last_tts_end_time > 0.0
                        and (now - self._last_tts_end_time) < self.post_tts_refractory_s
                    )

                    # 1. Hot Mic Ingestion: Wait for user to speak follow-up (reject isolated typing transients)
                    if is_hot_mic and not speech_started:
                        if is_speaking or in_post_tts_cooldown:
                            hot_mic_consecutive_chunks = 0
                            hot_mic_pending_frames.clear()
                            if is_speaking:
                                loop_start = now
                        elif now - loop_start > hot_mic_timeout_s:
                            logger.debug("Hot mic window closed (timeout reached)")
                            return None
                        elif voice_rms >= speech_threshold:
                            hot_mic_consecutive_chunks += 1
                            hot_mic_pending_frames.append(audio_chunk)
                            if hot_mic_consecutive_chunks >= HOT_MIC_ONSET_CHUNKS:
                                logger.info("Speech detected during hot mic window. Locking audio capture open.")
                                speech_started = True
                                speech_start_time = now
                                is_recording = True
                                self.is_recording_command = True
                                recording_start = now
                                last_speech_time = recording_start
                                audio_frames.extend(pre_roll_buffer)
                                hot_mic_pending_frames.clear()
                                continue
                        else:
                            hot_mic_consecutive_chunks = 0
                            hot_mic_pending_frames.clear()

                    # 2. Wake Word Inspection (Silero VAD -> non-blocking Whisper check)
                    if wake_check_task is not None and not wake_check_meta and wake_check_task.done():
                        _cancel_wake_task()
                    if (not is_hot_mic or is_speaking) and not is_recording and self.wakeword_model is not None:
                        if in_post_tts_cooldown:
                            _cancel_wake_task()
                            _reset_wake_buffers()
                        else:
                            # Buffer incoming audio while an async Whisper wake check is in flight
                            if wake_check_task is not None and wake_check_meta:
                                pending_post_check_frames.append(audio_chunk)
                                wake_check_meta["post_speech"] |= voice_rms >= speech_threshold
                                if now - wake_check_meta["started"] > WAKE_CHECK_TIMEOUT_S:
                                    _cancel_wake_task()
                                    _reset_wake_buffers()

                            # Collect result if an async Whisper wake check just finished
                            if wake_check_task is not None and wake_check_meta and wake_check_task.done():
                                task_ref = wake_check_task
                                meta = dict(wake_check_meta)
                                post_frames = list(pending_post_check_frames)
                                wake_check_task = None
                                self._wake_future = None
                                wake_check_meta.clear()
                                pending_post_check_frames.clear()

                                matched_phrase = None
                                heard_text = ""
                                try:
                                    matched_phrase, heard_text = task_ref.result()
                                except FutureCancelledError:
                                    pass
                                except Exception as exc:
                                    logger.warning("Wake transcription check failed: %s", exc)

                                if is_speaking and not _WAKE_BARGE_IN_RE.search(heard_text):
                                    matched_phrase = None
                                if matched_phrase and (now - self._last_wake_time > WAKE_REFRACTORY_S):
                                    logger.info(
                                        "Wake word detected (%r in %r)",
                                        matched_phrase,
                                        heard_text,
                                    )
                                    self._last_wake_time = now
                                    _reset_wake_buffers()
                                    self.wakeword_model.reset()

                                    if is_speaking:
                                        if self.on_barge_in:
                                            self.on_barge_in()

                                    if self.on_wake_detected:
                                        self.on_wake_detected()
                                    if not self._is_running or (enabled_checker and not enabled_checker()):
                                        return None

                                    remainder = _extract_command_remainder(heard_text)
                                    remainder_words = [w for w in remainder.split() if w]
                                    is_peek = bool(meta.get("is_peek"))
                                    checked_int16 = meta.get("audio_int16")

                                    # Fast complete one-shot ("Hey Gideon, pause music" then paused):
                                    # return immediately without forcing the user to repeat it.
                                    if (
                                        not is_peek
                                        and len(remainder_words) >= 1
                                        and not is_speaking
                                        and not meta["post_speech"]
                                        and voice_rms < speech_threshold
                                        and checked_int16 is not None
                                    ):
                                        logger.info(
                                            "Complete one-shot command captured with wake word (%r).",
                                            heard_text,
                                        )
                                        play_sound("acknowledged.wav")
                                        audio_frames = [checked_int16] + post_frames
                                        break

                                    # Enter command recording mode
                                    is_recording = True
                                    self.is_recording_command = True
                                    recording_start = now
                                    last_speech_time = recording_start
                                    audio_frames.clear()
                                    silence_chunk_count = 0

                                    if is_peek or meta["post_speech"] or len(remainder_words) >= 1:
                                        # Continuous one-shot speech ("Hey Gideon, play some jazz..."):
                                        # keep full captured speech + post-check frames so zero words clip.
                                        speech_started = True
                                        speech_start_time = recording_start
                                        if checked_int16 is not None:
                                            audio_frames.append(checked_int16)
                                        audio_frames.extend(post_frames)
                                        current_frame_buffered = True
                                        logger.info("Contiguous speech following wake word (one-shot mode).")
                                    else:
                                        # Two-step wake ("Hey Gideon" -> chime -> command)
                                        speech_started = False
                                        if post_frames:
                                            audio_frames.extend(post_frames[-2:])
                                            current_frame_buffered = True
                                        play_sound("activate.wav")
                                elif heard_text:
                                    logger.debug("[Wake Ignore] heard=%r", heard_text)

                            # Feed Silero VAD and trigger wake check on pause or mid-sentence peek
                            if not is_recording and (now - self._last_wake_time > WAKE_REFRACTORY_S):
                                vad_p = float(self.wakeword_model.vad_prob(raw_float))
                                if vad_p >= self.wake_threshold:
                                    if not wake_in_speech:
                                        wake_in_speech = True
                                        wake_speech_chunks = list(wake_pre_roll)
                                        wake_speech_audio = list(pre_roll_buffer)[-len(wake_pre_roll):]
                                        wake_voiced_chunks = 0
                                        wake_silence_chunks = 0
                                        wake_chunks_since_peek = 0
                                        wake_peek_done = False
                                    else:
                                        wake_speech_chunks.append(raw_float)
                                        wake_speech_audio.append(audio_chunk)
                                    wake_voiced_chunks += 1
                                    wake_silence_chunks = 0
                                    wake_chunks_since_peek += 1
                                elif wake_in_speech:
                                    wake_speech_chunks.append(raw_float)
                                    wake_speech_audio.append(audio_chunk)
                                    wake_silence_chunks += 1
                                    wake_chunks_since_peek += 1

                                # Bound rolling buffer during long room conversations (~2.8s max)
                                if len(wake_speech_chunks) > 35:
                                    wake_speech_chunks = wake_speech_chunks[-30:]
                                    wake_speech_audio = wake_speech_audio[-30:]

                                should_check = False
                                is_peek = False
                                if wake_in_speech and wake_check_task is None:
                                    if not wake_peek_done:
                                        if wake_voiced_chunks >= 3 and wake_silence_chunks >= 3:
                                            # ~240ms pause after >=240ms of voiced speech
                                            should_check = True
                                            is_peek = False
                                        elif wake_voiced_chunks >= 12 and wake_chunks_since_peek >= 14:
                                            # Continuous speech peek (~1.2s in) for one-shot commands
                                            should_check = True
                                            is_peek = True
                                        elif wake_silence_chunks >= 5 and wake_voiced_chunks < 3:
                                            # Tiny click/cough burst: discard cleanly
                                            _reset_wake_buffers()
                                    else:
                                        # Initial peek already ran for this voiced burst; do not re-trigger mid-burst.
                                        # Wait until a real silence reset (wake_silence_chunks >= 3) before checking a new phrase.
                                        if wake_silence_chunks >= 3:
                                            _reset_wake_buffers()

                                if should_check:
                                    clip_float = np.concatenate(wake_speech_chunks)
                                    clip_int16 = np.concatenate(wake_speech_audio)
                                    require_prefix = bool(is_speaking)
                                    wake_check_meta = {
                                        "is_peek": is_peek,
                                        "audio_int16": clip_int16,
                                        "started": now,
                                        "post_speech": False,
                                    }
                                    pending_post_check_frames.clear()
                                    if self._wake_executor is None:
                                        self._wake_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wake-whisper")
                                    wake_check_task = self._wake_executor.submit(
                                        self.wakeword_model.detect_wake, clip_float, require_prefix,
                                    )
                                    self._wake_future = wake_check_task
                                    if is_peek:
                                        wake_chunks_since_peek = 0
                                        wake_peek_done = True
                                    else:
                                        wake_speech_chunks.clear()
                                        wake_speech_audio.clear()
                                        wake_in_speech = False
                                        wake_voiced_chunks = 0
                                        wake_silence_chunks = 0
                                        wake_chunks_since_peek = 0
                                        wake_peek_done = False

                    # 3. Speech Accumulation & Adaptive Trailing Silence Detection
                    if is_recording:
                        if not current_frame_buffered:
                            audio_frames.append(audio_chunk)

                        # Guard against wake-word tail / chime tripping premature onset
                        if not speech_started:
                            if now - recording_start < 0.15:
                                continue

                            if voice_rms >= speech_threshold:
                                speech_started = True
                                speech_start_time = now
                                last_speech_time = speech_start_time
                                silence_chunk_count = 0
                                logger.debug("Command speech onset detected.")
                            elif now - recording_start > 4.5:
                                logger.debug("No speech detected following wake word within 4.5s.")
                                self.is_recording_command = False
                                return None
                        else:
                            speech_duration = now - speech_start_time
                            if voice_rms < silence_threshold:
                                silence_chunk_count += 1
                            else:
                                # Leaky reset: allow brief breath/noise without wiping out silence progression
                                last_speech_time = now
                                silence_chunk_count = max(0, silence_chunk_count - 2)

                            # Require at least 0.50s of command speech AND ~700-800ms of sustained trailing silence
                            required_silence_chunks = max(1, int(np.ceil(max(0.48, silence_timeout_s) / chunk_s)))
                            if speech_duration >= 0.50 and silence_chunk_count >= required_silence_chunks:
                                logger.info("Command speech finished (trailing silence detected).")
                                play_sound("acknowledged.wav")
                                break

                            # Sliding capture budget: the clock restarts on every
                            # active-speech chunk, so a trailing thought is never cut
                            # at a fixed wall-clock mark. ABSOLUTE_MAX_CAPTURE_S is a
                            # safety valve only; the real cutoff is trailing silence.
                            if (
                                now - last_speech_time > max_duration_s
                                or now - recording_start > ABSOLUTE_MAX_CAPTURE_S
                            ):
                                logger.info("Max recording duration reached.")
                                play_sound("acknowledged.wav")
                                break

                    await asyncio.sleep(0.001)
        finally:
            _cancel_wake_task()
            self.is_recording_command = False
            if self.wakeword_model is not None:
                self.wakeword_model.reset()

        if audio_frames:
            full_audio = np.concatenate(audio_frames)
            if len(full_audio) > self.sample_rate * 0.35:
                return full_audio
        return None
