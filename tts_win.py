"""Windows Fish Audio TTS player and non-repetitive ack clip player.

Streams Fish Audio WebSocket PCM (or HTTP WAV fallback) straight into a
WASAPI ``sounddevice.RawOutputStream`` without spawning ``pw-play`` or needing
``numpy``. Also manages pre-generated varied WAV acknowledgments under
``sounds/acks/<category>/``.
"""
from __future__ import annotations

import array
import asyncio
from dataclasses import dataclass, field
import io
import logging
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Optional
import wave
import winsound

logger = logging.getLogger("gideon.win.tts")

ROOT = Path(__file__).resolve().parent
SOUNDS_DIR = ROOT / "sounds"
ACKS_DIR = SOUNDS_DIR / "acks"

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_WS_URL = os.environ.get("GIDEON_FISH_WS_URL", "wss://api.fish.audio/v1/tts/live")
FISH_REF_ID = os.environ.get("FISH_REF_ID", "98655a12fa944e26b274c535e5e03842")
FISH_MODEL = os.environ.get("FISH_MODEL", "s2.1-pro-free")
WARM_WS_TTL_S = 25.0
TTS_GAIN = float(os.environ.get("GIDEON_TTS_GAIN", "2.6"))


def apply_pcm16_gain(pcm: bytes, gain: float) -> bytes:
    """Apply linear gain to little-endian signed 16-bit PCM with hard clipping."""
    if not pcm or abs(gain - 1.0) < 1e-3:
        return pcm
    even_len = len(pcm) & ~1
    if even_len == 0:
        return pcm
    arr = array.array("h")
    arr.frombytes(pcm[:even_len])
    for i, sample in enumerate(arr):
        scaled = int(sample * gain)
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32768:
            scaled = -32768
        arr[i] = scaled
    return arr.tobytes()


def clean_text_for_tts(raw: str) -> Optional[str]:
    """Sanitize LLM output for Fish Audio neural TTS playback."""
    if not raw:
        return None
    text = re.sub(r"(?m)^\s*#{1,6}\s*(?:\d+[.:)\-]?\s*)?", "", raw)
    text = re.sub(r"(?m)^\s*[>→⇒]\s*", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://[^\s)]+?(?=[.,!?;:]?(?:\s|$))", "the linked website", text)
    text = re.sub(r"[A-Za-z]:\\(?:[^\\\s]+\\)+([^\s\\]+)", r"\1", text)
    text = re.sub(r"(?:~/|/)(?:[a-zA-Z0-9_.-]+/)+([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*)/?(?=[.,!?;:]?(?:\s|$))", r"\1", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?m)^\s*(?:\d{1,2}[.)]\s+|[-*•](?!\d)\s*)", "", text)
    text = text.replace("—", ", ").replace("–", ", ")
    text = " ".join(text.split()).strip()
    if not text or len(text) < 3 or re.match(r"^\d{1,2}[\.\)]?$", text):
        return None
    if not any(c.isalnum() for c in text):
        return None
    lower = text.lower()
    if lower.startswith(("let me check", "let me inspect", "let me search", "checking for", "running command")):
        return None
    return text


class AckPlayer:
    """Plays non-repeating pre-generated WAV acks and soft chimes via WASAPI."""

    def __init__(self, acks_dir: Path = ACKS_DIR, sounds_dir: Path = SOUNDS_DIR) -> None:
        self.acks_dir = acks_dir
        self.sounds_dir = sounds_dir
        self._last_pick: dict[str, Path] = {}
        self._gen_lock = threading.Lock()
        self._play_gen = 0
        self._active_stream: Optional[Any] = None

    def _play_wav_path(self, path: Path, *, async_play: bool = True) -> None:
        with self._gen_lock:
            self._play_gen += 1
            my_gen = self._play_gen
            old_stream = self._active_stream
        if old_stream is not None:
            try:
                old_stream.abort()
            except Exception:
                pass

        def _worker() -> None:
            try:
                import sounddevice as sd

                with wave.open(str(path), "rb") as wf:
                    sr = wf.getframerate() or 44100
                    ch = wf.getnchannels() or 1
                    pcm = wf.readframes(wf.getnframes())
                if not pcm:
                    return
                with self._gen_lock:
                    if my_gen != self._play_gen:
                        return
                out = sd.RawOutputStream(samplerate=sr, channels=ch, dtype="int16")
                with self._gen_lock:
                    if my_gen != self._play_gen:
                        out.close()
                        return
                    self._active_stream = out
                out.start()
                try:
                    chunk_bytes = max(4096, sr * ch * 2 // 10)
                    for idx in range(0, len(pcm), chunk_bytes):
                        if my_gen != self._play_gen:
                            break
                        out.write(pcm[idx : idx + chunk_bytes])
                finally:
                    with self._gen_lock:
                        if self._active_stream is out:
                            self._active_stream = None
                    try:
                        out.stop()
                        out.close()
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("sounddevice ack fallback (%s): %s", path.name, exc)
                try:
                    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
                except Exception:
                    pass

        if async_play:
            threading.Thread(target=_worker, name="gideon-ack-play", daemon=True).start()
        else:
            _worker()

    def play_earcon(self, sound_name: str, *, async_play: bool = True) -> None:
        path = self.sounds_dir / sound_name
        if not path.is_file():
            return
        self._play_wav_path(path, async_play=async_play)

    def pick_clip(self, category: str) -> Optional[Path]:
        cat_dir = self.acks_dir / category
        if not cat_dir.is_dir():
            return None
        clips = sorted(p for p in cat_dir.glob("*.wav") if p.is_file() and p.stat().st_size > 1024)
        if not clips:
            return None
        last = self._last_pick.get(category)
        pool = [c for c in clips if c != last] if len(clips) > 1 and last in clips else clips
        chosen = random.choice(pool)
        self._last_pick[category] = chosen
        return chosen

    def play_ack(self, category: str, *, async_play: bool = True) -> Optional[Path]:
        clip = self.pick_clip(category)
        if clip is None:
            return None
        self._play_wav_path(clip, async_play=async_play)
        return clip

    def stop(self) -> None:
        with self._gen_lock:
            self._play_gen += 1
            stream = self._active_stream
            self._active_stream = None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass


@dataclass
class _FishStream:
    text: str
    chunk_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=48))
    task: Optional[asyncio.Task] = None
    fallback_wav: Optional[bytes] = None
    error: Optional[str] = None
    received_audio: bool = False


class WinTTSPlayer:
    """Fish Audio neural speech player outputting straight to WASAPI via sounddevice."""

    def __init__(self, bus: Any, ack_player: Optional[AckPlayer] = None) -> None:
        self.bus = bus
        self.acks = ack_player or AckPlayer()
        self._is_speaking = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._current_synth_tasks: set[asyncio.Task] = set()
        self._http_client: Optional[Any] = None
        self._warm_ws: Optional[Any] = None
        self._warm_ws_ts: float = 0.0
        self._synth_slots = asyncio.Semaphore(2)
        self._closed = False
        self._cancel_playback = False
        self._active_stream: Optional[Any] = None

    @staticmethod
    def _api_key() -> str:
        return os.environ.get("FISH_API_KEY", "").strip()

    def play_sound(self, sound_name: str) -> None:
        self.acks.play_earcon(sound_name, async_play=True)

    def play_ack(self, category: str, *, async_play: bool = True) -> Optional[Path]:
        return self.acks.play_ack(category, async_play=async_play)

    def start_worker(self) -> None:
        if self._closed:
            return
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._playback_loop())

    def queue_sentence(self, text: str) -> None:
        if self._closed or not text.strip():
            return
        self._cancel_playback = False
        self.start_worker()
        if self._ws_enabled():
            stream = _FishStream(text=text)
            task = asyncio.create_task(self._run_ws_stream(stream))
            stream.task = task
            self._current_synth_tasks.add(task)
            task.add_done_callback(lambda t: self._current_synth_tasks.discard(t))
            self._queue.put_nowait(stream)
        else:
            task = asyncio.create_task(self._queued_synthesis(text))
            self._current_synth_tasks.add(task)
            task.add_done_callback(lambda t: self._current_synth_tasks.discard(t))
            self._queue.put_nowait(task)

    def speak(self, text: str) -> None:
        self.queue_sentence(text)

    async def wait_until_done(self, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._queue.empty() and not self._is_speaking and not self._current_synth_tasks:
                return
            await asyncio.sleep(0.05)

    def abort_active_speech(self) -> None:
        """Synchronously abort live Fish TTS playback without stopping an ack clip."""
        self._cancel_playback = True
        stream = self._active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

    async def interrupt(self, *, stop_acks: bool = True) -> None:
        self._cancel_playback = True
        if stop_acks:
            self.acks.stop()
        stream = self._active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        for t in list(self._current_synth_tasks):
            t.cancel()
        if self._current_synth_tasks:
            await asyncio.gather(*list(self._current_synth_tasks), return_exceptions=True)
        self._current_synth_tasks.clear()
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, _FishStream) and item.task and not item.task.done():
                item.task.cancel()
            elif isinstance(item, asyncio.Task) and not item.done():
                item.cancel()
            self._queue.task_done()
        self._is_speaking = False

    async def close(self) -> None:
        self._closed = True
        await self.interrupt()
        if self._worker_task and self._worker_task is not asyncio.current_task():
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker_task = None
        if self._warm_ws is not None:
            await self._close_ws_quiet(self._warm_ws)
            self._warm_ws = None
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

    def _ws_enabled(self) -> bool:
        if not self._api_key():
            return False
        flag = os.environ.get("GIDEON_FISH_WS", "1").strip().lower()
        return flag not in {"0", "false", "no", "off"}

    def _get_http_client(self):
        import httpx
        if self._http_client is None or getattr(self._http_client, "is_closed", False):
            self._http_client = httpx.AsyncClient(timeout=20.0)
        return self._http_client

    async def synthesize(self, text: str) -> Optional[bytes]:
        api_key = self._api_key()
        if not api_key:
            logger.error("Fish TTS skipped: FISH_API_KEY is empty.")
            return None
        payload = {
            "text": text,
            "reference_id": FISH_REF_ID,
            "format": "wav",
            "sample_rate": 44100,
            "latency": "low",
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": FISH_MODEL,
        }
        try:
            resp = await self._get_http_client().post(FISH_TTS_URL, headers=headers, json=payload)
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content
            logger.error("Fish TTS failed (%s): %s", resp.status_code, resp.text[:150])
        except Exception as exc:
            logger.error("Fish TTS error: %s", exc)
        return None

    async def _queued_synthesis(self, text: str) -> Optional[bytes]:
        async with self._synth_slots:
            return await self.synthesize(text)

    @staticmethod
    def _ws_is_open(ws: Any) -> bool:
        return getattr(getattr(ws, "state", None), "name", "") == "OPEN"

    @staticmethod
    async def _close_ws_quiet(ws: Any) -> None:
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=2.0)
        except Exception:
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
        api_key = self._api_key()
        if not api_key or not self._ws_enabled() or self._warm_ws is not None:
            return
        cm = None
        try:
            import websockets
            cm = websockets.connect(
                FISH_WS_URL,
                additional_headers={"Authorization": f"Bearer {api_key}", "model": FISH_MODEL},
                open_timeout=8,
                close_timeout=2,
            )
            ws = await cm.__aenter__()
            if self._ws_is_open(ws):
                self._offer_warm_ws(ws)
                cm = None
        except Exception:
            pass
        finally:
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _ws_session(self, ws: Any, stream: _FishStream, msgpack: Any) -> tuple[bool, bool]:
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
            "prosody": {"speed": 1, "volume": 0, "normalize_loudness": False},
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
                    stream.received_audio = True
                    await stream.chunk_queue.put(combined)
            elif event == "finish":
                finished = True
                break
            elif event == "error":
                stream.error = str(msg.get("message") or msg.get("error") or msg)
                break
        return finished, received_audio

    async def _run_ws_stream(self, stream: _FishStream) -> None:
        api_key = self._api_key()
        was_cancelled = False
        finished = False
        ws = None
        cm = None
        reused = False
        try:
            import msgpack
            import websockets
            headers = {"Authorization": f"Bearer {api_key}", "model": FISH_MODEL}
            ws = self._take_warm_ws()
            if ws is not None:
                reused = True
            else:
                cm = websockets.connect(FISH_WS_URL, additional_headers=headers, open_timeout=8, close_timeout=2)
                ws = await cm.__aenter__()
            try:
                finished, _ = await self._ws_session(ws, stream, msgpack)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not reused or stream.received_audio:
                    raise
                await self._close_ws_quiet(ws)
                ws = None
                cm = websockets.connect(FISH_WS_URL, additional_headers=headers, open_timeout=8, close_timeout=2)
                ws = await cm.__aenter__()
                reused = False
                finished, _ = await self._ws_session(ws, stream, msgpack)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        except Exception as exc:
            stream.error = str(exc)
            logger.warning("Fish WS failed (%s); falling back to HTTP.", exc)
        finally:
            if not was_cancelled and finished and stream.error is None and ws is not None and self._ws_is_open(ws):
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
            if not was_cancelled and not stream.received_audio:
                stream.fallback_wav = await self.synthesize(stream.text)
            if was_cancelled:
                while not stream.chunk_queue.empty():
                    stream.chunk_queue.get_nowait()
            await stream.chunk_queue.put(None)

    async def _playback_loop(self) -> None:
        while not self._closed:
            item = await self._queue.get()
            try:
                if isinstance(item, _FishStream):
                    await self._play_ws_stream(item)
                elif isinstance(item, asyncio.Task):
                    wav_bytes = await item
                    if wav_bytes:
                        await self._play_wav_bytes(wav_bytes)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("TTS playback error: %s", exc)
            finally:
                self._queue.task_done()

    async def _play_ws_stream(self, stream: _FishStream) -> None:
        first_chunk = await stream.chunk_queue.get()
        if first_chunk is None:
            if stream.fallback_wav:
                await self._play_wav_bytes(stream.fallback_wav)
            return

        import sounddevice as sd

        self.acks.stop()
        self._is_speaking = True
        self.bus.set_state("speaking")
        loop = asyncio.get_running_loop()
        out_stream = sd.RawOutputStream(samplerate=44100, channels=1, dtype="int16")
        self._active_stream = out_stream

        def _write_pcm(pcm: bytes) -> None:
            if not self._cancel_playback:
                out_stream.write(apply_pcm16_gain(pcm, TTS_GAIN))

        try:
            out_stream.start()
            await loop.run_in_executor(None, _write_pcm, first_chunk)
            while True:
                chunk = await stream.chunk_queue.get()
                if chunk is None or self._cancel_playback:
                    break
                await loop.run_in_executor(None, _write_pcm, chunk)
        finally:
            self._active_stream = None
            try:
                out_stream.stop()
                out_stream.close()
            except Exception:
                pass
            self._is_speaking = False
            if self._queue.empty():
                self.bus.set_state("idle")

    async def _play_wav_bytes(self, wav_bytes: bytes) -> None:
        import sounddevice as sd

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sr = wf.getframerate() or 44100
            ch = wf.getnchannels() or 1
            pcm = wf.readframes(wf.getnframes())
        pcm = apply_pcm16_gain(pcm, TTS_GAIN)

        self.acks.stop()
        self._is_speaking = True
        self.bus.set_state("speaking")
        loop = asyncio.get_running_loop()
        out_stream = sd.RawOutputStream(samplerate=sr, channels=ch, dtype="int16")
        self._active_stream = out_stream

        def _play_sync() -> None:
            chunk_size = sr * ch * 2 // 10  # 100ms blocks for instant interrupt
            for idx in range(0, len(pcm), chunk_size):
                if self._cancel_playback:
                    break
                out_stream.write(pcm[idx : idx + chunk_size])

        try:
            out_stream.start()
            await loop.run_in_executor(None, _play_sync)
        finally:
            self._active_stream = None
            try:
                out_stream.stop()
                out_stream.close()
            except Exception:
                pass
            self._is_speaking = False
            if self._queue.empty():
                self.bus.set_state("idle")
