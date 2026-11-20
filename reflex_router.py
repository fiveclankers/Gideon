"""Deterministic, zero-model Tier-0 command router for GIDEON."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MUSIC_BIN = os.path.expanduser("~/.local/bin/music")

_MUSIC_MOOD_ALIASES = {
    "chill": "chill", "focus": "focus", "energetic": "energetic",
    "uplifting": "uplifting", "late night": "late-night",
    "late-night": "late-night", "nostalgic": "nostalgic",
    "romantic": "romantic", "melancholy": "melancholy",
}
_MUSIC_GENRE_ALIASES = {
    "rock": "rock", "alternative": "alternative", "alt": "alternative",
    "indie": "alternative", "pop": "pop", "hip hop": "hip-hop",
    "hip-hop": "hip-hop", "rap": "hip-hop", "r&b": "rnb-soul",
    "r and b": "rnb-soul", "rnb": "rnb-soul", "soul": "rnb-soul",
    "electronic": "electronic", "edm": "electronic", "dance": "electronic",
    "jazz": "jazz", "funk": "funk-disco", "disco": "funk-disco",
    "folk": "folk-acoustic", "acoustic": "folk-acoustic",
    "country": "folk-acoustic", "blues": "blues", "metal": "metal",
    "soundtrack": "soundtrack", "score": "soundtrack", "world": "world",
    "ambient": "ambient-chill", "chillout": "ambient-chill",
}

# Coding/debugging language wins even when a command phrase appears later.
_UNSAFE_CONTEXT = re.compile(
    r"\b(?:fix|bug|script|why|how|explain)\b|\bwrite\s+a\b|\bwhat\s+is\b|\bcode\b",
    re.IGNORECASE,
)
_PERCENT = r"([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?"
_WORKSPACE_RE = re.compile(
    r"(?:switch\s+(?:back\s+)?(?:to\s+)?|go\s+(?:back\s+)?(?:to\s+)?|take\s+me\s+to\s+|jump\s+to\s+|move\s+to\s+|back\s+to\s+)?(?:(?:(?:the\s+)?workspace\s+([a-zA-Z0-9\s-]+))|(?:(?:the\s+)?([a-zA-Z0-9\s-]+)\s+workspace))",
    re.I,
)
_VOLUME_SET_RES = (
    re.compile(r"set\s+volume\s+to\s+" + _PERCENT, re.I),
    re.compile(r"volume\s+" + _PERCENT, re.I),
)
_VOLUME_UP_RE = re.compile(
    r"(?:increase\s+volume(?:\s+by)?|turn\s+volume\s+up|volume\s+up)(?:\s+([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?)?",
    re.I,
)
_VOLUME_DOWN_RE = re.compile(
    r"(?:decrease\s+volume(?:\s+by)?|lower\s+volume|turn\s+volume\s+down|volume\s+down)(?:\s+([0-9]+(?:\.[0-9]+)?|[a-zA-Z\s-]+?)\s*(?:%|\bpercent\b)?)?",
    re.I,
)
_NOTE_RE = re.compile(
    r"^(?:note\s+down|note\s+this\s+down|flash\s+note|take\s+a\s+note|quick\s+memo|add\s+note|jot\s+this\s+down)\s*:\s*(.+?)\s*$",
    re.I | re.S,
)
_WINDOW_CONTROL_MAP = {
    "focus window left": ["hyprctl", "dispatch", "movefocus", "l"],
    "focus window right": ["hyprctl", "dispatch", "movefocus", "r"],
    "focus window up": ["hyprctl", "dispatch", "movefocus", "u"],
    "focus window down": ["hyprctl", "dispatch", "movefocus", "d"],
    "toggle fullscreen": ["hyprctl", "dispatch", "fullscreen", "1"],
    "close active window": ["hyprctl", "dispatch", "killactive"],
}
_AGENT_WORKSPACE_PHRASES = frozenset({
    "show me what you're doing",
    "show me what you are doing",
    "show agent workspace",
    "show the agent workspace",
    "toggle agent workspace",
    "hide agent workspace",
    "hide the agent workspace",
})
_AGENT_WORKSPACE_ARGV = ["hyprctl", "dispatch", 'hl.dsp.workspace.toggle_special("agent")']
_AUDIO_OUTPUT_MAP = {
    "switch audio to headphones": "GIDEON_HEADPHONES_SINK",
    "switch audio to speakers": "GIDEON_SPEAKERS_SINK",
}
_GRAPH_QUERY_RES = (
    re.compile(r"query\s+graph\s+for\s+(.+)", re.I),
    re.compile(r"graphify\s+search\s+(.+)", re.I),
)
_MUSIC_PLAYLIST_RE = re.compile(r"^(?:play|put\s+on)\s+(?:my\s+|the\s+)?(.+?)\s+playlist$", re.I)
_MUSIC_PLAYLIST_ALT_RE = re.compile(r"^(?:play|put\s+on)\s+(?:the\s+)?playlist\s+(.+)$", re.I)
_MUSIC_PLAY_RE = re.compile(r"^(?:play|put\s+on)\s+(?:some\s+|something\s+)?(.+?)(?:\s+music)?$", re.I)
_MUSIC_WANT_RE = re.compile(
    r"^(?:i\s+want\s+to|i'd\s+like\s+to|i\s+would\s+like\s+to|let's|let\s+me)\s+"
    r"(?:play|hear|listen\s+to)\s+(?:some\s+)?(.+?)(?:\s+music)?$",
    re.I,
)
_MUSIC_ARTIST_RE = re.compile(r"^(?:play|put\s+on)\s+(?:some\s+)?music\s+by\s+(.+)$", re.I)
_MUSIC_ARTIST_ALT_RE = re.compile(r"^(?:play|put\s+on)\s+artist\s+(.+)$", re.I)
_MUSIC_ARTIST_SONG_RE = re.compile(
    r"^(?:play|put\s+on)\s+(?:a\s+song|a\s+track|something)\s+by\s+(.+)$", re.I
)
_MUSIC_GENERIC_VALUES = {
    "music", "something", "anything", "some", "tunes", "songs", "a song", "a track",
    "any song", "any track", "random", "random music", "whatever", "stuff",
}

# "Just play something": a request for *some* music with no artist, genre, mood
# or playlist named. These start the local library on shuffle rather than
# resuming whichever player happens to be paused (often a browser tab).
_MUSIC_ANY_RE = re.compile(
    r"^(?:(?:hey\s+)?(?:gideon|jarvis)\s*[,:]?\s*)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:please\s+|just\s+)*"
    r"(?:play|put\s+on|shuffle)\s+"
    r"(?:me\s+|us\s+)?"
    r"(?:(?:some|any|a|the|my)\s+)*"
    r"(?:random\s+)?"
    r"(?:music|something|anything|whatever|songs?|tracks?|tunes|stuff|library"
    r"|from\s+(?:my\s+)?library)"
    r"(?:\s+(?:on\s+shuffle|please|now))?$",
    re.I,
)


def is_generic_music_request(text: str) -> bool:
    """True when the utterance asks for music without naming what to play."""
    if not text:
        return False
    return bool(_MUSIC_ANY_RE.fullmatch(re.sub(r"[.!?]+$", "", text).strip()))

# Farewell dismissal ("bye bye" anywhere in the utterance). A single "bye" or
# "goodbye" is deliberately NOT a dismissal: ASR routinely transcribes "by" as
# "bye". The only exception to "bye bye" winning is an explicit media/music
# request (e.g. "play NSync's Bye Bye Bye").
_BYE_BYE_CORE_RE = re.compile(
    r"\bbye(?:[\s,.\-!?;:']*bye)+\b",
    re.IGNORECASE,
)
_BYE_BYE_MEDIA_EXCEPTION_RE = re.compile(
    r"\b(?:play|put\s+on|queue)\b.*?\bbye(?:[\s,.\-!?;:']*bye){2,}\b"
    r"|"
    r"\b(?:play|put\s+on|queue)\b.*?\bbye(?:[\s,.\-!?;:']*bye)+\b.*?\bn['’\-]?\s*sync\b"
    r"|"
    r"\b(?:play|put\s+on|queue)\b.*?\bn['’\-]?\s*sync\b.*?\bbye(?:[\s,.\-!?;:']*bye)+\b"
    r"|"
    r"\bn['’\-]?\s*sync\b.*?\b(?:play|put\s+on|queue)\b.*?\bbye(?:[\s,.\-!?;:']*bye)+\b",
    re.IGNORECASE,
)

# Conversational statements about a play/performance must not be mistaken for
# media commands. These checks run only after the deterministic parser misses.
_CONVERSATIONAL_PLAY_RES = (
    re.compile(r"\b(?:that|this|the|a|an|nice|good|great|interesting|wonderful|amazing)\s+play\b", re.I),
    re.compile(r"\bplay\s+(?:seems|sounds|looks|was|were|is|are)\b", re.I),
    re.compile(
        r"\b(?:liked|enjoyed|loved|hated|watched|saw|went\s+to|think\s+of|thought\s+of)\s+"
        r"(?:that|the|this|a|an)?\s*play\b",
        re.I,
    ),
)
_CONVERSATIONAL_MARKERS_RE = re.compile(
    r"\b(?:seems|sounds|looks|was|were|liked|enjoyed|loved|hated|watched|saw|think|thought|"
    r"wonder|guess|suppose|maybe|perhaps)\b",
    re.I,
)
_CONVERSATIONAL_SUBJECT_RE = re.compile(r"^(?:i|we|you|they|he|she|it|that|this|the|my|our|there)\b", re.I)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_GRAPH_QUERY_CHARS = 160
_MAX_MUSIC_VALUE_CHARS = 80
_GRAPH_TASKS: set[asyncio.Task[Any]] = set()

_FOCUS_STOP_RE = re.compile(
    r"^(?:please\s+)?(?:stop|end|exit|quit|cancel|turn\s+off|disable)\s+(?:the\s+|my\s+)?focus\s+(?:mode|session|timer)$",
    re.I,
)
_FOCUS_STATUS_RE = re.compile(
    r"^(?:(?:check|what(?:'s|\s+is)|how\s+much)\s+)?(?:(?:my|the)\s+)?focus(?:\s+mode|\s+session)?\s+(?:status|time(?:\s+left|\s+remaining)?)$|^am\s+i\s+in\s+focus\s+mode$",
    re.I,
)
_FOCUS_START_RE = re.compile(
    r"^(?:please\s+)?(?:(?:start|begin|enable|turn\s+on|enter)\s+(?:a\s+)?focus\s+(?:mode|session)|focus\s+mode\s+on)"
    r"(?:\s+for\s+(?P<num>an?|\d+(?:\.\d+)?|[a-z-]+(?:\s+[a-z-]+)?)\s*(?P<unit>minutes?|mins?|hours?|hrs?))?"
    r"(?:\s+(?:for|on|about)\s+(?P<goal>.+))?$",
    re.I,
)
_FOCUS_PREFIX_START_RE = re.compile(
    r"^(?:please\s+)?(?:start|begin)\s+(?:a\s+)?(?P<num>an?|\d+(?:\.\d+)?|[a-z-]+(?:\s+[a-z-]+)?)\s*"
    r"(?P<unit>minute|min|hour|hr)\s+focus\s+(?:mode|session)(?:\s+(?:for|on|about)\s+(?P<goal>.+))?$",
    re.I,
)
_DEBUG_BUNDLE_RE = re.compile(
    r"^(?:please\s+)?(?:create|generate|export|save|dump|make|collect)\s+(?:a\s+|the\s+)?(?:gideon\s+)?debug\s+(?:bundle|logs|report)$|^gideon\s+debug\s+bundle$",
    re.I,
)

_NUMBER_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def _parse_number(token: str | None) -> float | None:
    """Parse numeric digits, percentages, or spoken number words into a float."""
    if not token:
        return None
    cleaned = re.sub(r"\s*(?:%|\bpercent\b)\s*$", "", token.strip(), flags=re.I).strip().lower()
    try:
        return float(cleaned)
    except ValueError:
        pass
    words = cleaned.replace("-", " ").split()
    total = 0.0
    matched = False
    for w in words:
        if w == "hundred":
            total = (total if matched else 1.0) * 100.0
            matched = True
        elif w in _NUMBER_WORDS:
            total += _NUMBER_WORDS[w]
            matched = True
        elif w.isdigit():
            total += float(w)
            matched = True
        else:
            return None
    return total if matched else None


def ensure_hyprland_env() -> str | None:
    """Ensure HYPRLAND_INSTANCE_SIGNATURE is set in os.environ by dynamically checking runtime sockets."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if sig:
        sock_path = Path(xdg_runtime) / "hypr" / sig / ".socket.sock"
        if sock_path.exists():
            return sig

    hypr_base = Path(xdg_runtime) / "hypr"
    if hypr_base.is_dir():
        candidates = []
        for p in hypr_base.iterdir():
            if p.is_dir() and (p / ".socket.sock").exists():
                try:
                    candidates.append((p.stat().st_mtime, p.name))
                except OSError:
                    pass
        if candidates:
            candidates.sort(reverse=True)
            discovered = candidates[0][1]
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = discovered
            logger.info("Auto-discovered HYPRLAND_INSTANCE_SIGNATURE: %s", discovered)
            return discovered
    return None


_MUSIC_NON_SEARCH_FILLERS = frozenset({
    "more", "some more", "else", "something else", "anything else",
    "new", "something new", "different", "something different",
    "other", "another", "another song", "another track", "songs", "tracks",
})


def _clean_music_value(value: str) -> str | None:
    """Strip whitespace/quotes and reject unsafe or oversized music names."""
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    if not cleaned or len(cleaned) > _MAX_MUSIC_VALUE_CHARS or _CONTROL_CHARS_RE.search(cleaned):
        return None
    return cleaned


def _looks_conversational(text: str) -> bool:
    """Return True when an utterance is conversation, not a media command.

    This is a narrow guard for the reported failure mode: a short reply such as
    "That play seems nice" must reach the LLM, not the media handler.
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Multiple sentences: commands are short and single-sentence.
    if re.search(r"[.!?]\s+[A-Za-z0-9]", stripped):
        return True
    if len(stripped.split()) > 28:
        return True
    if any(pattern.search(stripped) for pattern in _CONVERSATIONAL_PLAY_RES):
        return True
    if _CONVERSATIONAL_SUBJECT_RE.search(stripped) and _CONVERSATIONAL_MARKERS_RE.search(stripped):
        return True
    return False


def _is_dismissal(value: str) -> bool:
    """True when the utterance contains a "bye bye" farewell core anywhere.

    Accepts any preceding/trailing words or Whisper punctuation variants
    ("you thank you bye bye", "blah blah okay bye-bye", "Bye, bye.", "byebye",
    "bye bye for now") while rejecting a single bare "bye", "by", "goodbye",
    or "bypass", and exempting explicit media/music requests (e.g.
    "play bye bye bye by nsync").
    """
    if not isinstance(value, str) or not _BYE_BYE_CORE_RE.search(value):
        return False
    if _BYE_BYE_MEDIA_EXCEPTION_RE.search(value):
        return False
    return True


def parse_command(text: str) -> dict[str, Any] | None:
    """Return a structured action without importing or allocating a model."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if _is_dismissal(raw):
        return {"intent": "dismiss", "summary": "back to idle"}
    if _UNSAFE_CONTEXT.search(text):
        return None
    value = re.sub(r"[.!?]+$", "", raw).strip().lower()
    # Strip optional wake word prefix for zero-latency contiguous one-shot commands
    value = re.sub(r"^(?:hey\s+)?jarvis\s*[,:]?\s*", "", value).strip()
    raw_stripped = re.sub(r"^(?:hey\s+)?jarvis\s*[,:]?\s*", "", raw, flags=re.I).strip()
    raw_command = re.sub(r"[.!?]+$", "", raw_stripped).strip()
    # Music commands are often polite requests; strip the wrapper before matching.
    music_command = re.sub(r"^(?:hey\s+)?(?:gideon|jarvis)\s*[,:]?\s*", "", raw_command, flags=re.I).strip()
    music_command = re.sub(r"^(?:can|could|would|will)\s+you\s+", "", music_command, flags=re.I).strip()
    music_command = re.sub(r"^please\s+", "", music_command, flags=re.I).strip()

    note_match = _NOTE_RE.fullmatch(raw) or _NOTE_RE.fullmatch(raw_stripped)
    if note_match and note_match.group(1).strip():
        return {"intent": "flash_note", "body": note_match.group(1).strip()}

    if value in {"mute", "mute audio", "mute volume", "silence audio"}:
        return {"intent": "system_action", "argv": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"], "summary": "muted audio"}
    if value in {"unmute", "unmute audio", "unmute volume"}:
        return {"intent": "system_action", "argv": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], "summary": "unmuted audio"}

    for regex, direction in ((_VOLUME_UP_RE, "up"), (_VOLUME_DOWN_RE, "down")):
        match = regex.fullmatch(value)
        if match:
            raw_amt = match.group(1)
            amount = _parse_number(raw_amt) if raw_amt else 10.0
            if amount is None or not (0 < amount <= 100):
                return None
            amount_text = str(int(amount)) if amount.is_integer() else str(amount)
            argv = (["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{amount_text}%+"] if direction == "up"
                    else ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{amount_text}%-"])
            return {"intent": "system_action", "argv": argv, "summary": f"volume {direction} {amount_text}%"}
    for regex in _VOLUME_SET_RES:
        match = regex.fullmatch(value)
        if match:
            number = _parse_number(match.group(1))
            if number is None or not (0 <= number <= 100):
                return None
            number_text = str(int(number)) if number.is_integer() else str(number)
            return {"intent": "system_action", "argv": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{number_text}%"], "summary": f"volume set to {number_text}%"}

    if is_generic_music_request(music_command):
        return {
            "intent": "music_play",
            "kind": "any",
            "value": "",
            "summary": "play shuffled library",
        }

    for regex in (_MUSIC_PLAYLIST_RE, _MUSIC_PLAYLIST_ALT_RE):
        match = regex.fullmatch(music_command)
        if match:
            name = _clean_music_value(match.group(1))
            if name is None:
                return None
            return {
                "intent": "music_play",
                "kind": "playlist",
                "value": name,
                "summary": f"play playlist {name}",
            }

    for regex in (_MUSIC_ARTIST_RE, _MUSIC_ARTIST_ALT_RE, _MUSIC_ARTIST_SONG_RE):
        match = regex.fullmatch(music_command)
        if match:
            name = _clean_music_value(match.group(1))
            if name is None:
                return None
            return {
                "intent": "music_play",
                "kind": "artist",
                "value": name,
                "summary": f"play artist {name}",
            }

    for regex in (_MUSIC_PLAY_RE, _MUSIC_WANT_RE):
        match = regex.fullmatch(music_command)
        if not match:
            continue
        music_value = " ".join(match.group(1).lower().split())
        if music_value in _MUSIC_MOOD_ALIASES:
            return {
                "intent": "music_play",
                "kind": "mood",
                "value": _MUSIC_MOOD_ALIASES[music_value],
                "summary": f"play mood {_MUSIC_MOOD_ALIASES[music_value]}",
            }
        if music_value in _MUSIC_GENRE_ALIASES:
            return {
                "intent": "music_play",
                "kind": "genre",
                "value": _MUSIC_GENRE_ALIASES[music_value],
                "summary": f"play genre {_MUSIC_GENRE_ALIASES[music_value]}",
            }
        if music_value in _MUSIC_GENERIC_VALUES:
            return {
                "intent": "music_play",
                "kind": "any",
                "value": "",
                "summary": "play shuffled library",
            }
        # Anything else is a local-library search. Reject non-specific fillers
        # like "more"/"else" so "play something else" escalates cleanly to LLM.
        if music_value in _MUSIC_NON_SEARCH_FILLERS:
            return None
        search_value = _clean_music_value(match.group(1))
        if search_value is not None:
            return {
                "intent": "music_play",
                "kind": "search",
                "value": search_value,
                "summary": f"search music {search_value}",
            }

    media = {
        "pause": "pause", "pause music": "pause", "pause media": "pause", "pause playback": "pause", "pause spotify": "pause",
        "resume": "play", "resume music": "play", "resume media": "play", "resume playback": "play", "resume spotify": "play",
        "stop music": "stop", "stop media": "stop", "stop playback": "stop", "stop spotify": "stop",
        "next track": "next", "skip track": "next", "next song": "next", "previous track": "previous", "previous song": "previous",
    }
    if value in media:
        action = media[value]
        return {"intent": "media_control", "action": action, "summary": f"media {action}"}

    if value in _AGENT_WORKSPACE_PHRASES:
        return {
            "intent": "system_action",
            "argv": list(_AGENT_WORKSPACE_ARGV),
            "summary": "toggle agent workspace",
        }

    match = _WORKSPACE_RE.fullmatch(value)
    if match:
        target_str = (match.group(1) or match.group(2) or "").strip().lower()
        if target_str in {"previous", "last", "prior", "back"}:
            return {"intent": "system_action", "argv": ["hyprctl", "dispatch", "workspace", "previous"], "summary": "previous workspace"}
        parsed = _parse_number(target_str)
        if parsed is not None and parsed.is_integer():
            workspace = int(parsed)
            if 1 <= workspace <= 10:
                return {"intent": "system_action", "argv": ["hyprctl", "dispatch", "workspace", str(workspace)], "summary": f"workspace {workspace}"}
        return None

    if value in _WINDOW_CONTROL_MAP:
        argv = list(_WINDOW_CONTROL_MAP[value])
        return {"intent": "system_action", "argv": argv, "summary": " ".join(argv[2:])}

    # Fast Tier-0 app launch patterns (<1ms)
    app_patterns = {
        "open learnharness": "launch_learnharness",
        "launch learnharness": "launch_learnharness",
        "open learn harness": "launch_learnharness",
        "launch learn harness": "launch_learnharness",
        "open obsidian": "launch_obsidian",
        "launch obsidian": "launch_obsidian",
        "open chrome": "launch_browser",
        "launch chrome": "launch_browser",
        "open terminal": "launch_terminal",
        "launch terminal": "launch_terminal",
        "open kitty": "launch_terminal",
        "launch kitty": "launch_terminal",
        "open spotify": "launch_spotify",
        "launch spotify": "launch_spotify",
        "open brave": "launch_brave",
        "launch brave": "launch_brave",
        "open code": "launch_code",
        "launch code": "launch_code",
    }
    if value in app_patterns:
        return {"intent": "app_launch", "app": app_patterns[value], "summary": f"app {app_patterns[value]}"}

    for regex in _GRAPH_QUERY_RES:
        match = regex.fullmatch(value)
        if match:
            topic = match.group(1).strip()
            if not topic or len(topic) > _MAX_GRAPH_QUERY_CHARS or _CONTROL_CHARS_RE.search(topic):
                return None
            return {"intent": "graph_query", "query": topic, "summary": f"graph query: {topic}"}

    if value in _AUDIO_OUTPUT_MAP:
        sink = os.environ.get(_AUDIO_OUTPUT_MAP[value], "@DEFAULT_AUDIO_SINK@")
        device = value.rsplit(" ", 1)[-1]
        return {"intent": "system_action", "argv": ["wpctl", "set-default", sink], "summary": f"audio output switched to {device}"}

    if _FOCUS_STOP_RE.fullmatch(value):
        return {"intent": "focus_stop", "summary": "focus mode stopped"}

    if _FOCUS_STATUS_RE.fullmatch(value):
        return {"intent": "focus_status", "summary": "focus status"}

    focus_match = _FOCUS_START_RE.fullmatch(value) or _FOCUS_PREFIX_START_RE.fullmatch(value)
    if focus_match:
        num_raw = (focus_match.group("num") or "").strip().lower()
        unit_raw = (focus_match.group("unit") or "min").strip().lower()
        goal = (focus_match.group("goal") or "").strip()
        minutes: float | None = None
        if num_raw:
            parsed_num = 1.0 if num_raw in ("a", "an") else _parse_number(num_raw)
            if parsed_num is not None and parsed_num > 0:
                minutes = parsed_num * 60.0 if unit_raw.startswith(("hour", "hr")) else parsed_num
        return {
            "intent": "focus_start",
            "minutes": minutes,
            "goal": goal,
            "summary": f"focus mode {int(minutes) if minutes else 25}m",
        }

    if _DEBUG_BUNDLE_RE.fullmatch(value):
        return {"intent": "debug_bundle", "summary": "debug bundle saved"}

    return None


def _to_hypr_lua(argv: list[str]) -> str | None:
    if len(argv) >= 4 and argv[0] == "hyprctl" and argv[1] == "dispatch" and argv[2] == "workspace":
        if argv[3] == "previous":
            return 'hl.dsp.focus({ workspace = "previous" })'
        return f"hl.dsp.focus({{ workspace = {argv[3]} }})"
    if len(argv) >= 4 and argv[0] == "hyprctl" and argv[1] == "dispatch" and argv[2] == "movefocus":
        dmap = {"l": "left", "r": "right", "u": "up", "d": "down"}
        direction = dmap.get(argv[3], argv[3])
        return f'hl.dsp.focus({{ direction = "{direction}" }})'
    if len(argv) >= 3 and argv[0] == "hyprctl" and argv[1] == "dispatch" and argv[2] == "fullscreen":
        return "hl.dsp.window.fullscreen()"
    if len(argv) >= 3 and argv[0] == "hyprctl" and argv[1] == "dispatch" and argv[2] == "killactive":
        return "hl.dsp.window.close()"
    if len(argv) >= 3 and argv[0] == "hyprctl" and argv[1] == "dispatch" and argv[2].startswith("hl."):
        return argv[2]
    return None


async def hypr_socket_dispatch(cmd_str: str, timeout_s: float = 0.5) -> tuple[int, str]:
    """Execute a dispatch command directly over Hyprland's UNIX domain socket in <1ms without subprocess fork."""
    sig = ensure_hyprland_env()
    if not sig:
        return 1, "HYPRLAND_INSTANCE_SIGNATURE not set"
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sock_path = Path(xdg_runtime) / "hypr" / sig / ".socket.sock"
    if not sock_path.exists():
        return 1, f"socket not found at {sock_path}"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(sock_path)), timeout=timeout_s
        )
        writer.write(("dispatch " + cmd_str + "\n").encode("utf-8"))
        await writer.drain()
        resp_bytes = await asyncio.wait_for(reader.read(1024), timeout=timeout_s)
        writer.close()
        await writer.wait_closed()
        resp = resp_bytes.decode("utf-8", errors="replace").strip()
        return (0, resp) if resp == "ok" else (1, resp)
    except Exception as exc:
        return 1, str(exc)


async def _exec_raw(argv: list[str], timeout_s: float) -> tuple[int, str]:
    if argv[0] == "hyprctl":
        ensure_hyprland_env()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        return int(process.returncode or 0), output.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "command timed out"
    except FileNotFoundError as exc:
        return 127, str(exc)
    except OSError as exc:
        return 126, str(exc)


async def run_fixed(argv: list[str], timeout_s: float = 1.5) -> tuple[int, str]:
    """Run a fixed argv without a shell; return (status, combined output)."""
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
        raise ValueError("argv must be a non-empty list of strings")
    # Fast path: dispatch Hyprland actions directly over UNIX domain socket (<1ms)
    if argv[0] == "hyprctl" and len(argv) >= 3 and argv[1] == "dispatch":
        lua_str = _to_hypr_lua(argv)
        if lua_str:
            status, output = await hypr_socket_dispatch(lua_str, timeout_s=min(timeout_s, 0.5))
            if status == 0:
                return 0, output
    status, output = await _exec_raw(argv, timeout_s=timeout_s)
    if status != 0 and argv[0] == "hyprctl":
        lua_str = _to_hypr_lua(argv)
        if lua_str:
            status_lua, output_lua = await _exec_raw(["hyprctl", "dispatch", lua_str], timeout_s=timeout_s)
            if status_lua == 0:
                return status_lua, output_lua
    return status, output


_NODE_LINE_RE = re.compile(r"^\s*NODE\s+(\S.*?)\s*$", re.MULTILINE)


def _extract_graph_nodes(output: str) -> list[str]:
    """Pull up to the first three NODE labels out of graphify query output."""
    return _NODE_LINE_RE.findall(output or "")[:3]


async def _run_graph_query(query: str, bus: Any = None, tts: Any = None) -> None:
    """Run graphify query off the hot path and report results through tts/bus."""
    try:
        status, output = await run_fixed(["graphify", "query", query], timeout_s=10.0)
        nodes = _extract_graph_nodes(output) if status == 0 else []
        if nodes:
            if tts is not None:
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak(f"Graph query for {query}: found {len(nodes)} nodes. {nodes[0]}."))
            if bus is not None:
                broadcast = getattr(bus, "broadcast_ui", None)
                if broadcast:
                    await _await_if_needed(broadcast({"event": "graph_query_result", "query": query, "nodes": nodes, "status": "ok"}))
        else:
            if tts is not None:
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak(f"No graph results found for {query}."))
            if bus is not None:
                broadcast = getattr(bus, "broadcast_ui", None)
                if broadcast:
                    await _await_if_needed(broadcast({"event": "graph_query_result", "query": query, "nodes": [], "status": "empty"}))
    except Exception:
        logger.exception("Graph query reflex failed for %r", query)


def lyra_enabled() -> bool:
    """Check opt-in, free VRAM, and declared footprint before loading Lyra."""
    if os.environ.get("GIDEON_ENABLE_LYRA") != "1":
        return False
    try:
        result = __import__("subprocess").run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
        free_mib = max(int(line.strip()) for line in result.stdout.splitlines() if line.strip())
        footprint = int(os.environ["GIDEON_LYRA_FOOTPRINT_MIB"])
        return result.returncode == 0 and free_mib > 1200 and 0 < footprint <= free_mib - 1200
    except (OSError, ValueError, TypeError):
        return False


_lyra_instance: Any = None
_gideon_dispatcher_instance: Any = None
_dispatcher_init_lock = threading.Lock()


def get_gideon_dispatcher() -> Any | None:
    """Lazily load the Tier-1 BGE-small INT8 ONNX dispatcher (thread-safe)."""
    global _gideon_dispatcher_instance
    if _gideon_dispatcher_instance is not None:
        return _gideon_dispatcher_instance if _gideon_dispatcher_instance is not False else None
    with _dispatcher_init_lock:
        if _gideon_dispatcher_instance is not None:
            return _gideon_dispatcher_instance if _gideon_dispatcher_instance is not False else None
        try:
            from gideon_handler import GideonDispatcher
            _gideon_dispatcher_instance = GideonDispatcher()
            logger.info("Initialized Tier-1 GideonDispatcher (BGE-small INT8 ONNX)")
            return _gideon_dispatcher_instance
        except Exception:
            logger.exception("Unable to load Tier-1 GideonDispatcher")
            _gideon_dispatcher_instance = False
            return None


def get_lyra_classifier() -> Any | None:
    """Lazily load an optional Lyra model only after the VRAM safety checks."""
    global _lyra_instance
    if _lyra_instance is not None:
        return _lyra_instance
    if not lyra_enabled():
        return None
    model_path = os.environ.get("GIDEON_LYRA_MODEL")
    if not model_path:
        return None
    try:
        import laya  # type: ignore[import-not-found]
        _lyra_instance = laya.load(model_path)
        return _lyra_instance
    except Exception:
        logger.exception("Unable to load optional Lyra classifier")
        return None


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


CONFIRMATION_EXPIRY_S: float = 20.0

_AFFIRMATIVE_REPLIES = frozenset({
    "yes", "yeah", "yep", "confirm", "confirmed", "do it",
    "go ahead", "proceed", "affirmative", "yes do it", "yes please",
})

_NEGATIVE_REPLIES = frozenset({
    "no", "nope", "nah", "cancel", "cancelled", "canceled", "stop",
    "abort", "never mind", "nevermind", "don't", "dont", "do not",
    "no thanks", "no thank you",
})

_CONFIRMATION_QUESTIONS: dict[str, str] = {
    "system_shutdown": "Shut down the system? Say yes to confirm.",
    "system_reboot": "Reboot the system? Say yes to confirm.",
    "system_logout": "Log out of the session? Say yes to confirm.",
    "omarchy_update": "Update system packages? Say yes to confirm.",
    "window_close": "Close the active window? Say yes to confirm.",
}


def _normalize_confirmation_reply(text: str) -> str:
    """Normalize an utterance for whole-utterance confirmation matching."""
    if not isinstance(text, str):
        return ""
    cleaned = text.strip().lower()
    cleaned = re.sub(r"^(?:hey\s+)?(?:gideon|jarvis)\s*[,:]?\s*", "", cleaned)
    cleaned = re.sub(r"[^\w\s']", " ", cleaned)
    return " ".join(cleaned.split())


def is_affirmative_reply(text: str) -> bool:
    """Return True only when the entire normalized utterance is an explicit affirmative."""
    return _normalize_confirmation_reply(text) in _AFFIRMATIVE_REPLIES


def is_negative_reply(text: str) -> bool:
    """Return True when the entire normalized utterance is an explicit cancellation/refusal."""
    return _normalize_confirmation_reply(text) in _NEGATIVE_REPLIES


def format_confirmation_question(intent_id: str) -> str:
    """Derive a short human confirmation question from the intent ID."""
    if intent_id in _CONFIRMATION_QUESTIONS:
        return _CONFIRMATION_QUESTIONS[intent_id]
    human = intent_id.replace("_", " ").strip().capitalize()
    return f"{human}? Say yes to confirm."


class PendingConfirmation:
    """Represents a single pending confirmation for a confirm/dangerous intent."""
    __slots__ = ("utterance", "intent_id", "params", "risk_tier", "created_at", "expiry_s")

    def __init__(
        self,
        utterance: str,
        intent_id: str,
        params: dict[str, Any] | None = None,
        risk_tier: str = "confirm",
        created_at: float | None = None,
        expiry_s: float = CONFIRMATION_EXPIRY_S,
    ) -> None:
        self.utterance = utterance
        self.intent_id = intent_id
        self.params = dict(params or {})
        self.risk_tier = risk_tier
        self.created_at = time.monotonic() if created_at is None else created_at
        self.expiry_s = expiry_s

    def is_expired(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return (current - self.created_at) >= self.expiry_s


class PendingConfirmationStore:
    """Holds at most one active pending confirmation with automatic expiry."""
    def __init__(self, expiry_s: float = CONFIRMATION_EXPIRY_S) -> None:
        self.default_expiry_s = expiry_s
        self._pending: PendingConfirmation | None = None

    def set(
        self,
        utterance: str,
        intent_id: str,
        params: dict[str, Any] | None = None,
        risk_tier: str = "confirm",
        created_at: float | None = None,
        expiry_s: float | None = None,
    ) -> PendingConfirmation:
        exp = self.default_expiry_s if expiry_s is None else expiry_s
        self._pending = PendingConfirmation(
            utterance=utterance,
            intent_id=intent_id,
            params=params,
            risk_tier=risk_tier,
            created_at=created_at,
            expiry_s=exp,
        )
        return self._pending

    def get(self, now: float | None = None) -> PendingConfirmation | None:
        if self._pending is None:
            return None
        if self._pending.is_expired(now=now):
            self._pending = None
            return None
        return self._pending

    def pop(self, now: float | None = None) -> tuple[PendingConfirmation | None, bool]:
        """Remove and return (pending, was_expired)."""
        if self._pending is None:
            return None, False
        pending = self._pending
        self._pending = None
        if pending.is_expired(now=now):
            return None, True
        return pending, False

    def clear(self) -> None:
        self._pending = None


_pending_confirmation_store = PendingConfirmationStore()


def get_pending_confirmation_store() -> PendingConfirmationStore:
    return _pending_confirmation_store


async def _set_reflex_bus_state(bus: Any, label_text: str, *, preserve_hotmic: bool = False) -> None:
    if bus is None:
        return
    if preserve_hotmic:
        if type(bus).__name__ == "SignalBus":
            await _await_if_needed(bus.set_state("hotmic", f"⚡ BGE: {label_text}"))
        return
    if type(bus).__name__ == "SignalBus":
        label = f"⚡ BGE: {label_text}"
        await _await_if_needed(bus.set_state("bge_reflex", label))
        async def _revert_tier1_idle():
            await asyncio.sleep(2.0)
            if getattr(bus, "current_state", "") == "bge_reflex":
                await _await_if_needed(bus.set_state("idle"))
        task = asyncio.create_task(_revert_tier1_idle())
        _GRAPH_TASKS.add(task)
        task.add_done_callback(_GRAPH_TASKS.discard)
    else:
        await _await_if_needed(bus.set_state("idle"))


async def _arm_hot_mic_if_available(bus: Any, timeout_s: float = CONFIRMATION_EXPIRY_S) -> None:
    if bus is None:
        return
    arm = getattr(bus, "arm_hot_mic", None)
    if callable(arm):
        try:
            sig = inspect.signature(arm)
            if "timeout_s" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                await _await_if_needed(arm(timeout_s=timeout_s))
            elif len(sig.parameters) >= 1:
                await _await_if_needed(arm(timeout_s))
            else:
                await _await_if_needed(arm())
        except (ValueError, TypeError):
            await _await_if_needed(arm(timeout_s=timeout_s))


async def try_reflex(
    text: str, bus: Any = None, tts: Any = None,
    set_engine: Callable[[str], Any] | None = None,
) -> bool:
    """Execute a matching deterministic reflex and report whether it handled it."""
    started = time.perf_counter()
    if not isinstance(text, str) or not text.strip():
        return False

    # 1. Check for dismissal first ('bye bye' always wins, clears any pending confirmation silently)
    action = parse_command(text)
    if action is not None and action.get("intent") == "dismiss":
        _pending_confirmation_store.clear()
        disarm = getattr(bus, "dismiss_hot_mic", None) if bus is not None else None
        if callable(disarm):
            await _await_if_needed(disarm())
        if tts is not None:
            interrupt = getattr(tts, "interrupt", None)
            if callable(interrupt):
                await _await_if_needed(interrupt())
        if bus is not None:
            await _await_if_needed(bus.set_state("idle"))
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("[Tier-0 Reflex] dismiss (%.1fms): back to idle", elapsed_ms)
        return True

    # 2. Check for pending confirmation before running Tier-0 or BGE
    pending, _was_expired = _pending_confirmation_store.pop()
    cancelled_pending = False
    if pending is not None:
        if is_affirmative_reply(text):
            dispatcher = get_gideon_dispatcher()
            if dispatcher is None:
                return False
            try:
                if hasattr(dispatcher, "dispatch_confirmed"):
                    res = await dispatcher.dispatch_confirmed(
                        intent_id=pending.intent_id,
                        params=pending.params,
                        utterance=pending.utterance,
                        risk_tier=pending.risk_tier,
                    )
                else:
                    res = await dispatcher.dispatch(pending.utterance, confirmed=True)
                if res.ok and not getattr(res, "needs_confirmation", False):
                    if tts is not None:
                        play = getattr(tts, "play_sound", None)
                        if play:
                            await _await_if_needed(play("acknowledged.wav"))
                        speak = getattr(tts, "speak", None)
                        if speak and res.message:
                            await _await_if_needed(speak(res.message))
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    logger.info("[Tier-1 Confirmed] %s (%.1fms): %s", pending.intent_id, elapsed_ms, res.message)
                    await _set_reflex_bus_state(bus, res.message or pending.intent_id)
                    return True
                logger.warning("Tier-1 confirmed execution for %s failed: %s", pending.intent_id, getattr(res, "error", None))
                if bus is not None:
                    await _await_if_needed(bus.set_state("idle"))
                return True
            except Exception:
                logger.exception("Tier-1 confirmed execution failed for %s", pending.intent_id)
                if bus is not None:
                    await _await_if_needed(bus.set_state("idle"))
                return True

        logger.info("[Tier-1 Confirmation] Cancelled pending %s on reply: %r", pending.intent_id, text[:80])
        if is_negative_reply(text):
            if tts is not None:
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak("Cancelled."))
            if bus is not None:
                await _await_if_needed(bus.set_state("idle"))
            return True
        cancelled_pending = True

    async def _speak_cancelled_if_needed() -> None:
        nonlocal cancelled_pending
        if cancelled_pending:
            cancelled_pending = False
            if tts is not None:
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak("Cancelled."))

    if action is None:
        if _UNSAFE_CONTEXT.search(text):
            await _speak_cancelled_if_needed()
            return False
        # Conversation about a play/performance must not be classified as a
        # media command. BGE is trained on atomic commands and over-fires on
        # "play" in short conversational replies.
        if _looks_conversational(text):
            logger.info("[Tier-1 bypass] conversational utterance routed to LLM: %r", text[:120])
            await _speak_cancelled_if_needed()
            return False
        # Tier-1: Neural Reflex Dispatcher (GIDEON v2 BGE-small INT8 ONNX sequence classification)
        dispatcher = get_gideon_dispatcher()
        if dispatcher is not None:
            try:
                res = await dispatcher.dispatch(text)
                intent = res.data.get("intent") if res.data else None
                passthroughs = {"conversation", "hard_goal", "web_search", "browser_use_passthrough"}
                # Only intercept if it's a valid local deterministic action and not escalated to LLM
                if res.ok and intent and intent not in passthroughs and not res.data.get("escalated"):
                    if getattr(res, "needs_confirmation", False):
                        risk_tier = (res.data or {}).get("risk_tier", "confirm")
                        params = (res.data or {}).get("params") or {}
                        _pending_confirmation_store.set(
                            utterance=text,
                            intent_id=intent,
                            params=params,
                            risk_tier=risk_tier,
                        )
                        question = format_confirmation_question(intent)
                        if tts is not None:
                            speak = getattr(tts, "speak", None)
                            if speak:
                                await _await_if_needed(speak(question))
                        await _arm_hot_mic_if_available(bus, timeout_s=CONFIRMATION_EXPIRY_S)
                        elapsed_ms = (time.perf_counter() - started) * 1000
                        logger.info("[Tier-1 Confirmation Needed] %s (%s, %.1fms): %s", intent, risk_tier, elapsed_ms, question)
                        await _set_reflex_bus_state(bus, question, preserve_hotmic=True)
                        return True
                    await _speak_cancelled_if_needed()
                    if intent == "engine_switch":
                        target_engine = res.data.get("params", {}).get("engine", "codex")
                        if target_engine in ("antigravity", "anti-gravity", "anti gravity", "gemini"):
                            target_engine = "agy"
                        elif target_engine in ("gpt-6-luna", "luna", "gpt"):
                            target_engine = "codex"
                        if target_engine not in ("codex", "agy"):
                            logger.info("Tier-1 engine_switch: unsupported engine %r; only Codex/Antigravity supported", target_engine)
                            res.message = "Only Codex and Antigravity engines are supported."
                        elif set_engine is not None:
                            switched = await _await_if_needed(set_engine(target_engine))
                            label = "Codex" if target_engine == "codex" else "Antigravity"
                            res.message = f"Switched engine to {label}." if switched else f"Couldn't switch engine to {label}."
                        else:
                            active = getattr(bus, "engine_name", "codex") if bus is not None else "codex"
                            active_label = "Antigravity" if active == "agy" else "Codex"
                            target_label = "Antigravity" if target_engine == "agy" else "Codex"
                            res.message = (
                                f"{target_label} is already the active engine."
                                if active == target_engine
                                else f"Engine switcher is unavailable; staying on {active_label}."
                            )
                    if tts is not None:
                        play = getattr(tts, "play_sound", None)
                        if play:
                            await _await_if_needed(play("acknowledged.wav"))
                        speak = getattr(tts, "speak", None)
                        if speak and res.message:
                            await _await_if_needed(speak(res.message))
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    logger.info("[Tier-1 Neural Reflex] %s (%.1fms): %s", intent, elapsed_ms, res.message)
                    await _set_reflex_bus_state(bus, res.message or intent)
                    return True
            except Exception:
                logger.exception("Tier-1 neural reflex failed; falling through to LLM")
        await _speak_cancelled_if_needed()
        return False

    await _speak_cancelled_if_needed()
    intent = action["intent"]
    speech_msg: str | None = None
    try:
        if intent == "graph_query":
            query = action["query"]
            task = asyncio.create_task(_run_graph_query(query, bus=bus, tts=tts))
            _GRAPH_TASKS.add(task)
            task.add_done_callback(_GRAPH_TASKS.discard)
            logger.info("[Tier-0 Reflex] %s (launched): %s", intent, action["summary"])
            return True
        if intent == "flash_note":
            destination = Path(os.environ.get("GIDEON_NOTES_FILE", str(Path.home() / "notes" / "daily_ledger.md"))).expanduser()
            destination.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with destination.open("a", encoding="utf-8") as file:
                file.write(f"- **[{stamp}]** {action['body']}\n")
            summary = "note saved"
            speech_msg = "Note saved."
        elif intent == "focus_start":
            from focus_mode import get_focus_manager
            mgr = get_focus_manager(bus=bus, tts=tts)
            res = mgr.start(duration_minutes=action.get("minutes"), goal=action.get("goal", ""))
            mins = int(round(res.get("duration_minutes", 25)))
            summary = f"focus {mins}m"
            speech_msg = res["message"]
        elif intent == "focus_stop":
            from focus_mode import get_focus_manager
            res = get_focus_manager(bus=bus, tts=tts).stop()
            summary = "focus stopped"
            speech_msg = res["message"]
        elif intent == "focus_status":
            from focus_mode import get_focus_manager
            res = get_focus_manager(bus=bus, tts=tts).status()
            summary = "focus status"
            speech_msg = res["message"]
        elif intent == "debug_bundle":
            from gideon_debug import collect_debug_bundle
            out_path, _bundle_summary = await asyncio.to_thread(collect_debug_bundle)
            summary = f"debug bundle {out_path.name}"
            speech_msg = f"Debug bundle saved to {out_path.parent.name} as {out_path.name}."
            logger.info("[DebugBundle] %s", out_path)
        elif intent == "media_control":
            from handlers_system import run_media_action
            result = await run_media_action(action["action"])
            if not result.ok:
                logger.warning("Tier-0 media command failed: %s", result.error)
                return False
            summary = action["summary"]
        elif intent == "app_launch":
            from gideon_handler import INTENT_DISPATCH_MAP, TIER0_EXTRA_HANDLERS
            app_intent = action["app"]
            handler = INTENT_DISPATCH_MAP.get(app_intent) or TIER0_EXTRA_HANDLERS.get(app_intent)
            if not handler:
                return False
            result = await handler({}, raw_text=text)
            if not result.ok:
                logger.warning("Tier-0 app launch failed: %s", result.error)
                return False
            summary = result.message
            speech_msg = summary
        elif intent == "music_play":
            if action.get("kind") == "any":
                argv = [MUSIC_BIN, "play", "--shuffle", "--limit", "50"]
            else:
                argv = [MUSIC_BIN, "play", f"--{action['kind']}", action["value"]]
            status, output = await run_fixed(
                argv,
                timeout_s=6.0,
            )
            if status != 0:
                logger.warning("Tier-0 music play failed (%s): %s", status, output)
                return False
            summary = action["summary"]
        else:
            status, output = await run_fixed(action["argv"])
            if status != 0:
                logger.warning("Tier-0 command failed (%s): %s", status, output)
                return False
            summary = action["summary"]
        if tts is not None:
            play = getattr(tts, "play_sound", None)
            if play:
                await _await_if_needed(play("acknowledged.wav"))
            if speech_msg:
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak(speech_msg))
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("[Tier-0 Reflex] %s (%.1fms): %s", intent, elapsed_ms, summary)
        if bus is not None:
            if type(bus).__name__ == "SignalBus":
                label = f"⚡ BGE: {summary}"
                await _await_if_needed(bus.set_state("bge_reflex", label))
                async def _revert_tier0_idle():
                    await asyncio.sleep(2.0)
                    if getattr(bus, "current_state", "") == "bge_reflex":
                        await _await_if_needed(bus.set_state("idle"))
                task = asyncio.create_task(_revert_tier0_idle())
                _GRAPH_TASKS.add(task)
                task.add_done_callback(_GRAPH_TASKS.discard)
            else:
                await _await_if_needed(bus.set_state("idle"))
        return True
    except Exception:
        logger.exception("Tier-0 reflex failed")
        return False


async def dispatch(
    text: str, engine: Any, bus: Any = None, tts: Any = None,
    set_engine: Callable[[str], Any] | None = None, *, delegate: bool = True,
) -> bool:
    """Run Tier-0 first; optionally delegate unmatched prompts to the engine."""
    if await try_reflex(text, bus=bus, tts=tts, set_engine=set_engine):
        return True
    if delegate:
        if bus is not None and type(bus).__name__ == "SignalBus":
            await _await_if_needed(bus.set_state("conversing", "LLM: Thinking…"))
        await _await_if_needed(engine.send_prompt(text))
    return False
