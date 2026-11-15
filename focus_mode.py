"""Hyprland Focus Mode monitor for GIDEON.

Polls the active Hyprland window during timed study/work sessions, applies a
consecutive-hit grace window so fast alt-tabs never nag, and speaks calm nudges
with a strict cooldown when sustained distractions are detected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("gideon.focus")

DEFAULT_FOCUS_CONFIG_PATH = Path(
    os.environ.get("GIDEON_FOCUS_CONFIG", str(Path.home() / ".config" / "gideon" / "focus_rules.json"))
).expanduser()


@dataclass
class FocusRules:
    """Configurable distraction rules and timing thresholds for Focus Mode."""

    poll_interval_s: float = 20.0
    grace_hits: int = 2
    cooldown_s: float = 180.0
    default_duration_min: float = 25.0
    blocked_classes: tuple[str, ...] = (
        "discord",
        "vesktop",
        "steam",
        "lutris",
        "heroic",
        "telegram-desktop",
        "org.telegram.desktop",
        "whatsapp-for-linux",
    )
    blocked_title_keywords: tuple[str, ...] = (
        "youtube",
        "shorts",
        "reddit",
        "instagram",
        "tiktok",
        "twitch",
        "netflix",
        "prime video",
        "hotstar",
        "twitter",
        "x.com",
        " / x",
        "threads",
        "facebook",
        "9gag",
    )
    allowed_title_keywords: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path = DEFAULT_FOCUS_CONFIG_PATH) -> "FocusRules":
        """Load overrides from JSON if present, falling back to safe defaults."""
        defaults = cls()
        if not path.is_file():
            return defaults
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed parsing focus config %s: %s", path, exc)
            return defaults
        if not isinstance(data, dict):
            return defaults

        def _tuple_str(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            raw = data.get(key)
            if isinstance(raw, list):
                return tuple(str(x).strip().lower() for x in raw if str(x).strip())
            return fallback

        return cls(
            poll_interval_s=max(2.0, float(data.get("poll_interval_s", defaults.poll_interval_s))),
            grace_hits=max(1, int(data.get("grace_hits", defaults.grace_hits))),
            cooldown_s=max(10.0, float(data.get("cooldown_s", defaults.cooldown_s))),
            default_duration_min=max(1.0, float(data.get("default_duration_min", defaults.default_duration_min))),
            blocked_classes=_tuple_str("blocked_classes", defaults.blocked_classes),
            blocked_title_keywords=_tuple_str("blocked_title_keywords", defaults.blocked_title_keywords),
            allowed_title_keywords=_tuple_str("allowed_title_keywords", defaults.allowed_title_keywords),
        )


def _friendly_label(token: str) -> str:
    """Return a spoken-friendly name for a matched distraction token."""
    mapping = {
        "youtube": "YouTube",
        "shorts": "YouTube Shorts",
        "reddit": "Reddit",
        "instagram": "Instagram",
        "tiktok": "TikTok",
        "twitch": "Twitch",
        "netflix": "Netflix",
        "prime video": "Prime Video",
        "hotstar": "Hotstar",
        "twitter": "Twitter",
        "x.com": "X",
        " / x": "X",
        "threads": "Threads",
        "facebook": "Facebook",
        "discord": "Discord",
        "vesktop": "Discord",
        "steam": "Steam",
        "lutris": "Lutris",
        "heroic": "Heroic Games",
        "telegram-desktop": "Telegram",
        "org.telegram.desktop": "Telegram",
        "whatsapp-for-linux": "WhatsApp",
        "9gag": "9GAG",
    }
    return mapping.get(token.lower(), token.strip().title())


def evaluate_window_distraction(window: dict[str, Any], rules: FocusRules) -> Optional[str]:
    """Return a friendly distraction label when `window` violates `rules`, else None."""
    if not isinstance(window, dict):
        return None
    win_class = str(window.get("class") or window.get("initialClass") or "").strip().lower()
    win_title = str(window.get("title") or window.get("initialTitle") or "").strip().lower()
    if not win_class and not win_title:
        return None
    if win_class in ("quickshell", "gideon") or "gideon prime" in win_title:
        return None

    for allow_kw in rules.allowed_title_keywords:
        if allow_kw and allow_kw in win_title:
            return None

    for cls_name in rules.blocked_classes:
        if cls_name and (win_class == cls_name or cls_name in win_class):
            return _friendly_label(cls_name)

    for kw in rules.blocked_title_keywords:
        if kw and kw in win_title:
            return _friendly_label(kw)

    return None


async def get_active_hyprland_window(timeout_s: float = 1.5) -> dict[str, Any]:
    """Query the focused Hyprland window via UDS socket first, falling back to hyprctl."""
    from reflex_router import ensure_hyprland_env

    sig = ensure_hyprland_env()
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if sig:
        sock_path = Path(xdg_runtime) / "hypr" / sig / ".socket.sock"
        if sock_path.exists():
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(sock_path)),
                    timeout=timeout_s,
                )
                writer.write(b"j/activewindow")
                await writer.drain()
                raw = await asyncio.wait_for(reader.read(8192), timeout=timeout_s)
                writer.close()
                await writer.wait_closed()
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

    try:
        proc = await asyncio.create_subprocess_exec(
            "hyprctl",
            "activewindow",
            "-j",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _speak_safe(tts: Any, message: str) -> None:
    """Queue TTS speech cleanly whether `tts.speak` is sync or async."""
    if tts is None or not message:
        return
    speak = getattr(tts, "speak", None)
    if callable(speak):
        res = speak(message)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)


def _play_sound_safe(tts: Any, sound_name: str) -> None:
    if tts is None:
        return
    play = getattr(tts, "play_sound", None)
    if callable(play):
        res = play(sound_name)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)


class FocusSessionManager:
    """Manages timed Hyprland Focus Mode sessions and gentle distraction nudges."""

    def __init__(
        self,
        *,
        bus: Any = None,
        tts: Any = None,
        rules: Optional[FocusRules] = None,
        window_provider: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.tts = tts
        self.rules = rules or FocusRules.load()
        self._window_provider = window_provider or get_active_hyprland_window
        self._clock = clock

        self.active: bool = False
        self.goal: str = ""
        self.duration_minutes: float = self.rules.default_duration_min
        self.started_at: float = 0.0
        self.ends_at: float = 0.0
        self.distraction_hits: int = 0
        self.nudges_sent: int = 0
        self.consecutive_hits: int = 0
        self.last_nudge_at: float = -1e9
        self.last_distraction_label: str = ""
        self._task: Optional[asyncio.Task] = None

    def attach(self, *, bus: Any = None, tts: Any = None) -> None:
        """Bind live SignalBus and TTSPlayer references once daemon components boot."""
        if bus is not None:
            self.bus = bus
        if tts is not None:
            self.tts = tts

    def start(
        self,
        duration_minutes: Optional[float] = None,
        goal: str = "",
        *,
        reload_rules: bool = True,
    ) -> dict[str, Any]:
        """Start (or restart) a timed focus session and spawn its background monitor."""
        if reload_rules:
            self.rules = FocusRules.load()
        mins = float(duration_minutes if duration_minutes is not None else self.rules.default_duration_min)
        mins = max(1.0, min(480.0, mins))

        self._cancel_task()
        now = self._clock()
        self.active = True
        self.goal = goal.strip()
        self.duration_minutes = mins
        self.started_at = now
        self.ends_at = now + mins * 60.0
        self.distraction_hits = 0
        self.nudges_sent = 0
        self.consecutive_hits = 0
        self.last_nudge_at = -1e9
        self.last_distraction_label = ""

        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._monitor_loop())
        except RuntimeError:
            self._task = None

        mins_label = f"{int(mins)}" if mins.is_integer() else f"{mins:.1f}"
        goal_part = f" for {self.goal}" if self.goal else ""
        message = f"Focus mode started for {mins_label} minutes{goal_part}."
        logger.info("[FocusMode] %s", message)
        return {**self.status(), "message": message}

    def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def stop(self) -> dict[str, Any]:
        """Stop an active focus session early and return a short summary."""
        was_active = self.active
        elapsed_min = max(0.0, (self._clock() - self.started_at) / 60.0) if self.active else 0.0
        nudges = self.nudges_sent
        self._cancel_task()
        self.active = False
        self.consecutive_hits = 0

        if not was_active:
            msg = "Focus mode is not active."
        else:
            nudge_word = "nudge" if nudges == 1 else "nudges"
            msg = f"Focus mode stopped after {elapsed_min:.0f} minutes with {nudges} {nudge_word}."
        logger.info("[FocusMode] %s", msg)
        return {"active": False, "was_active": was_active, "nudges_sent": nudges, "message": msg}

    def status(self) -> dict[str, Any]:
        """Return structured live status and a concise spoken message."""
        if not self.active:
            return {
                "active": False,
                "goal": "",
                "duration_minutes": 0.0,
                "remaining_seconds": 0,
                "remaining_minutes": 0.0,
                "distraction_hits": self.distraction_hits,
                "nudges_sent": self.nudges_sent,
                "message": "Focus mode is off.",
            }
        remaining_s = max(0.0, self.ends_at - self._clock())
        rem_min = remaining_s / 60.0
        goal_part = f" on {self.goal}" if self.goal else ""
        mins_int = max(1, round(rem_min)) if remaining_s > 0 else 0
        unit = "minute" if mins_int == 1 else "minutes"
        msg = f"Focus mode active{goal_part}: {mins_int} {unit} left, {self.nudges_sent} nudges so far."
        return {
            "active": True,
            "goal": self.goal,
            "duration_minutes": self.duration_minutes,
            "remaining_seconds": int(round(remaining_s)),
            "remaining_minutes": round(rem_min, 1),
            "distraction_hits": self.distraction_hits,
            "nudges_sent": self.nudges_sent,
            "message": msg,
        }

    async def check_once(self, window: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Evaluate one poll tick against current rules and emit a nudge if due."""
        if not self.active:
            return {"status": "inactive"}

        now = self._clock()
        if now >= self.ends_at:
            self._complete_session()
            return {"status": "completed"}

        if window is None:
            res = self._window_provider()
            window = await res if asyncio.iscoroutine(res) else res

        label = evaluate_window_distraction(window, self.rules)
        if not label:
            self.consecutive_hits = 0
            return {"status": "focused"}

        self.distraction_hits += 1
        self.consecutive_hits += 1
        self.last_distraction_label = label

        if self.consecutive_hits < self.rules.grace_hits:
            return {"status": "grace", "label": label, "consecutive_hits": self.consecutive_hits}

        if (now - self.last_nudge_at) < self.rules.cooldown_s:
            return {"status": "cooldown", "label": label}

        self.last_nudge_at = now
        self.nudges_sent += 1
        goal_suffix = f" Let's get back to {self.goal}." if self.goal else " Let's get back on track."
        nudge_msg = f"Focus check: you drifted to {label}.{goal_suffix}"
        logger.info("[FocusMode] %s", nudge_msg)

        _play_sound_safe(self.tts, "activate.wav")
        _speak_safe(self.tts, nudge_msg)
        if self.bus is not None and getattr(self.bus, "current_state", "") == "idle":
            self.bus.set_state("bge_reflex", f"⚡ Focus: {label}")
        return {"status": "nudged", "label": label, "message": nudge_msg}

    def _complete_session(self) -> str:
        nudges = self.nudges_sent
        goal = self.goal
        self.active = False
        self.consecutive_hits = 0
        goal_part = f" on {goal}" if goal else ""
        if nudges == 0:
            summary = f"Focus session{goal_part} complete. Zero distractions."
        else:
            nudge_word = "nudge" if nudges == 1 else "nudges"
            summary = f"Focus session{goal_part} complete with {nudges} {nudge_word}."
        logger.info("[FocusMode] %s", summary)
        _play_sound_safe(self.tts, "acknowledged.wav")
        _speak_safe(self.tts, summary)
        return summary

    async def _monitor_loop(self) -> None:
        try:
            while self.active:
                remaining_s = self.ends_at - self._clock()
                if remaining_s <= 0:
                    self._complete_session()
                    break
                await asyncio.sleep(min(self.rules.poll_interval_s, max(0.2, remaining_s)))
                if not self.active:
                    break
                await self.check_once()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[FocusMode] monitor loop error")


_FOCUS_MANAGER: Optional[FocusSessionManager] = None


def get_focus_manager(*, bus: Any = None, tts: Any = None) -> FocusSessionManager:
    """Return the shared FocusSessionManager singleton."""
    global _FOCUS_MANAGER
    if _FOCUS_MANAGER is None:
        _FOCUS_MANAGER = FocusSessionManager(bus=bus, tts=tts)
    else:
        _FOCUS_MANAGER.attach(bus=bus, tts=tts)
    return _FOCUS_MANAGER
