"""Ultra-lean Tier-0 Reflex Router for Windows Gideon.

Deliberately restricted to exact, zero-ambiguity high-frequency literals
(dismissal, bare "play something" local FLAC playback, and short transport/mute
commands). All natural/varied phrasings ("ask antigravity...", "is antigravity
done?", mood/vibe music, app launches, screenshots) intentionally bypass Tier-0
and flow to Gemini Decision 1.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_TRAIL_PUNCT_RE = re.compile(r"[.!?,\s]+$")
_WAKE_PREFIX_RE = re.compile(r"^(?:hey\s+)?(?:gideon|jarvis)\s*[,:]?\s*", re.I)

_DISMISSAL_LITERALS = frozenset({
    "bye",
    "bye bye",
    "bye-bye",
    "byebye",
    "goodbye",
    "goodbye gideon",
    "bye gideon",
    "bye jarvis",
    "goodbye jarvis",
})

_LOCAL_PLAY_LITERALS = frozenset({
    "play something",
    "play some music",
    "put on some music",
    "put on something",
    "play music",
})

_TRANSPORT_LITERALS: dict[str, dict[str, str]] = {
    "pause": {"intent": "music_control", "action": "pause", "summary": "paused music"},
    "pause music": {"intent": "music_control", "action": "pause", "summary": "paused music"},
    "resume": {"intent": "music_control", "action": "resume", "summary": "resumed music"},
    "resume music": {"intent": "music_control", "action": "resume", "summary": "resumed music"},
    "skip": {"intent": "music_control", "action": "next", "summary": "next track"},
    "next track": {"intent": "music_control", "action": "next", "summary": "next track"},
    "next song": {"intent": "music_control", "action": "next", "summary": "next track"},
    "previous track": {"intent": "music_control", "action": "previous", "summary": "previous track"},
    "previous song": {"intent": "music_control", "action": "previous", "summary": "previous track"},
    "stop music": {"intent": "music_control", "action": "stop", "summary": "stopped music"},
    "mute": {"intent": "audio_control", "action": "mute", "summary": "muted audio"},
    "unmute": {"intent": "audio_control", "action": "unmute", "summary": "unmuted audio"},
}


def normalize_literal(text: str) -> str:
    cleaned = _TRAIL_PUNCT_RE.sub("", (text or "").strip())
    cleaned = _WAKE_PREFIX_RE.sub("", cleaned).strip()
    cleaned = _TRAIL_PUNCT_RE.sub("", cleaned).strip()
    return " ".join(cleaned.lower().split())


def is_dismissal(text: str) -> bool:
    return normalize_literal(text) in _DISMISSAL_LITERALS


def parse_tier0(text: str) -> Optional[dict[str, Any]]:
    """Return a Tier-0 action dict ONLY for exact whitelisted commands."""
    norm = normalize_literal(text)
    if not norm:
        return None
    if norm in _DISMISSAL_LITERALS:
        return {"intent": "dismiss", "summary": "dismissed"}
    if norm in _LOCAL_PLAY_LITERALS:
        return {"intent": "music_play_local_shuffle", "summary": "playing local library"}
    if norm in _TRANSPORT_LITERALS:
        return dict(_TRANSPORT_LITERALS[norm])
    return None
