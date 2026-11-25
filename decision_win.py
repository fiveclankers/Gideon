"""Windows Gemini Decision 1 Tier-1 Intent Router.

Wraps ``decision_router.GeminiDecisionDispatcher`` against
``intents_schema_win.json`` with BGE-small fallback completely disabled so
``onnxruntime`` and ``tokenizers`` are never imported on Windows.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

from decision_router import DecisionAPIError, GeminiDecisionDispatcher

logger = logging.getLogger("gideon.win.decision")

ROOT = Path(__file__).resolve().parent
WIN_SCHEMA_PATH = ROOT / "intents_schema_win.json"

_WIN_COMPACT_CRITERIA: Dict[str, str] = {
    "music_play_local": "Play local FLAC by mood, genre, artist, or track",
    "music_play_online": "Play new, obscure, non-local song or online vibe",
    "music_control": "Pause, resume, skip, stop, queue, now playing",
    "audio_control": "System volume %, up/down, mute speakers or mic",
    "launch_app": "Open or launch a Windows app or game",
    "agy_status": "Is Antigravity done, what is it doing, how long",
    "ask_agy": "Ask Antigravity or delegate deep long-horizon task",
    "screen_context": "Look at my screen, window, or error right now",
    "web_search": "Live info, news, prices, game wiki, facts",
    "conversation": "Casual chat, quick question, general reasoning",
}


class WinDecisionRouter(GeminiDecisionDispatcher):
    """Zero-BGE Decision 1 router for Windows Gideon."""

    def __init__(self, schema_path: Path | str = WIN_SCHEMA_PATH, timeout_s: float = 2.5) -> None:
        os.environ.setdefault("GIDEON_TIER1_ROUTER", "decision")
        super().__init__(schema_path=schema_path, timeout_s=timeout_s, compact_criteria=True)
        self.criteria = {
            k: _WIN_COMPACT_CRITERIA.get(k, v)
            for k, v in self.criteria.items()
        }

    def _fallback_predict(self, text: str) -> Tuple[str, float, Dict[str, float]]:
        """Never load BGE/ONNX on Windows; fall back safely to conversation."""
        self.fallback_count += 1
        logger.warning("Decision 1 unavailable; falling back cleanly to 'conversation' (no BGE).")
        return "conversation", 0.0, {"conversation": 1.0}

    def classify(self, text: str) -> Tuple[str, float, Dict[str, float]]:
        intent, conf, probs = self.predict(text)
        return intent, conf, probs
