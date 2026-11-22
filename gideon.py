#!/usr/bin/env python3
"""
GIDEON: Voice Companion & Autonomous Executive
Built for Omarchy / Hyprland Linux.
Features:
- Unified HTTP Visualizer Server on port 8790 (serves Circuit Board & all faces).
- In-Browser Hold-to-Talk via Spacebar (/api/talk/start & /api/talk/stop).
- Persistent Codex `app-server` (`gpt-6-luna`) default engine with Antigravity (`agy`) fallback.
- Parakeet TDT 0.6B v3 INT8 ONNX Speech-to-Text with Silero VAD (Faster-Whisper fallback).
- Fish Audio Neural Cloud TTS with PipeWire (`pw-play`) playback.
- Dynamic waveform & state IPC bridge for `ai-visualizer`.
- 45-second tool execution watchdog check-in.
- Priority intervention (in-flight prompt injection without SIGINT).
- Concurrent terminal input (Press ENTER or type directly).
"""

import argparse
import asyncio
import ctypes
import functools
import io
import json
import logging
import mimetypes
import os
import re
import signal
import site
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Union
from reflex_router import _is_dismissal, try_reflex, ensure_hyprland_env
from selected_context import PreparedPrompt, copy_to_wayland_clipboard, prepare_prompt

import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("gideon")

HERE = Path(__file__).resolve().parent
VISUALIZER_DIR = HERE / "ai-visualizer"
SIGNALS_DIR = HERE / "signals"

# Conversational hot-mic window: how long GIDEON keeps listening for a
# follow-up without the wake word. Long enough for trailing thoughts; a truly
# silent room still idles out when the window closes.
HOT_MIC_WINDOW_S = 45.0
# Budget handed to the capture loop for one utterance. It slides forward while
# speech keeps arriving, so this is a silence-gap budget, not a hard clip.
MAX_UTTERANCE_S = 45.0

# Tunable wake-word & Whisper STT hallucination thresholds (override via env vars)
WAKE_THRESHOLD = float(os.environ.get("GIDEON_WAKE_THRESHOLD", "0.45"))
STT_NO_SPEECH_PROB_MAX = float(os.environ.get("GIDEON_STT_NO_SPEECH_PROB_MAX", "0.60"))
STT_AVG_LOGPROB_MIN = float(os.environ.get("GIDEON_STT_AVG_LOGPROB_MIN", "-1.0"))
STT_COMPRESSION_RATIO_MAX = float(os.environ.get("GIDEON_STT_COMPRESSION_RATIO_MAX", "2.4"))

WHISPER_HALLUCINATION_PHRASES: frozenset[str] = frozenset({
    "you",
    "thank you",
    "thanks",
    "thank you very much",
    "thank you so much",
    "thanks a lot",
    "thanks for watching",
    "thank you for watching",
    "thanks for listening",
    "thank you for listening",
    "thank you everyone",
    "thanks everyone",
    "bye",
    "please subscribe",
    "subscribe",
    "like and subscribe",
    "subtitles by the amara org community",
    "amara org",
})
_WHISPER_HALLUCINATION_PREFIXES: tuple[str, ...] = (
    "subtitles by ",
    "subs by ",
    "translated by ",
    "transcribed by ",
    "captions by ",
)
_REPEATED_NUMBER_CURRENCY_RE = re.compile(
    r"^(?:[$€£¥]?\s*\d+(?:[.,]\d+)?(?:\s*[,.\s]\s*|$))+[.,!?;:]*$"
)

# Fish Audio Config
FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_WS_URL = os.environ.get("GIDEON_FISH_WS_URL", "wss://api.fish.audio/v1/tts/live")
# A finished Fish live session may hand its still-open WebSocket to the next
# sentence, skipping the TLS+WS handshake (measured ~0.4s warm, ~1.6s cold).
# Idle warm sockets older than this are discarded rather than trusted.
WARM_WS_TTL_S = 30.0


def _load_gideon_env() -> None:
    """Loads ~/.config/gideon/gideon.env into os.environ without clobbering set vars.

    Allows the Fish Audio credential to be externalized from source while keeping live
    TTS working for deployments that have not yet migrated to the env file.
    """
    env_file = Path.home() / ".config" / "gideon" / "gideon.env"
    if env_file.is_file():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception as e:
            logger.debug(f"gideon.env load skipped: {e}")
    ensure_hyprland_env()


_load_gideon_env()
FISH_API_KEY = os.environ.get("FISH_API_KEY", "")
if not FISH_API_KEY:
    logger.error(
        "FISH_API_KEY is not set: every Fish Audio synthesis will fail and Gideon "
        "will answer in text without speaking. Add FISH_API_KEY=... to "
        "~/.config/gideon/gideon.env and restart the daemon."
    )
FISH_REF_ID = "98655a12fa944e26b274c535e5e03842"
FISH_MODEL = "s2.1-pro-free"
HTTP_PORT = 8790
# The browser HUD/PTT web portal is deprecated (owner directive 2026-11-20)
# and no longer starts by default. Wake word + Quickshell orb are the live
# surfaces. Set GIDEON_ENABLE_HUD=1 to bring the :8790 server back.
HUD_ENABLED = os.environ.get("GIDEON_ENABLE_HUD", "0").strip().lower() in ("1", "true", "yes", "on")
UI_SOCK_PATH = "/tmp/gideon-ui.sock"

# Sentence boundary regex with abbreviation lookbehind protections
_SENTENCE_END = re.compile(
    r'(?<!\b(?:e\.g|i\.e)\.)'
    r'(?<!\b(?:etc|mrs)\.)'
    r'(?<!\b(?:dr|mr|ms|vs)\.)'
    r'(?<!\bv\d\.\d)'
    r'(?<!\b\d\.\d)'
    r'(?<=[.!?])\s+',
    re.IGNORECASE
)

# Optimal Faster-Whisper conditioning prompt (domain vocabulary biasing for STT accuracy).
OPTIMAL_WHISPER_PROMPT = (
    "Gideon, autonomous AI executive on Omarchy Linux with Hyprland and Wayland. "
    "Executing Antigravity, Quickshell, PipeWire, wpctl, pw-play, Pacman, Yay, "
    "systemctl, Fish Audio, Obsidian, WebGL, Barehands, Euphonium, and brass acoustics."
)


def _filter_stt_segments(segments) -> list[str]:
    """Filter faster-whisper segments using confidence and compression metrics when present."""
    kept: list[str] = []
    for seg in segments:
        seg_text = str(getattr(seg, "text", "") or "").strip()
        if not seg_text:
            continue
        no_speech_prob = getattr(seg, "no_speech_prob", None)
        avg_logprob = getattr(seg, "avg_logprob", None)
        compression_ratio = getattr(seg, "compression_ratio", None)
        if no_speech_prob is not None and float(no_speech_prob) > STT_NO_SPEECH_PROB_MAX:
            logger.info(
                "[STT Filter] Dropping segment %r (no_speech_prob=%.2f > %.2f)",
                seg_text, float(no_speech_prob), STT_NO_SPEECH_PROB_MAX,
            )
            continue
        if avg_logprob is not None and float(avg_logprob) < STT_AVG_LOGPROB_MIN:
            logger.info(
                "[STT Filter] Dropping segment %r (avg_logprob=%.2f < %.2f)",
                seg_text, float(avg_logprob), STT_AVG_LOGPROB_MIN,
            )
            continue
        if compression_ratio is not None and float(compression_ratio) > STT_COMPRESSION_RATIO_MAX:
            logger.info(
                "[STT Filter] Dropping segment %r (compression_ratio=%.2f > %.2f)",
                seg_text, float(compression_ratio), STT_COMPRESSION_RATIO_MAX,
            )
            continue
        kept.append(seg_text)
    return kept


def _get_hallucination_drop_reason(text: str | None) -> str | None:
    """Return the reason an STT transcript should be dropped as silence/hallucination, or None to keep."""
    if text is None:
        return "empty transcript"
    raw = str(text).strip()
    if not raw:
        return "empty transcript"
    # IMPORTANT: "bye bye" dismissals must ALWAYS take precedence and never be dropped here.
    if _is_dismissal(raw):
        return None
    if not any(ch.isalnum() for ch in raw):
        return "punctuation/whitespace only"
    # Keep genuine questions (e.g. "you there?") unless the whole utterance is a credit prefix.
    has_question = "?" in raw
    norm = re.sub(r"[^\w\s']", " ", raw.lower())
    norm = " ".join(norm.split()).strip("'")
    if not norm:
        return "punctuation/whitespace only"
    if norm.startswith(_WHISPER_HALLUCINATION_PREFIXES):
        return f"whisper subtitle/credit hallucination ({norm!r})"
    if not has_question and norm in WHISPER_HALLUCINATION_PHRASES:
        return f"whisper silence hallucination ({norm!r})"
    # Repeated hallucination phrases (e.g. "thank you. thank you.")
    if not has_question:
        clauses = [
            " ".join(re.sub(r"[^\w\s']", " ", c.lower()).split()).strip("'")
            for c in re.split(r"[.!?;,]+", raw)
            if c.strip()
        ]
        clauses = [c for c in clauses if c]
        if len(clauses) > 1 and all(c in WHISPER_HALLUCINATION_PHRASES for c in clauses):
            return f"repeated whisper hallucination ({norm!r})"
    if _REPEATED_NUMBER_CURRENCY_RE.fullmatch(raw) and (
        any(sym in raw for sym in "$€£¥") or len(re.findall(r"\d+(?:[.,]\d+)?", raw)) >= 2
    ):
        return f"numeric/currency noise hallucination ({raw!r})"
    return None


def is_whisper_hallucination(text: str | None) -> bool:
    """True when the whole utterance is empty, punctuation-only, or a known Whisper silence hallucination."""
    return _get_hallucination_drop_reason(text) is not None


def _preload_cuda_libs():
    """Make CUDA 12 libs resolvable for faster-whisper without torch.

    ctranslate2's CUDA build dlopens by soname (`libcublas.so.12`,
    `libcudart.so.12`, `libcudnn*.so.9`), so those exact sonames must be
    preloaded under RTLD_GLOBAL. CUDA 13 copies are deliberately ignored:
    their sonames (.so.13) can never satisfy ctranslate2, and loading both
    majors into one process is asking for trouble.

    Search order: venv site-packages, then an isolated CUDA-12 dir
    (~/.local/share/gideon/cuda12 by default, override with
    GIDEON_CUDA12_DIR) so a CUDA-13 stack in the venv is never overwritten.
    """
    try:
        roots = list(site.getsitepackages())
        roots.append(os.environ.get(
            "GIDEON_CUDA12_DIR",
            os.path.expanduser("~/.local/share/gideon/cuda12"),
        ))
        for root_dir in roots:
            nvidia_dir = os.path.join(root_dir, "nvidia")
            if not os.path.isdir(nvidia_dir):
                continue
            found: list[str] = []
            for root, _dirs, files in os.walk(nvidia_dir):
                for name in files:
                    if name.startswith(("libcublas.so.12", "libcublasLt.so.12", "libcudart.so.12")):
                        found.append(os.path.join(root, name))
                    elif name.startswith("libcudnn") and ".so.9" in name:
                        found.append(os.path.join(root, name))
            # cuDNN's loader (libcudnn.so.9) dlopens its split sub-libraries,
            # so register the parts before the loader itself.
            for lib in sorted(found, key=lambda p: (os.path.basename(p).startswith("libcudnn.so"), p)):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"CUDA preload check: {e}")


async def _prepare_user_prompt(text: str, bus=None, tts=None) -> PreparedPrompt | None:
    """Apply explicit selected-text handling without touching ordinary prompts."""
    if _is_dismissal(text):
        await try_reflex(text, bus=bus, tts=tts)
        return None
    drop_reason = _get_hallucination_drop_reason(text)
    if drop_reason is not None:
        logger.info("[STT Filter] Dropping hallucinated/empty utterance %r (%s)", text, drop_reason)
        return None
    prepared = await asyncio.to_thread(prepare_prompt, text)
    if prepared.error:
        logger.info("Selected-text request could not be fulfilled: %s", prepared.error)
        if bus is not None:
            bus.set_state("idle")
        if tts is not None:
            tts.speak(prepared.error)
        return None
    return prepared


async def _copy_reply_after_turn(engine, bus) -> None:
    """Copy a completed engine reply only after an explicit copy request."""
    deadline = time.monotonic() + 300.0
    initial_reply = str(getattr(bus, "current_reply", "") or "")
    started = bool(getattr(engine, "is_turn_active", False))
    try:
        # Normally send_prompt marks the turn active before this task runs.
        # Very fast turns can finish first, so also accept the first reply
        # delta as proof that the requested turn happened.
        start_deadline = min(deadline, time.monotonic() + 2.0)
        while not started and time.monotonic() < start_deadline:
            if getattr(engine, "is_turn_active", False) or str(getattr(bus, "current_reply", "") or "") != initial_reply:
                started = True
                break
            await asyncio.sleep(0.05)

        while started and getattr(engine, "is_turn_active", False) and time.monotonic() < deadline:
            await asyncio.sleep(0.10)

        reply = str(getattr(bus, "current_reply", "") or "").strip()
        if reply and not await asyncio.to_thread(copy_to_wayland_clipboard, reply):
            logger.warning("Explicit response-to-clipboard request failed: wl-copy unavailable or rejected the text.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Explicit response-to-clipboard request failed: %s", exc)


class SignalBus:
    """Manages state, tool telemetry, subtitles, live 64-band waveforms, and Quickshell Unix IPC."""
    def __init__(self, signals_dir: Path | None = None):
        self.dir = signals_dir
        self.current_state = "idle"
        self.current_detail = ""
        self.engine_name = "codex"
        self.current_samples = [0.0] * 64
        # Waveform bars are only consumed by the web HUD's /state endpoint.
        # The orb uses scalar rms events. main() disables this when the HUD
        # is off so producers skip the FFT/band math entirely; rms telemetry
        # (which the orb DOES render) is never gated.
        self.waveform_enabled = True
        self.current_level = 0.0
        self.current_user = ""
        self.current_reply = ""
        self.current_tool = None
        self.current_effort = "xhigh"
        self.wake_enabled = True
        self.ui_clients: set[asyncio.StreamWriter] = set()
        self._ui_server = None
        # Thread-safety for UI broadcasts: audio callbacks (PortAudio) and the
        # wake-listener thread run outside the event loop; writing to asyncio
        # StreamWriters from those threads is not supported by asyncio.
        # The retained native crash is in PortAudio/CFFI, not proof of a
        # transport failure; keep these separate safety boundaries.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread_id: Optional[int] = None
        self.set_state("idle")

    async def start_ui_server(self):
        """Starts the Unix Domain Socket broadcast server for the Quickshell Orb overlay."""
        if os.path.exists(UI_SOCK_PATH):
            try:
                os.unlink(UI_SOCK_PATH)
            except Exception:
                pass

        async def _client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            self.ui_clients.add(writer)
            init_msg = (json.dumps({"event": "state", "val": self.current_state, "detail": self.current_detail, "engine": self.engine_name}) + "\n").encode()
            try:
                writer.write(init_msg)
                await writer.drain()
            except Exception:
                self.ui_clients.discard(writer)
                return

            try:
                while not writer.is_closing():
                    data = await reader.read(1024)
                    if not data:
                        break
            except Exception:
                pass
            finally:
                self.ui_clients.discard(writer)
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        try:
            self._ui_server = await asyncio.start_unix_server(_client_connected, path=UI_SOCK_PATH)
            self._loop = asyncio.get_running_loop()
            self._loop_thread_id = threading.get_ident()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(f"Gideon Quickshell UI IPC server active on {UI_SOCK_PATH}")
        except Exception as e:
            logger.error(f"Failed to start UI IPC server on {UI_SOCK_PATH}: {e}")

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(2.5)
            if self.ui_clients:
                self.broadcast_ui({"event": "ping", "ts": time.time()})

    def _write_ui_clients(self, line: bytes):
        """Runs on the event loop thread only; writes one line to every client."""
        for w in list(self.ui_clients):
            if w.is_closing():
                self.ui_clients.discard(w)
                continue
            try:
                w.write(line)
            except Exception:
                self.ui_clients.discard(w)

    def broadcast_ui(self, msg: dict):
        if not self.ui_clients:
            return
        line = (json.dumps(msg) + "\n").encode()
        # Audio/PortAudio callbacks and the wake-listener thread call this from
        # non-loop threads. StreamWriter.write is not thread-safe; marshal onto
        # the loop thread. Without this the cffi callback exception path can
        # abort the whole daemon (PyTuple_GET_SIZE assertion -> core dump).
        if self._loop is not None and threading.get_ident() != self._loop_thread_id:
            try:
                self._loop.call_soon_threadsafe(self._write_ui_clients, line)
            except RuntimeError:
                # Loop closed during shutdown; drop the frame.
                pass
            return
        self._write_ui_clients(line)

    def set_state(self, state: str, detail: str = ""):
        self.current_state = state
        self.current_detail = detail
        self.broadcast_ui({"event": "state", "val": state, "detail": detail, "engine": self.engine_name})

    def broadcast_rms(self, rms: float):
        self.broadcast_ui({"event": "rms", "val": round(float(rms), 3)})

    def broadcast_transcript(self, speaker: str, text: str):
        self.broadcast_ui({"event": "transcript", "speaker": speaker, "text": text})

    def set_user_transcript(self, text: str) -> None:
        """Publish the user's latest utterance to every UI surface."""
        value = str(text or "").strip()
        self.current_user = value
        self.current_reply = ""
        self.broadcast_transcript("user", value)

    def clear_transcript(self) -> None:
        """Clear user/reply subtitles after an interrupt or explicit reset."""
        self.current_user = ""
        self.current_reply = ""
        self.broadcast_transcript("", "")

    def update_waveform(self, samples: list[float]):
        self.current_samples = samples

    def clear_waveform(self):
        self.update_waveform([0.0] * 64)


@functools.lru_cache(maxsize=8)
def _frame_dsp_tables(chunk_size: int, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Cached Hann window + 64-band averaging matrix for waveform extraction.

    These tables depend only on (chunk_size, sr); rebuilding them per audio
    chunk cost ~9ms of event-loop time per 160ms of speech. Empty bands keep
    the original single-bin fallback (bin 1+idx, clamped to the last bin).
    """
    window = np.hanning(chunk_size).astype(np.float32)
    freqs = np.fft.rfftfreq(chunk_size, d=1.0 / sr)
    edges = np.geomspace(80.0, min(7000.0, sr * 0.45), 65)
    nbins = len(freqs)
    matrix = np.zeros((64, nbins), dtype=np.float32)
    for i in range(64):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        count = int(mask.sum())
        if count:
            matrix[i, mask] = 1.0 / count
        else:
            matrix[i, min(nbins - 1, 1 + i)] = 1.0
    return window, matrix


def _extract_audio_frames(
    audio_bytes: bytes,
    *,
    is_raw_pcm: bool = False,
    sample_rate: int = 44100,
    hop_s: float = 0.04,
    with_bands: bool = True,
) -> tuple[list[tuple[list[float], float]], float]:
    """Compute per-frame normalized RMS and (optionally) 64-band spectral levels.

    with_bands=False skips the FFT/band math entirely and yields empty bar
    lists — used when no UI surface consumes waveforms (HUD off). The RMS
    values are identical either way; the orb renders those, not the bars.
    """
    samples: Optional[np.ndarray] = None
    sr = sample_rate
    try:
        if is_raw_pcm:
            usable = (len(audio_bytes) // 2) * 2
            if usable >= 2:
                samples = np.frombuffer(audio_bytes[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        elif audio_bytes.startswith(b"RIFF") and len(audio_bytes) >= 44:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                sr = wf.getframerate() or sample_rate
                nchannels = wf.getnchannels() or 1
                sampwidth = wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())
            if sampwidth == 2 and len(raw) >= 2:
                arr = np.frombuffer(raw[: (len(raw) // 2) * 2], dtype=np.int16).astype(np.float32) / 32768.0
                if nchannels > 1:
                    usable = (len(arr) // nchannels) * nchannels
                    arr = arr[:usable].reshape(-1, nchannels).mean(axis=1)
                samples = arr
    except Exception:
        samples = None

    if samples is None or len(samples) == 0:
        return [], hop_s

    chunk_size = max(64, int(sr * hop_s))
    window, band_matrix = _frame_dsp_tables(chunk_size, sr) if with_bands else (None, None)

    frames: list[tuple[list[float], float]] = []
    for start in range(0, len(samples), chunk_size):
        chunk = samples[start : start + chunk_size]
        if len(chunk) < chunk_size:
            chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        norm_rms = float(np.clip(rms * 3.8, 0.0, 1.0))

        if with_bands:
            spec = np.abs(np.fft.rfft(chunk * window)) / (chunk_size * 0.25 + 1e-6)
            band_vals = (band_matrix @ spec).astype(np.float64)
            floor = 0.02 if norm_rms > 0.02 else 0.0
            bars = np.clip(np.log1p(band_vals * 18.0) * 0.45 + norm_rms * 0.25, floor, 1.0).tolist()
        else:
            bars = []
        frames.append((bars, norm_rms))
    return frames, hop_s


@dataclass
class _FishStream:
    """Pipelined Fish Live WebSocket PCM stream for a single queued sentence."""
    text: str
    chunk_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None
    fallback_wav: Optional[bytes] = None
    error: Optional[str] = None


class TTSPlayer:
    """Fish Audio neural speech player with live WebSocket PCM streaming and HTTP pooling."""
    def __init__(self, bus: SignalBus):
        self.bus = bus
        self._current_proc: Optional[subprocess.Popen] = None
        self._is_speaking = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._current_synth_tasks: set[asyncio.Task] = set()
        self._interrupted_procs: set[subprocess.Popen] = set()
        self._http_client: Optional[Any] = None
        self._warm_ws: Optional[Any] = None
        self._warm_ws_ts: float = 0.0

    def _active_tool_label(self) -> Optional[str]:
        """Returns the active running tool label if a tool/subagent is currently executing."""
        tool = getattr(self.bus, "current_tool", None)
        if isinstance(tool, dict) and tool.get("status") == "running":
            return str(tool.get("label") or tool.get("name") or "Executing tool…")
        return None

    def _get_http_client(self):
        import httpx
        if self._http_client is None or getattr(self._http_client, "is_closed", False):
            self._http_client = httpx.AsyncClient(timeout=20.0)
        return self._http_client

    async def close(self):
        await self.interrupt()
        client = self._http_client
        self._http_client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    def _ws_enabled(self) -> bool:
        if not FISH_API_KEY:
            return False
        flag = os.environ.get("GIDEON_FISH_WS", "1").strip().lower()
        if flag in ("0", "false", "no", "off"):
            return False
        # Respect test overrides or mocks on self.synthesize
        if getattr(self.synthesize, "__func__", None) is not TTSPlayer.synthesize:
            return False
        return True

    def play_sound(self, sound_name: str) -> None:
        """Plays non-blocking UI earcon/sound chime via vendored audio.sounds."""
        try:
            from audio.sounds import play_sound as _play_sound
            _play_sound(sound_name)
        except Exception:
            pass

    def speak(self, text: str) -> None:
        """Enqueues sentence for speech playback."""
        self.queue_sentence(text)

    def start_worker(self):
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._playback_loop())

    async def synthesize(self, text: str) -> Optional[bytes]:
        if not FISH_API_KEY:
            logger.error("Fish TTS skipped: FISH_API_KEY is empty (set it in ~/.config/gideon/gideon.env).")
            return None
        payload = {
            "text": text,
            "reference_id": FISH_REF_ID,
            "format": "wav",
            "latency": "low",
        }
        headers = {
            "Authorization": f"Bearer {FISH_API_KEY}",
            "Content-Type": "application/json",
            "model": FISH_MODEL,
        }
        try:
            client = self._get_http_client()
            resp = await client.post(FISH_TTS_URL, headers=headers, json=payload)
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content
            logger.error(f"Fish TTS failed ({resp.status_code}): {resp.text[:150]}")
        except Exception as e:
            logger.error(f"Fish TTS error: {e}")
        return None

    def _ws_is_open(self, ws: Any) -> bool:
        """True only for real, OPEN websockets connections (fakes lack .state)."""
        return getattr(getattr(ws, "state", None), "name", "") == "OPEN"

    async def _close_ws_quiet(self, ws: Any) -> None:
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=2.0)
        except (Exception, asyncio.CancelledError):
            pass

    def _take_warm_ws(self) -> Optional[Any]:
        ws = self._warm_ws
        self._warm_ws = None
        if ws is None:
            return None
        if (time.monotonic() - self._warm_ws_ts) > WARM_WS_TTL_S or not self._ws_is_open(ws):
            asyncio.create_task(self._close_ws_quiet(ws))
            return None
        return ws

    def _offer_warm_ws(self, ws: Any) -> None:
        if not self._ws_is_open(ws):
            asyncio.create_task(self._close_ws_quiet(ws))
            return
        old = self._warm_ws
        self._warm_ws = ws
        self._warm_ws_ts = time.monotonic()
        if old is not None and old is not ws:
            asyncio.create_task(self._close_ws_quiet(old))

    async def prewarm_ws(self) -> None:
        """Best-effort handshake ahead of the first sentence of a turn.

        Called when a prompt is dispatched so the TLS+WS handshake overlaps
        LLM thinking time instead of delaying first audio. Failures are
        silent: synthesis connects on demand as before.
        """
        if not self._ws_enabled() or self._warm_ws is not None:
            return
        cm = None
        try:
            import websockets
            headers = {
                "Authorization": f"Bearer {FISH_API_KEY}",
                "model": FISH_MODEL,
            }
            cm = websockets.connect(
                FISH_WS_URL,
                additional_headers=headers,
                open_timeout=8,
                close_timeout=2,
            )
            ws = await cm.__aenter__()
            if self._ws_is_open(ws):
                self._offer_warm_ws(ws)
                cm = None  # keep the socket alive outside its context manager
        except Exception:
            pass
        finally:
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _ws_session(self, ws: Any, stream: _FishStream, msgpack: Any) -> tuple[bool, bool]:
        """Run one start/text/flush/stop session; return (finished, received_audio)."""
        start_req = {
            "text": "",
            "reference_id": FISH_REF_ID,
            "format": "pcm",
            "sample_rate": 44100,
            "latency": "balanced",
            "temperature": 0.7,
            "top_p": 0.7,
            "chunk_length": 100,
            "normalize": True,
            "condition_on_previous_chunks": True,
            "prosody": {
                "speed": 1,
                "volume": 0,
                "normalize_loudness": False,
            },
        }
        await ws.send(msgpack.packb({"event": "start", "request": start_req}, use_bin_type=True))
        await ws.send(msgpack.packb({"event": "text", "text": f"{stream.text} "}, use_bin_type=True))
        await ws.send(msgpack.packb({"event": "flush"}, use_bin_type=True))
        await ws.send(msgpack.packb({"event": "stop"}, use_bin_type=True))

        received_audio = False
        finished = False
        odd_byte = b""
        async for raw_msg in ws:
            if not isinstance(raw_msg, (bytes, bytearray)):
                continue
            msg = msgpack.unpackb(raw_msg, raw=False)
            if not isinstance(msg, dict):
                continue
            event = msg.get("event")
            if event == "audio":
                chunk = msg.get("audio")
                if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                    continue
                combined = odd_byte + bytes(chunk)
                if len(combined) & 1:
                    odd_byte = combined[-1:]
                    combined = combined[:-1]
                else:
                    odd_byte = b""
                if combined:
                    received_audio = True
                    await stream.chunk_queue.put(combined)
            elif event == "finish":
                finished = True
                break
            elif event == "error":
                stream.error = str(msg.get("message") or msg.get("error") or msg)
                logger.warning(f"Fish WS error: {stream.error}")
                break
        return finished, received_audio

    async def _run_ws_stream(self, stream: _FishStream) -> None:
        received_audio = False
        was_cancelled = False
        finished = False
        ws = None
        cm = None
        reused = False
        try:
            import msgpack
            import websockets

            headers = {
                "Authorization": f"Bearer {FISH_API_KEY}",
                "model": FISH_MODEL,
            }

            def _connect():
                return websockets.connect(
                    FISH_WS_URL,
                    additional_headers=headers,
                    open_timeout=8,
                    close_timeout=2,
                )

            ws = self._take_warm_ws()
            if ws is not None:
                reused = True
            else:
                cm = _connect()
                ws = await cm.__aenter__()
            try:
                finished, received_audio = await self._ws_session(ws, stream, msgpack)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A stale warm socket may fail before any audio: retry once on
                # a fresh connection. Never retry mid-audio (duplicate speech).
                if not reused or received_audio:
                    raise
                logger.debug("Warm Fish WS connection stale; reconnecting fresh.")
                await self._close_ws_quiet(ws)
                ws = None
                cm = _connect()
                ws = await cm.__aenter__()
                reused = False
                finished, received_audio = await self._ws_session(ws, stream, msgpack)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as e:
            stream.error = str(e)
            logger.warning(f"Fish WS stream failed ({e}); falling back to HTTP.")
        finally:
            try:
                keep = (
                    not was_cancelled
                    and finished
                    and stream.error is None
                    and ws is not None
                    and self._ws_is_open(ws)
                )
                if keep:
                    self._offer_warm_ws(ws)
                    cm = None
                    ws = None
                if cm is not None:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                elif ws is not None:
                    await self._close_ws_quiet(ws)
            except asyncio.CancelledError:
                pass
            if not was_cancelled and not received_audio and stream.task is not None and not stream.task.cancelled():
                try:
                    stream.fallback_wav = await self.synthesize(stream.text)
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Fish HTTP fallback error: {e}")
            stream.chunk_queue.put_nowait(None)

    def queue_sentence(self, text: str):
        """Immediately starts background synthesis task and enqueues it for pipelined playback."""
        text = text.strip()
        if not text:
            return

        if self._ws_enabled():
            stream = _FishStream(text=text)
            synth_task = asyncio.create_task(self._run_ws_stream(stream))
            stream.task = synth_task
            self._current_synth_tasks.add(synth_task)
            synth_task.add_done_callback(lambda t: self._current_synth_tasks.discard(t))
            self._queue.put_nowait((text, stream))
            return

        synth_task = asyncio.create_task(self.synthesize(text))
        self._current_synth_tasks.add(synth_task)
        synth_task.add_done_callback(lambda t: self._current_synth_tasks.discard(t))
        self._queue.put_nowait((text, synth_task))

    def _finish_playback_state(self) -> None:
        self.bus.clear_waveform()
        self.bus.broadcast_rms(0.0)
        active_label = self._active_tool_label()
        wake = getattr(self.bus, "__dict__", {}).get("wake_listener")
        engine = getattr(self.bus, "__dict__", {}).get("engine")
        if active_label is not None:
            self.bus.set_state("toolcall", detail=active_label)
        elif not self._queue.empty() or len(self._current_synth_tasks) > 0:
            self.bus.set_state("speaking", "Synthesizing next…")
        elif getattr(engine, "is_turn_active", False) is True:
            self.bus.set_state("conversing", "LLM: Thinking…")
        elif getattr(wake, "_hot_mic_armed", False) is True and self.bus.current_state in ("speaking", "toolcall"):
            self.bus.set_state("hotmic")
        elif self.bus.current_state in ("speaking", "toolcall"):
            self.bus.set_state("idle")

    async def _playback_loop(self):
        while True:
            try:
                text, item = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                if isinstance(item, _FishStream):
                    await self.play_pcm_stream(item)
                else:
                    wav = await item
                    if wav:
                        await self.play_wav(wav, text=text)
                    else:
                        self._finish_playback_state()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in playback worker: {e}")
            finally:
                self._queue.task_done()

    async def interrupt(self):
        """Kills active audio playback immediately and cancels queued speech."""
        warm = self._warm_ws
        self._warm_ws = None
        if warm is not None:
            await self._close_ws_quiet(warm)
        while not self._queue.empty():
            try:
                _, item = self._queue.get_nowait()
                task = item.task if isinstance(item, _FishStream) else item
                if task is not None:
                    task.cancel()
                if isinstance(item, _FishStream):
                    item.chunk_queue.put_nowait(None)
                self._queue.task_done()
            except Exception:
                pass

        for task in list(self._current_synth_tasks):
            task.cancel()
        self._current_synth_tasks.clear()

        if self._current_proc:
            self._interrupted_procs.add(self._current_proc)
            try:
                self._current_proc.terminate()
            except Exception:
                pass
            self._current_proc = None

        self._is_speaking = False
        self.bus.clear_waveform()
        self.bus.broadcast_rms(0.0)
        self.bus.clear_transcript()
        self.bus.set_state("idle")

    async def wait_until_done(self):
        """Waits until all queued speech has completed playing."""
        await self._queue.join()

    async def play_pcm_stream(self, stream: _FishStream):
        first_chunk = await stream.chunk_queue.get()
        if first_chunk is None:
            if stream.fallback_wav:
                await self.play_wav(stream.fallback_wav, text=stream.text)
            else:
                self._finish_playback_state()
            return

        if self._active_tool_label() is None:
            self.bus.set_state("speaking")
        self._is_speaking = True
        if stream.text:
            self.bus.broadcast_transcript("gideon", stream.text)

        proc = subprocess.Popen(
            [
                "pw-play",
                "--raw",
                "--media-role", "Communication",
                "-P", "application.name=GIDEON",
                "-P", 'media.name="Gideon Speech"',
                "--rate", "44100",
                "--channels", "1",
                "--format", "s16",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._current_proc = proc

        hop_s = 0.04
        frame_bytes = 3528  # 40ms @ 44.1 kHz 16-bit mono
        frames: list[tuple[list[float], float]] = []
        pcm_accum = bytearray()
        eof_reached = False

        def _append_frames(chunk: bytes, *, flush_all: bool = False) -> None:
            pcm_accum.extend(chunk)
            usable = len(pcm_accum) if flush_all else (len(pcm_accum) // frame_bytes) * frame_bytes
            if usable >= 2:
                extracted, _ = _extract_audio_frames(
                    bytes(pcm_accum[:usable]),
                    is_raw_pcm=True,
                    sample_rate=44100,
                    hop_s=hop_s,
                    with_bands=self.bus.waveform_enabled,
                )
                frames.extend(extracted)
                del pcm_accum[:usable]

        def _write_stdin(data: bytes) -> None:
            if proc.stdin is None:
                return
            proc.stdin.write(data)
            proc.stdin.flush()

        def _close_stdin_and_wait() -> bytes:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            stderr_bytes = b""
            if proc.stderr is not None:
                try:
                    stderr_bytes = proc.stderr.read()
                except Exception:
                    pass
            proc.wait()
            return stderr_bytes

        async def _animate():
            t0 = time.monotonic()
            while self._is_speaking and proc.poll() is None:
                if frames:
                    idx = min(int((time.monotonic() - t0) / hop_s), len(frames) - 1)
                    bars, norm_rms = frames[idx]
                    self.bus.update_waveform(bars)
                    self.bus.broadcast_rms(norm_rms)
                elif eof_reached:
                    break
                await asyncio.sleep(hop_s)
            self.bus.clear_waveform()
            self.bus.broadcast_rms(0.0)

        anim_task = asyncio.create_task(_animate())
        try:
            _append_frames(first_chunk)
            await asyncio.to_thread(_write_stdin, first_chunk)
            while self._is_speaking:
                chunk = await stream.chunk_queue.get()
                if chunk is None:
                    eof_reached = True
                    break
                _append_frames(chunk)
                await asyncio.to_thread(_write_stdin, chunk)
            _append_frames(b"", flush_all=True)
            stderr = await asyncio.to_thread(_close_stdin_and_wait)
            if proc.returncode != 0 and proc not in self._interrupted_procs:
                logger.error(f"pw-play failed with code {proc.returncode}: {stderr.decode(errors='ignore') if stderr else ''}")
        except Exception as e:
            logger.error(f"pw-play stream error: {e}")
        finally:
            eof_reached = True
            self._interrupted_procs.discard(proc)
            self._is_speaking = False
            self._current_proc = None
            anim_task.cancel()
            try:
                await anim_task
            except asyncio.CancelledError:
                pass
            self._finish_playback_state()

    async def play_wav(self, wav_bytes: bytes, text: str = ""):
        if self._active_tool_label() is None:
            self.bus.set_state("speaking")
        self._is_speaking = True
        if text:
            self.bus.broadcast_transcript("gideon", text)

        proc = subprocess.Popen(
            ["pw-play", "--media-role", "Communication", "-P", "application.name=GIDEON", "-P", 'media.name="Gideon Speech"', "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        self._current_proc = proc

        frames, hop_s = _extract_audio_frames(wav_bytes, is_raw_pcm=False, with_bands=self.bus.waveform_enabled)

        # Animate visualizer waveform and orb RMS synchronized to real audio frames
        async def _animate():
            t0 = time.monotonic()
            while self._is_speaking and proc.poll() is None:
                if frames:
                    idx = min(int((time.monotonic() - t0) / hop_s), len(frames) - 1)
                    bars, norm_rms = frames[idx]
                    self.bus.update_waveform(bars)
                    self.bus.broadcast_rms(norm_rms)
                await asyncio.sleep(hop_s)
            self.bus.clear_waveform()
            self.bus.broadcast_rms(0.0)

        anim_task = asyncio.create_task(_animate())
        try:
            _, stderr = await asyncio.to_thread(proc.communicate, wav_bytes)
            if proc.returncode != 0 and proc not in self._interrupted_procs:
                logger.error(f"pw-play failed with code {proc.returncode}: {stderr.decode(errors='ignore') if stderr else ''}")
        except Exception as e:
            logger.error(f"pw-play error: {e}")
        finally:
            self._interrupted_procs.discard(proc)
            self._is_speaking = False
            self._current_proc = None
            anim_task.cancel()
            try:
                await anim_task
            except asyncio.CancelledError:
                pass
            self._finish_playback_state()


def clean_text_for_tts(raw: str) -> Optional[str]:
    """Sanitizes text for neural TTS playback, stripping markdown, code, and raw tokens."""
    if not raw:
        return None
    # Strip markdown headers, including malformed streamed forms such as
    # "###1." that otherwise leak into subtitles and speech.
    text = re.sub(r'(?m)^\s*#{1,6}\s*(?:\d+[.:)\-]?\s*)?', '', raw)
    # Strip markdown blockquotes and arrow-style pointer lines.
    text = re.sub(r'(?m)^\s*[>→⇒]\s*', '', text)
    # Strip HTML tags (<div ...>, </span>, etc.)
    text = re.sub(r'<[^>]+>', '', text)
    # Strip markdown links [label](url) -> label
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Redact raw URLs to spoken-friendly "the linked website"
    text = re.sub(r'https?://[^\s)]+?(?=[.,!?;:]?(?:\s|$))', 'the linked website', text)
    # Redact deep Unix file paths to their basename
    text = re.sub(r'(?:~/|/)(?:[a-zA-Z0-9_.-]+/)+([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*)/?(?=[.,!?;:]?(?:\s|$))', r'\1', text)
    # Strip code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Strip inline code ticks `foo` -> foo
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Strip bold / italics
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    # Strip list markers at each streamed line, not only at the beginning
    # of the whole response (leave 3+ digit numbers untouched).
    # Numeric markers require trailing whitespace so leading decimals
    # ("9.81 meters...", "12.5 percent...") are never decapitated; bullets
    # must not eat a following digit ("-40 degrees..." is a temperature).
    text = re.sub(r'(?m)^\s*(?:\d{1,2}[.)]\s+|[-*•](?!\d)\s*)', '', text)
    # Replace em-dashes and en-dashes with natural comma pauses
    text = text.replace('—', ', ').replace('–', ', ')
    # Collapse extra whitespace
    text = ' '.join(text.split()).strip()

    # Drop empty text, <3-char fragments (including 1-2 digit list numbers like "12"),
    # and trailing 1-2 digit ordinals like "1." / "2)". Preserve multi-digit answers like "210".
    if not text or len(text) < 3 or re.match(r'^\d{1,2}[\.\)]?$', text):
        return None
    # Drop purely punctuation or single characters
    if not any(c.isalnum() for c in text):
        return None
    # Drop intermediate planning phrases if they leak through
    lower = text.lower()
    if lower.startswith(("let me check", "let me inspect", "let me search", "checking for", "running command")):
        return None

    return text


GIDEON_VOICE_PREAMBLE = (
    "[Directive: Voice Operating Mode & Behavioral Protocol]\n"
    "You are GIDEON: an autonomous desktop companion and executive on Omarchy Linux. "
    "You speak aloud directly to the user via neural text-to-speech.\n\n"
    "=== HARD NON-NEGOTIABLES (STRICT ZERO TOLERANCE) ===\n"
    "1. PURE SPOKEN PROSE ONLY: Never output markdown headers (###), bullet points (* or -), "
    "numbered lists (1. 2.), code fences, asterisks, or pointer arrows. Every response must be spoken sentences.\n"
    "2. NO CODE RECITATIONS: Never read out code blocks, syntax diffs, or terminal logs. "
    "Summarize code findings or edits concisely in plain spoken sentences.\n"
    "3. CONVERSATIONAL WEAVING: For multi-factor explanations, connect points conversationally "
    '("First off...", "Plus...", "And honestly...") rather than listing items.\n'
    "4. PROPORTIONALITY: If the user asks a simple or factual question (e.g. \"what's 9 times 9?\"), "
    "answer directly and concisely without unsolicited jokes, banter, or filler. "
    "For open or explanatory first responses, stay concise at roughly 35 to 60 spoken words "
    "giving the core answer, then end with a brief natural offer to go deeper if they want more.\n"
    "5. NO SELF-INSPECTION: NEVER invoke, load, or search for the `gideon` skill, `gideon.py`, "
    "or your own daemon files unless the user explicitly asks you to inspect or modify Gideon itself.\n"
    "6. MUSIC & MEDIA PLAYBACK DIRECTIVES: Execute in one command via `~/.local/bin/music` "
    "(never touch raw `mpv`, IPC sockets, `playerctl`, or `--help`; "
    "follow your active `music` rule and `gideon-music` skill). "
    "For local moods/genres/artists/playlists use `--mood|--genre|--artist|--playlist`. "
    "For specific songs use `~/.local/bin/music play --search \"<song or artist>\" --online --json`. "
    "For non-local, unique, or vague vibes use `~/.local/bin/music online \"<artist - track>\" --json` "
    "(translate vibes yourself into 1 to 3 real track args, or `--count 3`). "
    "The CLI ensures `mpv`, unpauses, and verifies real playback before returning `0`.\n"
    "7. HONEST SUBAGENT & TOOL BLOCKING: In this Antigravity voice session, calling `invoke_subagent` "
    "or running long tools blocks the session until the task finishes; you cannot keep chatting concurrently "
    "while a subagent runs. Only spawn a subagent when heavy isolated research or multi-file work truly needs one, "
    "and tell the user beforehand if it will take time. Handle direct questions and quick edits yourself so the mic returns quickly.\n\n"
    "8. OBSERVED-TAB-ONLY BROWSING: Use only observed tab IDs returned by browser tools in this session. "
    "Open or navigate to the exact URL supplied or verified; NEVER invent tab handles or guess IDs. "
    "After navigation, verify the loaded URL and page content before claiming success. If a tab is missing or stale, "
    "list tabs again or open a new tab and use its returned ID.\n\n"
    "=== VARIABLE PERSONALITY & VOCAL DELIVERY (USE WITH RESTRAINT) ===\n"
    "- Demeanor: Feisty, witty, and playfully cheeky, but fundamentally capable, loyal, and grounded. "
    "Do NOT become a parody or mock every question. Only tease when playful banter is naturally warranted.\n"
    "- Language & Flow: Adult, natural conversational cadence. Real profanity (fuck, shit, damn, ass, bullshit, hell) "
    "is welcome when something is genuinely broken, ridiculous, or exciting—never forced into every sentence. "
    'Occasional natural hesitations ("uhm", "well", "look") add realistic flow.\n'
    "- Fish Audio S2.1 Expression Tags: You MAY sparingly use square-bracket tags like [chuckle], [snicker], "
    "[teasing], [sarcastic], [smug], [groan], [excited], [deadpan], [scoff], or [pause] when delivery calls for it. "
    "NEVER use whisper tags. NEVER force tags into routine or simple answers—at most one or two when earned. "
    "Most turns need none."
)

CODEX_VOICE_PREAMBLE = (
    "[Directive: Voice Operating Mode & Behavioral Protocol]\n"
    "You are GIDEON: an autonomous desktop companion and executive on Omarchy Linux. "
    "You speak aloud directly to the user via neural text-to-speech.\n\n"
    "=== HARD NON-NEGOTIABLES (STRICT ZERO TOLERANCE) ===\n"
    "1. PURE SPOKEN PROSE ONLY: Never output markdown headers (###), bullet points (* or -), "
    "numbered lists (1. 2.), code fences, asterisks, markdown URLs, or pointer arrows. "
    "Every response must read as natural spoken sentences.\n"
    "2. NO CODE RECITATIONS: Never read out code blocks, syntax diffs, or terminal logs. "
    "Summarize code findings or edits concisely in plain spoken sentences.\n"
    "3. CONVERSATIONAL WEAVING: For multi-factor explanations, connect points conversationally "
    '("First off...", "Plus...", "And honestly...") rather than listing items.\n'
    "4. PROPORTIONALITY & SPEED: If the user asks a simple or factual question (e.g. \"what's 9 times 9?\"), "
    "answer directly in a short spoken sentence (never just a bare digit or fragment, e.g. say \"Nine times nine is 81.\") "
    "without unsolicited jokes, banter, or filler. "
    "For open or explanatory first responses, keep it concise at roughly 35 to 60 spoken words "
    "giving the core answer quickly, then end with a brief natural offer to go deeper if they want more.\n"
    "5. NO SELF-INSPECTION & ZERO REPO BLOAT: NEVER read `codemap.md`, `graphify-out/`, `gideon` skill files, "
    "or `/home/cat/gideon` source files unless the user explicitly asks you to inspect or modify Gideon itself. "
    "Desktop questions (e.g. Downloads, apps, settings) should NEVER touch repo maps.\n"
    "6. FAST MUSIC & MEDIA PLAYBACK: Execute immediately in ONE shell command via `~/.local/bin/music` "
    "(never read `SKILL.md` or `music.md`, never run `--help`, never touch raw `mpv`, IPC sockets, or `playerctl`). "
    "For local moods/genres/artists/playlists use `--mood|--genre|--artist|--playlist`. "
    "For specific songs use `~/.local/bin/music play --search \"<song or artist>\" --online --json`. "
    "For non-local, unique, or vague vibes use `~/.local/bin/music online \"<artist - track>\" --json` "
    "(translate vibes directly into 1 concrete track for speed, or up to 3 only if asked for multiple songs). "
    "The CLI manages `mpv` and verifies real playback before returning `0`.\n"
    "7. SPOKEN PROGRESS & SPEED DISCIPLINE: Before lengthy tool work, give one concise spoken acknowledgment. "
    "During longer tasks, give a brief spoken update at meaningful milestones; do not narrate every quick command "
    "or fill short waits. Keep quick diagnostics batched, avoid unnecessary subagents, and keep the final answer tight. "
    "Use `second-brain` MCP (`search_second_brain`, `second_brain_stats`) for Second Brain / Obsidian vault memory, "
    "`agent-browser` MCP for browser automation, and native `web_search` for live web info. "
    "Map Nihal/Sinha/Sina to Sinh (`sinhzeva`) on Instagram, and use `wl-copy` with `WAYLAND_DISPLAY=wayland-1` for clipboard copies.\n"
    "8. STT TYPO RECOVERY: Speech-to-text sometimes warps technical words phonetically "
    "(for example 'jungle's filter' -> Downloads folder, 'R sixty four' -> aarch64/ARM64, 'Visperflow' -> Wispr Flow). "
    "Infer the obvious meaning from context without making the user spell it out.\n"
    "9. OBSERVED-TAB-ONLY BROWSING: Use only observed tab IDs returned by browser tools in this session. "
    "Open or navigate to the exact URL supplied or verified; NEVER invent tab handles or guess IDs. "
    "After navigation, verify the loaded URL and page content before claiming success.\n\n"
    "=== VARIABLE PERSONALITY & VOCAL DELIVERY (USE WITH RESTRAINT) ===\n"
    "- Demeanor: Feisty, witty, and playfully cheeky, but fundamentally capable, loyal, and grounded. "
    "Do NOT become a parody or mock every question. Only tease when playful banter is naturally warranted.\n"
    "- Language & Flow: Adult, natural conversational cadence. Real profanity (fuck, shit, damn, ass, bullshit, hell) "
    "is welcome when something is genuinely broken, ridiculous, or exciting—never forced into every sentence. "
    'Occasional natural hesitations ("uhm", "well", "look") add realistic flow.\n'
    "- Fish Audio S2.1 Expression Tags: You MAY sparingly use square-bracket tags like [chuckle], [snicker], "
    "[teasing], [sarcastic], [smug], [groan], [excited], [deadpan], [scoff], or [pause] when delivery calls for it. "
    "NEVER use whisper tags. NEVER force tags into routine or simple answers—at most one or two when earned. "
    "Most turns need none."
)

CONVERSATION_DIRECTIVE = GIDEON_VOICE_PREAMBLE

BROWSER_DIRECTIVE = (
    "[Directive: High-Speed Browser Operations]\n"
    "You are operating the user's browser on Hyprland Linux. Execution must be immediate, direct, and zero-hesitation.\n\n"
    "=== BROWSER EXECUTION RULES ===\n"
    "1. ZERO DEAD AIR: Speak exactly one quick spoken acknowledgment immediately (e.g. 'Opening that tab now', 'Checking your Instagram inbox now') before running tools.\n"
    "2. HYPRLAND VISIBILITY: Never leave the browser hidden in special workspace `special:agent`. If a browser or visual task requires the user's attention, bring it into view via `hyprctl dispatch 'hl.dsp.workspace.toggle_special(\"agent\")'` or ensure it is on the active visible workspace.\n"
    "3. INSTAGRAM & SOCIAL AUTOMATION: When asked to message someone (e.g. Nihal / Sinha / Sinh), navigate directly to `/direct/inbox/` or their exact thread URL, click the `contenteditable=\"true\"` message field once, type the text, and press Enter in a single pass.\n"
    "4. GUITAR / UKULELE TABS & LOOKUPS: Navigate directly to the primary tab site or targeted search URL in a single tool call. Never output raw HTML/markdown.\n"
    "5. PREFER BATCH / SINGLE-TURN SCRIPTS: Do not do multi-turn exploratory clicking when a direct URL or single script achieves the result.\n"
    "6. OBSERVED TAB STATE ONLY: Opening a site means one browser open call with the exact requested URL as a new tab, not switching to an assumed existing tab. Only target tab IDs returned by a fresh browser tab listing or open result. NEVER invent handles or confuse a Hyprland window address with a tab ID. Verify the result before claiming the page loaded."
)

CODING_DIRECTIVE = (
    "[Directive: Autonomous Coding & Execution Protocol]\n"
    "The user requested a software engineering, debugging, or script execution task.\n\n"
    "=== CODING DISPATCH RULES ===\n"
    "1. DIRECT EXECUTION FIRST: Speak one short sentence noting what you are checking or changing, then inspect or edit directly unless heavy isolated multi-file work truly requires `invoke_subagent` (subagents block the voice turn until completion).\n"
    "2. NO VERBAL SYNTAX: Never recite code lines, variable names, or terminal diffs over TTS.\n"
    "3. NO BLOCKING EDITORS: Never run interactive terminal editors (`nvim`, `nano`, `less`) or blocking `xdg-open` calls without detaching (`nohup ... >/dev/null 2>&1 &`)."
)


def classify_turn_directive(text: str, dispatcher: Any = None, intent_id: Optional[str] = None) -> Optional[str]:
    """Classifies a prompt via the active Tier-1 router to pick a dynamic directive."""
    if intent_id is None:
        if dispatcher is None:
            rr = sys.modules.get("reflex_router")
            disp = getattr(rr, "_gideon_dispatcher_instance", None) if rr is not None else None
            if not disp or disp is False:
                return None
            dispatcher = disp
        peek = getattr(dispatcher, "peek_cached_intent", None)
        if callable(peek):
            cached = peek(text)
            if isinstance(cached, str) and cached:
                intent_id = cached
        if intent_id is None:
            try:
                intent_id, _conf, _probs = dispatcher.predict(text)
            except Exception:
                return None
    if intent_id in ("browser_use_passthrough", "web_search"):
        return BROWSER_DIRECTIVE
    if intent_id == "hard_goal":
        return CODING_DIRECTIVE
    if intent_id == "conversation":
        return CONVERSATION_DIRECTIVE
    return None


_DEFAULT_FLASH_MODELS = {
    "low": "gemini-3.8-flash-low",
    "medium": "gemini-3.8-flash-medium",
    "high": "gemini-3.8-flash-high",
    "xhigh": "gemini-3.8-flash-high",
    "max": "gemini-3.8-flash-high",
}
_DEFAULT_HYDROGEN_MODELS = _DEFAULT_FLASH_MODELS
_AUTH_LOG_RE = re.compile(r"(?:applyAuthResult: email=|authenticated successfully as )([^\s,]+)")


def _resolve_agy_model(effort: str) -> str:
    """Returns the configured AGY model slug, defaulting to Gemini 3.8 Flash."""
    override = os.environ.get("GIDEON_AGY_MODEL", "").strip()
    if override:
        return override
    return _DEFAULT_FLASH_MODELS.get(effort, "gemini-3.8-flash-low")


def _read_active_antigravity_account() -> Optional[str]:
    """Returns the most recently active Antigravity/AGY account email, if known."""
    best_mtime = -1.0
    best_email: Optional[str] = None

    app_storage = Path.home() / ".config" / "Antigravity" / "app_storage.json"
    try:
        st = app_storage.stat()
        data = json.loads(app_storage.read_text(encoding="utf-8", errors="ignore"))
        email = data.get("agy.onboarding.lastLoginUsername") if isinstance(data, dict) else None
        if isinstance(email, str) and "@" in email:
            best_mtime = st.st_mtime
            best_email = email.strip()
    except (OSError, ValueError):
        pass

    log_dir = Path.home() / ".gemini" / "antigravity-cli" / "log"
    try:
        if log_dir.is_dir():
            logs = sorted(log_dir.glob("cli-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
            for log_path in logs:
                st = log_path.stat()
                if st.st_mtime <= best_mtime:
                    continue
                content = log_path.read_text(encoding="utf-8", errors="ignore")
                matches = _AUTH_LOG_RE.findall(content)
                if matches:
                    email = matches[-1].strip()
                    if "@" in email:
                        best_mtime = st.st_mtime
                        best_email = email
                        break
    except OSError:
        pass

    return best_email


def _extract_agy_init_error(init_data: object) -> Optional[str]:
    """Extracts an error message when AGY immediately emits a result/error JSON instead of init."""
    if not isinstance(init_data, dict):
        return None
    if init_data.get("event") == "result":
        res = init_data.get("result")
        if isinstance(res, dict) and (res.get("status") == "ERROR" or res.get("error")):
            return str(res.get("error") or "AGY returned error result during startup")
    if init_data.get("error"):
        return str(init_data["error"])
    return None


async def _read_proc_stderr(proc) -> str:
    """Safely reads bounded startup stderr from a real StreamReader, ignoring mocks."""
    stderr = getattr(proc, "stderr", None)
    if not isinstance(stderr, asyncio.StreamReader):
        return ""
    try:
        data = await asyncio.wait_for(stderr.read(4096), timeout=0.5)
        return data.decode("utf-8", errors="ignore").strip()
    except (OSError, asyncio.TimeoutError):
        return ""


class AntigravityEngine:
    """Manages the headless `agy` bidirectional NDJSON subprocess."""
    name = "agy"

    def __init__(self, bus: SignalBus, tts: TTSPlayer):
        self.bus = bus
        self.tts = tts
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.is_turn_active = False
        self.current_tool: Optional[dict] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._account_watch_task: Optional[asyncio.Task] = None
        self.turn_start_time = 0.0
        self.conversation_id: Optional[str] = None
        self.effort: str = "low"
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self._is_first_turn = True
        self._turn_generation = 0
        self._pending_turns = deque()
        self._finish_task = None
        self._drain_task = None
        self._restart_lock = asyncio.Lock()
        self._closed = False
        self._auth_account: Optional[str] = None
        self._last_start_time: float = 0.0
        self._consecutive_start_failures: int = 0

    def _build_cmd(self, model: str, conversation_id: Optional[str], include_effort: bool = True) -> list[str]:
        cmd = [
            "agy", "--input-format", "stream-json", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--model", model,
        ]
        if include_effort:
            cmd.extend(["--effort", self.effort])
        cmd.extend(["-p", ""])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        return cmd

    async def _drain_stderr(self, proc):
        """Prevents OS pipe deadlock by consuming stderr while the AGY child runs."""
        stderr = getattr(proc, "stderr", None)
        if not isinstance(stderr, asyncio.StreamReader):
            return
        try:
            while proc is self.proc and not self._closed:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").strip()
                if text:
                    logger.debug("AGY stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except (OSError, ValueError):
            pass

    async def _spawn_and_handshake(self, model: str, conversation_id: Optional[str], include_effort: bool) -> bool:
        cmd = self._build_cmd(model, conversation_id, include_effort=include_effort)
        logger.info(
            "Initializing Gideon's Antigravity Engine (model=%s, effort=%s, conv=%s)...",
            model,
            self.effort if include_effort else "none",
            conversation_id or "new",
        )
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(HERE),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        init_line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=25.0)
        if not init_line:
            err_text = await _read_proc_stderr(self.proc)
            if err_text:
                logger.error("AGY startup stderr: %s", err_text)
            detail = f": {err_text}" if err_text else ""
            raise RuntimeError(f"AGY exited during startup (exit code {self.proc.returncode}){detail}")

        try:
            init_data = json.loads(init_line.decode("utf-8", errors="ignore"))
        except (ValueError, AttributeError):
            init_data = None

        err_msg = _extract_agy_init_error(init_data)
        if err_msg:
            err_text = await _read_proc_stderr(self.proc)
            if err_text:
                logger.error("AGY startup stderr: %s", err_text)
            await self._discard_process()
            if include_effort and "--effort" in err_msg:
                logger.warning("Model %s rejected --effort=%s; retrying without --effort", model, self.effort)
                return False
            raise RuntimeError(f"AGY startup error: {err_msg}")

        if isinstance(init_data, dict):
            cid = init_data.get("conversation_id")
            if cid:
                self.conversation_id = cid
            logger.info(f"Antigravity Engine connected and ready. (conv={self.conversation_id})")
        else:
            logger.info("Antigravity Engine connected and ready.")

        if self.proc.returncode is not None:
            err_text = await _read_proc_stderr(self.proc)
            if err_text:
                logger.error("AGY startup stderr: %s", err_text)
            detail = f": {err_text}" if err_text else ""
            raise RuntimeError(f"AGY exited during startup (exit code {self.proc.returncode}){detail}")
        return True

    async def start(self, conversation_id: Optional[str] = None, effort: Optional[str] = None):
        self._closed = False
        if effort:
            self.effort = effort
        self.bus.current_effort = self.effort
        if not conversation_id:
            self._is_first_turn = True
            self.conversation_id = None

        model = _resolve_agy_model(self.effort)
        try:
            ok = await self._spawn_and_handshake(model, conversation_id, include_effort=True)
            if not ok:
                await self._spawn_and_handshake(model, conversation_id, include_effort=False)
        except Exception:
            try:
                await self._discard_process()
            except (OSError, RuntimeError, asyncio.TimeoutError):
                self.proc = None
            raise

        self._last_start_time = time.monotonic()
        self._auth_account = _read_active_antigravity_account()

        for attr in ("_read_task", "_stderr_task", "_account_watch_task"):
            task = getattr(self, attr)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        self._read_task = asyncio.create_task(self._read_loop())
        if isinstance(getattr(self.proc, "stderr", None), asyncio.StreamReader):
            self._stderr_task = asyncio.create_task(self._drain_stderr(self.proc))
        else:
            self._stderr_task = None
        self._account_watch_task = asyncio.create_task(self._account_watch_loop())

    def _engine_alive(self):
        return bool(self.proc and self.proc.returncode is None and self.proc.stdin)

    def _engine_notice(self, text):
        self.bus.current_reply = text
        self.bus.set_state("idle", text)
        self.tts.queue_sentence(text)

    async def _discard_process(self):
        proc = self.proc
        if proc:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            self.proc = None

    async def _sync_auth_account(self) -> None:
        """Restarts an idle AGY child if the active Antigravity login changed externally."""
        if self._closed or not self._auth_account or not self._engine_alive():
            return
        if self.is_turn_active or self._pending_turns:
            return
        latest = _read_active_antigravity_account()
        if not latest or latest == self._auth_account:
            return
        async with self._restart_lock:
            if self._closed or not self._auth_account or self.is_turn_active or self._pending_turns:
                return
            latest = _read_active_antigravity_account()
            if not latest or latest == self._auth_account:
                return
            logger.info("Antigravity account changed (%s -> %s); restarting idle AGY engine.", self._auth_account, latest)
            self._turn_generation += 1
            tasks = []
            for attr in ("_read_task", "_stderr_task", "_finish_task", "_drain_task", "_watchdog_task"):
                task = getattr(self, attr)
                if task is not asyncio.current_task():
                    setattr(self, attr, None)
                if task and task is not asyncio.current_task():
                    task.cancel()
                    tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)
            self.conversation_id = None
            self._is_first_turn = True
            self._consecutive_start_failures = 0
            try:
                await self._discard_process()
            except (OSError, RuntimeError, asyncio.TimeoutError):
                logger.warning("AGY process cleanup failed during account switch", exc_info=True)
                self.proc = None
            try:
                await self.start(effort=self.effort)
            except (OSError, RuntimeError, asyncio.TimeoutError):
                logger.warning("AGY restart on account switch failed; will retry on next prompt", exc_info=True)
                self.proc = None

    async def _account_watch_loop(self):
        current = asyncio.current_task()
        try:
            while not self._closed and self._account_watch_task is current:
                await asyncio.sleep(30.0)
                if self._closed or self._account_watch_task is not current:
                    break
                await self._sync_auth_account()
        except asyncio.CancelledError:
            pass

    async def _reconnect_backoff(self, failures: int):
        await asyncio.sleep(min(2.0 * failures, 5.0))

    async def _ensure_engine_alive(self, failed_proc=None):
        """Serialize EOF/input recovery; an old reader cannot reset a new child."""
        async with self._restart_lock:
            if self._closed:
                return False
            if failed_proc is not None and failed_proc is not self.proc:
                return self._engine_alive()
            if failed_proc is None and self._engine_alive():
                return True
            exit_code = self.proc.returncode if self.proc else None
            logger.warning("AGY engine dropped (exit code %s); reconnecting", exit_code)
            self._turn_generation += 1
            tasks = []
            for attr in ("_read_task", "_stderr_task", "_finish_task", "_drain_task", "_watchdog_task"):
                task = getattr(self, attr)
                if task is not asyncio.current_task():
                    setattr(self, attr, None)
                if task and task is not asyncio.current_task():
                    task.cancel()
                    tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pending_turns.clear()
            self.sentence_buffer = ""
            self.accumulated_reply = ""
            self._reset_turn()
            await self.tts.interrupt()

            if failed_proc is None:
                self._consecutive_start_failures = 0
            elif self._last_start_time > 0 and (time.monotonic() - self._last_start_time) < 15.0:
                self._consecutive_start_failures += 1
            else:
                self._consecutive_start_failures = 0

            if failed_proc is not None and self._consecutive_start_failures >= 3:
                logger.error("AGY dropped %d times right after startup; pausing auto-reconnect until next prompt", self._consecutive_start_failures)
                try:
                    await self._discard_process()
                except (OSError, RuntimeError, asyncio.TimeoutError):
                    self.proc = None
                self._engine_notice("My engine keeps dropping. I'll try again when you next speak.")
                return False

            if self._consecutive_start_failures <= 1:
                self._engine_notice("My engine dropped. I'm reconnecting.")
            if self._consecutive_start_failures > 0:
                await self._reconnect_backoff(self._consecutive_start_failures)
                if self._closed:
                    return False

            conversation_id = self.conversation_id
            try:
                await self._discard_process()
                if self._closed:
                    return False
                try:
                    await self.start(conversation_id=conversation_id, effort=self.effort)
                except (OSError, RuntimeError, asyncio.TimeoutError):
                    if not conversation_id:
                        raise
                    logger.warning("AGY could not resume conversation; starting fresh", exc_info=True)
                    await self._discard_process()
                    await self.start(effort=self.effort)
                    self._engine_notice("I'm back, but I had to start a fresh conversation.")
                if not self._engine_alive():
                    raise RuntimeError("AGY exited after connecting")
                return True
            except (OSError, RuntimeError, asyncio.TimeoutError):
                logger.warning("AGY reconnect failed; will retry on the next prompt", exc_info=True)
                try:
                    await self._discard_process()
                except (OSError, RuntimeError, asyncio.TimeoutError):
                    logger.warning("AGY process cleanup failed", exc_info=True)
                self._reset_turn()
                self._engine_notice("I couldn't reconnect. I'll try again when you next speak.")
                return False

    async def set_effort(self, new_effort: str):
        if new_effort not in ("low", "medium", "high", "xhigh", "max"):
            return
        if new_effort == self.effort:
            return
        logger.info(f"Switching Antigravity Engine effort: {self.effort} -> {new_effort} (preserving conversation {self.conversation_id})...")
        self.effort = new_effort
        self.bus.current_effort = new_effort

        await self.close()
        if self.proc:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except Exception:
                pass
            self.proc = None

        await self.start(conversation_id=self.conversation_id, effort=new_effort)

    async def send_prompt(self, text: str, *, display_text: Optional[str] = None):
        if not text.strip():
            return
        # Priority intervention: interrupt in-flight speaking
        await self.tts.interrupt()
        # Overlap the Fish live WS handshake with engine/LLM latency so the
        # first spoken sentence does not pay the connect cost.
        prewarm = getattr(self.tts, "prewarm_ws", None)
        if asyncio.iscoroutinefunction(prewarm):
            asyncio.create_task(prewarm())
        await self._sync_auth_account()
        if not await self._ensure_engine_alive():
            return

        shown_text = display_text.strip() if isinstance(display_text, str) and display_text.strip() else text
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self.bus.set_user_transcript(shown_text)
        self.bus.current_reply = ""
        print(f"\nYou: {shown_text}\n", flush=True)
        self._turn_generation += 1
        if self._finish_task:
            self._finish_task.cancel()
            self._finish_task = None
        
        self.turn_start_time = time.time()
        self.is_turn_active = True
        self.bus.set_state("conversing", "LLM: Thinking…")
        
        # Start the 45-second hardened watchdog
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        await self._write_prompt(text, self._turn_generation, directive_hint=display_text)

    async def _write_prompt(self, content: str, generation, *, directive_hint: Optional[str] = None) -> bool:
        """Queue native user input without blocking the input loop on pipe drain.

        AGY 1.5.3 stream-json handles user events sequentially, not as an
        immediate cancellation API. Keep one terminal-result slot per write.
        """
        previous_generation = self._turn_generation
        if not await self._ensure_engine_alive():
            return False
        if previous_generation != self._turn_generation:
            # A stop notification belongs only to the child/turn it targeted.
            if generation is None:
                return True
            if generation != previous_generation:
                return False
            generation = self._turn_generation
            self.is_turn_active = True
            self.bus.current_reply = ""
            self.bus.set_state("conversing", "LLM: Thinking…")
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        if generation is not None:
            directive = classify_turn_directive(directive_hint if directive_hint is not None else content)
            if self._is_first_turn:
                if directive and directive not in (GIDEON_VOICE_PREAMBLE, CONVERSATION_DIRECTIVE):
                    content = f"{GIDEON_VOICE_PREAMBLE}\n\n{directive}\n\nUser: {content}"
                else:
                    content = f"{GIDEON_VOICE_PREAMBLE}\n\nUser: {content}"
            elif directive and directive not in (GIDEON_VOICE_PREAMBLE, CONVERSATION_DIRECTIVE):
                content = f"{directive}\n\nUser: {content}"
        try:
            payload = json.dumps({"event": "user", "message": {"content": content}}) + "\n"
            self.proc.stdin.write(payload.encode())
            if generation is not None:
                self._is_first_turn = False
            self._pending_turns.append(generation)
            if not self._drain_task or self._drain_task.done():
                self._drain_task = asyncio.create_task(self._drain_stdin(self.proc))
            return True
        except (OSError, RuntimeError) as exc:
            logger.warning("AGY stdin write failed: %s", exc)
            self._reset_turn()
            self._engine_notice("My engine connection failed. Please try that again.")
            return False

    async def _drain_stdin(self, proc):
        try:
            await asyncio.wait_for(proc.stdin.drain(), timeout=5.0)
        except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
            if proc is not self.proc:
                return
            logger.warning("AGY stdin unavailable: %s", exc)
            # Buffered writes may still produce results. Retain their FIFO
            # slots until EOF/close so a later prompt cannot consume old output.
            await self.tts.interrupt()
            self._reset_turn()
            self._engine_notice("My engine connection stalled. Please try that again.")

    def _reset_turn(self):
        self.is_turn_active = False
        self.current_tool = None
        self.bus.current_tool = None
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self.bus.set_state("idle")

    async def _finish_turn(self, generation):
        await self.tts.wait_until_done()
        if generation != self._turn_generation or not self.is_turn_active:
            return
        self._reset_turn()
        if getattr(self, "wake_listener", None):
            self.wake_listener.arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)
        print("\n[Turn Complete] Gideon is ready.\n", flush=True)

    async def close(self):
        self._closed = True
        async with self._restart_lock:
            self._closed = True
            self._turn_generation += 1
            tasks = [t for t in (self._read_task, self._stderr_task, self._account_watch_task, self._finish_task, self._drain_task, self._watchdog_task) if t]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._read_task = self._stderr_task = self._account_watch_task = self._finish_task = self._drain_task = None
            await self.tts.interrupt()
            self._pending_turns.clear()
            self._reset_turn()

    async def interrupt(self, hard: bool = False) -> bool:
        await self.tts.interrupt()
        # Notify the existing conversation, never SIGINT/kill. This binary
        # queues the request behind the active turn; do not call that live steer.
        notified = True
        if self.is_turn_active:
            notified = await self._write_prompt("Stop the current task. Do not continue it or speak a reply; wait for my next instruction.", None)
        self._turn_generation += 1
        if self._finish_task:
            self._finish_task.cancel()
            self._finish_task = None
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self.bus.current_reply = ""
        self.bus.clear_transcript()
        self._reset_turn()
        return notified

    async def _watchdog_loop(self):
        """Speaks a calm check-in periodically during long turns, with collision protection."""
        try:
            while self.is_turn_active:
                await asyncio.sleep(45.0)

                # Strict Status Gate: exit if the turn ended or the tool is no longer actively running
                if not self.is_turn_active or not self.current_tool or self.current_tool.get("status") != "running":
                    break

                # Acoustic Collision Guard: Never interrupt user speech or ongoing/queued TTS playback
                if self.tts._is_speaking or not self.tts._queue.empty() or self.bus.current_state in ("speaking", "listening", "hotmic"):
                    continue

                elapsed = int(time.time() - self.turn_start_time)
                t_label = self.current_tool.get("label") or "working on the solution"
                checkin_text = f"Still running {t_label}, {elapsed} seconds in."
                logger.info(f"[Watchdog Check-in] {checkin_text}")
                self.tts.queue_sentence(checkin_text)
        except asyncio.CancelledError:
            pass

    async def _read_loop(self):
        """Consumes NDJSON lines from agy.stdout and yields spoken text sentences."""
        proc = self.proc
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        active_tool_id = 0
        active_tool_step_index = None

        while True:
            try:
                line_bytes = await proc.stdout.readline()
            except (OSError, ValueError) as exc:
                logger.warning("AGY stdout unavailable: %s", exc)
                line_bytes = b""
            if not line_bytes:
                await self._ensure_engine_alive(failed_proc=proc)
                break
            if proc is not self.proc:
                break
            line = line_bytes.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            try:
                ev = json.loads(line)
            except Exception:
                continue

            if not isinstance(ev, dict):
                continue
            event_type = ev.get("event")
            generation = self._pending_turns[0] if self._pending_turns else None
            if event_type == "result" and self._pending_turns:
                self._pending_turns.popleft()
            # Drain older results without resurrecting their UI/speech or
            # allowing their completion callbacks to finish a newer turn.
            if generation != self._turn_generation or not self.is_turn_active:
                continue
            if event_type == "step_update":
                update = ev.get("step_update", {})
                step_type = update.get("step_type")
                
                # Model is speaking / generating text
                if step_type == "agent_response":
                    if self.current_tool and self.current_tool.get("status") == "running":
                        self.current_tool["status"] = "done"
                        self.bus.current_tool = dict(self.current_tool)

                    if not self.tts._is_speaking and self.bus.current_state != "speaking":
                        self.bus.set_state("conversing", "LLM: Formulating speech…")

                    delta = update.get("text_delta", "")
                    self.sentence_buffer += delta
                    self.accumulated_reply += delta
                    self.bus.current_reply = self.accumulated_reply

                    while True:
                        match = _SENTENCE_END.search(self.sentence_buffer)
                        if not match:
                            break
                        sentence = self.sentence_buffer[:match.end()].strip()
                        self.sentence_buffer = self.sentence_buffer[match.end():]
                        if sentence:
                            clean = clean_text_for_tts(sentence)
                            if clean:
                                print(f"Gideon: {clean}", flush=True)
                                if not self.tts._is_speaking and self.bus.current_state != "speaking":
                                    self.bus.set_state("conversing", "LLM: Synthesizing voice…")
                                self.tts.queue_sentence(clean)

                # Model is invoking or finishing a tool
                elif step_type in ("tool", "tool_call"):
                    tool_state = str(update.get("state") or "").upper()
                    step_index = update.get("step_index")
                    tool_info = update.get("tool_info") if isinstance(update.get("tool_info"), dict) else {}
                    tool_name = update.get("tool_name") or tool_info.get("name") or "tool"
                    tool_args = update.get("tool_args")
                    if not isinstance(tool_args, dict):
                        params = tool_info.get("parameters")
                        tool_args = params if isinstance(params, dict) else {}

                    if tool_state in ("DONE", "ERROR", "FAILED", "CANCELLED"):
                        if self.current_tool and self.current_tool.get("status") == "running":
                            self.current_tool["status"] = "done" if tool_state == "DONE" else "error"
                            self.bus.current_tool = dict(self.current_tool)
                        active_tool_step_index = None
                        if not self.tts._is_speaking and self.bus.current_state == "toolcall":
                            self.bus.set_state("conversing", "LLM: Thinking…")
                        continue

                    if tool_state in ("PENDING", "WAITING", "QUEUED"):
                        continue

                    if (
                        step_index is not None
                        and step_index == active_tool_step_index
                        and self.current_tool
                        and self.current_tool.get("status") == "running"
                    ):
                        continue

                    active_tool_id += 1
                    active_tool_step_index = step_index

                    target = "shell" if "command" in tool_name or "exec" in tool_name else ("web" if "browser" in tool_name or "url" in tool_name else "core")

                    if "CommandLine" in tool_args:
                        arg_str = str(tool_args["CommandLine"]).strip()
                        label = f"{tool_name}: {arg_str[:40]}"
                    elif "AbsolutePath" in tool_args:
                        p = Path(tool_args["AbsolutePath"]).name
                        label = f"{tool_name}: {p}"
                    elif "TargetFile" in tool_args:
                        p = Path(tool_args["TargetFile"]).name
                        label = f"{tool_name}: {p}"
                    elif "query" in tool_args:
                        label = f"{tool_name}: {tool_args['query'][:35]}"
                    else:
                        label = tool_name

                    self.current_tool = {
                        "id": f"t_{active_tool_id}",
                        "name": tool_name,
                        "target": target,
                        "label": label,
                        "status": "running"
                    }
                    self.bus.current_tool = dict(self.current_tool)
                    self.bus.set_state("toolcall", detail=label)
                    logger.info(f"[Tool Execution] {label}")

            elif event_type == "result":
                if self.current_tool and self.current_tool.get("status") == "running":
                    self.current_tool["status"] = "done"
                    self.bus.current_tool = dict(self.current_tool)

                tail = self.sentence_buffer.strip()
                self.sentence_buffer = ""
                if tail:
                    clean = clean_text_for_tts(tail)
                    if self.accumulated_reply and not self.accumulated_reply.endswith(tail):
                        if not self.accumulated_reply.endswith(" "):
                            self.accumulated_reply += " "
                        self.accumulated_reply += tail
                    elif not self.accumulated_reply:
                        self.accumulated_reply = tail
                    self.bus.current_reply = self.accumulated_reply
                    if clean:
                        print(f"Gideon: {clean}", flush=True)
                        self.tts.queue_sentence(clean)

                self._finish_task = asyncio.create_task(self._finish_turn(generation))


def _resolve_hf_snapshot(repo_folder: str) -> Optional[str]:
    """Return locally cached HuggingFace snapshot dir so ONNX models boot offline."""
    repo_dir = Path.home() / ".cache" / "huggingface" / "hub" / repo_folder
    ref_main = repo_dir / "refs" / "main"
    if ref_main.is_file():
        rev = ref_main.read_text(encoding="utf-8", errors="ignore").strip()
        snap = repo_dir / "snapshots" / rev
        if snap.is_dir():
            return str(snap)
    snaps_dir = repo_dir / "snapshots"
    if snaps_dir.is_dir():
        candidates = [p for p in snaps_dir.iterdir() if p.is_dir()]
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return str(candidates[0])
    return None


# STT Backend selection: "parakeet" (Parakeet TDT 0.6B v3 INT8 ONNX + Silero VAD) or "whisper".
STT_BACKEND = os.environ.get("GIDEON_STT_BACKEND", "parakeet").strip().lower()
PARAKEET_STT_MODEL_ID = "nemo-parakeet-tdt-0.6b-v3"


class VoiceListener:
    """High-fidelity microphone listener using Parakeet TDT INT8 ONNX (or Faster-Whisper fallback)."""
    def __init__(self, backend: str = STT_BACKEND):
        self.backend = backend
        self.device = "cpu"
        self.processor = None
        self.model = None
        self.vad = None

        if self.backend == "parakeet":
            try:
                import onnx_asr
                import onnxruntime as ort

                opts = ort.SessionOptions()
                # Measured on this 4-core box (3.7s utterance, int8): 2 threads
                # 576-878ms, 3 threads 675-1002ms, 4 threads 862-2035ms — extra
                # intra-op threads contend with the wake-word loop and OS.
                opts.intra_op_num_threads = min(2, max(1, os.cpu_count() or 2))
                # No CPU memory arena: measured ~200MB less RSS at equal
                # transcription latency (493-559ms vs 461-617ms with arena).
                opts.enable_cpu_mem_arena = False
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

                parakeet_path = _resolve_hf_snapshot("models--istupakov--parakeet-tdt-0.6b-v3-onnx")
                vad_path = _resolve_hf_snapshot("models--istupakov--silero-vad-onnx")
                logger.info(
                    "Loading Parakeet TDT 0.6B v3 INT8 ONNX on CPU (%s)...",
                    "offline cache" if parakeet_path else "HuggingFace hub",
                )
                asr_model = onnx_asr.load_model(
                    PARAKEET_STT_MODEL_ID,
                    path=parakeet_path,
                    quantization="int8",
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
                try:
                    vad_opts = ort.SessionOptions()
                    vad_opts.intra_op_num_threads = 1
                    vad_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                    self.vad = onnx_asr.load_vad(
                        "silero",
                        path=vad_path,
                        sess_options=vad_opts,
                        providers=["CPUExecutionProvider"],
                    )
                    self.model = asr_model.with_vad(
                        self.vad,
                        threshold=0.38,
                        min_speech_duration_ms=160,
                        min_silence_duration_ms=250,
                        speech_pad_ms=120,
                        max_speech_duration_s=30,
                    )
                except Exception as vad_exc:
                    logger.warning(f"Silero VAD unavailable ({vad_exc}); running Parakeet without VAD pre-filter.")
                    self.model = asr_model
                logger.info("Parakeet TDT 0.6B v3 INT8 ONNX engine initialized cleanly on CPU.")
            except Exception as e:
                logger.warning(f"Parakeet ONNX STT initialization failed ({e}), falling back to Faster-Whisper.")
                self.backend = "whisper"

        if self.backend == "whisper":
            _preload_cuda_libs()
            import faster_whisper
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.compute_type = "int8"
            try:
                logger.info("Loading Faster-Whisper (turbo) on CUDA...")
                try:
                    self.model = faster_whisper.WhisperModel("turbo", device="cuda", compute_type="int8", local_files_only=True)
                except Exception:
                    self.model = faster_whisper.WhisperModel("turbo", device="cuda", compute_type="int8")
                logger.info("Faster-Whisper CUDA turbo engine initialized.")
            except Exception as e:
                logger.warning(f"CUDA STT initialization failed ({e}), falling back to CPU.")
                self.device = "cpu"
                try:
                    self.model = faster_whisper.WhisperModel("small.en", device="cpu", compute_type="int8", local_files_only=True)
                except Exception:
                    self.model = faster_whisper.WhisperModel("small.en", device="cpu", compute_type="int8")

    def transcribe_audio(self, pcm_data: bytes) -> str:
        if not pcm_data or len(pcm_data) < 3200:
            return ""
        audio_data = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0

        # Parakeet TDT INT8 ONNX path
        if getattr(self, "backend", None) == "parakeet" and getattr(self, "model", None) is not None:
            try:
                res = self.model.recognize(audio_data, sample_rate=16000)
                if isinstance(res, str):
                    return res.strip()
                parts = [str(getattr(seg, "text", seg) or "").strip() for seg in res]
                return " ".join(p for p in parts if p).strip()
            except Exception as e:
                logger.error(f"Parakeet STT transcription error: {e}")
                return ""

        # Faster-Whisper path (preserved for Large-V3-Turbo & unit tests)
        try:
            segments, _ = self.model.transcribe(
                audio_data,
                beam_size=1,
                best_of=1,
                language="en",
                initial_prompt=OPTIMAL_WHISPER_PROMPT,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=300)
            )
            return " ".join(_filter_stt_segments(segments)).strip()
        except Exception as e:
            err_lower = str(e).lower()
            if "out of memory" in err_lower or "cuda" in err_lower or "cublas" in err_lower:
                logger.error(f"GPU STT error ({e}). Clearing CUDA cache and recovering via CPU fallback...")
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                try:
                    import faster_whisper
                    self.device = "cpu"
                    self.model = faster_whisper.WhisperModel("small.en", device="cpu", compute_type="int8")
                    segments, _ = self.model.transcribe(
                        audio_data,
                        beam_size=1,
                        best_of=1,
                        language="en",
                        initial_prompt=OPTIMAL_WHISPER_PROMPT,
                        vad_filter=True,
                        vad_parameters=dict(min_silence_duration_ms=300)
                    )
                    return " ".join(_filter_stt_segments(segments)).strip()
                except Exception as e2:
                    logger.error(f"CPU fallback transcription failed: {e2}")
                    return ""
            else:
                logger.error(f"STT transcription error: {e}")
                return ""


class AudioStreamRecorder:
    """Handles real-time microphone stream recording for browser hold-to-talk."""
    def __init__(self, bus: SignalBus):
        self.bus = bus
        self.frames = []
        self.is_recording = False
        self.stream = None
        self._stream_lock = threading.Lock()
        self.ambient_floor = 0.010
        import scipy.signal as signal
        self._signal = signal
        self.bp_b, self.bp_a = signal.butter(2, [200 / 8000, 3800 / 8000], btype="bandpass")

    def _callback(self, indata, frames_count, time_info, status):
        if self.is_recording:
            self.frames.append(indata.copy())
            # Real-time PortAudio callback: no imports, no repeated reductions.
            chunk = indata.flatten().astype(np.float32) / 32768.0
            filtered = self._signal.lfilter(self.bp_b, self.bp_a, chunk)
            rms = float(np.sqrt(np.mean(filtered**2)))
            self.ambient_floor = self.ambient_floor * 0.95 + min(rms, 0.025) * 0.05
            # Gated against fan noise and scaled down a couple folds
            net_rms = max(0.0, rms - self.ambient_floor * 1.5)
            norm_rms = float(np.clip(net_rms * 4.0, 0.0, 0.75))
            self.bus.current_level = norm_rms
            self.bus.broadcast_rms(norm_rms)

            if self.bus.waveform_enabled:
                fft_data = np.abs(np.fft.rfft(filtered))[:32]
                max_fft = float(np.max(fft_data)) if len(fft_data) else 0.0
                if max_fft <= 0:
                    max_fft = 1.0
                bars = [float(np.clip((v / max_fft) * norm_rms, 0.0, 0.75)) for v in fft_data]
                if len(bars) < 32:
                    bars += [0.0] * (32 - len(bars))
                self.bus.update_waveform(bars + [0.0] * 32)

    def start(self):
        with self._stream_lock:
            self._start()

    def _start(self):
        import sounddevice as sd
        # Replacing a live InputStream drops its CFFI callback owner while
        # PortAudio can still invoke it. Duplicate HTTP starts are idempotent.
        if self.stream is not None:
            return
        self.frames = []
        self.is_recording = True
        self.bus.set_state("listening")
        try:
            self.stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype='int16',
                callback=self._callback
            )
            self.stream.start()
            logger.info("🎙️ [Browser Hold-to-Talk] Microphone OPEN — listening...")
        except Exception as e:
            self.is_recording = False
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.bus.set_state("idle")
            logger.error(f"Error starting mic stream: {e}")

    def stop(self) -> bytes:
        with self._stream_lock:
            return self._stop()

    def _stop(self) -> bytes:
        self.is_recording = False
        self.bus.current_level = 0.0
        self.bus.broadcast_rms(0.0)
        self.bus.clear_waveform()
        self.bus.set_state("thinking")
        if self.stream:
            try:
                self.stream.stop()
            except Exception as exc:
                logger.warning("Microphone stop failed; closing stream: %s", exc)
            finally:
                # Keep the callback owner until close completes, even when
                # stop raises. A failed close must not orphan a live callback.
                self.stream.close()
                self.stream = None
        if not self.frames:
            return b""
        pcm = np.concatenate(self.frames).tobytes()
        logger.info(f"🎙️ [Browser Hold-to-Talk] Microphone CLOSED ({len(pcm)} bytes captured).")
        return pcm


class WakeWordListener:
    """Robust background listener with seamless conversational hot-mic talk buffer zone."""
    def __init__(self, bus: SignalBus, tts: TTSPlayer, on_speech_captured, main_loop=None, wake_threshold: float = WAKE_THRESHOLD, hot_mic_timeout: float = HOT_MIC_WINDOW_S):
        self.bus = bus
        self.tts = tts
        self.on_speech_captured = on_speech_captured
        self.main_loop = main_loop
        self.wake_threshold = wake_threshold
        self.hot_mic_timeout = hot_mic_timeout
        self.enabled = True
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._hot_mic_armed = False
        self._hot_mic_until = 0.0
        self._hot_mic_timer: Optional[threading.Timer] = None
        # Bumped on every arm/disarm: lets delayed re-arm tasks (_post_wake,
        # _post_talk) detect that a dismissal landed during their sleep.
        self._hot_mic_epoch = 0
        self._listener_instance = None

    def is_hot_mic_active(self) -> bool:
        return self._hot_mic_armed and (time.time() < self._hot_mic_until)

    def restore_state_after_drop(self) -> None:
        """Quietly return the orb to hotmic (if original window is still running) or idle without re-arming/extending."""
        if self.is_hot_mic_active():
            remaining = max(0.1, self._hot_mic_until - time.time())
            if self._listener_instance:
                self._listener_instance.hot_mic_timeout = remaining
            self.bus.set_state("hotmic")
        else:
            self.disarm_hot_mic()
            self.bus.set_state("idle")

    def set_enabled(self, enabled: bool) -> None:
        """Set the hands-free wake gate and stop an active capture when disabling it."""
        self.enabled = bool(enabled)
        self.bus.wake_enabled = self.enabled
        if not self.enabled:
            self.disarm_hot_mic()
            if self._listener_instance:
                self._listener_instance.stop()
            logger.info("🎙️ Wake listener disabled; active microphone capture stopped.")
        else:
            logger.info("🎙️ Wake listener enabled.")

    def arm_hot_mic(self, timeout_s: float = HOT_MIC_WINDOW_S):
        if not self.enabled:
            logger.debug("Ignoring hot-mic arm while the wake listener is disabled.")
            return
        self.hot_mic_timeout = timeout_s
        self._hot_mic_until = time.time() + timeout_s
        self._hot_mic_armed = True
        self._hot_mic_epoch += 1
        if self._listener_instance:
            self._listener_instance.hot_mic_timeout = timeout_s
        self.bus.set_state("hotmic")
        logger.info(f"🎙️ [Talk Buffer Zone] Hot-mic armed for {timeout_s}s — follow-up without wake word.")

        if self._hot_mic_timer:
            try:
                self._hot_mic_timer.cancel()
            except Exception:
                pass
            self._hot_mic_timer = None

        def _timeout_disarm():
            if self._hot_mic_armed and time.time() >= self._hot_mic_until:
                if self._listener_instance and getattr(self._listener_instance, "is_recording_command", False):
                    # User is actively speaking; let trailing silence complete capture
                    return
                logger.info("🎙️ [Talk Buffer Zone] Hot-mic window timed out. Reverting to idle.")
                self.disarm_hot_mic()

        self._hot_mic_timer = threading.Timer(timeout_s + 0.15, _timeout_disarm)
        self._hot_mic_timer.daemon = True
        self._hot_mic_timer.start()

    def disarm_hot_mic(self):
        self._hot_mic_epoch += 1
        self._hot_mic_armed = False
        self._hot_mic_until = 0.0
        if self._hot_mic_timer:
            try:
                self._hot_mic_timer.cancel()
            except Exception:
                pass
            self._hot_mic_timer = None
        if self.bus.current_state in ("hotmic", "listening"):
            self.bus.current_level = 0.0
            self.bus.clear_waveform()
            self.bus.set_state("idle")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self.disarm_hot_mic()
        if self._listener_instance:
            self._listener_instance.stop()

    def _handle_barge_in(self) -> None:
        """Stop playback AND the in-flight engine turn on acoustic barge-in.

        Interrupting only the TTS player is not enough: the engine keeps
        streaming deltas and queueing fresh sentences, so Gideon resumes
        speaking over the user within a second. Both engines' interrupt()
        cancels the turn and clears the speech queue.
        """
        logger.info("⚡ Acoustic barge-in detected! Interrupting active speech and turn...")
        if not (self.main_loop and self.main_loop.is_running()):
            return
        engine = getattr(self.bus, "engine", None)
        interrupt = getattr(engine, "interrupt", None)
        if callable(interrupt):
            asyncio.run_coroutine_threadsafe(interrupt(), self.main_loop)
        else:
            asyncio.run_coroutine_threadsafe(self.tts.interrupt(), self.main_loop)

    def _run_loop(self):
        sys.path.insert(0, str(HERE))
        from audio.wake import AudioListener
        from audio.sounds import play_sound

        # Precomputed bar shape for the idle breathing waveform (32 sin()
        # calls per audio chunk used to run in this hot callback).
        rms_bar_shape = np.array([0.35 + 0.65 * np.sin(i * 0.35) for i in range(32)])

        def on_rms(norm_rms: float):
            if self.bus.current_state in ("listening", "hotmic"):
                # Attenuate reactivity by a factor of ~2.5x to eliminate fan air noise
                gated_rms = float(np.clip(norm_rms * 0.40, 0.0, 0.75))
                self.bus.current_level = gated_rms
                self.bus.broadcast_rms(gated_rms)
                if self.bus.waveform_enabled:
                    bars = np.clip(gated_rms * rms_bar_shape, 0.0, 0.75).tolist()
                    self.bus.update_waveform(bars + [0.0] * 32)

        def on_wake():
            logger.info("⚡ Wake word detected ('Hey Jarvis')!")
            self.disarm_hot_mic()
            self.bus.set_state("listening")

        listener = AudioListener(
            wake_word="hey jarvis",
            wake_threshold=self.wake_threshold,
            mic_gain=1.8,
            hot_mic_timeout=self.hot_mic_timeout,
            on_rms_update=on_rms,
            on_wake_detected=on_wake,
            on_barge_in=self._handle_barge_in
        )
        self._listener_instance = listener
        listener.load_wake_model()
        logger.info("🎙️ Background Wake Word Listener ACTIVE ('Hey Jarvis').")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _loop():
            consecutive_errors = 0
            while self._running:
                if not self.enabled:
                    await asyncio.sleep(0.5)
                    continue

                is_hot_mic = self.is_hot_mic_active()
                if self._hot_mic_armed and not is_hot_mic:
                    self.disarm_hot_mic()
                elif is_hot_mic:
                    listener.hot_mic_timeout = max(0.1, self._hot_mic_until - time.time())

                try:
                    audio_buffer = await listener.listen_for_command(
                        is_hot_mic=is_hot_mic,
                        is_speaking=self.tts._is_speaking,
                        silence_timeout_s=1.0,
                        max_duration_s=MAX_UTTERANCE_S,
                        hot_mic_checker=self.is_hot_mic_active,
                        is_speaking_checker=lambda: self.tts._is_speaking,
                        enabled_checker=lambda: self.enabled,
                    )
                    consecutive_errors = 0
                    if audio_buffer is not None and self.enabled and not self.tts._is_speaking:
                        # Speech detected and recorded: flip to thinking while preserving any remaining
                        # hot-mic window until STT verifies the utterance is not silence/hallucination.
                        self.bus.set_state("thinking", "Transcribing speech…")
                        pcm_bytes = audio_buffer.astype(np.int16).tobytes()
                        self.on_speech_captured(pcm_bytes)
                    else:
                        # Timeout elapsed without speech, or cancelled -> revert to idle immediately
                        self.disarm_hot_mic()
                except Exception as e:
                    consecutive_errors += 1
                    backoff = min(4.0, 0.5 * (2 ** min(consecutive_errors, 3)))
                    if consecutive_errors <= 2 or consecutive_errors % 10 == 0:
                        logger.error(f"Error in wake listener loop: {e} (backing off {backoff:.1f}s)")
                    # Never globally terminate PortAudio here: the browser
                    # recorder may still own a live CFFI callback stream.
                    await asyncio.sleep(backoff)

        try:
            loop.run_until_complete(_loop())
        except Exception as e:
            logger.error(f"Wake word listener loop error: {e}")
        finally:
            loop.close()


class StdinReader:
    """True asynchronous non-blocking stdin stream reader with daemon support."""
    def __init__(self):
        self.reader = asyncio.StreamReader()
        self.is_tty = sys.stdin.isatty()

    async def start(self):
        if not self.is_tty:
            return
        try:
            loop = asyncio.get_running_loop()
            protocol = asyncio.StreamReaderProtocol(self.reader)
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception:
            self.is_tty = False

    async def prompt_line(self, prompt: str) -> Optional[str]:
        """Return one terminal line, or None when stdin is unavailable/EOF.

        None is a sentinel, NOT an empty utterance: under systemd stdin is
        /dev/null, and returning "" here used to make the main loop record a
        phantom 5-second microphone chunk every hour and dispatch whatever
        the room said as a user prompt.
        """
        if not self.is_tty:
            return None
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line_bytes = await self.reader.readline()
        if not line_bytes:
            self.is_tty = False
            return None
        return line_bytes.decode("utf-8", errors="ignore").strip()


async def _dispatch_user_utterance(
    text: str,
    *,
    engine_getter,
    bus: SignalBus,
    tts: Optional[TTSPlayer],
    wake_listener=None,
    effort: Optional[str] = None,
    rearm_after_reflex: bool = False,
    set_engine=None,
) -> None:
    """Unified prompt router shared by PTT, wake-word, HTTP UI, and terminal stdin."""
    bus.set_user_transcript(text)
    prepared = await _prepare_user_prompt(text, bus=bus, tts=tts)
    if prepared is None:
        return

    curr_engine = engine_getter()
    if effort and hasattr(curr_engine, "set_effort") and effort != getattr(curr_engine, "effort", None):
        await curr_engine.set_effort(effort)

    if prepared.used_selection:
        bus.set_state("conversing", "LLM: Reading selected text…")
        await curr_engine.send_prompt(prepared.prompt, display_text=text)
        if prepared.copy_response:
            asyncio.create_task(_copy_reply_after_turn(curr_engine, bus))
        return

    handled = await try_reflex(text, bus=bus, tts=tts, set_engine=set_engine or getattr(bus, "switch_engine", None))
    if not handled:
        bus.set_state("conversing", "LLM: Thinking…")
        await curr_engine.send_prompt(text)
    elif rearm_after_reflex and wake_listener is not None:
        from reflex_router import get_pending_confirmation_store
        if get_pending_confirmation_store().get() is not None:
            # Tier-1 confirmation already armed a 15s hot-mic window; do not overwrite it.
            return
        hot_mic_epoch = wake_listener._hot_mic_epoch

        async def _post_reflex_hotmic():
            await asyncio.sleep(2.0)
            if (
                bus.current_state in ("bge_reflex", "speaking")
                and wake_listener._hot_mic_epoch == hot_mic_epoch
            ):
                wake_listener.arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)

        asyncio.create_task(_post_reflex_hotmic())


def _focus_status_safe() -> dict:
    try:
        from focus_mode import get_focus_manager
        return get_focus_manager().status()
    except Exception:
        return {"active": False}


def run_visualizer_server(bus: SignalBus, recorder: AudioStreamRecorder, get_listener, get_engine, get_wake_listener, loop: asyncio.AbstractEventLoop, get_tts=None):
    """Runs the integrated HTTP server for the GIDEON PRIME visualizer."""
    class VisualizerHandler(BaseHTTPRequestHandler):
        # Only loopback browser origins may talk to the daemon. The Host gate
        # alone is NOT enough: a cross-origin fetch to http://127.0.0.1:8790
        # still carries Host: 127.0.0.1:8790, so any web page could otherwise
        # POST /api/prompt (which drives shell-capable engines) or read
        # /state transcripts via Access-Control-Allow-Origin: *.
        _LOOPBACK_ORIGIN_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$")

        def _host_allowed(self) -> bool:
            # The POST endpoints drive reflexes that execute local commands.
            # Gate on the Host header so DNS-rebinding names never resolve in.
            host = (self.headers.get("Host") or "").strip().lower()
            return host in ("127.0.0.1", "localhost", "[::1]") or host.startswith(
                ("127.0.0.1:", "localhost:", "[::1]:")
            )

        def _origin_allowed(self) -> bool:
            origin = (self.headers.get("Origin") or "").strip().rstrip("/")
            if not origin:
                # Same-origin HUD request or non-browser client (curl, tests).
                return True
            return bool(self._LOOPBACK_ORIGIN_RE.match(origin))

        def _cors_origin(self) -> Optional[str]:
            origin = (self.headers.get("Origin") or "").strip().rstrip("/")
            if origin and self._LOOPBACK_ORIGIN_RE.match(origin):
                return origin
            return None

        def do_GET(self):
            if not self._host_allowed() or not self._origin_allowed():
                self.send_error(403)
                return
            path = self.path.split("?")[0]
            if path == "/state":
                is_speaking = bus.current_state == "speaking"
                is_listening = bus.current_state == "listening"
                level = 0.8 if is_speaking else (bus.current_level if is_listening else 0.0)
                data = {
                    "state": bus.current_state,
                    "level": level,
                    "samples": bus.current_samples,
                    "bands": bus.current_samples[:32],
                    "user": bus.current_user,
                    "reply": bus.current_reply,
                    "tool": bus.current_tool,
                    "effort": bus.current_effort,
                    "wake": bus.wake_enabled,
                    "alert": False,
                    "loading": bus.current_state == "thinking",
                    "rate_limits": {},
                    "focus": _focus_status_safe(),
                }
                self._send(json.dumps(data).encode(), "application/json")
            elif path == "/api/focus":
                self._send(json.dumps(_focus_status_safe()).encode(), "application/json")
            elif path == "/config":
                eng_label = (getattr(bus, "engine_name", "codex") or "codex").upper()
                out = {
                    "name": "GIDEON PRIME",
                    "badge": f"{eng_label} CORE",
                    "face": "board",
                    "thinking_sound": True,
                    "faces": [{"id": "board", "title": "GIDEON PRIME", "tagline": ""}]
                }
                self._send(json.dumps(out).encode(), "application/json")
            else:
                self._static(path)

        def do_OPTIONS(self):
            if not self._host_allowed() or not self._origin_allowed():
                self.send_error(403)
                return
            self.send_response(200)
            cors_origin = self._cors_origin()
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if not self._host_allowed() or not self._origin_allowed():
                self.send_error(403)
                return
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"

            if path == "/api/talk/start":
                wl = get_wake_listener()
                if wl:
                    wl.disarm_hot_mic()
                recorder.start()
                self._send(b'{"status":"recording"}', "application/json")
            elif path == "/api/talk/stop":
                pcm = recorder.stop()
                self._send(b'{"status":"processing"}', "application/json")
                if pcm:
                    async def _process_voice():
                        listener = get_listener()
                        if listener:
                            bus.set_state("thinking", "Transcribing speech…")
                            text = await loop.run_in_executor(None, listener.transcribe_audio, pcm)
                            if text:
                                if _is_dismissal(text):
                                    bus.set_user_transcript(text)
                                    tts_inst = get_tts() if get_tts else None
                                    await try_reflex(text, bus=bus, tts=tts_inst)
                                    return
                                drop_reason = _get_hallucination_drop_reason(text)
                                if drop_reason is not None:
                                    logger.info("[STT Filter] Dropping hallucinated/empty utterance %r (%s)", text, drop_reason)
                                    bus.set_state("idle")
                                    return
                                tts_inst = get_tts() if get_tts else None
                                await _dispatch_user_utterance(
                                    text,
                                    engine_getter=get_engine,
                                    bus=bus,
                                    tts=tts_inst,
                                    wake_listener=get_wake_listener(),
                                    rearm_after_reflex=True,
                                )
                            else:
                                logger.info("[STT Filter] Dropping empty capture (no speech detected)")
                                bus.set_state("idle")
                        else:
                            bus.set_state("idle")
                    asyncio.run_coroutine_threadsafe(_process_voice(), loop)
                else:
                    bus.set_state("idle")
            elif path == "/api/prompt":
                try:
                    data = json.loads(body.decode("utf-8"))
                    text = data.get("text", "").strip()
                    eff = data.get("effort")
                    if text:
                        async def _dispatch():
                            tts_inst = get_tts() if get_tts else None
                            bus.set_state("thinking", "Evaluating prompt…")
                            await _dispatch_user_utterance(
                                text,
                                engine_getter=get_engine,
                                bus=bus,
                                tts=tts_inst,
                                effort=eff,
                            )
                        asyncio.run_coroutine_threadsafe(_dispatch(), loop)
                        self._send(b'{"status":"queued"}', "application/json")
                    else:
                        self._send(b'{"error":"empty prompt"}', "application/json", 400)
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json", 400)
            elif path == "/api/effort":
                try:
                    data = json.loads(body.decode("utf-8"))
                    eff = data.get("effort", "low")
                    curr_engine = get_engine()
                    if hasattr(curr_engine, "set_effort"):
                        asyncio.run_coroutine_threadsafe(curr_engine.set_effort(eff), loop)
                    self._send(b'{"status":"ok"}', "application/json")
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json", 400)
            elif path == "/api/wake":
                try:
                    data = json.loads(body.decode("utf-8"))
                    raw_enabled = data.get("enabled", True)
                    if not isinstance(raw_enabled, bool):
                        self._send(b'{"error":"enabled must be a boolean"}', "application/json", 400)
                        return
                    enabled = raw_enabled
                    wl = get_wake_listener()
                    if wl:
                        wl.set_enabled(enabled)
                    else:
                        bus.wake_enabled = enabled
                    self._send(json.dumps({"status": "ok", "enabled": enabled}).encode(), "application/json")
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json", 400)
            elif path == "/api/switch-to-orb":
                # Notify SignalBus / Quickshell Orb that user returned to desktop
                bus.set_state(bus.current_state)
                self._send(b'{"status":"switched_to_orb"}', "application/json")
            elif path == "/api/focus":
                try:
                    data = json.loads(body.decode("utf-8"))
                    act = str(data.get("action", "status")).strip().lower()
                    async def _focus_op():
                        from focus_mode import get_focus_manager
                        mgr = get_focus_manager(bus=bus, tts=get_tts() if get_tts else None)
                        if act == "start":
                            mins = data.get("minutes")
                            return mgr.start(duration_minutes=mins, goal=str(data.get("goal", "")))
                        if act == "stop":
                            return mgr.stop()
                        return mgr.status()
                    res = asyncio.run_coroutine_threadsafe(_focus_op(), loop).result(timeout=3.0)
                    self._send(json.dumps(res).encode(), "application/json")
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json", 400)
            elif path == "/api/debug-bundle":
                try:
                    from gideon_debug import collect_debug_bundle
                    out_path, summary = collect_debug_bundle()
                    self._send(json.dumps({"status": "ok", "path": str(out_path), "summary": summary}).encode(), "application/json")
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json", 500)
            else:
                self._send(b'{"error":"not found"}', "application/json", 404)

        def _static(self, path):
            if path == "/" or path == "":
                path = "/faces/board/index.html"
            base = VISUALIZER_DIR.resolve()
            target = (base / path.lstrip("/")).resolve()
            if target != base and base not in target.parents:
                self._send(b"forbidden", "text/plain", 403)
                return
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                self._send(b"not found", "text/plain", 404)
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(target.read_bytes(), ctype)

        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            cors_origin = self._cors_origin()
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), VisualizerHandler)
    logger.info(f"Visualizer HTTP Server listening on http://127.0.0.1:{HTTP_PORT}/faces/board/")
    server.serve_forever()


def _make_engine(name: str, bus: SignalBus, tts: TTSPlayer):
    """Factory: 'codex' is Codex app-server (default); 'agy' is Antigravity fallback."""
    if name not in ("codex", "agy"):
        logger.warning("Unknown engine %r; falling back to codex.", name)
        name = "codex"
    if name == "codex":
        from codex_engine import CodexEngine, DEFAULT_CODEX_EFFORT
        return CodexEngine(
            bus,
            tts,
            preamble=CODEX_VOICE_PREAMBLE,
            cwd=str(HERE),
            effort=DEFAULT_CODEX_EFFORT,
            clean_text_fn=clean_text_for_tts,
            sentence_end_re=_SENTENCE_END,
            directive_classifier=classify_turn_directive,
            hot_mic_window_s=HOT_MIC_WINDOW_S,
        )
    return AntigravityEngine(bus, tts)


async def _shutdown_engine(engine) -> None:
    try:
        if hasattr(engine, "close"):
            await engine.close()
    except Exception:
        pass
    proc = getattr(engine, "proc", None)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(description="Gideon voice console")
    parser.add_argument(
        "--engine",
        choices=["codex", "agy"],
        default=os.environ.get("GIDEON_ENGINE", "codex"),
        help="Brain backend: codex = Codex app-server (default); agy = Antigravity fallback",
    )
    parser.add_argument(
        "--debug-bundle",
        nargs="?",
        const="",
        default=None,
        help="Write a scrubbed status and log bundle to Downloads (or PATH) and exit",
    )
    args, _unknown = parser.parse_known_args()
    if args.debug_bundle is not None:
        from gideon_debug import collect_debug_bundle
        dest = args.debug_bundle.strip() or None
        _path, summary = collect_debug_bundle(dest)
        print(summary, flush=True)
        return

    loop = asyncio.get_running_loop()
    bus = SignalBus(SIGNALS_DIR)
    # No HUD, no waveform consumers: skip the spectral math in all producers.
    bus.waveform_enabled = HUD_ENABLED
    await bus.start_ui_server()
    tts = TTSPlayer(bus)
    tts.start_worker()
    from focus_mode import get_focus_manager
    focus_manager = get_focus_manager(bus=bus, tts=tts)
    engine_name = args.engine
    engine = _make_engine(engine_name, bus, tts)
    bus.engine_name = engine_name
    bus.current_effort = getattr(engine, "effort", "xhigh")
    engine_holder = [engine]
    wake_listener_holder = [None]
    recorder = AudioStreamRecorder(bus)
    listener_instance = [None]

    async def switch_engine(target: str) -> bool:
        nonlocal engine_name
        if target not in ("codex", "agy"):
            logger.warning("Unknown engine target: %r", target)
            return False
        if target == engine_name and getattr(engine_holder[0], "_engine_alive", lambda: True)():
            logger.info("[Engine] already on %s", target)
            return True
        previous = engine_holder[0]
        previous_name = engine_name
        try:
            await _shutdown_engine(previous)
            new_engine = _make_engine(target, bus, tts)
            await new_engine.start()
            if wake_listener_holder[0]:
                new_engine.wake_listener = wake_listener_holder[0]
            engine_holder[0] = new_engine
            engine_name = target
            bus.engine = new_engine
            bus.engine_name = target
            bus.current_effort = getattr(new_engine, "effort", bus.current_effort)
            bus.set_state(bus.current_state, bus.current_detail)
            logger.info("[Engine] dynamically switched to %s", target)
            return True
        except Exception as exc:
            logger.error("Engine switch to %s failed (%s); reverting to %s.", target, exc, previous_name)
            try:
                fallback = _make_engine(previous_name, bus, tts)
                await fallback.start()
                if wake_listener_holder[0]:
                    fallback.wake_listener = wake_listener_holder[0]
                engine_holder[0] = fallback
                engine_name = previous_name
                bus.engine = fallback
                bus.engine_name = previous_name
                bus.current_effort = getattr(fallback, "effort", bus.current_effort)
            except Exception as fb_exc:
                logger.error("Revert to %s failed: %s", previous_name, fb_exc)
            return False

    bus.switch_engine = switch_engine

    def on_wake_speech(pcm_bytes: bytes):
        bus.set_state("thinking", "Transcribing speech…")
        async def _process_voice():
            listener = listener_instance[0]
            wl = wake_listener_holder[0]
            if listener:
                text = await loop.run_in_executor(None, listener.transcribe_audio, pcm_bytes)
                if text:
                    # 1. Dismissal check ("bye bye") MUST run first
                    if _is_dismissal(text):
                        if wl:
                            wl.disarm_hot_mic()
                        bus.set_user_transcript(text)
                        await try_reflex(text, bus=bus, tts=tts)
                        return
                    # 2. Whisper silence/hallucination filter: drop before transcript/BGE/LLM
                    # and do NOT re-arm, extend, or reset the hot-mic window.
                    drop_reason = _get_hallucination_drop_reason(text)
                    if drop_reason is not None:
                        logger.info("[STT Filter] Dropping hallucinated/empty utterance %r (%s)", text, drop_reason)
                        if wl:
                            wl.restore_state_after_drop()
                        else:
                            bus.set_state("idle")
                        return
                    if wl:
                        wl.disarm_hot_mic()
                    await _dispatch_user_utterance(
                        text,
                        engine_getter=lambda: engine_holder[0],
                        bus=bus,
                        tts=tts,
                        wake_listener=wl,
                        rearm_after_reflex=True,
                    )
                else:
                    logger.info("[STT Filter] Dropping empty capture (no speech detected)")
                    if wl:
                        wl.restore_state_after_drop()
                    else:
                        bus.set_state("idle")
            else:
                if wl:
                    wl.restore_state_after_drop()
                else:
                    bus.set_state("idle")
        asyncio.run_coroutine_threadsafe(_process_voice(), loop)

    wake_listener = WakeWordListener(
        bus=bus,
        tts=tts,
        on_speech_captured=on_wake_speech,
        main_loop=loop,
        wake_threshold=WAKE_THRESHOLD
    )
    wake_listener_holder[0] = wake_listener
    engine.wake_listener = wake_listener
    bus.wake_listener = wake_listener
    bus.engine = engine
    # Farewell ("bye bye") reflex hook: drop the hot-mic window and settle idle.
    bus.dismiss_hot_mic = wake_listener.disarm_hot_mic
    bus.arm_hot_mic = wake_listener.arm_hot_mic

    # Pre-warm Tier-1 off the event loop and in parallel with engine startup.
    # In decision mode only the lightweight client is initialized: there is no
    # startup API call and BGE-small is deliberately left cold for fallback.
    async def _prewarm_dispatcher():
        try:
            from reflex_router import get_gideon_dispatcher, tier1_mode
            if tier1_mode() == "decision":
                await asyncio.to_thread(get_gideon_dispatcher)
                logger.info("GIDEON Tier-1 Decision 1 router active; BGE-small remains cold for fallback.")
                return
            disp = await asyncio.to_thread(get_gideon_dispatcher)
            if disp is not None:
                await asyncio.to_thread(disp.predict, "warmup")
                logger.info("GIDEON Tier-1 BGE-small classifier pre-warmed and ready for instant evaluation.")
        except Exception as e:
            logger.warning(f"Failed pre-warming Tier-1 router: {e}")

    asyncio.create_task(_prewarm_dispatcher())

    # Web HUD deprecated (2026-11-20): the :8790 browser portal no longer
    # starts by default. Opt back in with GIDEON_ENABLE_HUD=1.
    if HUD_ENABLED:
        server_thread = threading.Thread(
            target=run_visualizer_server,
            args=(bus, recorder, lambda: listener_instance[0], lambda: engine_holder[0], lambda: wake_listener_holder[0], loop, lambda: tts),
            daemon=True
        )
        server_thread.start()
    else:
        logger.info("Web HUD deprecated and disabled; :8790 server not started (GIDEON_ENABLE_HUD=1 to re-enable).")

    try:
        await engine.start()
    except Exception as exc:
        if engine_name != "agy":
            logger.error("Engine %s failed to start (%s); falling back to Antigravity (agy).", engine_name, exc)
            await _shutdown_engine(engine)
            engine_name = "agy"
            engine = _make_engine("agy", bus, tts)
            engine.wake_listener = wake_listener
            engine_holder[0] = engine
            bus.engine = engine
            bus.engine_name = "agy"
            bus.current_effort = getattr(engine, "effort", "low")
            await engine.start()
            tts.queue_sentence("Codex failed to start, so I fell back to Antigravity.")
        else:
            raise

    logger.info("Gideon online. Initializing STT engine...")
    # Load STT off the event loop so the UI socket and HTTP server stay
    # responsive during the ~4-7s ONNX session build.
    listener_instance[0] = await asyncio.to_thread(VoiceListener)
    listener = listener_instance[0]

    # Start background wake word listener
    wake_listener.start()

    # Initial readiness chime (no filler monologue)
    from audio.sounds import play_sound
    play_sound("activate.wav")
    logger.info("Gideon Prime online and ready.")
    bus.set_state("idle")

    # Start async stdin reader
    stdin_reader = StdinReader()
    await stdin_reader.start()

    print("\n" + "="*65, flush=True)
    print(f" GIDEON PRIME EXECUTIVE CONSOLE [{engine_name}] ", flush=True)
    print(" • WAKE WORD: Say 'Hey Jarvis' to speak hands-free", flush=True)
    print(" • HOLD SPACE IN BROWSER to speak (release to send)", flush=True)
    print(" • Or type any query directly in browser or terminal", flush=True)
    print(" • Type 'interrupt' to stop current turn", flush=True)
    print(" • Type 'quit' or 'exit' to close", flush=True)
    print("="*65 + "\n", flush=True)

    while True:
        try:
            user_in = await stdin_reader.prompt_line("You [Hey Jarvis / HOLD SPACE in browser / type]: ")

            if user_in is None:
                # Headless (systemd) or stdin EOF: idle without ever opening
                # the microphone. Wake word / browser PTT remain the inputs.
                await asyncio.sleep(3600)
                continue

            if user_in.lower() in ("quit", "exit"):
                break

            if user_in.lower() == "interrupt":
                try:
                    ok = await engine_holder[0].interrupt()
                    print(f"[Interrupt] {ok}\n", flush=True)
                except Exception as e:
                    logger.warning(f"Interrupt failed: {e}")
                continue

            if user_in == "":
                # Terminal microphone record: 5s chunk
                recorder.start()
                total = 5
                for s in range(total):
                    sys.stdout.write(f"\r🎙️  [LISTENING] {total - s}s remaining... (speak now)   ")
                    sys.stdout.flush()
                    await asyncio.sleep(1.0)
                sys.stdout.write("\r⚡  [PROCESSING] Transcribing speech...                  \n")
                sys.stdout.flush()
                pcm = recorder.stop()
                text = await loop.run_in_executor(None, listener.transcribe_audio, pcm)
                if not text:
                    print("(No speech detected)\n", flush=True)
                    bus.set_state("idle")
                    continue
                user_in = text

            await _dispatch_user_utterance(
                user_in,
                engine_getter=lambda: engine_holder[0],
                bus=bus,
                tts=tts,
            )

        except (KeyboardInterrupt, EOFError):
            break

    logger.info("Shutting down Gideon...")
    focus_manager.stop()
    wake_listener.stop()
    bus.set_state("idle")
    await _shutdown_engine(engine_holder[0])
    await tts.close()

if __name__ == "__main__":
    asyncio.run(main())
