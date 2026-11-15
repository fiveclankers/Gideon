"""Subagent 1: OS, Window, Audio, Media, App Launch Handler Module.

Implements all handlers for:
- audio_* (volume set, up, down, mute, unmute, mic mute/unmute, outputs)
- media_* (play, pause, play-pause, next, previous, stop)
- brightness_* (up, down, set)
- window_* (close, fullscreen, focus left/right/up/down, float)
- workspace_* (switch, previous, next, prev)
- launch_* (browser, brave, spotify, terminal, obsidian, code, file manager)
- system_* (status, lock, logout, reboot, shutdown)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

from gideon_shared import HandlerResult, extract_params, run_cmd, run_cmd_async

logger = logging.getLogger("gideon.handlers.system")

MUSIC_BIN = os.path.expanduser("~/.local/bin/music")


async def handle_audio_volume_set(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    level = params.get("level")
    if level is None:
        return HandlerResult(ok=False, error="Missing volume level parameter")
    try:
        level_val = float(level)
    except (ValueError, TypeError):
        return HandlerResult(ok=False, error=f"Invalid volume level: {level}")
    level_val = max(0.0, min(100.0, level_val))
    code, out, err = await run_cmd_async(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{int(level_val)}%"])
    if code == 0:
        return HandlerResult(ok=True, message=f"Volume set to {int(level_val)}%", data={"level": level_val})
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_volume_up(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    step = params.get("step", 10.0)
    try:
        step_val = float(step)
    except (ValueError, TypeError):
        step_val = 10.0
    step_val = max(1.0, min(100.0, step_val))
    code, out, err = await run_cmd_async(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{int(step_val)}%+"])
    if code == 0:
        return HandlerResult(ok=True, message=f"Volume increased by {int(step_val)}%", data={"step": step_val})
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_volume_down(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    step = params.get("step", 10.0)
    try:
        step_val = float(step)
    except (ValueError, TypeError):
        step_val = 10.0
    step_val = max(1.0, min(100.0, step_val))
    code, out, err = await run_cmd_async(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{int(step_val)}%-"])
    if code == 0:
        return HandlerResult(ok=True, message=f"Volume decreased by {int(step_val)}%", data={"step": step_val})
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_mute(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
    if code == 0:
        return HandlerResult(ok=True, message="Audio output muted")
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_unmute(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
    if code == 0:
        return HandlerResult(ok=True, message="Audio output unmuted")
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_mute_toggle(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
    if code == 0:
        return HandlerResult(ok=True, message="Audio output mute toggled")
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_mic_mute(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"])
    if code == 0:
        return HandlerResult(ok=True, message="Microphone muted")
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_mic_unmute(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "0"])
    if code == 0:
        return HandlerResult(ok=True, message="Microphone unmuted")
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_mic_mute_toggle(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "toggle"])
    if code == 0:
        return HandlerResult(ok=True, message="Microphone mute toggled")
    return HandlerResult(ok=False, error=f"wpctl failed: {err}")


async def handle_audio_output_headphones(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    sink = os.environ.get("GIDEON_HEADPHONES_SINK")
    if sink:
        code, out, err = await run_cmd_async(["wpctl", "set-default", sink])
    else:
        code, out, err = await run_cmd_async(["omarchy", "audio", "output", "switch"])
    if code == 0:
        return HandlerResult(ok=True, message="Audio output switched to headphones")
    return HandlerResult(ok=False, error=f"Failed switching audio output: {err}")


async def handle_audio_output_speakers(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    sink = os.environ.get("GIDEON_SPEAKERS_SINK")
    if sink:
        code, out, err = await run_cmd_async(["wpctl", "set-default", sink])
    else:
        code, out, err = await run_cmd_async(["omarchy", "audio", "output", "switch"])
    if code == 0:
        return HandlerResult(ok=True, message="Audio output switched to speakers")
    return HandlerResult(ok=False, error=f"Failed switching audio output: {err}")


# Media Handlers
async def _music_status() -> Dict[str, Any] | None:
    """Return the local music player's status, or None when it is unavailable."""
    if not os.path.exists(MUSIC_BIN):
        return None
    code, out, _ = await run_cmd_async([MUSIC_BIN, "status", "--json"])
    if code != 0:
        return None
    try:
        status = json.loads(out)
    except (TypeError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _local_music_active(status: Dict[str, Any] | None) -> bool:
    """True only when the local player owns a queue or is playing/paused."""
    if not status or not status.get("mpv"):
        return False
    return bool(
        status.get("playing")
        or status.get("paused")
        or (status.get("queue_count") or 0) > 0
    )


_MEDIA_ACTIONS: Dict[str, tuple[list[str], list[str], str]] = {
    "play": (["resume"], ["play"], "Media playback resumed"),
    "pause": (["pause"], ["pause"], "Media playback paused"),
    "toggle": (["toggle"], ["play-pause"], "Media playback toggled"),
    "next": (["next"], ["next"], "Skipped to next track"),
    "previous": (["previous"], ["previous"], "Returned to previous track"),
    "stop": (["stop"], ["stop"], "Media playback stopped"),
}


async def run_media_action(action: str) -> HandlerResult:
    """Run a named media action through the local-first/playerctl-fallback path."""
    try:
        music_args, playerctl_args, ok_message = _MEDIA_ACTIONS[action]
    except KeyError:
        return HandlerResult(ok=False, error=f"Unknown media action: {action}")
    return await _run_media_command(music_args, playerctl_args, ok_message)


async def _run_media_command(
    music_args: list[str],
    playerctl_args: list[str],
    ok_message: str,
) -> HandlerResult:
    """Control local music when it is active; otherwise fall back to playerctl.

    The local CLI exits 0 for `stop` even when its queue is empty, so calling it
    unconditionally would swallow stop/pause requests meant for browser audio.
    """
    status = await _music_status()
    if _local_music_active(status):
        code, _, err = await run_cmd_async([MUSIC_BIN, *music_args])
        if code == 0:
            return HandlerResult(ok=True, message=ok_message)
        logger.debug(
            "music %s failed (%s); falling back to playerctl",
            " ".join(music_args),
            (err or "").strip(),
        )
    code, _, err = await run_cmd_async(["playerctl", *playerctl_args])
    if code == 0:
        return HandlerResult(ok=True, message=ok_message)
    return HandlerResult(
        ok=False, error=f"media control failed: {err or 'no player active'}"
    )


async def _play_shuffled_library(limit: int = 50) -> HandlerResult:
    """Start the local library on shuffle, replacing whatever is queued."""
    code, out, err = await run_cmd_async(
        [MUSIC_BIN, "play", "--shuffle", "--limit", str(limit)],
        timeout=8.0,
    )
    if code == 0:
        return HandlerResult(
            ok=True,
            message="Playing shuffled library",
            data={"shuffled": True, "limit": limit},
        )
    return HandlerResult(ok=False, error=f"music library play failed: {err or out}")


async def handle_media_play(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    # The BGE tier labels "play some music" / "play anything" as media_play,
    # which by itself resumes whatever player is paused. When the utterance
    # names nothing specific, start the local library on shuffle instead.
    if raw_text:
        try:
            from reflex_router import is_generic_music_request

            if is_generic_music_request(raw_text):
                return await _play_shuffled_library()
        except Exception:  # never let the guard break a plain resume
            logger.debug("generic music guard failed", exc_info=True)
    return await run_media_action("play")


async def handle_media_pause(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return await run_media_action("pause")


async def handle_media_play_pause(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return await run_media_action("toggle")


async def handle_media_next(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return await run_media_action("next")


async def handle_media_previous(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return await run_media_action("previous")


async def handle_media_stop(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return await run_media_action("stop")


# Brightness Handlers
async def handle_brightness_up(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    step = params.get("step", 10)
    try:
        step_val = int(step)
    except (ValueError, TypeError):
        step_val = 10
    step_val = max(1, min(100, step_val))
    code, out, err = await run_cmd_async(["omarchy", "brightness", "display", f"+{step_val}%"])
    if code == 0:
        return HandlerResult(ok=True, message=f"Brightness increased by {step_val}%")
    return HandlerResult(ok=False, error=f"Brightness adjustment failed: {err}")


async def handle_brightness_down(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    step = params.get("step", 10)
    try:
        step_val = int(step)
    except (ValueError, TypeError):
        step_val = 10
    step_val = max(1, min(100, step_val))
    code, out, err = await run_cmd_async(["omarchy", "brightness", "display", f"{step_val}%-"])
    if code == 0:
        return HandlerResult(ok=True, message=f"Brightness decreased by {step_val}%")
    return HandlerResult(ok=False, error=f"Brightness adjustment failed: {err}")


async def handle_brightness_set(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    level = params.get("level")
    if level is None:
        return HandlerResult(ok=False, error="Missing brightness level parameter")
    try:
        level_val = int(level)
    except (ValueError, TypeError):
        return HandlerResult(ok=False, error=f"Invalid brightness level: {level}")
    level_val = max(0, min(100, level_val))
    code, out, err = await run_cmd_async(["omarchy", "brightness", "display", f"{level_val}%"])
    if code == 0:
        return HandlerResult(ok=True, message=f"Brightness set to {level_val}%")
    return HandlerResult(ok=False, error=f"Brightness set failed: {err}")


# Helper for Hyprland dispatch (supports fast UDS socket + Lua DSL and classic fallback)
async def _dispatch_hypr(lua_cmd: str, fallback_argv: list[str]) -> tuple[int, str, str]:
    from reflex_router import hypr_socket_dispatch

    sock_code, sock_out = await hypr_socket_dispatch(lua_cmd)
    if sock_code == 0 and "error:" not in sock_out:
        return 0, sock_out, ""
    if len(fallback_argv) >= 3 and fallback_argv[:2] == ["hyprctl", "dispatch"]:
        plain_cmd = " ".join(fallback_argv[2:])
        sock_code, sock_out = await hypr_socket_dispatch(plain_cmd)
        if sock_code == 0 and "error:" not in sock_out:
            return 0, sock_out, ""
    code, out, err = await run_cmd_async(["hyprctl", "dispatch", lua_cmd])
    if code == 0 and "error:" not in out:
        return code, out, err
    return await run_cmd_async(fallback_argv)


# Window Handlers
async def handle_window_close(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr("hl.dsp.window.close()", ["hyprctl", "dispatch", "killactive"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Closed active window")
    return HandlerResult(ok=False, error=f"hyprctl killactive failed: {err or out}")


async def handle_window_fullscreen_toggle(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr("hl.dsp.window.fullscreen()", ["hyprctl", "dispatch", "fullscreen"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Fullscreen toggled")
    return HandlerResult(ok=False, error=f"hyprctl fullscreen failed: {err or out}")


async def handle_window_focus_left(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ direction = "left" })', ["hyprctl", "dispatch", "movefocus", "l"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Focused left window")
    return HandlerResult(ok=False, error=f"hyprctl movefocus failed: {err or out}")


async def handle_window_focus_right(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ direction = "right" })', ["hyprctl", "dispatch", "movefocus", "r"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Focused right window")
    return HandlerResult(ok=False, error=f"hyprctl movefocus failed: {err or out}")


async def handle_window_focus_up(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ direction = "up" })', ["hyprctl", "dispatch", "movefocus", "u"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Focused window above")
    return HandlerResult(ok=False, error=f"hyprctl movefocus failed: {err or out}")


async def handle_window_focus_down(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ direction = "down" })', ["hyprctl", "dispatch", "movefocus", "d"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Focused window below")
    return HandlerResult(ok=False, error=f"hyprctl movefocus failed: {err or out}")


async def handle_window_floating_toggle(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr("hl.dsp.window.toggle_floating()", ["hyprctl", "dispatch", "togglefloating"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Floating window state toggled")
    return HandlerResult(ok=False, error=f"hyprctl togglefloating failed: {err or out}")


# Workspace Handlers
async def handle_workspace_switch(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    workspace = params.get("workspace")
    if workspace is None:
        return HandlerResult(ok=False, error="Missing workspace target number")
    try:
        ws_num = int(workspace)
    except (ValueError, TypeError):
        return HandlerResult(ok=False, error=f"Invalid workspace target: {workspace}")
    ws_num = max(1, min(10, ws_num))
    code, out, err = await _dispatch_hypr(f"hl.dsp.focus({{ workspace = {ws_num} }})", ["hyprctl", "dispatch", "workspace", str(ws_num)])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message=f"Switched to workspace {ws_num}", data={"workspace": ws_num})
    return HandlerResult(ok=False, error=f"hyprctl workspace failed: {err or out}")


async def handle_workspace_previous(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ workspace = "previous" })', ["hyprctl", "dispatch", "workspace", "previous"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Switched to previous workspace")
    return HandlerResult(ok=False, error=f"hyprctl workspace previous failed: {err or out}")


async def handle_workspace_next(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ workspace = "+1" })', ["hyprctl", "dispatch", "workspace", "e+1"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Switched to next workspace")
    return HandlerResult(ok=False, error=f"hyprctl workspace e+1 failed: {err or out}")


async def handle_workspace_prev(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await _dispatch_hypr('hl.dsp.focus({ workspace = "-1" })', ["hyprctl", "dispatch", "workspace", "e-1"])
    if code == 0 and "error:" not in out:
        return HandlerResult(ok=True, message="Switched to preceding workspace")
    return HandlerResult(ok=False, error=f"hyprctl workspace e-1 failed: {err or out}")


# App Launch Helper (Fast, verification-first spawn)
def _find_active_window(match_patterns: list[str]) -> bool:
    """Check if an app window is already running and mapped in Hyprland."""
    try:
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not sig:
            # Detect latest active signature if missing from subshell
            u = os.getuid()
            hypr_dir = Path(f"/run/user/{u}/hypr")
            if hypr_dir.exists():
                sigs = sorted(hypr_dir.glob("*"), key=os.path.getmtime, reverse=True)
                if sigs:
                    sig = sigs[0].name
                    os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = sig
        if not sig:
            return False

        out = subprocess.check_output(["hyprctl", "clients", "-j"], timeout=1.0)
        clients = json.loads(out.decode("utf-8", errors="ignore"))
        patterns = [p.lower() for p in match_patterns]
        for c in clients:
            c_class = (c.get("class") or "").lower()
            c_initial = (c.get("initialClass") or "").lower()
            c_title = (c.get("title") or "").lower()
            if any(p in c_class or p in c_initial or p in c_title for p in patterns):
                return True
        return False
    except Exception:
        return False


def _spawn_app(bin_name: str, app_label: str, match_patterns: list[str] = None, *, url: str | None = None) -> HandlerResult:
    patterns = match_patterns or [bin_name, app_label]
    if url is None and _find_active_window(patterns):
        # Already running and mapped!
        return HandlerResult(ok=True, message=f"{app_label} is already open")

    try:
        env = os.environ.copy()
        if "WAYLAND_DISPLAY" not in env:
            env["WAYLAND_DISPLAY"] = "wayland-1"
        if "XDG_RUNTIME_DIR" not in env:
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"

        # Use uwsm app if present for proper Hyprland desktop integration, else direct spawn
        spawn_cmd = ["uwsm", "app", "--", bin_name] if shutil.which("uwsm") else [bin_name]
        if url is not None:
            # Let the browser's own single-instance IPC reuse its window, or
            # create one. Submit once: no blank launch, guessed tab ID or retry.
            spawn_cmd.extend(["--new-tab", url])

        subprocess.Popen(
            spawn_cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return HandlerResult(ok=True, message=f"Opening {app_label}", data={"url": url} if url is not None else {})
    except FileNotFoundError:
        return HandlerResult(ok=False, error=f"Executable '{bin_name}' not found on system")
    except Exception as exc:
        return HandlerResult(ok=False, error=f"Failed launching {app_label}: {exc}")


async def handle_launch_browser(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    url = params.get("url", extract_params("launch_browser", raw_text).get("url"))
    if url is not None:
        try:
            if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError
        except ValueError:
            return HandlerResult(ok=False, error="Browser URL must be a single absolute HTTP(S) URL")
    for bin_name, label, patterns in (
        ("google-chrome-stable", "Google Chrome", ["chrome", "chromium", "google-chrome"]),
        ("brave", "Brave Browser", ["brave", "brave-browser"]),
        ("firefox", "Firefox", ["firefox"]),
    ):
        if shutil.which(bin_name):
            return _spawn_app(bin_name, label, patterns, url=url)
    return HandlerResult(ok=False, error="No known browser executable found")


async def handle_launch_brave(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return _spawn_app("brave", "Brave Browser", ["brave", "brave-browser"])


async def handle_launch_spotify(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return _spawn_app("spotify", "Spotify", ["spotify"])


async def handle_launch_terminal(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return _spawn_app("kitty", "Kitty Terminal", ["kitty"])


async def handle_launch_obsidian(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return _spawn_app("obsidian", "Obsidian", ["obsidian"])


async def handle_launch_code(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return _spawn_app("code", "VS Code", ["code", "visual-studio-code"])


async def handle_launch_file_manager(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    return _spawn_app("thunar", "Thunar File Manager", ["thunar"])


async def handle_launch_learnharness(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    app_image = "/home/cat/Applications/LearnHarness.AppImage"
    return _spawn_app(app_image, "Learning Harness", ["learnharness", "LearnHarness"])


# System / Power Handlers
async def handle_system_status(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "system", "stats"])
    if code == 0:
        return HandlerResult(ok=True, message="System status retrieved", data={"stats": out})
    return HandlerResult(ok=False, error=f"omarchy system stats failed: {err}")


async def handle_system_lock(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "system", "lock"])
    if code == 0:
        return HandlerResult(ok=True, message="Session locked")
    return HandlerResult(ok=False, error=f"omarchy system lock failed: {err}")


async def handle_system_logout(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    if not params.get("confirmed", False):
        return HandlerResult(ok=False, needs_confirmation=True, message="Confirmation required to log out of session")
    code, out, err = await run_cmd_async(["omarchy", "system", "logout"])
    if code == 0:
        return HandlerResult(ok=True, message="Logged out of session")
    return HandlerResult(ok=False, error=f"omarchy system logout failed: {err}")


async def handle_system_reboot(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    if not params.get("confirmed", False):
        return HandlerResult(ok=False, needs_confirmation=True, message="Confirmation required to reboot system")
    code, out, err = await run_cmd_async(["omarchy", "system", "reboot"])
    if code == 0:
        return HandlerResult(ok=True, message="Rebooting system...")
    return HandlerResult(ok=False, error=f"omarchy system reboot failed: {err}")


async def handle_system_shutdown(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    if not params.get("confirmed", False):
        return HandlerResult(ok=False, needs_confirmation=True, message="Confirmation required to shut down system")
    code, out, err = await run_cmd_async(["omarchy", "system", "shutdown"])
    if code == 0:
        return HandlerResult(ok=True, message="Shutting down system...")
    return HandlerResult(ok=False, error=f"omarchy system shutdown failed: {err}")
