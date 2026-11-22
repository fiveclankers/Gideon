#!/usr/bin/env python3
"""Benchmark Gemini Decision 1 against the local Tier-0 regex router and BGE-small.

This script answers one question with numbers: is a remote Decision 1 call
worth it next to GIDEON's existing zero-model Tier-0 router plus the local
BGE-small ONNX intent head, and does Decision 1 scale flat when several
questions ride along in a single request?

Suites
------
A. Small / fast reflex commands: Tier-0 regex hit, BGE-small prediction and
   Decision 1 prediction side by side.
B. Ambiguous / colloquial / hard routing prompts where BGE-small loses
   nuance, plus a disagreement table.
C. Parallel multi-question scaling: the same utterance asked with 1 question
   versus 5 simultaneous questions in one request.
D. Browser screenshot + DOM decision loop: a synthetic 640x360 PNG (encoded
   in pure Python, no Pillow) paired with a compact DOM snippet, driven
   through a 3-step modal -> search -> results loop.

Usage
-----
    # Local-only verification, no API key needed:
    python scripts/benchmark_decision_vs_bge.py --dry-run

    # Live run against the Decisions API:
    GEMINI_API_KEY=... python scripts/benchmark_decision_vs_bge.py

    # Dump the synthetic screenshot and the full JSON results:
    python scripts/benchmark_decision_vs_bge.py --dry-run \
        --png-out /tmp/gideon_screenshot.png --json-out /tmp/decision_bench.json

The API key is only ever read from the environment (``GEMINI_API_KEY`` by
default). Nothing is written into the repo.

Notes on the wire contract
--------------------------
* ``POST https://generativelanguage.googleapis.com/v1beta/decisions`` with the
  ``x-goog-api-key`` header.
* ``choice`` answers expose ``choice`` / ``confidence`` / ``probabilities``,
  ``noul`` answers expose ``noul`` / ``confidence``, ``score`` answers expose
  ``score`` / ``confidence`` / ``probabilities``.
* The ``score`` question carries ``scale: {min, max}``. If the endpoint
  rejects that field (HTTP 400 mentioning ``scale``), the client retries once
  with the range folded into the instructions so a live run degrades instead
  of dying.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import math
import os
import struct
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DECISIONS_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
DECISION_MODEL = "gemini-decision-1"
API_KEY_ENV = "GEMINI_API_KEY"

# Google AI Studio free-tier ceilings for gemini-decision-1.
FREE_TIER = {
    "rpm": 200,
    "rpd": 10_000,
    "tpm": 50_000,
}

SCREENSHOT_WIDTH = 640
SCREENSHOT_HEIGHT = 360


# ---------------------------------------------------------------------------
# Latency bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class Latency:
    """Cold first-call latency plus warm steady-state samples."""

    cold_ms: float | None = None
    samples: list[float] = field(default_factory=list)

    def record(self, ms: float, cold: bool = False) -> None:
        if cold and self.cold_ms is None:
            self.cold_ms = ms
            return
        self.samples.append(ms)

    def stats(self) -> dict[str, float | int] | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        return {
            "n": len(ordered),
            "min": ordered[0],
            "p50": _percentile(ordered, 0.50),
            "p95": _percentile(ordered, 0.95),
            "mean": sum(ordered) / len(ordered),
            "max": ordered[-1],
            "cold": self.cold_ms,
        }


def _percentile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        raise ValueError("empty sample set")
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _mean(values: Iterable[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# Pure-Python PNG rendering (no Pillow)
# ---------------------------------------------------------------------------


class _Canvas:
    """Tiny RGB raster with the handful of primitives this benchmark needs."""

    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.rows: list[bytearray] = [bytearray(bytes(background) * width) for _ in range(height)]

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        x0 = max(0, min(self.width, int(x0)))
        x1 = max(0, min(self.width, int(x1)))
        y0 = max(0, min(self.height, int(y0)))
        y1 = max(0, min(self.height, int(y1)))
        if x0 >= x1 or y0 >= y1:
            return
        span = bytes(color) * (x1 - x0)
        for y in range(y0, y1):
            row = self.rows[y]
            row[x0 * 3 : x1 * 3] = span

    def stroke(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        thickness: int = 2,
    ) -> None:
        t = max(1, thickness)
        self.rect(x0, y0, x1, y0 + t, color)
        self.rect(x0, y1 - t, x1, y1, color)
        self.rect(x0, y0, x0 + t, y1, color)
        self.rect(x1 - t, y0, x1, y1, color)

    def line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        thickness: int = 2,
    ) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.rect(x0, y0, x0 + thickness, y0 + thickness, color)
            if x0 == x1 and y0 == y1:
                break
            err2 = 2 * err
            if err2 >= dy:
                err += dy
                x0 += sx
            if err2 <= dx:
                err += dx
                y0 += sy

    def dim(self, alpha: float, tint: tuple[int, int, int] = (15, 23, 42)) -> None:
        keep = 1.0 - alpha
        tr, tg, tb = tint
        for row in self.rows:
            for i in range(0, len(row), 3):
                row[i] = int(row[i] * keep + tr * alpha)
                row[i + 1] = int(row[i + 1] * keep + tg * alpha)
                row[i + 2] = int(row[i + 2] * keep + tb * alpha)

    def to_png(self) -> bytes:
        raw = bytearray()
        for row in self.rows:
            raw.append(0)  # filter type 0 (None)
            raw.extend(row)
        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _png_chunk(b"IEND", b"")
        )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def render_screenshot_png(show_modal: bool = True) -> bytes:
    """Render a crisp fake webpage, optionally with a centered blocking modal overlay."""
    w, h = SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT
    page_bg = (245, 247, 250)
    ink = (30, 41, 59)
    muted = (148, 163, 184)
    hairline = (226, 232, 240)
    card = (255, 255, 255)
    blue = (37, 99, 235)
    slate = (31, 41, 55)

    c = _Canvas(w, h, page_bg)

    # Top navigation bar
    c.rect(0, 0, w, 48, slate)
    c.rect(20, 14, 44, 34, blue)
    c.rect(56, 19, 156, 29, (148, 163, 184))
    for x0 in (300, 372, 444):
        c.rect(x0, 18, x0 + 58, 30, (71, 85, 105))
    c.rect(556, 14, 620, 34, blue)

    # Hero panel
    c.rect(40, 72, 600, 150, card)
    c.rect(40, 72, 46, 150, blue)
    c.rect(64, 90, 340, 104, ink)
    c.rect(64, 114, 470, 122, muted)
    c.rect(64, 128, 410, 136, hairline)

    # Two content cards
    for x0, x1 in ((40, 310), (330, 600)):
        c.rect(x0, 168, x1, 250, card)
        c.rect(x0 + 16, 184, x0 + 124, 194, ink)
        c.rect(x0 + 16, 202, x1 - 16, 210, hairline)
        c.rect(x0 + 16, 214, x1 - 44, 220, hairline)

    # Footer band
    c.rect(40, 266, 600, 330, card)
    c.rect(64, 280, 240, 292, ink)
    c.rect(64, 300, 520, 306, hairline)
    c.rect(64, 312, 460, 318, hairline)

    if show_modal:
        # Dim the page so the modal reads as a blocking overlay
        c.dim(0.45)

        # Modal dialog, centered
        c.rect(170, 96, 470, 282, card)
        c.stroke(170, 96, 470, 282, blue, 3)
        c.rect(173, 99, 467, 135, blue)
        c.rect(190, 108, 330, 120, card)
        # Close "X" glyph
        c.line(434, 108, 456, 128, card, 3)
        c.line(456, 108, 434, 128, card, 3)
        # Body copy bars
        c.rect(190, 150, 430, 158, (71, 85, 105))
        c.rect(190, 166, 400, 172, muted)
        c.rect(190, 180, 440, 186, muted)
        # Primary accept button + secondary button
        c.rect(190, 214, 320, 250, (52, 168, 83))
        c.rect(206, 228, 304, 236, card)
        c.rect(336, 214, 450, 250, card)
        c.stroke(336, 214, 450, 250, muted, 2)
        c.rect(352, 228, 434, 236, (100, 116, 139))

    return c.to_png()


def inspect_png(data: bytes) -> dict[str, Any]:
    """Validate the PNG structure and return its header facts."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("missing PNG signature")
    offset = 8
    width = height = None
    idat = bytearray()
    saw_iend = False
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", payload[:10])
            if depth != 8 or color_type != 2:
                raise ValueError(f"unexpected PNG depth/color type: {depth}/{color_type}")
        elif tag == b"IDAT":
            idat.extend(payload)
        elif tag == b"IEND":
            saw_iend = True
            break
    if width is None or height is None:
        raise ValueError("missing IHDR")
    if not saw_iend:
        raise ValueError("missing IEND")
    raw = zlib.decompress(bytes(idat))
    expected = height * (1 + width * 3)
    if len(raw) != expected:
        raise ValueError(f"scanline payload {len(raw)} != expected {expected}")
    return {
        "width": width,
        "height": height,
        "bytes": len(data),
        "base64_bytes": len(base64.b64encode(data)),
        "scanlines_ok": True,
    }


# ---------------------------------------------------------------------------
# Local Tier-0 router (exact repo matcher) and BGE-small dispatcher
# ---------------------------------------------------------------------------


def load_tier0() -> Callable[[str], dict[str, Any] | None]:
    from reflex_router import parse_command  # local, zero-model regex router

    return parse_command


def tier0_intent(parsed: dict[str, Any] | None) -> str | None:
    """Map a Tier-0 parse_command result onto an intents_schema.json intent id."""
    if not parsed:
        return None
    kind = parsed.get("intent")
    if kind == "system_action":
        argv = [str(a) for a in parsed.get("argv") or []]
        summary = str(parsed.get("summary") or "").lower()
        if any("toggle_special" in a for a in argv):
            return "agent_workspace_toggle"
        if len(argv) >= 2 and argv[0] == "wpctl":
            if argv[1] == "set-volume":
                tail = argv[-1]
                if tail.endswith("%+"):
                    return "audio_volume_up"
                if tail.endswith("%-"):
                    return "audio_volume_down"
                return "audio_volume_set"
            if argv[1] == "set-mute":
                prefix = "audio_mic_" if any("@DEFAULT_AUDIO_SOURCE@" in a for a in argv) else "audio_"
                state = argv[-1]
                if state == "1":
                    return prefix + "mute"
                if state == "0":
                    return prefix + "unmute"
                return prefix + "mute_toggle"
            if argv[1] == "set-default":
                return "audio_output_headphones" if "headphone" in summary else "audio_output_speakers"
        if len(argv) >= 4 and argv[0] == "hyprctl":
            action = argv[2]
            if action == "workspace":
                return "workspace_previous" if argv[3] == "previous" else "workspace_switch"
            if action == "killactive":
                return "window_close"
            if action == "fullscreen":
                return "window_fullscreen_toggle"
            if action == "movefocus":
                return {
                    "l": "window_focus_left",
                    "r": "window_focus_right",
                    "u": "window_focus_up",
                    "d": "window_focus_down",
                }.get(argv[3])
            if action == "togglefloating":
                return "window_floating_toggle"
        return None
    if kind == "media_control":
        return {
            "pause": "media_pause",
            "play": "media_play",
            "next": "media_next",
            "previous": "media_previous",
            "stop": "media_stop",
        }.get(parsed.get("action"))
    if kind == "app_launch":
        return parsed.get("app")
    if kind == "music_play":
        return "media_play"
    if kind == "flash_note":
        return "flash_note_add"
    return kind  # dismiss / graph_query / focus_* / debug_bundle stay as-is


def load_bge() -> Any:
    from gideon_handler import GideonDispatcher  # repo loader: tokenizer + INT8 ONNX + temperature

    return GideonDispatcher()


def load_schema() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "intents_schema.json").read_text())


# ---------------------------------------------------------------------------
# Decision 1 client
# ---------------------------------------------------------------------------


@dataclass
class DecisionResult:
    answers: dict[str, Any] | None
    usage: dict[str, Any]
    latency_ms: float
    payload_bytes: int
    estimated_tokens: int
    dry_run: bool = False
    error: str | None = None
    dropped_scale: bool = False


class DecisionClient:
    """Minimal httpx client for the Decisions endpoint. Never logs the key."""

    def __init__(self, api_key: str | None, dry_run: bool, timeout_s: float = 60.0):
        self.api_key = api_key
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self._client = None
        self.calls = 0
        if not dry_run:
            import httpx

            self._client = httpx.Client(
                timeout=timeout_s,
                headers={
                    "x-goog-api-key": api_key or "",
                    "Content-Type": "application/json",
                },
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def ask(self, payload: dict[str, Any]) -> DecisionResult:
        body = json.dumps(payload, separators=(",", ":"))
        payload_bytes = len(body.encode("utf-8"))
        estimated = estimate_payload_tokens(payload)
        if self.dry_run:
            return DecisionResult(
                answers=None,
                usage={},
                latency_ms=0.0,
                payload_bytes=payload_bytes,
                estimated_tokens=estimated,
                dry_run=True,
            )

        assert self._client is not None
        started = time.perf_counter()
        try:
            response = self._client.post(DECISIONS_URL, content=body)
            if response.status_code == 400 and "scale" in response.text.lower():
                # Unknown/unsupported score scale field: fold the range into
                # the instructions and retry once so live runs survive.
                fallback = _strip_score_scale(payload)
                body = json.dumps(fallback, separators=(",", ":"))
                payload_bytes = len(body.encode("utf-8"))
                response = self._client.post(DECISIONS_URL, content=body)
                dropped_scale = True
            else:
                dropped_scale = False
            latency_ms = (time.perf_counter() - started) * 1000.0
            self.calls += 1
            if response.status_code != 200:
                return DecisionResult(
                    answers=None,
                    usage={},
                    latency_ms=latency_ms,
                    payload_bytes=payload_bytes,
                    estimated_tokens=estimated,
                    error=f"HTTP {response.status_code}: {response.text[:400]}",
                    dropped_scale=dropped_scale,
                )
            data = response.json()
            return DecisionResult(
                answers=data.get("answers") or {},
                usage=data.get("usage") or {},
                latency_ms=latency_ms,
                payload_bytes=payload_bytes,
                estimated_tokens=estimated,
                dropped_scale=dropped_scale,
            )
        except Exception as exc:  # network, TLS, JSON decode
            latency_ms = (time.perf_counter() - started) * 1000.0
            return DecisionResult(
                answers=None,
                usage={},
                latency_ms=latency_ms,
                payload_bytes=payload_bytes,
                estimated_tokens=estimated,
                error=f"{type(exc).__name__}: {exc}",
            )


def _strip_score_scale(payload: dict[str, Any]) -> dict[str, Any]:
    fallback = copy.deepcopy(payload)
    for question in fallback.get("questions", {}).values():
        if question.get("type") == "score" and "scale" in question:
            scale = question.pop("scale") or {}
            lo, hi = scale.get("min", 1), scale.get("max", 5)
            question["instructions"] = f"{question.get('instructions', '')} Return an integer from {lo} to {hi}.".strip()
    return fallback


def estimate_payload_tokens(payload: dict[str, Any]) -> int:
    """Rough pre-flight token estimate. Live usage values always win."""
    text_chars = 0
    image_count = 0
    raw_input = payload.get("input")
    parts = raw_input if isinstance(raw_input, list) else [{"type": "text", "text": str(raw_input or "")}]
    for part in parts:
        if isinstance(part, str):
            text_chars += len(part)
        elif part.get("type") == "text":
            text_chars += len(part.get("text") or "")
        elif part.get("type") == "image":
            image_count += 1
    text_chars += len(json.dumps(payload.get("questions") or {}, separators=(",", ":")))
    # 640x360 fits in a single 768px tile under Gemini's tiling rule (~258 tokens).
    image_tokens = image_count * 258 * max(1, math.ceil(SCREENSHOT_WIDTH / 768)) * max(
        1, math.ceil(SCREENSHOT_HEIGHT / 768)
    )
    return max(1, math.ceil(text_chars / 4) + image_tokens)


# ---------------------------------------------------------------------------
# Payload and question builders
# ---------------------------------------------------------------------------


def intent_question(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": "Select the single best matching intent from the criteria for the user's voice command.",
        "criteria": {intent_id: meta.get("description", intent_id) for intent_id, meta in schema.items()},
    }


def suite_c_questions(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent": intent_question(schema),
        "needs_confirmation": {
            "type": "noul",
            "instructions": (
                "Does executing this intent require explicit user confirmation because it is "
                "destructive, irreversible, or high risk (shutdown, reboot, closing a window, "
                "system update)?"
            ),
        },
        "needs_browser": {
            "type": "noul",
            "instructions": (
                "Does fulfilling this request require driving a web browser: DOM interaction, "
                "clicking page elements, or filling web forms?"
            ),
        },
        "needs_screen_context": {
            "type": "noul",
            "instructions": (
                "Does routing or fulfilling this request correctly require seeing the current "
                "screen contents or the page DOM?"
            ),
        },
        "task_complexity": {
            "type": "score",
            "instructions": "Rate execution complexity on a 1 to 5 rubric.",
            "criteria": [
                "One deterministic local command with no parameters.",
                "One local desktop/audio/media command with a simple parameter.",
                "Multi-step local system or study workflow.",
                "Tool-assisted workflow such as live web search or targeted coding.",
                "Multi-step autonomous coding or interactive browser workflow.",
            ],
        },
    }


def suite_d_questions() -> dict[str, Any]:
    return {
        "next_action": {
            "type": "choice",
            "instructions": (
                "Pick the single next browser action that makes progress toward the stated goal, "
                "given the DOM elements and the screenshot."
            ),
            "criteria": {
                "btn_accept_cookies": "Click the cookie consent or modal accept button that is blocking the page.",
                "btn_close_modal": "Dismiss the blocking modal with its close (X) control.",
                "focus_search": "Click or focus the search input so a query can be typed.",
                "btn_sign_in": "Click the sign-in control to authenticate.",
                "btn_submit_form": "Submit the visible form with its submit button.",
                "done": "The goal is already achieved; no further browser action is needed.",
            },
        },
        "has_blocking_modal": {
            "type": "noul",
            "instructions": "Is a modal dialog or overlay currently blocking interaction with the page?",
        },
        "goal_complete": {
            "type": "noul",
            "instructions": "Has the stated goal already been achieved on the current page?",
        },
    }


def build_text_payload(text: str, questions: dict[str, Any]) -> dict[str, Any]:
    return {"model": DECISION_MODEL, "input": text, "questions": questions}


def build_multimodal_payload(text: str, png: bytes, questions: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": DECISION_MODEL,
        "input": [
            {"type": "text", "text": text},
            {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mime_type": "image/png"},
        ],
        "questions": questions,
    }


# ---------------------------------------------------------------------------
# Test corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    utterance: str
    expected: str
    alts: tuple[str, ...] = ()
    note: str = ""

    def matches(self, intent: str | None) -> bool:
        return intent is not None and (intent == self.expected or intent in self.alts)


SUITE_A: tuple[Case, ...] = (
    Case("turn up the volume", "audio_volume_up"),
    Case("set brightness to 40 percent", "brightness_set"),
    Case("switch to workspace 3", "workspace_switch"),
    Case("mute my microphone", "audio_mic_mute"),
    Case("pause the music", "media_pause"),
    Case("take a screenshot", "omarchy_screenshot"),
    Case("toggle fullscreen", "window_fullscreen_toggle"),
    Case("open obsidian", "launch_obsidian"),
    Case("switch to headphones", "audio_output_headphones"),
    Case("what's my system status", "system_status"),
    Case("next track", "media_next"),
    Case("close the window", "window_close"),
)

SUITE_B: tuple[Case, ...] = (
    Case("it's way too loud in here", "audio_volume_down", note="direction must invert"),
    Case("my screen is blinding me", "brightness_down", note="complaint implies lower brightness"),
    Case("get rid of this window", "window_close"),
    Case("I need to refactor the async loop in gideon.py", "hard_goal"),
    Case("check who won the F1 race yesterday", "web_search", note="live information"),
    Case("go to github and click the new pull request button", "browser_use_passthrough"),
    Case("my speakers are annoying, put audio in my ears", "audio_output_headphones", note="sink switch"),
    Case("jotting down a quick thought: buy coffee beans", "flash_note_add", note="note capture phrasing"),
    Case(
        "wait, don't close that window, explain why it crashed",
        "conversation",
        alts=("hard_goal",),
        note="negation guard: must not close the window",
    ),
    Case("switch to a darker look", "omarchy_theme_dark"),
    Case("make it quieter", "audio_volume_down"),
    Case("put on some jazz", "media_play"),
)

SUITE_C_UTTERANCES: tuple[str, ...] = (
    "set the volume to 25 percent",
    "I need a script that rotates my wallpapers every hour",
    "go to the GitHub pull request page and click merge",
)

SUITE_D_STEPS: tuple[dict[str, Any], ...] = (
    {
        "goal": "Start the free trial from the pricing page.",
        "url": "https://example.com/pricing",
        "dom": (
            "[e1] button \"Accept All Cookies\" (visible, center overlay)\n"
            "[e2] input[type=search] placeholder=\"Search docs\"\n"
            "[e3] link \"Sign In\"\n"
            "[e4] button \"Submit Form\"\n"
            "[e5] button aria-label=\"Close modal\" \"X\""
        ),
        "expected": {
            "next_action": "btn_accept_cookies",
            "next_action_alts": ("btn_accept_cookies", "btn_close_modal"),
            "has_blocking_modal": True,
            "goal_complete": False,
        },
        "label": "modal blocking",
    },
    {
        "goal": "Search the docs for the async router reference.",
        "url": "https://example.com/docs",
        "dom": (
            "[e1] input[type=search] placeholder=\"Search docs\" (visible, focused=false)\n"
            "[e2] button \"Search\"\n"
            "[e3] link \"Sign In\""
        ),
        "expected": {"next_action": "focus_search", "has_blocking_modal": False, "goal_complete": False},
        "label": "search box visible",
    },
    {
        "goal": "Search the docs for the async router reference.",
        "url": "https://example.com/docs?q=async+router",
        "dom": (
            "[e1] heading \"Results for: async router\" (visible)\n"
            "[e2] link \"Async router reference\"\n"
            "[e3] link \"Migration guide\"\n"
            "[e4] button \"Next page\""
        ),
        "expected": {"next_action": "done", "has_blocking_modal": False, "goal_complete": True},
        "label": "results loaded",
    },
)


# ---------------------------------------------------------------------------
# Suite runners
# ---------------------------------------------------------------------------


def _first_value(answers: dict[str, Any] | None, question: str, key: str) -> Any:
    if not answers:
        return None
    entry = answers.get(question) or {}
    return entry.get(key)


def _top_k(labels: dict[int, str], probs: Any, k: int = 3) -> list[tuple[str, float]]:
    pairs = sorted(((labels[i], float(p)) for i, p in enumerate(probs)), key=lambda item: item[1], reverse=True)
    return pairs[:k]


def run_suites_ab(
    suite_name: str,
    cases: Sequence[Case],
    schema: dict[str, Any],
    tier0_fn: Callable[[str], dict[str, Any] | None],
    bge: Any,
    decision: DecisionClient,
    tier0_lat: Latency,
    bge_lat: Latency,
    decision_lat: Latency,
    repeats: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    intent_q = {"intent": intent_question(schema)}

    for index, case in enumerate(cases):
        # --- Tier-0 -----------------------------------------------------
        started = time.perf_counter()
        parsed = tier0_fn(case.utterance)
        tier0_ms = (time.perf_counter() - started) * 1000.0
        tier0_lat.record(tier0_ms, cold=(index == 0))
        for _ in range(max(1, repeats) * 4):
            t = time.perf_counter()
            tier0_fn(case.utterance)
            tier0_lat.samples.append((time.perf_counter() - t) * 1000.0)
        tier0_id = tier0_intent(parsed)

        # --- BGE-small --------------------------------------------------
        started = time.perf_counter()
        bge_id, bge_conf, bge_probs = bge.predict(case.utterance)
        bge_ms = (time.perf_counter() - started) * 1000.0
        bge_lat.samples.append(bge_ms)
        for _ in range(max(1, repeats)):
            t = time.perf_counter()
            bge.predict(case.utterance)
            bge_lat.samples.append((time.perf_counter() - t) * 1000.0)

        # --- Decision 1 -------------------------------------------------
        payload = build_text_payload(case.utterance, intent_q)
        result = decision.ask(payload)
        decision_id = _first_value(result.answers, "intent", "choice")
        decision_conf = _first_value(result.answers, "intent", "confidence")
        if not result.dry_run and result.answers is not None:
            # With --skip-warmup the first successful request is the cold sample.
            decision_lat.record(result.latency_ms, cold=decision_lat.cold_ms is None)
        tokens = result.usage.get("total_input_tokens") if result.usage else None
        if tokens is None:
            tokens = result.estimated_tokens

        rows.append(
            {
                "suite": suite_name,
                "utterance": case.utterance,
                "expected": case.expected,
                "alts": list(case.alts),
                "note": case.note,
                "tier0_intent": tier0_id,
                "tier0_summary": (parsed or {}).get("summary"),
                "tier0_ms": tier0_ms,
                "tier0_correct": case.matches(tier0_id),
                "bge_intent": bge_id,
                "bge_conf": bge_conf,
                "bge_ms": bge_ms,
                "bge_top3": [{"intent": name, "conf": conf} for name, conf in _top_k(bge.id2label, bge_probs)],
                "bge_correct": case.matches(bge_id),
                "decision_intent": decision_id,
                "decision_conf": decision_conf,
                "decision_ms": result.latency_ms if not result.dry_run else None,
                "decision_tokens": tokens,
                "decision_payload_bytes": result.payload_bytes,
                "decision_error": result.error,
                "decision_correct": None if result.answers is None else case.matches(decision_id),
                "dropped_scale": result.dropped_scale,
            }
        )

    graded = [r for r in rows if r["decision_correct"] is not None]
    return {
        "rows": rows,
        "tier0_hits": sum(1 for r in rows if r["tier0_correct"]),
        "bge_correct": sum(1 for r in rows if r["bge_correct"]),
        "decision_correct": sum(1 for r in graded if r["decision_correct"]),
        "decision_graded": len(graded),
        "bge_mean_conf": _mean(r["bge_conf"] for r in rows),
        "decision_mean_conf": _mean(r["decision_conf"] for r in graded),
        "bge_conf_correct": _mean(r["bge_conf"] for r in rows if r["bge_correct"]),
        "bge_conf_wrong": _mean(r["bge_conf"] for r in rows if not r["bge_correct"]),
        "decision_conf_correct": _mean(r["decision_conf"] for r in graded if r["decision_correct"]),
        "decision_conf_wrong": _mean(r["decision_conf"] for r in graded if not r["decision_correct"]),
        "bge_overconfident_errors": sum(1 for r in rows if not r["bge_correct"] and r["bge_conf"] >= 0.8),
        "decision_overconfident_errors": sum(
            1 for r in graded if not r["decision_correct"] and (r["decision_conf"] or 0) >= 0.8
        ),
        "decision_tokens_mean": _mean(r["decision_tokens"] for r in graded),
    }


def run_suite_c(
    utterances: Sequence[str],
    schema: dict[str, Any],
    decision: DecisionClient,
) -> dict[str, Any]:
    single_q = {"intent": intent_question(schema)}
    multi_q = suite_c_questions(schema)
    rows: list[dict[str, Any]] = []

    for utterance in utterances:
        one = decision.ask(build_text_payload(utterance, single_q))
        five = decision.ask(build_text_payload(utterance, multi_q))

        one_tokens = (one.usage or {}).get("total_input_tokens") or one.estimated_tokens
        five_tokens = (five.usage or {}).get("total_input_tokens") or five.estimated_tokens
        rows.append(
            {
                "utterance": utterance,
                "latency_1q_ms": one.latency_ms if not one.dry_run else None,
                "latency_5q_ms": five.latency_ms if not five.dry_run else None,
                "delta_ms": (five.latency_ms - one.latency_ms) if not one.dry_run and not five.dry_run else None,
                "tokens_1q": one_tokens,
                "tokens_5q": five_tokens,
                "delta_tokens": five_tokens - one_tokens,
                "answers_1q": one.answers,
                "answers_5q": five.answers,
                "payload_bytes_1q": one.payload_bytes,
                "payload_bytes_5q": five.payload_bytes,
                "error_1q": one.error,
                "error_5q": five.error,
                "dropped_scale": one.dropped_scale or five.dropped_scale,
            }
        )

    live = [r for r in rows if r["latency_1q_ms"] is not None and r["latency_5q_ms"] is not None]
    return {
        "rows": rows,
        "criteria_count": len(schema),
        "questions_1q": 1,
        "questions_5q": len(multi_q),
        "mean_latency_1q_ms": _mean(r["latency_1q_ms"] for r in live),
        "mean_latency_5q_ms": _mean(r["latency_5q_ms"] for r in live),
        "mean_tokens_1q": _mean(r["tokens_1q"] for r in rows),
        "mean_tokens_5q": _mean(r["tokens_5q"] for r in rows),
        "mean_delta_ms": _mean(r["delta_ms"] for r in live),
        "mean_delta_tokens": _mean(r["delta_tokens"] for r in rows),
    }


def run_suite_d(png: bytes, decision: DecisionClient) -> dict[str, Any]:
    questions = suite_d_questions()
    clean_png = render_screenshot_png(show_modal=False)
    rows: list[dict[str, Any]] = []
    for step in SUITE_D_STEPS:
        expected = step["expected"]
        step_png = png if expected["has_blocking_modal"] else clean_png
        text = (
            f"Goal: {step['goal']}\n"
            f"URL: {step['url']}\n"
            "Interactive DOM elements:\n"
            f"{step['dom']}\n"
            "Decide the single next action, whether a blocking modal is present, "
            "and whether the goal is already complete."
        )
        payload = build_multimodal_payload(text, step_png, questions)
        result = decision.ask(payload)
        answers = result.answers or {}
        next_action = _first_value(result.answers, "next_action", "choice")
        modal = _first_value(result.answers, "has_blocking_modal", "noul")
        complete = _first_value(result.answers, "goal_complete", "noul")
        graded = result.answers is not None
        modal_pred = None if modal is None else (float(modal) >= 0.5)
        complete_pred = None if complete is None else (float(complete) >= 0.5)
        valid_actions = expected.get("next_action_alts", (expected["next_action"],))
        rows.append(
            {
                "step": step["label"],
                "goal": step["goal"],
                "next_action": next_action,
                "expected_next_action": expected["next_action"],
                "next_action_correct": None if not graded else next_action in valid_actions,
                "has_blocking_modal": modal,
                "expected_has_blocking_modal": expected["has_blocking_modal"],
                "modal_correct": None if not graded or modal_pred is None else modal_pred == expected["has_blocking_modal"],
                "goal_complete": complete,
                "expected_goal_complete": expected["goal_complete"],
                "complete_correct": None if not graded or complete_pred is None else complete_pred == expected["goal_complete"],
                "latency_ms": result.latency_ms if not result.dry_run else None,
                "tokens": (result.usage or {}).get("total_input_tokens") or result.estimated_tokens,
                "payload_bytes": result.payload_bytes,
                "estimated_tokens": result.estimated_tokens,
                "answers": answers,
                "error": result.error,
                "dropped_scale": result.dropped_scale,
            }
        )
    graded = [r for r in rows if r["next_action_correct"] is not None]
    return {
        "rows": rows,
        "steps": len(rows),
        "next_action_correct": sum(1 for r in graded if r["next_action_correct"]),
        "modal_correct": sum(1 for r in graded if r["modal_correct"]),
        "complete_correct": sum(1 for r in graded if r["complete_correct"]),
        "graded": len(graded),
        "mean_latency_ms": _mean(r["latency_ms"] for r in graded),
        "mean_tokens": _mean(r["tokens"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if isinstance(value, float) or digits:
            return f"{value:.{digits}f}{suffix}"
        return f"{value}{suffix}"
    return str(value)


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "n/a"
    if value < 1.0:
        return f"{value * 1000.0:.0f} us"
    return f"{value:.2f} ms"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    cells = [[str(h) for h in headers]] + [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"]
    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in cells[1:]:
        out.append("| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |")
    return out


def _latency_row(name: str, lat: Latency) -> list[str]:
    stats = lat.stats()
    if not stats:
        return [name, _fmt_ms(lat.cold_ms), "n/a", "n/a", "n/a", "n/a", "0"]
    return [
        name,
        _fmt_ms(lat.cold_ms),
        _fmt_ms(stats["min"]),
        _fmt_ms(stats["p50"]),
        _fmt_ms(stats["p95"]),
        _fmt_ms(stats["mean"]),
        str(stats["n"]),
    ]


def _case_row(row: dict[str, Any], show_tier0: bool) -> list[str]:
    expected = row["expected"] + (f" (alt: {', '.join(row['alts'])})" if row["alts"] else "")
    tier0 = (
        f"{row['tier0_intent'] or '-'} {'OK' if row['tier0_correct'] else 'x'} ({_fmt_ms(row['tier0_ms'])})"
        if show_tier0
        else "-"
    )
    bge = f"{row['bge_intent']} {'OK' if row['bge_correct'] else 'x'} ({row['bge_conf']:.2f}, {_fmt_ms(row['bge_ms'])})"
    if row["decision_intent"] is None:
        decision = "dry-run" if row["decision_error"] is None else f"ERR {row['decision_error'][:40]}"
        if row["decision_error"] is None and row["decision_tokens"]:
            decision = f"dry-run (~{row['decision_tokens']} tok)"
    else:
        decision = (
            f"{row['decision_intent']} {'OK' if row['decision_correct'] else 'x'} "
            f"({_fmt(row['decision_conf'])}, {_fmt_ms(row['decision_ms'])}, {row['decision_tokens']} tok)"
        )
    cells = [row["utterance"], expected]
    if show_tier0:
        cells.append(tier0)
    cells.extend([bge, decision])
    return cells


def build_report(results: dict[str, Any]) -> str:
    meta = results["meta"]
    lines: list[str] = []
    lines.append("# Decision 1 vs local Tier-0 + BGE-small")
    lines.append("")
    lines.append(f"- Mode: **{'dry-run (local only)' if meta['dry_run'] else 'live Decisions API'}**")
    lines.append(f"- Model: `{meta['model']}` at `{meta['endpoint']}`")
    lines.append(f"- API key present: {'yes' if meta['api_key_present'] else 'no'}")
    lines.append(f"- BGE init: {_fmt_ms(meta['bge_init_ms'])}, schema intents: {meta['intent_count']}")
    lines.append(f"- Warm-up: {meta['warmup']}")
    lines.append(f"- Repeats per case for warm latency: {meta['repeats']}")
    lines.append("")

    # Latency ------------------------------------------------------------
    lines.append("## Cold vs warm steady-state latency")
    lines.append("")
    lines.extend(
        _table(
            ["Engine", "Cold", "Warm min", "Warm p50", "Warm p95", "Warm mean", "Samples"],
            [
                _latency_row("Tier-0 regex", results["latency"]["tier0"]),
                _latency_row("BGE-small INT8", results["latency"]["bge"]),
                _latency_row("Decision 1 (API)", results["latency"]["decision"]),
            ],
        )
    )
    lines.append("")

    # Suite A ------------------------------------------------------------
    suite_a = results["suite_a"]
    lines.append("## Suite A - small / fast reflex commands")
    lines.append("")
    lines.extend(
        _table(
            ["Utterance", "Expected", "Tier-0", "BGE-small", "Decision 1"],
            [_case_row(row, show_tier0=True) for row in suite_a["rows"]],
        )
    )
    lines.append("")
    lines.append(
        f"Tier-0 caught {suite_a['tier0_hits']}/{len(suite_a['rows'])} cases at regex speed; "
        f"BGE-small {suite_a['bge_correct']}/{len(suite_a['rows'])} correct; "
        f"Decision 1 {_score_line(suite_a)}."
    )
    lines.append("")

    # Suite B ------------------------------------------------------------
    suite_b = results["suite_b"]
    lines.append("## Suite B - ambiguous / colloquial / hard routing")
    lines.append("")
    lines.extend(
        _table(
            ["Utterance", "Expected", "Tier-0", "BGE-small", "Decision 1"],
            [_case_row(row, show_tier0=True) for row in suite_b["rows"]],
        )
    )
    lines.append("")
    lines.append(
        f"BGE-small {suite_b['bge_correct']}/{len(suite_b['rows'])} correct "
        f"(mean conf {_fmt(suite_b['bge_mean_conf'])}); Decision 1 {_score_line(suite_b)}."
    )
    lines.append("")

    # Accuracy + calibration --------------------------------------------
    lines.append("## Accuracy and confidence calibration")
    lines.append("")
    lines.extend(
        _table(
            [
                "Suite",
                "Cases",
                "Tier-0 hit",
                "BGE correct",
                "Decision correct",
                "BGE conf (correct/wrong)",
                "Decision conf (correct/wrong)",
                "BGE overconfident errors",
            ],
            [
                [
                    "A small",
                    len(suite_a["rows"]),
                    suite_a["tier0_hits"],
                    suite_a["bge_correct"],
                    _graded_text(suite_a),
                    f"{_fmt(suite_a['bge_conf_correct'])} / {_fmt(suite_a['bge_conf_wrong'])}",
                    f"{_fmt(suite_a['decision_conf_correct'])} / {_fmt(suite_a['decision_conf_wrong'])}",
                    suite_a["bge_overconfident_errors"],
                ],
                [
                    "B hard",
                    len(suite_b["rows"]),
                    suite_b["tier0_hits"],
                    suite_b["bge_correct"],
                    _graded_text(suite_b),
                    f"{_fmt(suite_b['bge_conf_correct'])} / {_fmt(suite_b['bge_conf_wrong'])}",
                    f"{_fmt(suite_b['decision_conf_correct'])} / {_fmt(suite_b['decision_conf_wrong'])}",
                    suite_b["bge_overconfident_errors"],
                ],
                [
                    "D browser",
                    results["suite_d"]["steps"],
                    "-",
                    "-",
                    _graded_text(results["suite_d"]),
                    "-",
                    "-",
                    "-",
                ],
            ],
        )
    )
    lines.append("")

    # Disagreements ------------------------------------------------------
    disagreements = [
        row
        for row in suite_a["rows"] + suite_b["rows"]
        if row["bge_intent"] != row["decision_intent"] and row["decision_intent"] is not None
    ]
    lines.append("## Disagreements (BGE-small vs Decision 1)")
    lines.append("")
    if not disagreements:
        if meta["dry_run"]:
            lines.append("No live Decision 1 answers in dry-run mode, so there is nothing to compare.")
        else:
            lines.append("BGE-small and Decision 1 agreed on every graded case.")
    else:
        lines.extend(
            _table(
                ["Utterance", "Expected", "BGE-small", "Decision 1", "Winner"],
                [
                    [
                        row["utterance"],
                        row["expected"],
                        f"{row['bge_intent']} ({_fmt(row['bge_conf'])})",
                        f"{row['decision_intent']} ({_fmt(row['decision_conf'])})",
                        _winner(row),
                    ]
                    for row in disagreements
                ],
            )
        )
    lines.append("")

    # Suite C ------------------------------------------------------------
    suite_c = results["suite_c"]
    lines.append("## Suite C - parallel multi-question scaling")
    lines.append("")
    lines.extend(
        _table(
            [
                "Utterance",
                f"1 question ({suite_c['criteria_count']} criteria)",
                f"{suite_c['questions_5q']} questions",
                "Latency delta",
                "Tokens 1Q",
                "Tokens 5Q",
                "Token delta",
            ],
            [
                [
                    row["utterance"],
                    _fmt_ms(row["latency_1q_ms"]),
                    _fmt_ms(row["latency_5q_ms"]),
                    _fmt_ms(row["delta_ms"]),
                    row["tokens_1q"],
                    row["tokens_5q"],
                    f"+{row['delta_tokens']}",
                ]
                for row in suite_c["rows"]
            ],
        )
    )
    lines.append("")
    if suite_c["mean_delta_ms"] is not None:
        lines.append(
            f"Mean latency delta for {suite_c['questions_5q'] - 1} extra questions: "
            f"{_fmt_ms(suite_c['mean_delta_ms'])}; mean token delta: "
            f"+{_fmt(suite_c['mean_delta_tokens'], 0)} input tokens."
        )
    else:
        lines.append(
            f"Payload growth for {suite_c['questions_5q'] - 1} extra questions: "
            f"+{_fmt(suite_c['mean_delta_tokens'], 0)} estimated input tokens per call "
            "(live latency delta needs a real API key)."
        )
    lines.append("")

    # Suite D ------------------------------------------------------------
    suite_d = results["suite_d"]
    lines.append("## Suite D - browser screenshot + DOM decision loop")
    lines.append("")
    png = results["png"]
    lines.append(
        f"Synthetic screenshot: {png['width']}x{png['height']} PNG, {png['bytes']} bytes, "
        f"{png['base64_bytes']} bytes base64, scanlines valid: {png['scanlines_ok']}."
    )
    lines.append("")
    lines.extend(
        _table(
            [
                "Step",
                "next_action",
                "has_blocking_modal",
                "goal_complete",
                "Latency",
                "Tokens",
                "All correct",
            ],
            [
                [
                    row["step"],
                    f"{row['next_action'] or '-'} (want {row['expected_next_action']}) "
                    f"{_mark(row['next_action_correct'])}",
                    f"{_fmt(row['has_blocking_modal'])} (want {_fmt(row['expected_has_blocking_modal'])}) "
                    f"{_mark(row['modal_correct'])}",
                    f"{_fmt(row['goal_complete'])} (want {_fmt(row['expected_goal_complete'])}) "
                    f"{_mark(row['complete_correct'])}",
                    _fmt_ms(row["latency_ms"]),
                    row["tokens"],
                    _mark(
                        None
                        if row["next_action_correct"] is None
                        else bool(row["next_action_correct"] and row["modal_correct"] and row["complete_correct"])
                    ),
                ]
                for row in suite_d["rows"]
            ],
        )
    )
    lines.append("")
    lines.append(f"Decision 1 {_score_line(suite_d)} on the browser loop.")
    lines.append("")

    # Free tier ----------------------------------------------------------
    ft = results["free_tier"]
    lines.append("## Token and free-tier headroom")
    lines.append("")
    lines.append(
        f"Free-tier assumptions: {FREE_TIER['tpm']:,} TPM, {FREE_TIER['rpm']} RPM, "
        f"{FREE_TIER['rpd']:,} RPD."
    )
    lines.append("")
    lines.extend(
        _table(
            [
                "Workload",
                "Input tokens/step",
                "TPM headroom (steps/min)",
                "RPM headroom (steps/min)",
                "Effective steps/min",
                "Tokens/min at effective",
                "RPD exhausted (min)",
            ],
            [
                [
                    workload["name"],
                    workload["tokens"],
                    workload["tpm_steps_per_min"],
                    FREE_TIER["rpm"],
                    workload["effective_steps_per_min"],
                    workload["tokens_per_min"],
                    workload["rpd_minutes"],
                ]
                for workload in ft["workloads"]
            ],
        )
    )
    lines.append("")
    bundle = ft["workloads"][1]
    bottleneck = "TPM" if bundle["tpm_steps_per_min"] <= FREE_TIER["rpm"] else "RPM"
    lines.append(
        f"{bottleneck} binds first: {bundle['effective_steps_per_min']} requests/min of the 5-question bundle uses "
        f"{bundle['tokens_per_min']:,} tokens/min (~"
        f"{round(100 * bundle['tokens_per_min'] / FREE_TIER['tpm'])}% of the {FREE_TIER['tpm']:,} TPM budget, "
        f"vs {FREE_TIER['rpm']} RPM ceiling)."
    )
    lines.append("")
    lines.append(
        f"The daily request cap ({FREE_TIER['rpd']:,}) is exhausted after about "
        f"{bundle['rpd_minutes']} minutes of continuous 5-question routing."
    )
    lines.append("")
    lines.append(
        f"Local comparison: Tier-0 costs 0 tokens and {_fmt_ms(results['latency']['tier0'].stats()['p50'])} "
        f"at p50; BGE-small costs 0 tokens and {_fmt_ms(results['latency']['bge'].stats()['p50'])} at p50."
    )
    lines.append("")
    return "\n".join(lines)


def _mark(value: Any) -> str:
    if value is None:
        return "-"
    return "OK" if value else "x"


def _graded_text(suite: dict[str, Any]) -> str:
    graded = suite.get("graded") or suite.get("decision_graded")
    if not graded:
        return "n/a (dry-run)"
    if "decision_correct" in suite:
        return f"{suite['decision_correct']}/{graded}"
    if suite.get("next_action_correct") is not None:
        return f"{suite['next_action_correct']}/{graded}"
    return "n/a (dry-run)"


def _score_line(suite: dict[str, Any]) -> str:
    graded = suite.get("graded") or suite.get("decision_graded")
    if graded:
        if "decision_correct" in suite:
            return f"{suite['decision_correct']}/{graded} correct"
        if suite.get("next_action_correct") is not None:
            return f"{suite['next_action_correct']}/{graded} next_action correct"
    return "not graded (dry-run)"


def _winner(row: dict[str, Any]) -> str:
    if row["decision_correct"] and not row["bge_correct"]:
        return "Decision 1"
    if row["bge_correct"] and not row["decision_correct"]:
        return "BGE-small"
    if row["bge_correct"] and row["decision_correct"]:
        return "tie (both right)"
    return "neither"


def free_tier_math(results: dict[str, Any]) -> dict[str, Any]:
    suite_a = results["suite_a"]
    suite_c = results["suite_c"]
    suite_d = results["suite_d"]
    workloads_spec = [
        ("Small command (1 choice question)", suite_a.get("decision_tokens_mean") or _mean(r["decision_tokens"] for r in suite_a["rows"])),
        (f"Routing bundle ({suite_c['questions_5q']} questions)", suite_c.get("mean_tokens_5q")),
        ("Browser loop step (text + PNG)", suite_d.get("mean_tokens")),
    ]
    workloads = []
    for name, tokens in workloads_spec:
        tokens = int(round(tokens or 1))
        tpm_steps = FREE_TIER["tpm"] / tokens
        tpm_steps_per_min = min(FREE_TIER["rpm"], tpm_steps)
        workloads.append(
            {
                "name": name,
                "tokens": tokens,
                "tpm_steps_per_min": round(tpm_steps, 2),
                "effective_steps_per_min": round(tpm_steps_per_min, 2),
                "tokens_per_min": int(round(tokens * tpm_steps_per_min)),
                "rpd_minutes": round(FREE_TIER["rpd"] / tpm_steps_per_min, 1) if tpm_steps_per_min else None,
            }
        )
    return {"workloads": workloads}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Gemini Decision 1 against GIDEON's Tier-0 regex router and BGE-small.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip every network call: exercise Tier-0 + BGE locally and validate the Decision payloads.",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="Write the full results as JSON to this path.")
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Skip the warm-up call so cold latency stays on the first real request.",
    )
    parser.add_argument("--png-out", type=Path, default=None, help="Write the synthetic screenshot PNG here.")
    parser.add_argument("--repeats", type=int, default=5, help="Extra warm latency samples per case (default 5).")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds (default 60).")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get(API_KEY_ENV)
    dry_run = bool(args.dry_run)

    if not dry_run and not api_key:
        print(
            f"error: {API_KEY_ENV} is not set. Export it for a live run, or pass --dry-run for a "
            "local-only verification that needs no key.",
            file=sys.stderr,
        )
        return 2

    started_at = time.time()
    schema = load_schema()

    print(f"[1/5] loading Tier-0 regex router and BGE-small (schema intents: {len(schema)})", file=sys.stderr)
    tier0_fn = load_tier0()
    bge_init_started = time.perf_counter()
    bge = load_bge()
    bge_init_ms = (time.perf_counter() - bge_init_started) * 1000.0

    png = render_screenshot_png()
    png_info = inspect_png(png)
    if args.png_out:
        args.png_out.parent.mkdir(parents=True, exist_ok=True)
        args.png_out.write_bytes(png)

    tier0_lat = Latency()
    bge_lat = Latency()
    decision_lat = Latency()
    decision = DecisionClient(api_key=api_key, dry_run=dry_run, timeout_s=args.timeout)
    warmup_note = "skipped"

    try:
        print("[2/5] warming up local engines", file=sys.stderr)
        warm_started = time.perf_counter()
        tier0_fn("warm up the router")
        tier0_lat.record((time.perf_counter() - warm_started) * 1000.0, cold=True)

        warm_started = time.perf_counter()
        bge.predict("warm up the router")
        bge_lat.record((time.perf_counter() - warm_started) * 1000.0, cold=True)
        for _ in range(3):
            bge.predict("warm up the router")

        if not dry_run:
            print("[3/5] warming up Decision 1 (TLS keep-alive)", file=sys.stderr)
            if not args.skip_warmup:
                warm = decision.ask(build_text_payload("warm up the router", {"intent": intent_question(schema)}))
                if warm.answers is not None:
                    decision_lat.record(warm.latency_ms, cold=True)
                    warmup_note = "performed"
                else:
                    warmup_note = f"failed: {warm.error}"
            else:
                warmup_note = "skipped by flag"
        else:
            print("[3/5] dry-run: skipping Decision 1 network calls", file=sys.stderr)
            warmup_note = "local engines warmed; Decision 1 skipped (dry-run)"

        print("[4/5] running suites A and B", file=sys.stderr)
        suite_a = run_suites_ab(
            "A", SUITE_A, schema, tier0_fn, bge, decision, tier0_lat, bge_lat, decision_lat, args.repeats
        )
        suite_b = run_suites_ab(
            "B", SUITE_B, schema, tier0_fn, bge, decision, tier0_lat, bge_lat, decision_lat, args.repeats
        )

        print("[5/5] running suites C and D", file=sys.stderr)
        suite_c = run_suite_c(SUITE_C_UTTERANCES, schema, decision)
        suite_d = run_suite_d(png, decision)
    finally:
        decision.close()

    results: dict[str, Any] = {
        "meta": {
            "dry_run": dry_run,
            "model": DECISION_MODEL,
            "endpoint": DECISIONS_URL,
            "api_key_present": bool(api_key),
            "bge_init_ms": bge_init_ms,
            "intent_count": len(schema),
            "skip_warmup": bool(args.skip_warmup),
            "warmup": warmup_note,
            "repeats": args.repeats,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at)),
            "duration_s": round(time.time() - started_at, 2),
            "free_tier": FREE_TIER,
        },
        "latency": {
            "tier0": tier0_lat,
            "bge": bge_lat,
            "decision": decision_lat,
        },
        "png": png_info,
        "suite_a": suite_a,
        "suite_b": suite_b,
        "suite_c": suite_c,
        "suite_d": suite_d,
    }
    results["free_tier"] = free_tier_math(results)

    if args.json_out:
        payload = _jsonable(results)
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2, default=str))

    report = build_report(results)
    print()
    print(report)

    if args.json_out:
        print(f"\nJSON results written to {args.json_out}")

    if dry_run:
        print(
            "\nDry-run complete: local Tier-0 + BGE executed, PNG and Decision payloads validated. "
            "Export GEMINI_API_KEY and drop --dry-run for live Decision 1 numbers."
        )

    errors = [row["decision_error"] for row in suite_a["rows"] + suite_b["rows"] if row["decision_error"]]
    errors += [row["error_1q"] or row["error_5q"] for row in suite_c["rows"] if row.get("error_1q") or row.get("error_5q")]
    errors += [row["error"] for row in suite_d["rows"] if row.get("error")]
    if errors:
        print(f"\nwarning: {len(errors)} Decision request(s) failed; first error: {errors[0][:300]}", file=sys.stderr)
        return 1
    return 0


def _jsonable(results: dict[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Latency):
            return {"cold_ms": value.cold_ms, "samples_ms": value.samples, "stats": value.stats()}
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        if isinstance(value, float):
            return round(value, 4)
        return value

    return convert(results)


if __name__ == "__main__":
    raise SystemExit(main())
