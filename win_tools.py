#!/usr/bin/env python3
"""Windows native utilities for Gideon and Codex.

Provides zero-bloat CLI + importable helpers for:
- ``agy-status``: Read the latest Antigravity ``transcript.jsonl`` in < 5 ms (0 MB RAM).
- ``screenshot``: Capture the active display frame on-demand via ``mss``.
- ``launch``: Launch desktop apps, URIs, and Start Menu / Desktop shortcuts.
- ``audio``: Core Audio volume/mute/mic via ``pycaw`` + Win32 media key fallback.
- ``is_fullscreen_active``: Check whether the foreground window covers the monitor.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

BRAIN_DIR = Path(
    os.environ.get(
        "GIDEON_AGY_BRAIN_DIR",
        str(Path.home() / ".gemini" / "antigravity" / "brain"),
    )
).expanduser()
CAPTURES_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    / "gideon-win"
    / "captures"
)

APP_MAP: dict[str, str] = {
    "obsidian": "obsidian://open?vault=Obsidian%20Vault",
    "notes": "obsidian://open?vault=Obsidian%20Vault",
    "foobar": "foobar2000",
    "foobar2000": "foobar2000",
    "chrome": "chrome",
    "google chrome": "chrome",
    "brave": "brave",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "opencode": r"C:\Users\cat\AppData\Local\Programs\@opencodedesktop\OpenCode.exe",
    "open code": r"C:\Users\cat\AppData\Local\Programs\@opencodedesktop\OpenCode.exe",
    "code": "code",
    "vscode": "code",
    "terminal": "wt",
    "windows terminal": "wt",
    "powershell": "powershell",
    "cmd": "cmd",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "task manager": "taskmgr",
    "taskmgr": "taskmgr",
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "explorer": "explorer",
    "file explorer": "explorer",
    "steam": "steam://",
    "discord": "discord://",
    "spotify": "spotify:",
    "antigravity": str(Path.home() / "Desktop" / "Antigravity.lnk"),
}

_SEARCH_DIRS = [
    Path.home() / "Desktop",
    Path.home() / "Desktop" / "Games",
    Path.home() / "Desktop" / "Tools",
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
]


# ---------------------------------------------------------------------------
# Fullscreen detection (no Xbox Game Bar)
# ---------------------------------------------------------------------------

def is_fullscreen_active() -> bool:
    """Return True when the foreground window covers its entire monitor (games/video)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor:
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
            return False
        mon = mi.rcMonitor
        # Ignore desktop shell windows
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls_buf, 64)
        if cls_buf.value in {"Progman", "WorkerW", "Shell_TrayWnd"}:
            return False
        return (
            rect.left <= mon.left
            and rect.top <= mon.top
            and rect.right >= mon.right
            and rect.bottom >= mon.bottom
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Antigravity transcript inspector (0 MB RAM, < 5 ms)
# ---------------------------------------------------------------------------

def _read_tail_lines(path: Path, max_lines: int = 35, chunk_bytes: int = 65536) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - chunk_bytes)
        fh.seek(start)
        raw = fh.read().decode("utf-8", errors="replace")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if start > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines[-max_lines:]


def _is_gideon_voice_transcript(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            for _ in range(3):
                line = fh.readline()
                if not line:
                    break
                if b"[Directive: Voice Operating Mode" in line:
                    return True
    except OSError:
        pass
    return False


def _format_elapsed(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"{sec} seconds ago"
    mins, rem = divmod(sec, 60)
    if mins < 60:
        return f"{mins} minute{'' if mins == 1 else 's'} ago"
    hours = mins // 60
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def get_agy_status(*, include_voice: bool = False) -> dict[str, Any]:
    """Inspect the latest Antigravity ``transcript.jsonl`` without spawning agy."""
    if not BRAIN_DIR.is_dir():
        return {"ok": False, "state": "none", "spoken_summary": "I couldn't find any Antigravity sessions on disk."}

    candidates: list[tuple[float, Path]] = []
    for p in BRAIN_DIR.glob("*/.system_generated/logs/transcript.jsonl"):
        try:
            st = p.stat()
            if st.st_size > 0:
                candidates.append((st.st_mtime, p))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)

    target: Path | None = None
    mtime = 0.0
    for ts, p in candidates:
        if not include_voice and _is_gideon_voice_transcript(p):
            continue
        target = p
        mtime = ts
        break

    if target is None:
        return {"ok": True, "state": "none", "spoken_summary": "No active Antigravity workspace sessions found."}

    now = time.time()
    elapsed_s = max(0.0, now - mtime)
    elapsed_txt = _format_elapsed(elapsed_s)
    conv_id = target.parents[2].name

    steps: list[dict[str, Any]] = []
    for line in _read_tail_lines(target, max_lines=30):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                steps.append(obj)
        except json.JSONDecodeError:
            continue

    if not steps:
        return {
            "ok": True,
            "conversation_id": conv_id,
            "state": "unknown",
            "elapsed_s": round(elapsed_s, 1),
            "spoken_summary": f"Antigravity's latest session was touched {elapsed_txt}, but the log tail is empty.",
        }

    last = steps[-1]
    stype = str(last.get("type") or "")
    status = str(last.get("status") or "").upper()
    tool_calls = last.get("tool_calls") or []
    last_tool = None
    if isinstance(tool_calls, list) and tool_calls:
        tc = tool_calls[-1]
        if isinstance(tc, dict):
            last_tool = tc.get("name") or tc.get("tool_name")

    content_excerpt = ""
    raw_content = last.get("content")
    if isinstance(raw_content, str) and raw_content.strip():
        cleaned = re.sub(r"\s+", " ", raw_content).strip()
        content_excerpt = cleaned[:260]

    # Extract concrete recent activity so follow-ups ("what is it actually doing?") have real context
    user_task = ""
    recent_activity: list[str] = []
    for step in steps[-12:]:
        s_type = str(step.get("type") or "")
        s_content = step.get("content")
        s_text = re.sub(r"\s+", " ", s_content).strip() if isinstance(s_content, str) else ""
        if s_type == "USER_INPUT" and s_text:
            user_task = s_text[:240]
            recent_activity.append(f"User asked: {s_text[:180]}")
        elif s_type == "PLANNER_RESPONSE":
            tcs = step.get("tool_calls") or []
            if isinstance(tcs, list) and tcs:
                tc_parts = []
                for tc in tcs[:4]:
                    if not isinstance(tc, dict):
                        continue
                    tname = str(tc.get("name") or tc.get("tool_name") or "tool")
                    targs = tc.get("arguments") or tc.get("args") or {}
                    detail = ""
                    if isinstance(targs, dict):
                        for k in ("focus", "toolSummary", "Path", "path", "CommandLine", "query", "Description"):
                            if targs.get(k):
                                detail = str(targs[k])[:100]
                                break
                    tc_parts.append(f"{tname}({detail})" if detail else tname)
                if tc_parts:
                    recent_activity.append("Tools: " + ", ".join(tc_parts))
            if s_text:
                recent_activity.append(f"Assistant: {s_text[:200]}")
        elif s_type == "TOOL_RESULT" and s_text:
            recent_activity.append(f"Tool output: {s_text[:140]}")
    recent_activity = recent_activity[-8:]

    # Heuristic state determination from Antigravity transcript step structure
    if status == "ERROR":
        state = "error"
        spoken = f"Antigravity hit an error {elapsed_txt}."
    elif stype == "USER_INPUT":
        state = "running"
        spoken = f"Antigravity got a prompt {elapsed_txt} and is still working on it."
    elif stype == "PLANNER_RESPONSE" and tool_calls:
        state = "running"
        tool_note = f" ({last_tool})" if last_tool else ""
        spoken = f"Still running — it fired a tool call{tool_note} {elapsed_txt}."
    elif stype == "PLANNER_RESPONSE" and status == "DONE":
        state = "done"
        spoken = f"Yeah, Antigravity finished {elapsed_txt} and is waiting on you."
    elif status in {"ACTIVE", "RUNNING", "PENDING"} or elapsed_s < 12.0:
        state = "running"
        spoken = f"Still churning — last activity was {elapsed_txt}."
    else:
        state = "waiting_or_idle"
        spoken = f"Last Antigravity step was {stype.lower()} ({status.lower()}) {elapsed_txt}."

    return {
        "ok": True,
        "conversation_id": conv_id,
        "transcript": str(target),
        "state": state,
        "last_step_type": stype,
        "last_status": status,
        "last_tool": last_tool,
        "elapsed_s": round(elapsed_s, 1),
        "user_task": user_task,
        "recent_activity": recent_activity,
        "excerpt": content_excerpt,
        "spoken_summary": spoken,
    }


# ---------------------------------------------------------------------------
# On-demand screenshot capture (mss, 0 MB idle)
# ---------------------------------------------------------------------------

def take_screenshot(*, keep_last: int = 5) -> dict[str, Any]:
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    # Rotate older screenshots
    existing = sorted(CAPTURES_DIR.glob("screen_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in existing[max(0, keep_last - 1) :]:
        try:
            old.unlink()
        except OSError:
            pass

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = CAPTURES_DIR / f"screen_{stamp}.png"

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    try:
        import mss
        import mss.tools

        with mss.MSS() as sct:
            mon_idx = 1 if len(sct.monitors) > 1 else 0
            try:
                user32 = ctypes.windll.user32
                hwnd = user32.GetForegroundWindow()
                if hwnd:
                    rect = wintypes.RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        cx = (rect.left + rect.right) // 2
                        cy = (rect.top + rect.bottom) // 2
                        for idx, mon in enumerate(sct.monitors[1:], start=1):
                            if mon["left"] <= cx < mon["left"] + mon["width"] and mon["top"] <= cy < mon["top"] + mon["height"]:
                                mon_idx = idx
                                break
            except Exception:
                pass
            shot = sct.grab(sct.monitors[mon_idx])
            mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))
        return {"ok": True, "path": str(out_path), "width": shot.width, "height": shot.height}
    except Exception as mss_exc:
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(all_screens=False)
            img.save(str(out_path), format="PNG")
            return {"ok": True, "path": str(out_path), "width": img.width, "height": img.height}
        except Exception as pil_exc:
            return {"ok": False, "error": f"Screen capture unavailable ({mss_exc}; {pil_exc})"}


# ---------------------------------------------------------------------------
# Windows app launcher
# ---------------------------------------------------------------------------

def _search_shortcuts(needle: str) -> Optional[Path]:
    q = needle.casefold()
    exact: list[Path] = []
    partial: list[Path] = []
    for root in _SEARCH_DIRS:
        if not root.is_dir():
            continue
        for item in root.rglob("*"):
            if item.suffix.casefold() not in {".lnk", ".url", ".exe", ".bat", ".cmd"}:
                continue
            stem = item.stem.casefold()
            if stem == q:
                exact.append(item)
            elif q in stem:
                partial.append(item)
    if exact:
        return exact[0]
    if partial:
        partial.sort(key=lambda p: len(p.stem))
        return partial[0]
    return None


def resolve_app(query: str) -> dict[str, Any]:
    cleaned = re.sub(r"^(?:open|launch|start|run|pull\s+up|bring\s+up)\s+", "", query.strip(), flags=re.I).strip()
    cleaned = re.sub(r"\s+(?:app|application|program|please|for\s+me)[.!?]*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"^(?:the|my)\s+", "", cleaned, flags=re.I).strip()
    if not cleaned:
        return {"ok": False, "error": "Empty launch target"}

    key = cleaned.casefold()
    if key in APP_MAP:
        return {"ok": True, "label": cleaned, "target": APP_MAP[key], "source": "app_map"}

    exe = shutil.which(cleaned)
    if exe:
        return {"ok": True, "label": cleaned, "target": exe, "source": "path"}

    shortcut = _search_shortcuts(cleaned)
    if shortcut is not None:
        return {"ok": True, "label": shortcut.stem, "target": str(shortcut), "source": "shortcut"}

    # Alias partial lookup in APP_MAP
    for alias, target in APP_MAP.items():
        if key in alias or alias in key:
            return {"ok": True, "label": alias, "target": target, "source": "app_map_fuzzy"}

    return {"ok": False, "error": f"Could not find application '{cleaned}'", "query": cleaned}


def launch_app(query: str, *, dry_run: bool = False) -> dict[str, Any]:
    resolved = resolve_app(query)
    if not resolved.get("ok"):
        return resolved
    if dry_run:
        return {**resolved, "dry_run": True}
    target = resolved["target"]
    try:
        os.startfile(target)
        return {**resolved, "launched": True}
    except OSError:
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=creationflags)
            return {**resolved, "launched": True}
        except Exception as exc:
            return {"ok": False, "error": f"Failed to launch {resolved['label']}: {exc}"}


# ---------------------------------------------------------------------------
# Core Audio (pycaw) + Win32 Media Keys
# ---------------------------------------------------------------------------

def _get_endpoint_volume(data_flow: int = 0):
    """data_flow: 0 = eRender (speakers/headphones), 1 = eCapture (mic)."""
    import comtypes
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    comtypes.CoInitialize()
    if data_flow == 0:
        device = AudioUtilities.GetSpeakers()
    else:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        device = enumerator.GetDefaultAudioEndpoint(1, 1)  # eCapture, eCommunications
    interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume)), comtypes


def audio_control(action: str, value: Optional[float] = None) -> dict[str, Any]:
    act = action.strip().lower()
    if act in {"media_play_pause", "media_next", "media_prev", "media_stop"}:
        vk_map = {
            "media_play_pause": 0xB3,
            "media_next": 0xB0,
            "media_prev": 0xB1,
            "media_stop": 0xB2,
        }
        _send_vk(vk_map[act])
        return {"ok": True, "action": act}

    data_flow = 1 if act.startswith("mic_") else 0
    comtypes_mod = None
    try:
        vol, comtypes_mod = _get_endpoint_volume(data_flow)
        if act in {"mute", "mic_mute"}:
            vol.SetMute(1, None)
            return {"ok": True, "action": act, "muted": True}
        if act in {"unmute", "mic_unmute"}:
            vol.SetMute(0, None)
            return {"ok": True, "action": act, "muted": False}
        if act in {"mute_toggle", "mic_toggle"}:
            cur = bool(vol.GetMute())
            vol.SetMute(0 if cur else 1, None)
            return {"ok": True, "action": act, "muted": not cur}
        if act == "volume_set":
            if value is None:
                return {"ok": False, "error": "volume_set requires a 0-100 value"}
            pct = max(0.0, min(100.0, float(value)))
            vol.SetMasterVolumeLevelScalar(pct / 100.0, None)
            return {"ok": True, "action": act, "volume": int(round(pct))}
        if act in {"volume_up", "volume_down"}:
            step = float(value if value is not None else 10.0)
            cur = float(vol.GetMasterVolumeLevelScalar()) * 100.0
            nxt = max(0.0, min(100.0, cur + step if act == "volume_up" else cur - step))
            vol.SetMasterVolumeLevelScalar(nxt / 100.0, None)
            return {"ok": True, "action": act, "volume": int(round(nxt))}
        if act == "status":
            return {
                "ok": True,
                "volume": int(round(float(vol.GetMasterVolumeLevelScalar()) * 100.0)),
                "muted": bool(vol.GetMute()),
            }
        return {"ok": False, "error": f"Unknown audio action: {action}"}
    except Exception as exc:
        return {"ok": False, "error": f"Core Audio failed: {exc}"}
    finally:
        if comtypes_mod is not None:
            try:
                comtypes_mod.CoUninitialize()
            except Exception:
                pass


def _send_vk(vk_code: int) -> None:
    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(vk_code, 0, 0, 0)
    user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="win_tools")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    agy_p = sub.add_parser("agy-status")
    agy_p.add_argument("--include-voice", action="store_true")
    agy_p.add_argument("--json", action="store_true")

    ss_p = sub.add_parser("screenshot")
    ss_p.add_argument("--json", action="store_true")

    ln_p = sub.add_parser("launch")
    ln_p.add_argument("query")
    ln_p.add_argument("--dry-run", action="store_true")
    ln_p.add_argument("--json", action="store_true")

    au_p = sub.add_parser("audio")
    au_p.add_argument("action")
    au_p.add_argument("value", nargs="?", type=float)
    au_p.add_argument("--json", action="store_true")

    fs_p = sub.add_parser("fullscreen")
    fs_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "agy-status":
        res = get_agy_status(include_voice=args.include_voice)
    elif args.cmd == "screenshot":
        res = take_screenshot()
    elif args.cmd == "launch":
        res = launch_app(args.query, dry_run=args.dry_run)
    elif args.cmd == "audio":
        res = audio_control(args.action, args.value)
    elif args.cmd == "fullscreen":
        res = {"ok": True, "fullscreen": is_fullscreen_active()}
    else:
        parser.print_help()
        return 0

    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
