"""Shared interface for GIDEON v2 Atomic Intent Dispatcher.

Defines the universal contract:
- HandlerResult: outcome dataclass for all handlers.
- run_cmd / run_cmd_async: non-blocking, robust subprocess execution with timeouts.
- extract_params: parameter extraction signature and regex/heuristic extraction logic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gideon.shared")


@dataclass
class HandlerResult:
    """Standard outcome contract across all GIDEON intent handlers."""
    ok: bool
    message: str = ""
    error: Optional[str] = None
    needs_confirmation: bool = False
    data: Optional[Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "error": self.error,
            "needs_confirmation": self.needs_confirmation,
            "data": self.data or {},
        }


# Spoken number dictionary
_NUMBER_WORDS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def parse_number(text: str | None) -> Optional[float]:
    """Parse numeric digits or spoken number words into a float."""
    if not text:
        return None
    cleaned = re.sub(r"\s*(?:%|\bpercent\b)\s*$", "", text.strip(), flags=re.I).strip().lower()
    try:
        return float(cleaned)
    except ValueError:
        pass
    words = cleaned.replace("-", " ").split()
    total = 0.0
    matched = False
    for w in words:
        if w in _NUMBER_WORDS:
            total += _NUMBER_WORDS[w]
            matched = True
        elif w.isdigit():
            total += float(w)
            matched = True
        else:
            return None
    return total if matched else None


async def run_cmd_async(
    argv: List[str] | str,
    timeout: float = 5.0,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None
) -> Tuple[int, str, str]:
    """Execute a command asynchronously via asyncio.subprocess with timeout and error capture."""
    exec_env = os.environ.copy()
    if env:
        exec_env.update(env)

    try:
        if isinstance(argv, str):
            proc = await asyncio.create_subprocess_shell(
                argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=exec_env,
                cwd=cwd,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=exec_env,
                cwd=cwd,
            )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()
        return proc.returncode if proc.returncode is not None else 0, stdout_str, stderr_str
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return 124, "", f"Command timed out after {timeout} seconds"
    except Exception as exc:
        return 1, "", str(exc)


def run_cmd(
    argv: List[str] | str,
    timeout: float = 5.0,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None
) -> Tuple[int, str, str]:
    """Synchronous wrapper for subprocess execution with timeout and capture."""
    exec_env = os.environ.copy()
    if env:
        exec_env.update(env)

    try:
        if isinstance(argv, str):
            res = subprocess.run(
                argv,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=exec_env,
                cwd=cwd,
            )
        else:
            res = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=exec_env,
                cwd=cwd,
            )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout} seconds"
    except Exception as exc:
        return 1, "", str(exc)


# Parameter extraction regular expressions
_VOL_LEVEL_RE = re.compile(
    r"(?:set\s+volume\s+(?:to\s+)?|volume\s+(?:to\s+)?)([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?$",
    re.I,
)
_VOL_DELTA_RE = re.compile(
    r"(?:increase|raise|turn\s+up|turn\s+down|lower|decrease|volume\s+up|volume\s+down)(?:\s+(?:volume|it))?(?:\s+by)?\s+([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?$",
    re.I,
)
_BRIGHT_LEVEL_RE = re.compile(
    r"(?:set\s+brightness\s+(?:to\s+)?|brightness\s+(?:to\s+)?)([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?$",
    re.I,
)
_BRIGHT_DELTA_RE = re.compile(
    r"(?:increase|raise|turn\s+up|turn\s+down|lower|decrease|brightness\s+up|brightness\s+down)(?:\s+(?:brightness|it))?(?:\s+by)?\s+([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?$",
    re.I,
)
_WORKSPACE_RE = re.compile(
    r"(?:switch\s+(?:to\s+)?|go\s+(?:to\s+)?|jump\s+(?:to\s+)?|move\s+(?:to\s+)?)?(?:workspace\s+([0-9]+|[a-zA-Z]+)|([0-9]+|[a-zA-Z]+)\s+workspace)",
    re.I,
)
_NOTE_RE = re.compile(
    r"^(?:note\s+down|note\s+this\s+down|flash\s+note|take\s+a\s+note|quick\s+memo|add\s+note|jot\s+this\s+down|jot\s+down)\s*[:,\-]?\s*(.+?)\s*$",
    re.I | re.S,
)
_THEME_NAME_RE = re.compile(
    r"(?:set\s+(?:the\s+)?theme\s+(?:to\s+)?|theme\s+(?:set\s+)?|apply\s+(?:the\s+)?theme\s+)([\w\s-]+?)(?:\s+theme)?$",
    re.I,
)
_CHAPTER_RE = re.compile(
    r"(?:chapter|for|topic)\s+([a-zA-Z0-9_\-\s]+)",
    re.I,
)


def extract_params(intent_id: str, utterance: str) -> Dict[str, Any]:
    """Extract structured slot parameters from an utterance according to the intent schema.
    
    Returns an empty dict if the intent takes no parameters or extraction yields default values.
    """
    text = utterance.strip()
    params: Dict[str, Any] = {}

    if intent_id == "audio_volume_set":
        m = _VOL_LEVEL_RE.search(text)
        if m:
            val = parse_number(m.group(1))
            if val is not None and 0 <= val <= 100:
                params["level"] = val
        else:
            # Fallback scan for any standalone number in the utterance
            nums = re.findall(r"\b([0-9]{1,3})\s*(?:%|\bpercent\b)?", text)
            if nums:
                val = float(nums[0])
                if 0 <= val <= 100:
                    params["level"] = val

    elif intent_id in ("audio_volume_up", "audio_volume_down"):
        m = _VOL_DELTA_RE.search(text)
        if m:
            val = parse_number(m.group(1))
            if val is not None and 0 < val <= 100:
                params["step"] = val
        else:
            nums = re.findall(r"\b([0-9]{1,3})\s*(?:%|\bpercent\b)?", text)
            if nums:
                val = float(nums[0])
                if 0 < val <= 100:
                    params["step"] = val
            else:
                params["step"] = 10.0  # default 10%

    elif intent_id == "brightness_set":
        m = _BRIGHT_LEVEL_RE.search(text)
        if m:
            val = parse_number(m.group(1))
            if val is not None and 0 <= val <= 100:
                params["level"] = int(val)
        else:
            nums = re.findall(r"\b([0-9]{1,3})\s*(?:%|\bpercent\b)?", text)
            if nums:
                val = int(nums[0])
                if 0 <= val <= 100:
                    params["level"] = val

    elif intent_id in ("brightness_up", "brightness_down"):
        m = _BRIGHT_DELTA_RE.search(text)
        if m:
            val = parse_number(m.group(1))
            if val is not None and 0 < val <= 100:
                params["step"] = int(val)
        else:
            nums = re.findall(r"\b([0-9]{1,3})\s*(?:%|\bpercent\b)?", text)
            if nums:
                val = int(nums[0])
                if 0 < val <= 100:
                    params["step"] = val
            else:
                params["step"] = 10  # default 10%

    elif intent_id == "workspace_switch":
        m = re.search(r"workspace\s+([0-9]+|[a-zA-Z]+)", text, re.I)
        if m:
            val = parse_number(m.group(1))
            if val is not None and 1 <= val <= 10:
                params["workspace"] = int(val)
        if "workspace" not in params:
            m2 = re.search(r"([0-9]+|[a-zA-Z]+)\s+workspace", text, re.I)
            if m2:
                val = parse_number(m2.group(1))
                if val is not None and 1 <= val <= 10:
                    params["workspace"] = int(val)
        if "workspace" not in params:
            nums = re.findall(r"\b([1-9]|10)\b", text)
            if nums:
                params["workspace"] = int(nums[0])
            else:
                for word, val in _NUMBER_WORDS.items():
                    if 1 <= val <= 10 and re.search(r"\b" + word + r"\b", text, re.I):
                        params["workspace"] = val
                        break

    elif intent_id == "omarchy_theme_set":
        m = _THEME_NAME_RE.search(text)
        if m:
            theme_candidate = m.group(1).strip()
            params["theme"] = theme_candidate
        else:
            # Check known themes
            known_themes = [
                "tokyo night", "gruvbox", "catppuccin", "catppuccin latte", "flexoki light",
                "everforest", "nord", "kanagawa", "rose pine", "miasma", "matte black",
                "lumon", "lupine", "solitude", "white", "ash", "aamis", "aether", "ethereal"
            ]
            lower = text.lower()
            for kt in known_themes:
                if kt in lower:
                    params["theme"] = kt.title()
                    break

    elif intent_id == "flash_note_add":
        m = _NOTE_RE.search(text)
        if m:
            params["note"] = m.group(1).strip()
        else:
            # Strip common prefix
            cleaned = re.sub(
                r"^(?:take a note|take note|flash note|note down|jot down|quick memo)\s*(?:that|about|:)?\s*",
                "",
                text,
                flags=re.I
            ).strip()
            params["note"] = cleaned if cleaned else text

    elif intent_id in ("study_traps_view", "study_sprint_start"):
        # Match chapter codes like phy_01, che_02, chem_02, mat_03, math_03 or chapter names
        code_match = re.search(r"\b(phy|che|chem|mat|math)[_-]?(\d{1,2})\b", text, re.I)
        if code_match:
            raw_subj = code_match.group(1).lower()
            subj_map = {"che": "chem", "chem": "chem", "mat": "math", "math": "math", "phy": "phy"}
            subj = subj_map.get(raw_subj, raw_subj)
            num = int(code_match.group(2))
            params["chapter"] = f"{subj}_{num:02d}"
        else:
            m = _CHAPTER_RE.search(text)
            if m:
                params["chapter"] = m.group(1).strip()

    elif intent_id == "engine_switch":
        lower = text.lower()
        if "opencode" in lower:
            params["engine"] = "opencode"
        elif "antigravity" in lower or "agy" in lower:
            params["engine"] = "antigravity"
        elif "lyra" in lower:
            params["engine"] = "lyra"

    return params
