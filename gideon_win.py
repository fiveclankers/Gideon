#!/usr/bin/env python3
"""Gideon for Windows — Lean, Gaming-Safe Voice Companion.

Design priorities:
- Zero resident ML models (~25-45 MB idle RAM, 0 MB VRAM).
- Ultra-lean Tier-0 literals (``bye bye``, ``play something``, exact short
  transport/mute) + Gemini Decision 1 natural-language routing.
- Music DJ hero workflow over ``C:\\Users\\cat\\Music`` FLACs + YouTube Music
  via headless ``mpv`` Named Pipe IPC.
- Lazy ``codex app-server`` for fast/casual turns + on-demand headless ``agy``
  CLI kept warm during hot-mic and cleanly terminated back to 0 MB on ``"bye bye"``.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import queue
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
ENV_FILE = Path.home() / ".config" / "gideon" / "gideon.env"
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
RUNTIME_ROOT = LOCAL_APP_DATA / "gideon-win"
LOG_DIR = RUNTIME_ROOT / "logs"
PID_FILE = RUNTIME_ROOT / "runtime.pid.json"
STOP_SIGNAL_FILE = RUNTIME_ROOT / "stop.signal"

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "gideon_win.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("gideon.win")


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load key=value pairs from gideon.env without overriding existing env vars."""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            key = key.lstrip("\ufeff").strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)


load_env_file()

from decision_win import WinDecisionRouter  # noqa: E402
from ducking_win import AudioDucker  # noqa: E402
from engines_win import WinAgyEngine, WinCodexEngine  # noqa: E402
import music_win  # noqa: E402
from reflex_win import is_dismissal, parse_tier0  # noqa: E402
from stt_win import WinSTT, pcm16_to_wav_bytes  # noqa: E402
from tts_win import AckPlayer, WinTTSPlayer, clean_text_for_tts  # noqa: E402
import win_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Single-instance Windows Mutex
# ---------------------------------------------------------------------------

def acquire_single_instance_mutex(name: str = r"Global\GideonWindowsSingleton"):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if err == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


# ---------------------------------------------------------------------------
# Pure-Python PCM16 RMS helper (zero numpy!)
# ---------------------------------------------------------------------------

def pcm16_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n <= 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    acc = sum(s * s for s in samples)
    return (acc / n) ** 0.5 / 32768.0


# ---------------------------------------------------------------------------
# State Bus
# ---------------------------------------------------------------------------

class WinSignalBus:
    def __init__(self) -> None:
        self.state = "idle"
        self.active_engine = "codex"
        self.hot_mic_until = 0.0
        self.state_changed_cb: Optional[Any] = None

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if self.state_changed_cb:
            try:
                self.state_changed_cb(state, detail)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Global PTT & Hot-Mic Audio Capture (sounddevice RawInputStream, no numpy)
# ---------------------------------------------------------------------------

_VIRTUAL_MIC_TOKENS = ("iriun", "steam streaming", "stereo mix", "midi", "sound mapper")


def resolve_input_device(preferred: Optional[str] = None) -> tuple[Optional[int], str]:
    """Select an MME-preferred physical microphone for 16 kHz mono capture."""
    try:
        import sounddevice as sd
    except Exception:
        return None, "default"

    needle = (preferred if preferred is not None else os.environ.get("GIDEON_MIC_DEVICE", "")).strip().lower()
    devices = sd.query_devices()
    hostapis = {i: ha["name"] for i, ha in enumerate(sd.query_hostapis())}

    inputs: list[tuple[int, str, str]] = []
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0:
            ha_name = hostapis.get(dev.get("hostapi", -1), "")
            if ha_name == "Windows WDM-KS":
                continue
            inputs.append((idx, str(dev.get("name", "")), ha_name))

    if needle:
        for want_mme in (True, False):
            for idx, name, ha in inputs:
                if want_mme and ha != "MME":
                    continue
                if needle in name.lower():
                    return idx, f"[{idx}] ({ha}) {name}"

    # Fallback: skip virtual mics like Iriun Webcam and prefer real MME USB/mic
    for want_usb in (True, False):
        for idx, name, ha in inputs:
            if ha != "MME":
                continue
            lname = name.lower()
            if any(tok in lname for tok in _VIRTUAL_MIC_TOKENS):
                continue
            if want_usb and "usb" not in lname:
                continue
            return idx, f"[{idx}] ({ha}) {name}"

    return None, "system default"


class WinAudioCapture:
    """Captures 16 kHz mono int16 PCM during PTT hold or hot-mic utterance."""

    def __init__(self, sample_rate: int = 16000, *, on_speech_start: Optional[Any] = None) -> None:
        self.sample_rate = sample_rate
        self.block_frames = int(sample_rate * 0.08)  # 80 ms chunks
        self.device_index, self.device_label = resolve_input_device()
        self._on_speech_start = on_speech_start
        self._ptt_active = threading.Event()
        self._ptt_lock = threading.Lock()
        self._ptt_frames: list[bytes] = []
        self._hot_mic_active = False
        self._hot_mic_speaking_paused = False
        self._hot_mic_triggered = threading.Event()
        self._utterance_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        logger.info("[Audio] Selected mic: %s", self.device_label)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gideon-audio-cap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def begin_ptt(self) -> None:
        with self._ptt_lock:
            self._ptt_frames = []
        self._ptt_active.set()

    def end_ptt(self) -> None:
        if not self._ptt_active.is_set():
            return
        time.sleep(0.12)  # brief tail buffer so the final syllable is not clipped
        self._ptt_active.clear()
        with self._ptt_lock:
            pcm = b"".join(self._ptt_frames)
            self._ptt_frames = []
        dur_s = len(pcm) / float(self.sample_rate * 2)
        rms = pcm16_rms(pcm)
        if len(pcm) >= int(self.sample_rate * 2 * 0.25):  # >= 250ms
            logger.info("[Audio] Captured %.2fs PTT audio (rms=%.4f)", dur_s, rms)
            self._enqueue_utterance(pcm)
        else:
            logger.info("[Audio] Ignored tiny PTT buffer (%.2fs, rms=%.4f)", dur_s, rms)

    def cancel_ptt(self) -> None:
        self._ptt_active.clear()
        with self._ptt_lock:
            self._ptt_frames = []

    def set_hot_mic(self, enabled: bool) -> None:
        self._hot_mic_active = enabled

    def set_tts_playing(self, playing: bool) -> None:
        self._hot_mic_speaking_paused = playing

    def get_utterance(self, timeout_s: float = 0.2) -> Optional[bytes]:
        try:
            return self._utterance_queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def _enqueue_utterance(self, pcm: bytes) -> None:
        try:
            self._utterance_queue.put_nowait(pcm)
        except queue.Full:
            try:
                self._utterance_queue.get_nowait()
            except queue.Empty:
                pass
            self._utterance_queue.put_nowait(pcm)

    def _refresh_device(self, *, reinit_portaudio: bool = False) -> None:
        if reinit_portaudio:
            try:
                import sounddevice as sd

                sd._terminate()
                sd._initialize()
            except Exception as exc:
                logger.debug("PortAudio refresh skipped: %s", exc)
        new_idx, new_label = resolve_input_device()
        if new_idx != self.device_index or new_label != self.device_label:
            self.device_index = new_idx
            self.device_label = new_label
            logger.info("[Audio] Refreshed mic: %s", self.device_label)

    def _run(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            logger.error("sounddevice unavailable: %s", exc)
            return

        vad_threshold = float(os.environ.get("GIDEON_VAD_THRESHOLD", "0.022"))
        silence_blocks_cutoff = 14  # ~1.12s silence ends hot-mic utterance
        max_blocks = int(20.0 / 0.08)

        while not self._stop.is_set():
            # Only open the mic stream while PTT is held or hot-mic is armed!
            # When idle/gaming, zero audio callback overhead.
            if not self._ptt_active.is_set() and not self._hot_mic_active:
                time.sleep(0.04)
                continue

            if self.device_index is None:
                self._refresh_device(reinit_portaudio=True)

            hot_buf: list[bytes] = []
            hot_started = False
            silent_count = 0

            try:
                with sd.RawInputStream(
                    device=self.device_index,
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=self.block_frames,
                ) as stream:
                    while not self._stop.is_set() and (self._ptt_active.is_set() or self._hot_mic_active):
                        data, _ = stream.read(self.block_frames)
                        raw = bytes(data)
                        if self._ptt_active.is_set():
                            with self._ptt_lock:
                                self._ptt_frames.append(raw)
                            hot_buf.clear()
                            hot_started = False
                            silent_count = 0
                            continue

                        if self._hot_mic_speaking_paused:
                            hot_buf.clear()
                            hot_started = False
                            silent_count = 0
                            continue

                        rms = pcm16_rms(raw)
                        if not hot_started:
                            if rms >= vad_threshold:
                                hot_started = True
                                hot_buf = [raw]
                                silent_count = 0
                                if self._on_speech_start:
                                    try:
                                        self._on_speech_start()
                                    except Exception:
                                        pass
                        else:
                            hot_buf.append(raw)
                            if rms < vad_threshold * 0.75:
                                silent_count += 1
                            else:
                                silent_count = 0
                            if silent_count >= silence_blocks_cutoff or len(hot_buf) >= max_blocks:
                                pcm = b"".join(hot_buf)
                                hot_buf.clear()
                                hot_started = False
                                silent_count = 0
                                if len(pcm) >= int(self.sample_rate * 2 * 0.3):
                                    self._enqueue_utterance(pcm)
            except Exception as exc:
                logger.warning("Mic stream error (%s); refreshing device and retrying shortly.", exc)
                self._refresh_device(reinit_portaudio=True)
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Global Hotkey Listener (Native Win32 WH_KEYBOARD_LL + Mouse Button fallback)
# ---------------------------------------------------------------------------

_WH_KEYBOARD_LL = 13
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_QUIT = 0x0012
_LLKHF_ALTDOWN = 0x20

_MODIFIER_VKS: dict[str, tuple[int, ...]] = {
    "alt": (0xA4, 0xA5, 0x12),      # LMENU, RMENU, MENU
    "ctrl": (0xA2, 0xA3, 0x11),     # LCONTROL, RCONTROL, CONTROL
    "shift": (0xA0, 0xA1, 0x10),    # LSHIFT, RSHIFT, SHIFT
    "win": (0x5B, 0x5C),            # LWIN, RWIN
}


def _parse_keyboard_spec(spec: str) -> tuple[set[str], int]:
    parts = [p.strip().lower() for p in (spec or "alt+space").split("+") if p.strip()]
    mods: set[str] = set()
    trigger_token = ""
    for p in parts:
        if p in _MODIFIER_VKS:
            mods.add(p)
        else:
            trigger_token = p
    if not trigger_token:
        trigger_token = "space"
    if trigger_token == "space":
        vk = 0x20
    elif len(trigger_token) == 1 and trigger_token.isalnum():
        vk = ord(trigger_token.upper())
    elif trigger_token.startswith("f") and trigger_token[1:].isdigit():
        fn = int(trigger_token[1:])
        vk = 0x70 + max(0, min(11, fn - 1))
    else:
        vk = 0x20
    return mods, vk


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_MEDIA_VKS: dict[int, str] = {
    0xB3: "play_pause",  # VK_MEDIA_PLAY_PAUSE
    0xB0: "next",        # VK_MEDIA_NEXT_TRACK
    0xB1: "previous",    # VK_MEDIA_PREV_TRACK
    0xB2: "stop",        # VK_MEDIA_STOP
}


class WinHotkeyListener:
    """Listens for Push-to-Talk + media keys via non-blocking WH_KEYBOARD_LL."""

    def __init__(
        self,
        spec: str,
        on_press: Any,
        on_release: Any,
        *,
        on_media_key: Any = None,
        should_intercept_media: Any = None,
    ) -> None:
        self.spec = (spec or "alt+space").strip().lower()
        self.on_press = on_press
        self.on_release = on_release
        self.on_media_key = on_media_key
        self.should_intercept_media = should_intercept_media
        self._mouse_listener: Any = None
        self._kb_thread: Optional[threading.Thread] = None
        self._cb_thread: Optional[threading.Thread] = None
        self._cb_queue: queue.Queue[Optional[tuple[str, Any]]] = queue.Queue(maxsize=64)
        self._kb_thread_id: Optional[int] = None
        self._kb_ready = threading.Event()
        self._hook_handle: Any = None
        self._hook_proc: Any = None
        self._stop_flag = threading.Event()
        self._active = False
        self._press_ts = 0.0
        self._mods_down: set[int] = set()
        self._swallowed_media_vks: set[int] = set()

    def _enqueue_event(self, kind: str, payload: Any = None) -> None:
        try:
            self._cb_queue.put_nowait((kind, payload))
        except queue.Full:
            pass

    def _run_cb_worker(self) -> None:
        while not self._stop_flag.is_set():
            try:
                item = self._cb_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            kind, payload = item
            try:
                if kind == "press":
                    self.on_press()
                elif kind == "release":
                    self.on_release(payload)
                elif kind == "media" and self.on_media_key:
                    self.on_media_key(payload)
            except Exception:
                logger.exception("Hotkey callback failed (%s)", kind)

    def start(self) -> None:
        self._stop_flag.clear()
        self._cb_thread = threading.Thread(target=self._run_cb_worker, name="gideon-ptt-cb", daemon=True)
        self._cb_thread.start()

        if self.spec in {"xbutton1", "xbutton2", "mouse4", "mouse5"}:
            try:
                from pynput import mouse
            except Exception as exc:
                logger.warning("pynput unavailable (%s); mouse PTT disabled.", exc)
                return
            target_btn = mouse.Button.x1 if self.spec in {"xbutton1", "mouse4"} else mouse.Button.x2

            def on_click(x, y, button, pressed):
                if button == target_btn:
                    if pressed and not self._active:
                        self._active = True
                        self._press_ts = time.monotonic()
                        logger.info("[PTT] %s DOWN", self.spec)
                        self._enqueue_event("press")
                    elif not pressed and self._active:
                        self._active = False
                        held = time.monotonic() - self._press_ts
                        logger.info("[PTT] %s UP (held=%.2fs)", self.spec, held)
                        self._enqueue_event("release", held)

            self._mouse_listener = mouse.Listener(on_click=on_click)
            self._mouse_listener.daemon = True
            self._mouse_listener.start()
            logger.info("PTT hotkey armed on mouse button: %s", self.spec)
            return

        self._kb_ready.clear()
        self._kb_thread = threading.Thread(target=self._run_win32_kb_hook, name="gideon-win32-ptt", daemon=True)
        self._kb_thread.start()
        self._kb_ready.wait(timeout=2.0)

    def _run_win32_kb_hook(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL

        required_mods, trigger_vk = _parse_keyboard_spec(self.spec)
        required_mod_vks = {vk for mod in required_mods for vk in _MODIFIER_VKS[mod]}

        def _mod_is_down(mod: str, flags: int) -> bool:
            if mod == "alt" and (flags & _LLKHF_ALTDOWN):
                return True
            for vk in _MODIFIER_VKS[mod]:
                if vk in self._mods_down or (user32.GetAsyncKeyState(vk) & 0x8000):
                    return True
            return False

        def _all_mods_down(flags: int) -> bool:
            return all(_mod_is_down(mod, flags) for mod in required_mods)

        def _hook_cb(nCode: int, wParam: int, lParam: int) -> int:
            if nCode >= 0 and lParam:
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                flags = int(kb.flags)
                is_down = wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
                is_up = wParam in (_WM_KEYUP, _WM_SYSKEYUP)

                media_action = _MEDIA_VKS.get(vk)
                if media_action and self.on_media_key:
                    if is_down:
                        intercept = True
                        if self.should_intercept_media is not None:
                            try:
                                intercept = bool(self.should_intercept_media())
                            except Exception:
                                intercept = False
                        if intercept:
                            self._swallowed_media_vks.add(vk)
                            self._enqueue_event("media", media_action)
                            return 1
                    elif is_up and vk in self._swallowed_media_vks:
                        self._swallowed_media_vks.discard(vk)
                        return 1

                if vk in required_mod_vks:
                    if is_down:
                        self._mods_down.add(vk)
                    elif is_up:
                        self._mods_down.discard(vk)

                if is_down and vk == trigger_vk and _all_mods_down(flags):
                    if not self._active:
                        self._active = True
                        self._press_ts = time.monotonic()
                        logger.info("[PTT] %s DOWN", self.spec)
                        self._enqueue_event("press")
                    # Swallow keydown so Windows never opens the System Menu on Alt+Space
                    return 1

                if is_up and self._active and (vk == trigger_vk or vk in required_mod_vks):
                    self._active = False
                    held = time.monotonic() - self._press_ts
                    logger.info("[PTT] %s UP (held=%.2fs)", self.spec, held)
                    self._enqueue_event("release", held)
                    if vk == trigger_vk:
                        # Swallow trigger keyup; pass modifier keyup through so Alt/Ctrl never sticks
                        return 1

            return user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

        self._hook_proc = HOOKPROC(_hook_cb)
        self._kb_thread_id = int(kernel32.GetCurrentThreadId())
        self._hook_handle = user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._hook_proc, None, 0)
        if not self._hook_handle:
            err = ctypes.get_last_error()
            logger.error("SetWindowsHookExW(WH_KEYBOARD_LL) failed (err=%s)", err)
            self._kb_ready.set()
            return

        logger.info("PTT hotkey armed via Win32 WH_KEYBOARD_LL: %s (hook=%s)", self.spec, self._hook_handle)
        self._kb_ready.set()

        msg = wintypes.MSG()
        while not self._stop_flag.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None

    def stop(self) -> None:
        self._stop_flag.set()
        self._enqueue_event("stop")
        try:
            self._cb_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._kb_thread_id:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW(self._kb_thread_id, _WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._kb_thread and self._kb_thread.is_alive():
            self._kb_thread.join(timeout=1.5)
        if self._cb_thread and self._cb_thread.is_alive():
            self._cb_thread.join(timeout=1.5)
        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Core Orchestrator
# ---------------------------------------------------------------------------

class GideonWindows:
    def __init__(self, *, speak_enabled: bool = True, track_pids: bool = False) -> None:
        self.speak_enabled = speak_enabled
        self._track_pids = track_pids
        self._started_ts = time.time()
        self.bus = WinSignalBus()
        self.acks = AckPlayer()
        self.tts = WinTTSPlayer(self.bus, self.acks)
        self.stt = WinSTT()
        self.decision = WinDecisionRouter()
        self.codex = WinCodexEngine(self.bus, self.tts, on_proc_change=self._write_runtime_pid_file)
        self.agy = WinAgyEngine(self.bus, self.tts, on_proc_change=self._write_runtime_pid_file)
        self.music_py = str((ROOT / ".venv" / "Scripts" / "python.exe") if (ROOT / ".venv" / "Scripts" / "python.exe").is_file() else Path(sys.executable))
        self.hot_mic_window_s = float(os.environ.get("GIDEON_HOT_MIC_WINDOW_S", "14.0"))
        self.agy_idle_ttl_s = float(os.environ.get("GIDEON_AGY_IDLE_TTL_S", "180.0"))
        self.codex_idle_ttl_s = float(os.environ.get("GIDEON_CODEX_IDLE_TTL_S", "600.0"))
        self._last_turn_was_agy = False
        self._last_agy_status_ts = 0.0
        if self._track_pids:
            self._write_runtime_pid_file()

    def _write_runtime_pid_file(self) -> None:
        if not self._track_pids:
            return
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "started": self._started_ts,
                "codex_pid": self.codex.pid,
                "agy_pid": self.agy.pid,
            }
            tmp = PID_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, PID_FILE)
        except OSError:
            pass

    async def _run_music(self, *args: str) -> dict[str, Any]:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = await asyncio.create_subprocess_exec(
            self.music_py,
            str(ROOT / "music_win.py"),
            *args,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        txt = out.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(txt) if txt else {"ok": proc.returncode == 0, "error": err.decode("utf-8", errors="replace")}
        except json.JSONDecodeError:
            return {"ok": proc.returncode == 0, "raw": txt}

    def _say(self, text: str) -> None:
        clean = clean_text_for_tts(text) or text
        print(f"Gideon: {clean}", flush=True)
        if self.speak_enabled:
            self.tts.queue_sentence(clean)

    def _arm_hot_mic(self) -> None:
        self.bus.hot_mic_until = time.monotonic() + self.hot_mic_window_s

    def _disarm_hot_mic(self) -> None:
        self.bus.hot_mic_until = 0.0

    async def handle_dismiss(self, *, play_clip: bool = True) -> dict[str, Any]:
        self._disarm_hot_mic()
        self._last_turn_was_agy = False
        await self.tts.interrupt()
        if play_clip and self.speak_enabled:
            self.tts.play_ack("bye_bye", async_play=True)
        await asyncio.gather(self.agy.shutdown(), self.codex.shutdown(), return_exceptions=True)
        self.bus.active_engine = "codex"
        self.bus.set_state("idle")
        return {"handled_by": "tier0", "intent": "dismiss", "reply": "Shutting engines down."}

    async def _dispatch_to_codex(
        self,
        prompt: str,
        *,
        allow_hot_mic: bool = True,
        image_paths: Optional[list[str]] = None,
    ) -> str:
        self._last_turn_was_agy = False
        self.bus.active_engine = "codex"
        reply = await self.codex.send_prompt(prompt, speak=self.speak_enabled, image_paths=image_paths)
        if allow_hot_mic:
            self._arm_hot_mic()
        self.bus.set_state("idle")
        return reply

    async def _dispatch_to_agy(self, prompt: str, *, play_ack: bool = True) -> str:
        self._last_turn_was_agy = True
        self.bus.active_engine = "agy"
        if play_ack and self.speak_enabled:
            self.tts.play_ack("ask_agy", async_play=True)
        reply = await self.agy.send_prompt(prompt, speak=self.speak_enabled)
        self._arm_hot_mic()
        self.bus.set_state("idle")
        return reply

    async def handle_utterance(self, text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {"handled_by": "empty"}

        print(f"\nYou: {raw}", flush=True)

        # 1. Ultra-lean Tier-0 literals ONLY
        tier0 = parse_tier0(raw)
        if tier0 is not None:
            intent = tier0["intent"]
            if intent == "dismiss":
                return await self.handle_dismiss(play_clip=True)
            if intent == "music_play_local_shuffle":
                if self.speak_enabled:
                    self.tts.play_ack("music_play", async_play=True)
                res = await self._run_music("play", "--shuffle")
                if not res.get("ok"):
                    self._say(f"Couldn't start music: {res.get('error', 'unknown error')}")
                self._disarm_hot_mic()
                return {"handled_by": "tier0", "intent": intent, "result": res}
            if intent == "music_control":
                action = tier0["action"]
                res = await self._run_music(action)
                if not res.get("ok") and action in {"pause", "resume", "next", "previous", "stop"}:
                    vk_map = {"pause": "media_play_pause", "resume": "media_play_pause", "next": "media_next", "previous": "media_prev", "stop": "media_stop"}
                    res = await asyncio.to_thread(win_tools.audio_control, vk_map[action])
                self._disarm_hot_mic()
                return {"handled_by": "tier0", "intent": intent, "result": res}
            if intent == "audio_control":
                res = await asyncio.to_thread(win_tools.audio_control, tier0["action"])
                if self.speak_enabled:
                    self.tts.play_sound("acknowledged.wav")
                self._disarm_hot_mic()
                return {"handled_by": "tier0", "intent": intent, "result": res}

        # 2. If Antigravity is currently warm in hot-mic follow-up, stay on agy unless
        # the user clearly switched to status/music/etc. — check Decision 1!
        intent, conf, _ = await asyncio.to_thread(self.decision.classify, raw)
        logger.info("Decision 1 -> %s (%.2f): %s", intent, conf, raw)

        if intent == "agy_status":
            if self.speak_enabled:
                self.tts.play_ack("check_agy", async_play=True)
            st = await asyncio.to_thread(win_tools.get_agy_status)
            summary = st.get("spoken_summary") or "Couldn't read Antigravity's status."
            now_mono = time.monotonic()
            is_followup = (now_mono - self._last_agy_status_ts) < 120.0
            low_raw = raw.lower()
            wants_detail = is_followup or any(
                k in low_raw
                for k in (
                    "what is it",
                    "what's it",
                    "what is antigravity",
                    "what's antigravity",
                    "doing",
                    "working on",
                    "up to",
                    "step",
                    "detail",
                    "more",
                    "why",
                    "explain",
                    "stuck",
                    "error",
                    "where is it",
                )
            )
            self._last_agy_status_ts = now_mono
            if wants_detail and st.get("ok") and (st.get("recent_activity") or st.get("excerpt") or st.get("user_task")):
                activity_lines = "\n".join(f"- {item}" for item in (st.get("recent_activity") or []))
                prompt = (
                    f'User asked about Antigravity: "{raw}"\n'
                    f"Latest live Antigravity transcript snapshot:\n"
                    f"- state: {st.get('state')} (last touched {st.get('elapsed_s')}s ago, last tool: {st.get('last_tool')})\n"
                    f"- user_task: {st.get('user_task') or '(unknown)'}\n"
                    f"- excerpt: {st.get('excerpt') or '(none)'}\n"
                    f"- recent_activity:\n{activity_lines or '(none)'}\n\n"
                    "Answer in 1 to 2 natural spoken sentences describing concretely what Antigravity is working on or just did (mention the specific task, tools, or files from the snapshot — no generic filler)."
                )
                reply = await self._dispatch_to_codex(prompt)
                return {"handled_by": "decision1", "intent": intent, "engine": "codex", "status": st, "reply": reply}
            self._say(summary)
            self._arm_hot_mic()
            return {"handled_by": "decision1", "intent": intent, "status": st}

        if intent == "ask_agy":
            reply = await self._dispatch_to_agy(raw, play_ack=True)
            return {"handled_by": "decision1", "intent": intent, "engine": "agy", "reply": reply}

        if intent == "screen_context":
            shot = await asyncio.to_thread(win_tools.take_screenshot)
            if not shot.get("ok"):
                self._say(f"Couldn't grab your screen: {shot.get('error')}")
                return {"handled_by": "decision1", "intent": intent, "error": shot.get("error")}
            if self._last_turn_was_agy and self.agy.is_running:
                augmented = (
                    f"The user asked about what's on their screen right now: \"{raw}\"\n"
                    f"A fresh screenshot was saved to: {shot['path']}\n"
                    "Inspect that image file and answer concisely in spoken prose."
                )
                reply = await self._dispatch_to_agy(augmented, play_ack=False)
                return {"handled_by": "decision1", "intent": intent, "engine": "agy", "screenshot": shot["path"], "reply": reply}
            codex_prompt = (
                f"The user asked about what's on their screen right now: \"{raw}\"\n"
                f"The fresh screenshot is attached directly to this turn (and saved at {shot['path']}).\n"
                "Inspect the attached image and answer concisely in spoken prose."
            )
            reply = await self._dispatch_to_codex(codex_prompt, image_paths=[shot["path"]])
            return {"handled_by": "decision1", "intent": intent, "engine": "codex", "screenshot": shot["path"], "reply": reply}

        if intent == "launch_app":
            res = await asyncio.to_thread(win_tools.launch_app, raw)
            if res.get("ok"):
                self._disarm_hot_mic()
                return {"handled_by": "decision1", "intent": intent, "result": res}
            # If simple lookup missed, let Codex try resolving & opening
            reply = await self._dispatch_to_codex(f"Open or launch this Windows app/item: {raw}", allow_hot_mic=False)
            return {"handled_by": "decision1", "intent": intent, "engine": "codex", "reply": reply}

        if intent in {"music_play_local", "music_play_online", "music_control"}:
            # Fast-path common music controls; delegate rich vibe/taste/search prompts to Codex
            norm = raw.lower().strip()
            if intent == "music_control" and any(w in norm for w in ("what song", "what's playing", "now playing", "current track")):
                st = await self._run_music("now")
                self._say(st.get("now") or "Nothing is playing right now.")
                return {"handled_by": "decision1", "intent": intent, "result": st}
            hint = (
                f"Music-related voice request (speech-to-text transcript; names may be misspelled phonetically): \"{raw}\". "
                "If it's a question, just answer it. "
                "If it asks to play something: identify the exact track first (use web search on lyrics or descriptions when the user doesn't name it), "
                "run music_win.py find to check the local library, play a local match with play --path, otherwise stream it with online. "
                "Only say something is playing after the command returns ok true, and name the exact artist and title from its output."
            )
            reply = await self._dispatch_to_codex(hint, allow_hot_mic=True)
            return {"handled_by": "decision1", "intent": intent, "engine": "codex", "reply": reply}

        if intent == "audio_control":
            # Let Codex invoke win_tools.py audio cleanly for arbitrary phrasing like "turn it down a notch"
            reply = await self._dispatch_to_codex(
                f"Execute this Windows volume/audio control request using win_tools.py audio and confirm briefly: {raw}",
                allow_hot_mic=False,
            )
            return {"handled_by": "decision1", "intent": intent, "engine": "codex", "reply": reply}

        # Default conversational / web_search / hot-mic continuation
        if self._last_turn_was_agy and self.agy.is_running and time.monotonic() <= self.bus.hot_mic_until:
            reply = await self._dispatch_to_agy(raw, play_ack=False)
            return {"handled_by": "hot_mic_agy", "intent": intent, "engine": "agy", "reply": reply}

        reply = await self._dispatch_to_codex(raw)
        return {"handled_by": "decision1", "intent": intent, "engine": "codex", "reply": reply}

    async def idle_reaper_tick(self) -> None:
        now = time.monotonic()
        if self.agy.is_running and (now - self.agy.last_used_ts) > self.agy_idle_ttl_s:
            logger.info("Idle TTL expired for agy; terminating back to 0 MB RAM.")
            self._last_turn_was_agy = False
            await self.agy.shutdown()
        if self.codex.is_running and (now - self.codex.last_used_ts) > self.codex_idle_ttl_s:
            logger.info("Idle TTL expired for codex; shutting down.")
            await self.codex.shutdown()

    async def close(self) -> None:
        await self.tts.close()
        await asyncio.gather(self.agy.shutdown(), self.codex.shutdown(), return_exceptions=True)
        self.decision.close()
        self.stt.close()


# ---------------------------------------------------------------------------
# Tray Icon + Daemon Loop
# ---------------------------------------------------------------------------

def _start_tray(loop: asyncio.AbstractEventLoop, daemon: GideonWindows, capture: WinAudioCapture, stop_event: threading.Event):
    try:
        import pystray
        from PIL import Image
    except Exception as exc:
        logger.warning("Tray disabled (%s)", exc)
        return None

    icon_path = ROOT / "icon.png"
    if icon_path.is_file():
        img = Image.open(icon_path).resize((64, 64))
    else:
        img = Image.new("RGB", (64, 64), color=(32, 160, 210))

    def on_dismiss(icon, item):
        asyncio.run_coroutine_threadsafe(daemon.handle_dismiss(play_clip=False), loop)

    def on_open_music(icon, item):
        try:
            os.startfile(str(Path.home() / "Music"))
        except OSError:
            pass

    def on_quit(icon, item):
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Gideon (Windows)", None, enabled=False),
        pystray.MenuItem("Unload Engines (Bye Bye)", on_dismiss),
        pystray.MenuItem("Open Music Folder", on_open_music),
        pystray.MenuItem("Quit", on_quit),
    )
    tray = pystray.Icon("gideon-win", img, "Gideon Windows", menu)
    t = threading.Thread(target=tray.run, name="gideon-tray", daemon=True)
    t.start()
    return tray


async def run_daemon(*, cli_mode: bool = False) -> int:
    try:
        STOP_SIGNAL_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    daemon = GideonWindows(speak_enabled=True, track_pids=True)
    ducker = AudioDucker()
    capture = WinAudioCapture(on_speech_start=ducker.duck)
    capture.start()
    stop_event = threading.Event()
    loop = asyncio.get_running_loop()
    mpv_active_ref = [await asyncio.to_thread(music_win.mpv_has_active_media)]
    convo_window_s = float(os.environ.get("GIDEON_CONVO_WINDOW_S", "300.0"))
    last_interaction_ts = [0.0]

    def _mpv_poll_loop() -> None:
        while not stop_event.wait(1.0):
            mpv_active_ref[0] = music_win.mpv_has_active_media()

    threading.Thread(target=_mpv_poll_loop, name="gideon-mpv-poll", daemon=True).start()

    def on_media_key(action: str) -> None:
        ok = music_win.mpv_media_key_action(action)
        if action == "stop" and ok:
            mpv_active_ref[0] = False
        logger.info("[MediaKey] %s -> mpv ok=%s", action, ok)

    def on_ptt_press():
        ducker.duck()
        daemon.tts.abort_active_speech()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(daemon.tts.interrupt(stop_acks=False)))
        if last_interaction_ts[0] > 0.0 and (time.monotonic() - last_interaction_ts[0]) < convo_window_s:
            daemon.acks.play_earcon("listen_soft.wav", async_play=True)
        else:
            daemon.acks.play_ack("listen_start", async_play=True)
        capture.begin_ptt()

    def on_ptt_release(held_s: float = 0.5):
        if held_s < 0.35:
            logger.info("[PTT] Quick tap detected (%.2fs) — arming single-turn VAD listen", held_s)
            capture.cancel_ptt()
            daemon.bus.hot_mic_until = time.monotonic() + 10.0
            capture.set_hot_mic(True)
        else:
            capture.end_ptt()

    hotkey_spec = os.environ.get("GIDEON_PTT_HOTKEY", "alt+space")
    hotkeys = WinHotkeyListener(
        hotkey_spec,
        on_ptt_press,
        on_ptt_release,
        on_media_key=on_media_key,
        should_intercept_media=lambda: mpv_active_ref[0],
    )
    hotkeys.start()
    tray = _start_tray(loop, daemon, capture, stop_event)

    if cli_mode:
        def _cli_reader():
            print("Gideon CLI ready. Type a command or 'quit' to exit.")
            while not stop_event.is_set():
                try:
                    line = input("> ").strip()
                except EOFError:
                    break
                if line.lower() in {"quit", "exit"}:
                    stop_event.set()
                    break
                if line:
                    fut = asyncio.run_coroutine_threadsafe(daemon.handle_utterance(line), loop)
                    try:
                        fut.result(timeout=180)
                    except Exception as exc:
                        print(f"[Error] {exc}")

        threading.Thread(target=_cli_reader, daemon=True).start()

    last_reap = time.monotonic()
    turn_active = False
    duck_idle_since: Optional[float] = None
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            capture.set_hot_mic(now < daemon.bus.hot_mic_until)
            capture.set_tts_playing(daemon.tts._is_speaking)

            pcm = capture.get_utterance(timeout_s=0.1)
            if pcm is not None:
                turn_active = True
                try:
                    dur_s = len(pcm) / float(capture.sample_rate * 2)
                    backend = daemon.stt.active_backend()
                    logger.info("[Audio] Transcribing %.2fs clip via %s", dur_s, backend)
                    wav_bytes = pcm16_to_wav_bytes(pcm, sample_rate=16000, channels=1)
                    t_stt = time.monotonic()
                    text = await asyncio.to_thread(daemon.stt.transcribe_wav_bytes, wav_bytes)
                    if text:
                        logger.info("[STT] (%.2fs) -> %r", time.monotonic() - t_stt, text)
                        t0 = parse_tier0(text)
                        if daemon.speak_enabled and (t0 is None or t0["intent"] not in {"dismiss", "music_play_local_shuffle"}):
                            daemon.acks.play_earcon("heard_soft.wav", async_play=True)
                        res = await daemon.handle_utterance(text)
                        if res.get("intent") == "dismiss":
                            last_interaction_ts[0] = 0.0
                        else:
                            last_interaction_ts[0] = time.monotonic()
                        if str(res.get("intent", "")).startswith("music_"):
                            mpv_active_ref[0] = await asyncio.to_thread(music_win.mpv_has_active_media)
                    else:
                        logger.info("[STT] Ignored empty/hallucination clip (%.2fs)", time.monotonic() - t_stt)
                        if daemon.speak_enabled:
                            daemon.acks.play_earcon("empty_soft.wav", async_play=True)
                finally:
                    turn_active = False

            is_idle_for_duck = (
                not capture._ptt_active.is_set()
                and not turn_active
                and not daemon.tts._is_speaking
                and daemon.tts._queue.empty()
                and daemon.acks._active_stream is None
            )
            if ducker.is_ducked and is_idle_for_duck:
                if duck_idle_since is None:
                    duck_idle_since = now
                elif (now - duck_idle_since) >= 0.7:
                    ducker.restore()
                    duck_idle_since = None
            else:
                duck_idle_since = None

            if (now - last_reap) >= 15.0:
                last_reap = now
                await daemon.idle_reaper_tick()
            if STOP_SIGNAL_FILE.is_file():
                logger.info("Stop signal received; shutting down Gideon, engines, and mpv.")
                try:
                    STOP_SIGNAL_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                await asyncio.to_thread(music_win.quit_mpv)
                stop_event.set()
                break
            await asyncio.sleep(0.02)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        hotkeys.stop()
        capture.stop()
        ducker.close()
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        await daemon.close()
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


async def run_oneshot(text: str, *, speak: bool = False) -> int:
    daemon = GideonWindows(speak_enabled=speak)
    try:
        res = await daemon.handle_utterance(text)
        if speak:
            await daemon.tts.wait_until_done(timeout_s=45.0)
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0
    finally:
        await daemon.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gideon_win")
    parser.add_argument("--cli", action="store_true", help="Enable interactive stdin prompt mode")
    parser.add_argument("--oneshot", help="Run a single utterance and exit")
    parser.add_argument("--speak", action="store_true", help="Enable Fish TTS during --oneshot")
    parser.add_argument("--no-mutex", action="store_true", help="Skip single-instance mutex check")
    args = parser.parse_args(argv)

    if args.oneshot:
        return asyncio.run(run_oneshot(args.oneshot, speak=args.speak))

    mutex = None if args.no_mutex else acquire_single_instance_mutex()
    if not args.no_mutex and mutex is None:
        print("Gideon Windows is already running.", file=sys.stderr)
        return 0

    def _sig_handler(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _sig_handler)
    return asyncio.run(run_daemon(cli_mode=args.cli))


if __name__ == "__main__":
    raise SystemExit(main())
