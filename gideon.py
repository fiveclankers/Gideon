#!/usr/bin/env python3
"""
GIDEON: Antigravity-Powered Voice Companion & Autonomous Executive
Built for Omarchy / Hyprland Linux.
Features:
- Unified HTTP Visualizer Server on port 8790 (serves Circuit Board & all faces).
- In-Browser Hold-to-Talk via Spacebar (/api/talk/start & /api/talk/stop).
- Headless bidirectional NDJSON Antigravity Engine (`agy`).
- Faster-Whisper on CUDA with automatic CPU fallback.
- Fish Audio Neural Cloud TTS with PipeWire (`pw-play`) playback.
- Dynamic waveform & state IPC bridge for `ai-visualizer`.
- 45-second tool execution watchdog check-in.
- Priority intervention (in-flight prompt injection without SIGINT).
- Concurrent terminal input (Press ENTER or type directly).
"""

import argparse
import asyncio
import ctypes
import io
import json
import logging
import mimetypes
import os
import re
import site
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union
from reflex_router import try_reflex, ensure_hyprland_env

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
SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
BAREHANDS_STATE_DIR = Path("/home/cat/barehands/state")
BAREHANDS_STATE_DIR.mkdir(parents=True, exist_ok=True)

# Conversational hot-mic window: how long GIDEON keeps listening for a
# follow-up without the wake word. Long enough for trailing thoughts; a truly
# silent room still idles out when the window closes.
HOT_MIC_WINDOW_S = 45.0
# Budget handed to the capture loop for one utterance. It slides forward while
# speech keeps arriving, so this is a silence-gap budget, not a hard clip.
MAX_UTTERANCE_S = 45.0

# Fish Audio Config
FISH_TTS_URL = "https://api.fish.audio/v1/tts"


def _load_gideon_env() -> None:
    """Loads ~/.config/gideon/gideon.env into os.environ without clobbering set vars.

    Allows the Fish Audio credential to be externalized from source while keeping live
    TTS working for deployments that have not yet migrated to the env file.
    """
    env_file = Path.home() / ".config" / "gideon" / "gideon.env"
    if not env_file.is_file():
        return
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


def _preload_cuda_libs():
    """Make bundled CUDA libs resolvable for faster-whisper without torch."""
    try:
        candidates = []
        for sp in site.getsitepackages():
            candidates.append(f"{sp}/nvidia/cublas/lib/libcublas.so.12")
            candidates.append(f"{sp}/nvidia/cudnn_cu12/lib/libcudnn.so.9")
            candidates.append(f"{sp}/nvidia/cuda_runtime/lib/libcudart.so.12")
        for lib in candidates:
            if os.path.exists(lib):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                    if "cublas" in lib:
                        break
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"CUDA preload check: {e}")


class SignalBus:
    """Manages state, tool telemetry, subtitles, live 64-band waveforms, and Quickshell Unix IPC."""
    def __init__(self, signals_dir: Path):
        self.dir = signals_dir
        self.state_file = self.dir / ".voice_state"
        self.waveform_file = self.dir / ".voice_waveform"
        self.current_state = "idle"
        self.current_detail = ""
        self.engine_name = "agy"
        self.current_samples = [0.0] * 64
        self.current_level = 0.0
        self.current_user = ""
        self.current_reply = ""
        self.current_tool = None
        self.current_effort = "low"
        self.wake_enabled = True
        self.ui_clients: set[asyncio.StreamWriter] = set()
        self._ui_server = None
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
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(f"Gideon Quickshell UI IPC server active on {UI_SOCK_PATH}")
        except Exception as e:
            logger.error(f"Failed to start UI IPC server on {UI_SOCK_PATH}: {e}")

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(2.5)
            if self.ui_clients:
                self.broadcast_ui({"event": "ping", "ts": time.time()})

    def broadcast_ui(self, msg: dict):
        if not self.ui_clients:
            return
        line = (json.dumps(msg) + "\n").encode()
        for w in list(self.ui_clients):
            if w.is_closing():
                self.ui_clients.discard(w)
                continue
            try:
                w.write(line)
            except Exception:
                self.ui_clients.discard(w)

    def set_state(self, state: str, detail: str = ""):
        self.current_state = state
        self.current_detail = detail
        try:
            tmp = self.dir / ".voice_state.tmp"
            tmp.write_text(state)
            tmp.replace(self.state_file)
        except Exception as e:
            logger.debug(f"Error writing voice state: {e}")

        # Mirror to barehands state airlock
        if BAREHANDS_STATE_DIR.is_dir():
            try:
                (BAREHANDS_STATE_DIR / "state").write_text(state)
                mood = "green" if state == "idle" else ("amber" if state in ("thinking", "listening", "toolcall") else "green")
                (BAREHANDS_STATE_DIR / "mood.json").write_text(json.dumps({"mood": mood, "ts": time.time()}))
            except Exception:
                pass

        self.broadcast_ui({"event": "state", "val": state, "detail": detail, "engine": self.engine_name})

    def broadcast_rms(self, rms: float):
        self.broadcast_ui({"event": "rms", "val": round(float(rms), 3)})

    def broadcast_transcript(self, speaker: str, text: str):
        self.broadcast_ui({"event": "transcript", "speaker": speaker, "text": text})

    def update_waveform(self, samples: list[float]):
        self.current_samples = samples
        try:
            payload = json.dumps({"ts": time.time(), "samples": samples})
            tmp = self.dir / ".voice_waveform.tmp"
            tmp.write_text(payload)
            tmp.replace(self.waveform_file)
        except Exception:
            pass

        # Mirror to barehands wave airlock
        if BAREHANDS_STATE_DIR.is_dir():
            try:
                (BAREHANDS_STATE_DIR / "wave.json").write_text(
                    json.dumps({"ts": time.time(), "samples": samples})
                )
            except Exception:
                pass

    def clear_waveform(self):
        self.update_waveform([0.0] * 64)


class TTSPlayer:
    """Fish Audio neural speech player with pipelined pre-fetching & PipeWire streaming."""
    def __init__(self, bus: SignalBus):
        self.bus = bus
        self._current_proc: Optional[subprocess.Popen] = None
        self._is_speaking = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._current_synth_tasks: set[asyncio.Task] = set()

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
        import httpx
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
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(FISH_TTS_URL, headers=headers, json=payload)
                if resp.status_code == 200 and len(resp.content) > 500:
                    return resp.content
                logger.error(f"Fish TTS failed ({resp.status_code}): {resp.text[:150]}")
        except Exception as e:
            logger.error(f"Fish TTS error: {e}")
        return None

    def queue_sentence(self, text: str):
        """Immediately starts background synthesis task and enqueues it for pipelined playback."""
        text = text.strip()
        if not text:
            return
        
        synth_task = asyncio.create_task(self.synthesize(text))
        self._current_synth_tasks.add(synth_task)
        synth_task.add_done_callback(lambda t: self._current_synth_tasks.discard(t))
        self._queue.put_nowait((text, synth_task))

    async def _playback_loop(self):
        while True:
            try:
                text, synth_task = await self._queue.get()
            except asyncio.CancelledError:
                break
            
            try:
                wav = await synth_task
                if wav:
                    await self.play_wav(wav, text=text)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in playback worker: {e}")
            finally:
                self._queue.task_done()

    async def interrupt(self):
        """Kills active audio playback immediately and cancels queued speech."""
        while not self._queue.empty():
            try:
                _, task = self._queue.get_nowait()
                task.cancel()
                self._queue.task_done()
            except Exception:
                pass

        for task in list(self._current_synth_tasks):
            task.cancel()
        self._current_synth_tasks.clear()

        if self._current_proc:
            try:
                self._current_proc.terminate()
            except Exception:
                pass
            self._current_proc = None

        self._is_speaking = False
        self.bus.clear_waveform()
        self.bus.broadcast_rms(0.0)
        self.bus.set_state("idle")

    async def wait_until_done(self):
        """Waits until all queued speech has completed playing."""
        await self._queue.join()

    async def play_wav(self, wav_bytes: bytes, text: str = ""):
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

        # Animate visualizer waveform and orb RMS while playing
        async def _animate():
            t0 = time.time()
            while self._is_speaking and proc.poll() is None:
                t = time.time() - t0
                bars = [
                    float(np.clip(0.3 + 0.6 * np.sin(t * 8 + i * 0.3) * np.cos(t * 3), 0.05, 1.0))
                    for i in range(64)
                ]
                self.bus.update_waveform(bars)
                norm_rms = float(np.mean(bars) * (0.6 + 0.4 * np.sin(t * 7)))
                self.bus.broadcast_rms(norm_rms)
                await asyncio.sleep(0.04)
            self.bus.clear_waveform()
            self.bus.broadcast_rms(0.0)

        anim_task = asyncio.create_task(_animate())
        try:
            _, stderr = await asyncio.to_thread(proc.communicate, wav_bytes)
            if proc.returncode != 0:
                logger.error(f"pw-play failed with code {proc.returncode}: {stderr.decode(errors='ignore') if stderr else ''}")
        except Exception as e:
            logger.error(f"pw-play error: {e}")
        finally:
            self._is_speaking = False
            self._current_proc = None
            anim_task.cancel()
            try:
                await anim_task
            except asyncio.CancelledError:
                pass
            self.bus.clear_waveform()
            self.bus.broadcast_rms(0.0)
            if not self._queue.empty() or len(self._current_synth_tasks) > 0:
                self.bus.set_state("speaking", "Synthesizing next…")


def clean_text_for_tts(raw: str) -> Optional[str]:
    """Sanitizes text for neural TTS playback, stripping markdown, code, and raw tokens."""
    if not raw:
        return None
    # Strip markdown headers (# Foo -> Foo)
    text = re.sub(r'^\s*#{1,6}\s*', '', raw)
    # Strip markdown blockquotes (> Foo -> Foo)
    text = re.sub(r'^\s*>\s*', '', text)
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
    # Strip leading numbered list or bullet markers (e.g. "1. ", "2) ", "- ", "* ")
    text = re.sub(r'^\s*(\d+[\.\)]|[-*•])\s*', '', text)
    # Replace em-dashes and en-dashes with natural comma pauses
    text = text.replace('—', ', ').replace('–', ', ')
    # Collapse extra whitespace
    text = ' '.join(text.split()).strip()

    # Drop single numbers, empty or trivial text
    if not text or len(text) < 3 or re.match(r'^\d+[\.\)]?$', text):
        return None
    # Drop purely punctuation or single characters
    if not any(c.isalnum() for c in text):
        return None
    # Drop intermediate planning phrases if they leak through
    lower = text.lower()
    if lower.startswith(("let me check", "let me inspect", "let me search", "checking for", "running command")):
        return None

    return text


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
        self.turn_start_time = 0.0
        self.conversation_id: Optional[str] = None
        self.effort: str = "low"
        self.sentence_buffer = ""
        self.accumulated_reply = ""

    async def start(self, conversation_id: Optional[str] = None, effort: Optional[str] = None):
        if effort:
            self.effort = effort
        self.bus.current_effort = self.effort

        cmd = [
            "agy", "--input-format", "stream-json", "--output-format", "stream-json",
            "--dangerously-skip-permissions", "--effort", self.effort, "-p", ""
        ]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])

        logger.info(f"Initializing Gideon's Antigravity Engine (effort={self.effort}, conv={conversation_id or 'new'})...")
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(HERE),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        init_line = await self.proc.stdout.readline()
        if init_line:
            try:
                init_data = json.loads(init_line.decode("utf-8", errors="ignore"))
                cid = init_data.get("conversation_id")
                if cid:
                    self.conversation_id = cid
                logger.info(f"Antigravity Engine connected and ready. (conv={self.conversation_id})")
            except Exception:
                logger.info("Antigravity Engine connected and ready.")

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        self._read_task = asyncio.create_task(self._read_loop())

    async def set_effort(self, new_effort: str):
        if new_effort not in ("low", "medium", "high", "xhigh", "max"):
            return
        if new_effort == self.effort:
            return
        logger.info(f"Switching Antigravity Engine effort: {self.effort} -> {new_effort} (preserving conversation {self.conversation_id})...")
        self.effort = new_effort
        self.bus.current_effort = new_effort

        if self.proc:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except Exception:
                pass
            self.proc = None

        await self.start(conversation_id=self.conversation_id, effort=new_effort)

    async def send_prompt(self, text: str):
        if not text.strip():
            return
        # Priority intervention: interrupt in-flight speaking
        await self.tts.interrupt()

        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self.bus.current_user = text
        self.bus.current_reply = ""
        self.bus.broadcast_transcript("user", text)
        print(f"\nYou: {text}\n", flush=True)
        payload = json.dumps({"event": "user", "message": {"content": text}}) + "\n"
        
        self.turn_start_time = time.time()
        self.is_turn_active = True
        self.bus.set_state("conversing", "🧠 LLM: Thinking…")
        
        # Start the 45-second hardened watchdog
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        if self.proc and self.proc.stdin:
            self.proc.stdin.write(payload.encode())
            await self.proc.stdin.drain()

    async def interrupt(self, hard: bool = False) -> bool:
        await self.tts.interrupt()
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self.bus.current_reply = ""
        self.is_turn_active = False
        self.current_tool = None
        self.bus.current_tool = None
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self.bus.set_state("idle")
        return True

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
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        active_tool_id = 0

        while True:
            line_bytes = await self.proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            try:
                ev = json.loads(line)
            except Exception:
                continue

            event_type = ev.get("event")
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

                # Model is invoking a tool
                elif step_type == "tool_call":
                    tool_name = update.get("tool_name", "tool")
                    tool_args = update.get("tool_args", {})
                    active_tool_id += 1

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

                async def _on_turn_done():
                    await self.tts.wait_until_done()
                    self.is_turn_active = False
                    self.current_tool = None
                    self.bus.current_tool = None
                    if self._watchdog_task:
                        self._watchdog_task.cancel()
                        self._watchdog_task = None
                    if hasattr(self, "wake_listener") and self.wake_listener:
                        self.wake_listener.arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)
                    else:
                        self.bus.set_state("idle")
                    print("\n[Turn Complete] Gideon is ready.\n", flush=True)

                asyncio.create_task(_on_turn_done())


class VoiceListener:
    """High-fidelity microphone listener with CUDA faster-whisper turbo STT."""
    def __init__(self):
        _preload_cuda_libs()
        import faster_whisper
        self.device = "cuda"
        self.compute_type = "int8"
        try:
            logger.info("Loading Faster-Whisper (turbo) on CUDA...")
            self.model = faster_whisper.WhisperModel("turbo", device="cuda", compute_type="int8")
            logger.info("Faster-Whisper CUDA turbo engine initialized.")
        except Exception as e:
            logger.warning(f"CUDA STT initialization failed ({e}), falling back to CPU.")
            self.device = "cpu"
            self.model = faster_whisper.WhisperModel("small.en", device="cpu", compute_type="int8")

    def transcribe_audio(self, pcm_data: bytes) -> str:
        if not pcm_data or len(pcm_data) < 3200:
            return ""
        audio_data = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
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
            text = " ".join([seg.text for seg in segments]).strip()
            if not text and len(audio_data) >= 8000:
                # Fallback: if VAD aggressively filtered out short spelling or soft utterance, retry without VAD
                segments_fallback, _ = self.model.transcribe(
                    audio_data,
                    beam_size=1,
                    best_of=1,
                    language="en",
                    initial_prompt=OPTIMAL_WHISPER_PROMPT,
                    vad_filter=False
                )
                text = " ".join([seg.text for seg in segments_fallback]).strip()
            return text
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
                    return " ".join([seg.text for seg in segments]).strip()
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
        self.ambient_floor = 0.010
        import scipy.signal as signal
        self.bp_b, self.bp_a = signal.butter(2, [200 / 8000, 3800 / 8000], btype="bandpass")

    def _callback(self, indata, frames_count, time_info, status):
        if self.is_recording:
            self.frames.append(indata.copy())
            import scipy.signal as signal
            chunk = indata.flatten().astype(np.float32) / 32768.0
            filtered = signal.lfilter(self.bp_b, self.bp_a, chunk)
            rms = float(np.sqrt(np.mean(filtered**2)))
            self.ambient_floor = self.ambient_floor * 0.95 + min(rms, 0.025) * 0.05
            # Gated against fan noise and scaled down a couple folds
            net_rms = max(0.0, rms - self.ambient_floor * 1.5)
            norm_rms = float(np.clip(net_rms * 4.0, 0.0, 0.75))
            self.bus.current_level = norm_rms
            self.bus.broadcast_rms(norm_rms)

            fft_data = np.abs(np.fft.rfft(filtered))[:32]
            max_fft = np.max(fft_data) if len(fft_data) and np.max(fft_data) > 0 else 1.0
            bars = [float(np.clip((v / max_fft) * norm_rms, 0.0, 0.75)) for v in fft_data]
            if len(bars) < 32:
                bars += [0.0] * (32 - len(bars))
            self.bus.update_waveform(bars + [0.0] * 32)

    def start(self):
        import sounddevice as sd
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
            logger.error(f"Error starting mic stream: {e}")

    def stop(self) -> bytes:
        self.is_recording = False
        self.bus.current_level = 0.0
        self.bus.broadcast_rms(0.0)
        self.bus.clear_waveform()
        self.bus.set_state("thinking")
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if not self.frames:
            return b""
        pcm = np.concatenate(self.frames).tobytes()
        logger.info(f"🎙️ [Browser Hold-to-Talk] Microphone CLOSED ({len(pcm)} bytes captured).")
        return pcm


class WakeWordListener:
    """Robust background listener with seamless conversational hot-mic talk buffer zone."""
    def __init__(self, bus: SignalBus, tts: TTSPlayer, on_speech_captured, main_loop=None, wake_threshold: float = 0.20, hot_mic_timeout: float = HOT_MIC_WINDOW_S):
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
        self._listener_instance = None

    def is_hot_mic_active(self) -> bool:
        return self._hot_mic_armed and (time.time() < self._hot_mic_until)

    def arm_hot_mic(self, timeout_s: float = HOT_MIC_WINDOW_S):
        self.hot_mic_timeout = timeout_s
        self._hot_mic_until = time.time() + timeout_s
        self._hot_mic_armed = True
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

    def _run_loop(self):
        sys.path.insert(0, str(HERE))
        from audio.wake import AudioListener
        from audio.sounds import play_sound

        def on_rms(norm_rms: float):
            if self.bus.current_state in ("listening", "hotmic"):
                # Attenuate reactivity by a factor of ~2.5x to eliminate fan air noise
                gated_rms = float(np.clip(norm_rms * 0.40, 0.0, 0.75))
                self.bus.current_level = gated_rms
                self.bus.broadcast_rms(gated_rms)
                bars = [float(np.clip(gated_rms * (0.35 + 0.65 * np.sin(i * 0.35)), 0.0, 0.75)) for i in range(32)]
                self.bus.update_waveform(bars + [0.0] * 32)

        def on_wake():
            logger.info("⚡ Wake word detected ('Hey Jarvis')!")
            self.disarm_hot_mic()
            self.bus.set_state("listening")

        def on_barge_in():
            logger.info("⚡ Acoustic barge-in detected! Interrupting active speech...")
            if self.main_loop and self.main_loop.is_running():
                asyncio.run_coroutine_threadsafe(self.tts.interrupt(), self.main_loop)

        listener = AudioListener(
            wake_word="hey jarvis",
            wake_threshold=self.wake_threshold,
            mic_gain=1.8,
            hot_mic_timeout=self.hot_mic_timeout,
            on_rms_update=on_rms,
            on_wake_detected=on_wake,
            on_barge_in=on_barge_in
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

                try:
                    audio_buffer = await listener.listen_for_command(
                        is_hot_mic=is_hot_mic,
                        is_speaking=self.tts._is_speaking,
                        silence_timeout_s=1.0,
                        max_duration_s=MAX_UTTERANCE_S,
                        hot_mic_checker=self.is_hot_mic_active,
                        is_speaking_checker=lambda: self.tts._is_speaking or not self.enabled
                    )
                    consecutive_errors = 0
                    if audio_buffer is not None and self.enabled and not self.tts._is_speaking:
                        # Follow-up speech detected and recorded! Immediately disarm and flip to thinking
                        self.disarm_hot_mic()
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
                    try:
                        import sounddevice as sd
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
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

    async def prompt_line(self, prompt: str) -> str:
        if not self.is_tty:
            await asyncio.sleep(3600)
            return ""
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line_bytes = await self.reader.readline()
        if not line_bytes:
            self.is_tty = False
            await asyncio.sleep(3600)
            return ""
        return line_bytes.decode("utf-8", errors="ignore").strip()


def run_visualizer_server(bus: SignalBus, recorder: AudioStreamRecorder, get_listener, get_engine, get_wake_listener, loop: asyncio.AbstractEventLoop, get_tts=None, switch_engine_cb=None):
    """Runs the integrated HTTP server for the GIDEON PRIME visualizer."""
    class VisualizerHandler(BaseHTTPRequestHandler):
        def _host_allowed(self) -> bool:
            # The POST endpoints drive reflexes that execute local commands.
            # A cross-origin page can bypass CORS preflight because this server
            # answers OPTIONS with Allow-Origin *, so gate on the Host header:
            # only loopback origins may talk to the daemon.
            host = (self.headers.get("Host") or "").strip().lower()
            return host in ("127.0.0.1", "localhost", "[::1]") or host.startswith(
                ("127.0.0.1:", "localhost:", "[::1]:")
            )

        def do_GET(self):
            if not self._host_allowed():
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
                    "rate_limits": {}
                }
                self._send(json.dumps(data).encode(), "application/json")
            elif path == "/config":
                out = {
                    "name": "GIDEON PRIME",
                    "badge": "ANTIGRAVITY CORE",
                    "face": "board",
                    "thinking_sound": True,
                    "faces": [{"id": "board", "title": "GIDEON PRIME", "tagline": ""}]
                }
                self._send(json.dumps(out).encode(), "application/json")
            else:
                self._static(path)

        def do_OPTIONS(self):
            if not self._host_allowed():
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if not self._host_allowed():
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
                                tts_inst = get_tts() if get_tts else None
                                handled = await try_reflex(text, bus=bus, tts=tts_inst, set_engine=switch_engine_cb)
                                if not handled:
                                    curr_engine = get_engine()
                                    bus.set_state("conversing", "🧠 LLM: Thinking…")
                                    await curr_engine.send_prompt(text)
                                else:
                                    wake_listener = get_wake_listener()
                                    if wake_listener:
                                        async def _post_talk():
                                            await asyncio.sleep(2.0)
                                            if bus.current_state in ("bge_reflex", "speaking"):
                                                wake_listener.arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)
                                        asyncio.create_task(_post_talk())
                            else:
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
                            handled = await try_reflex(text, bus=bus, tts=tts_inst, set_engine=switch_engine_cb)
                            if not handled:
                                curr_engine = get_engine()
                                bus.set_state("conversing", "🧠 LLM: Thinking…")
                                if eff and hasattr(curr_engine, "set_effort") and eff != curr_engine.effort:
                                    await curr_engine.set_effort(eff)
                                await curr_engine.send_prompt(text)
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
                    enabled = bool(data.get("enabled", True))
                    bus.wake_enabled = enabled
                    wl = get_wake_listener()
                    if wl:
                        wl.enabled = enabled
                    self._send(b'{"status":"ok"}', "application/json")
                except Exception as e:
                    self._send(json.dumps({"error": str(e)}).encode(), "application/json", 400)
            elif path == "/api/switch-to-orb":
                # Notify SignalBus / Quickshell Orb that user returned to desktop
                bus.set_state(bus.current_state)
                self._send(b'{"status":"switched_to_orb"}', "application/json")
            else:
                self._send(b'{"error":"not found"}', "application/json", 404)

        def _static(self, path):
            if path == "/" or path == "":
                path = "/faces/board/index.html"
            target = (VISUALIZER_DIR / path.lstrip("/")).resolve()
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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), VisualizerHandler)
    logger.info(f"Visualizer HTTP Server listening on http://127.0.0.1:{HTTP_PORT}/faces/board/")
    server.serve_forever()


def _make_engine(name: str, bus: SignalBus, tts: TTSPlayer):
    """Factory: 'agy' default preserves existing behavior; 'opencode' is opt-in sidecar."""
    if name == "opencode":
        from opencode_engine import OpenCodeEngine
        return OpenCodeEngine(bus, tts, directory=str(HERE))
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
    parser.add_argument("--engine", choices=["agy", "opencode"],
                        default=os.environ.get("GIDEON_ENGINE", "agy"),
                        help="Brain backend (default agy; opencode is opt-in sidecar)")
    args, _ = parser.parse_known_args()

    loop = asyncio.get_running_loop()
    bus = SignalBus(SIGNALS_DIR)
    await bus.start_ui_server()
    tts = TTSPlayer(bus)
    tts.start_worker()
    engine_name = args.engine
    engine = _make_engine(engine_name, bus, tts)
    bus.engine_name = engine_name
    engine_holder = [engine]
    wake_listener_holder = [None]
    recorder = AudioStreamRecorder(bus)
    listener_instance = [None]

    async def switch_engine(target: str) -> bool:
        nonlocal engine_name
        if target not in ("agy", "opencode"):
            logger.warning(f"Unknown engine target: {target}")
            return False
        if target == engine_name:
            logger.info(f"[Engine] already on {target}")
            return True
        try:
            await _shutdown_engine(engine_holder[0])
            new_engine = _make_engine(target, bus, tts)
            await new_engine.start()
            if wake_listener_holder[0]:
                new_engine.wake_listener = wake_listener_holder[0]
            engine_holder[0] = new_engine
            engine_name = target
            bus.engine_name = target
            logger.info(f"[Engine] dynamically switched to {target}")
            return True
        except Exception as e:
            logger.error(f"Engine switch to {target} failed ({e}); reverting to agy.")
            new_engine = _make_engine("agy", bus, tts)
            await new_engine.start()
            if wake_listener_holder[0]:
                new_engine.wake_listener = wake_listener_holder[0]
            engine_holder[0] = new_engine
            engine_name = "agy"
            bus.engine_name = "agy"
            return False

    def on_wake_speech(pcm_bytes: bytes):
        bus.set_state("thinking", "Transcribing speech…")
        async def _process_voice():
            curr_engine = engine_holder[0]
            if curr_engine and getattr(curr_engine, "is_turn_active", False):
                await curr_engine.interrupt()
            listener = listener_instance[0]
            if listener:
                text = await loop.run_in_executor(None, listener.transcribe_audio, pcm_bytes)
                if text:
                    handled = await try_reflex(text, bus=bus, tts=tts, set_engine=switch_engine)
                    if not handled:
                        curr_engine = engine_holder[0]
                        bus.set_state("conversing", "🧠 LLM: Thinking…")
                        await curr_engine.send_prompt(text)
                    else:
                        async def _post_wake():
                            await asyncio.sleep(2.0)
                            if wake_listener_holder[0] and bus.current_state in ("bge_reflex", "speaking"):
                                wake_listener_holder[0].arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)
                        asyncio.create_task(_post_wake())
                else:
                    bus.set_state("idle")
                    if wake_listener_holder[0]:
                        wake_listener_holder[0].arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)
            else:
                bus.set_state("idle")
        asyncio.run_coroutine_threadsafe(_process_voice(), loop)

    wake_listener = WakeWordListener(
        bus=bus,
        tts=tts,
        on_speech_captured=on_wake_speech,
        main_loop=loop,
        wake_threshold=0.20
    )
    wake_listener_holder[0] = wake_listener
    engine.wake_listener = wake_listener
    # Farewell ("bye bye") reflex hook: drop the hot-mic window and settle idle.
    bus.dismiss_hot_mic = wake_listener.disarm_hot_mic

    # Pre-warm BGE-small INT8 ONNX dispatcher immediately at startup
    try:
        from reflex_router import get_gideon_dispatcher
        disp = get_gideon_dispatcher()
        if disp is not None:
            disp.predict("warmup")
            logger.info("GIDEON Tier-1 BGE-small classifier pre-warmed and ready for instant evaluation.")
    except Exception as e:
        logger.warning(f"Failed pre-warming BGE-small: {e}")

    # Start integrated visualizer HTTP server IMMEDIATELY
    server_thread = threading.Thread(
        target=run_visualizer_server,
        args=(bus, recorder, lambda: listener_instance[0], lambda: engine_holder[0], lambda: wake_listener_holder[0], loop, lambda: tts, switch_engine),
        daemon=True
    )
    server_thread.start()

    await engine.start()

    logger.info("Gideon online. Initializing STT engine...")
    listener_instance[0] = VoiceListener()
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
    print(" • 'engine agy|opencode' to switch backend, 'interrupt' to stop turn", flush=True)
    print(" • Type 'quit' or 'exit' to close", flush=True)
    print("="*65 + "\n", flush=True)

    while True:
        try:
            user_in = await stdin_reader.prompt_line("You [Hey Jarvis / HOLD SPACE in browser / type]: ")

            if user_in.lower() in ("quit", "exit"):
                break

            # Runtime backend switch without breaking the agy path.
            if user_in.lower() in ("engine", "engine agy", "engine opencode"):
                parts = user_in.split()
                if len(parts) == 1:
                    print(f"[Engine] current: {engine_name}\n", flush=True)
                    continue
                target = parts[1]
                if target == engine_name:
                    print(f"[Engine] already on {target}\n", flush=True)
                    continue
                try:
                    await _shutdown_engine(engine_holder[0])
                    engine = _make_engine(target, bus, tts)
                    await engine.start()
                    if wake_listener_holder[0]:
                        engine.wake_listener = wake_listener_holder[0]
                    engine_holder[0] = engine
                    engine_name = target
                    bus.engine_name = target
                    print(f"[Engine] switched to {target}\n", flush=True)
                except Exception as e:
                    logger.error(f"Engine switch to {target} failed ({e}); reverting to agy.")
                    engine = _make_engine("agy", bus, tts)
                    await engine.start()
                    if wake_listener_holder[0]:
                        engine.wake_listener = wake_listener_holder[0]
                    engine_holder[0] = engine
                    engine_name = "agy"
                    bus.engine_name = "agy"
                continue

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

            handled = await try_reflex(user_in, bus=bus, tts=tts, set_engine=switch_engine)
            if not handled:
                await engine_holder[0].send_prompt(user_in)

        except (KeyboardInterrupt, EOFError):
            break

    logger.info("Shutting down Gideon...")
    wake_listener.stop()
    bus.set_state("idle")
    await _shutdown_engine(engine_holder[0])

if __name__ == "__main__":
    asyncio.run(main())
