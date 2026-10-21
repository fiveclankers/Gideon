"""Subagent 2: Omarchy and Desktop Environment Handler Module.

Implements all handlers for:
- omarchy_theme_list
- omarchy_theme_set
- omarchy_theme_next
- omarchy_theme_dark
- omarchy_theme_light
- omarchy_wallpaper_next
- omarchy_wallpaper_set
- omarchy_keybindings
- omarchy_screenshot
- omarchy_screenshot_full
- omarchy_update
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List

from gideon_shared import HandlerResult, run_cmd_async

logger = logging.getLogger("gideon.handlers.omarchy")

PREFERRED_DARK_THEME = "Tokyo Night"
PREFERRED_LIGHT_THEME = "Flexoki Light"


async def handle_omarchy_theme_list(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "theme", "list"])
    if code == 0:
        themes = [line.strip() for line in out.splitlines() if line.strip()]
        return HandlerResult(
            ok=True,
            message=f"Available themes: {', '.join(themes)}",
            data={"themes": themes}
        )
    return HandlerResult(ok=False, error=f"omarchy theme list failed (exit {code}): {err or out}")


async def handle_omarchy_theme_set(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    theme_name = params.get("theme")
    if not theme_name:
        return HandlerResult(ok=False, error="Missing theme name parameter")
    theme_name = str(theme_name).strip()
    code, out, err = await run_cmd_async(["omarchy", "theme", "set", theme_name], timeout=15.0)
    if code == 0:
        return HandlerResult(ok=True, message=f"Theme set to '{theme_name}'", data={"theme": theme_name})
    return HandlerResult(ok=False, error=f"Failed setting theme '{theme_name}' (exit {code}): {err or out}")


async def handle_omarchy_theme_next(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    # Query theme list and current theme
    code_list, out_list, err_list = await run_cmd_async(["omarchy", "theme", "list"])
    code_curr, out_curr, _ = await run_cmd_async(["omarchy", "theme", "current"])

    if code_list != 0 or not out_list:
        return HandlerResult(ok=False, error=f"Unable to query installed themes (exit {code_list}): {err_list or out_list}")

    themes = [line.strip() for line in out_list.splitlines() if line.strip()]
    if not themes:
        return HandlerResult(ok=False, error="No installed themes found")

    curr = out_curr.strip() if code_curr == 0 else ""
    if curr in themes:
        idx = themes.index(curr)
        next_theme = themes[(idx + 1) % len(themes)]
    else:
        logger.info("Current theme '%s' not found in theme list; falling back to first theme", curr)
        next_theme = themes[0]

    code_set, out_set, err_set = await run_cmd_async(["omarchy", "theme", "set", next_theme], timeout=15.0)
    if code_set == 0:
        return HandlerResult(ok=True, message=f"Cycled to next theme: {next_theme}", data={"theme": next_theme})
    return HandlerResult(ok=False, error=f"Failed cycling to theme {next_theme} (exit {code_set}): {err_set or out_set}")


async def handle_omarchy_theme_dark(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "theme", "set", PREFERRED_DARK_THEME], timeout=15.0)
    if code == 0:
        return HandlerResult(ok=True, message=f"Dark theme applied: {PREFERRED_DARK_THEME}", data={"theme": PREFERRED_DARK_THEME})
    return HandlerResult(ok=False, error=f"Failed applying dark theme (exit {code}): {err or out}")


async def handle_omarchy_theme_light(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "theme", "set", PREFERRED_LIGHT_THEME], timeout=15.0)
    if code == 0:
        return HandlerResult(ok=True, message=f"Light theme applied: {PREFERRED_LIGHT_THEME}", data={"theme": PREFERRED_LIGHT_THEME})
    return HandlerResult(ok=False, error=f"Failed applying light theme (exit {code}): {err or out}")


async def handle_omarchy_wallpaper_next(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "theme", "bg", "next"])
    if code == 0:
        return HandlerResult(ok=True, message="Switched to next wallpaper")
    return HandlerResult(ok=False, error=f"Failed switching wallpaper (exit {code}): {err or out}")


async def handle_omarchy_wallpaper_set(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    path = params.get("path")
    if not path:
        return HandlerResult(ok=False, error="Missing wallpaper image path")
    expanded_path = str(Path(path).expanduser())
    code, out, err = await run_cmd_async(["omarchy", "theme", "bg", "set", expanded_path])
    if code == 0:
        return HandlerResult(ok=True, message=f"Wallpaper set to {expanded_path}", data={"path": expanded_path})
    return HandlerResult(ok=False, error=f"Failed setting wallpaper (exit {code}): {err or out}")


async def handle_omarchy_keybindings(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "menu", "keybindings"])
    if code == 0:
        return HandlerResult(ok=True, message="Keybindings menu opened")
    return HandlerResult(ok=False, error=f"Failed opening keybindings (exit {code}): {err or out}")


async def handle_omarchy_screenshot(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "capture", "screenshot", "smart", "copy"])
    if code == 0:
        return HandlerResult(ok=True, message="Screenshot captured to clipboard")
    return HandlerResult(ok=False, error=f"Screenshot failed (exit {code}): {err or out}")


async def handle_omarchy_screenshot_full(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    code, out, err = await run_cmd_async(["omarchy", "capture", "screenshot", "fullscreen", "save"])
    if code == 0:
        saved_path = out.strip() if out.strip() else "saved to Pictures"
        return HandlerResult(ok=True, message=f"Fullscreen screenshot saved ({saved_path})", data={"path": saved_path})
    return HandlerResult(ok=False, error=f"Fullscreen screenshot failed (exit {code}): {err or out}")


async def handle_omarchy_update(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    if not params.get("confirmed", False):
        return HandlerResult(ok=False, needs_confirmation=True, message="Confirmation required to update system packages")
    # Update needs longer timeout (180s) to allow system packages to upgrade
    code, out, err = await run_cmd_async(["omarchy", "update", "-y"], timeout=180.0)
    if code == 0:
        return HandlerResult(ok=True, message="Omarchy update completed")
    return HandlerResult(ok=False, error=f"Omarchy update error (exit {code}): {err or out}")
