"""Deterministic, zero-model Tier-0 command router for GIDEON."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
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
# "open code" is exempt: it is the Antigravity/OpenCode engine name, and a bare
# "open code" must reach the engine router instead of being mistaken for coding.
_UNSAFE_CONTEXT = re.compile(
    r"\b(?:fix|bug|script|why|how|explain)\b|\bwrite\s+a\b|\bwhat\s+is\b|(?<!\bopen[\s-])\bcode\b",
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
# Spoken "switch engine" grammar for the deterministic Tier-0 router. The
# pattern is fullmatched against the normalized lowercase utterance: an
# optional politeness wrapper, a verb, an optional pronoun ("switch me to ..."),
# an optional leading role noun ("change MY ENGINE to opencode"), an optional
# preposition, the target, an optional trailing role noun ("switch to X
# ENGINE"), and an optional persistence tail ("... from now on", "... as the
# default"). Both noun-around-target orderings are accepted, the preposition is
# optional everywhere ("use X" == "use the X engine"), and the tail only fires
# on explicit persistence requests so generic "make ..." utterances without a
# target can never match.
_ENGINE_VERB_RE = (
    r"switch(?:ing)?|change(?:ing)?|swap(?:ping)?|shift(?:ing)?|move(?:ing)?|transfer(?:ring)?|"
    r"flip(?:ping)?|toggle(?:ing)?|set(?:t?ing)?|rout(?:e|ing)|redirect|migrat(?:e|ing)|hop|jump|"
    r"divert|mak(?:e|ing)|us(?:e|ing)|activat(?:e|ing)|enabl(?:e|ing)|engag(?:e|ing)|boot|"
    r"start\s+using|go\s+with|stick\s+with|keep"
)
_ENGINE_ROLE_NOMINAL_RE = (
    r"(?:(?:the|my|your|this|gideon's|jarvis's|cognitive)\s+)?"
    r"(?:(?:cognitive\s+)?(?:engine|backend|brain|model|llm|reasoner|runtime|session|assistant)\s+)?"
)
# Named groups split the two engine targets so the caller can tell them apart.
_ENGINE_TARGET_RE = (
    r"(?:(?P<target>(?:opencode|open\s+code|open-code))|(?P<target_agy>(?:antigravity|anti-gravity|agy)))"
)
_ENGINE_SWITCH_RE = re.compile(
    r"^(?:(?:can\s+you|could\s+you|would\s+you(?:\s+mind)?|please|just)\s+)?"
    r"(?:" + _ENGINE_VERB_RE + r")"
    r"(?:\s+me)?"
    r"\s*" + _ENGINE_ROLE_NOMINAL_RE +
    r"(?:to\s+|over\s+to\s+|over\s+|onto\s+|toward(?:s)?\s+)?"
    r"(?:(?:the|my|your|this)\s+)?"
    r"" + _ENGINE_TARGET_RE +
    r"(?:\s+(?:the\s+|my\s+|your\s+)?(?:cognitive\s+)?(?:engine|backend|brain|model|llm|reasoner|runtime|session|assistant))?"
    r"(?:\s+(?:from\s+now\s+on|for\s+good|for\s+the\s+(?:rest|future)|as\s+the\s+default(?:\s+engine)?|please))?"
    r"\s*$",
    re.I,
)
_WINDOW_CONTROL_MAP = {
    "focus window left": ["hyprctl", "dispatch", "movefocus", "l"],
    "focus window right": ["hyprctl", "dispatch", "movefocus", "r"],
    "focus window up": ["hyprctl", "dispatch", "movefocus", "u"],
    "focus window down": ["hyprctl", "dispatch", "movefocus", "d"],
    "toggle fullscreen": ["hyprctl", "dispatch", "fullscreen", "1"],
    "close active window": ["hyprctl", "dispatch", "killactive"],
}
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

# Farewell dismissal ("bye bye" and its polite mutations). A single "bye" is
# deliberately NOT a dismissal: ASR routinely transcribes "by" as "bye".
_DISMISS_PREFIX_FILLERS = frozenset({
    "ok", "okay", "k", "alright", "all", "right", "so", "yeah", "yep", "yup",
    "well", "good", "great", "cool", "nice", "and", "then", "now", "please",
    "thanks", "thank", "you", "thx", "cheers", "very", "much", "a", "lot",
    "i", "im", "i'm", "we", "we're", "were", "are", "is", "am", "done",
    "that", "thats", "that's", "it", "for", "hey", "gideon", "jarvis",
    "mate", "buddy",
})
_DISMISS_TRAILERS = frozenset({
    "gideon", "jarvis", "now", "then", "ok", "okay", "alright", "thanks",
    "thank", "you", "mate", "buddy", "cheers", "for", "good", "night", "to",
})

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
        if w in _NUMBER_WORDS:
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
    """True when the utterance is a farewell built on a repeated "bye bye" core.

    Accepts polite mutations ("ok, thank you, bye bye", "alright bye bye",
    "bye-bye for now") while rejecting a bare "bye", "by" or "goodbye", and
    rejecting sentences where "bye bye" is merely embedded in conversation.
    """
    tokens = [t for t in re.split(r"[^a-z']+", value.lower()) if t]
    while tokens and tokens[-1] in _DISMISS_TRAILERS:
        tokens.pop()
    if len(tokens) < 2 or tokens[-1] != "bye":
        return False
    start = len(tokens)
    while start > 0 and tokens[start - 1] == "bye":
        start -= 1
    if len(tokens) - start < 2:
        return False
    return all(t in _DISMISS_PREFIX_FILLERS for t in tokens[:start])


def parse_command(text: str) -> dict[str, Any] | None:
    """Return a structured action without importing or allocating a model."""
    if not isinstance(text, str) or not text.strip() or _UNSAFE_CONTEXT.search(text):
        return None
    raw = text.strip()
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

    if _is_dismissal(value):
        return {"intent": "dismiss", "summary": "back to idle"}

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
        # Anything else is a local-library search. This keeps "play some
        # Radiohead" out of the BGE media_play path, which resumes whatever else
        # is already playing (e.g. YouTube).
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

    engines = {
        "switch to opencode": "opencode", "use opencode engine": "opencode", "engine opencode": "opencode",
        "switch to antigravity": "agy", "switch to agy": "agy", "use antigravity engine": "agy", "engine agy": "agy",
    }
    if value in engines:
        name = engines[value]
        return {"intent": "engine_switch", "engine": name, "summary": f"switched to {'OpenCode' if name == 'opencode' else 'Antigravity'}"}
    engine_match = _ENGINE_SWITCH_RE.fullmatch(value)
    if engine_match:
        name = "opencode" if engine_match.group("target") else "agy"
        return {"intent": "engine_switch", "engine": name, "summary": f"switched to {'OpenCode' if name == 'opencode' else 'Antigravity'}"}
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


def get_gideon_dispatcher() -> Any | None:
    """Lazily load the Tier-1 BGE-small INT8 ONNX dispatcher."""
    global _gideon_dispatcher_instance
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


async def try_reflex(
    text: str, bus: Any = None, tts: Any = None,
    set_engine: Callable[[str], Any] | None = None,
) -> bool:
    """Execute a matching deterministic reflex and report whether it handled it."""
    started = time.perf_counter()
    if not isinstance(text, str) or not text.strip() or _UNSAFE_CONTEXT.search(text):
        return False
    action = parse_command(text)
    if action is None:
        # Conversation about a play/performance must not be classified as a
        # media command. BGE is trained on atomic commands and over-fires on
        # "play" in short conversational replies.
        if _looks_conversational(text):
            logger.info("[Tier-1 bypass] conversational utterance routed to LLM: %r", text[:120])
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
                    if intent == "engine_switch":
                        target_engine = res.data.get("params", {}).get("engine", "antigravity")
                        # Tier-1 BGE can surface either alias; the engine switcher
                        # only accepts {"agy", "opencode"}.
                        if target_engine in ("antigravity", "anti-gravity"):
                            target_engine = "agy"
                        if target_engine not in ("agy", "opencode"):
                            logger.warning("Tier-1 engine_switch: unknown engine %r; falling through", target_engine)
                            return False
                        if set_engine is not None:
                            if not await _await_if_needed(set_engine(target_engine)):
                                # A failed switch must not be acked as handled.
                                logger.warning("Tier-1 engine switch to %s failed; falling through", target_engine)
                                return False
                    if tts is not None:
                        play = getattr(tts, "play_sound", None)
                        if play:
                            await _await_if_needed(play("acknowledged.wav"))
                        speak = getattr(tts, "speak", None)
                        if speak and res.message:
                            await _await_if_needed(speak(res.message))
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    logger.info("[Tier-1 Neural Reflex] %s (%.1fms): %s", intent, elapsed_ms, res.message)
                    if bus is not None:
                        if type(bus).__name__ == "SignalBus":
                            label = f"⚡ BGE: {res.message or intent}"
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
                    return True
            except Exception:
                logger.exception("Tier-1 neural reflex failed; falling through to LLM")
        return False

    intent = action["intent"]
    try:
        if intent == "dismiss":
            # Farewell: drop the follow-up hot-mic window, stop any speech, and
            # settle straight back to idle without a TTS acknowledgement.
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
        elif intent == "engine_switch":
            if set_engine is None:
                return False
            await _await_if_needed(set_engine(action["engine"]))
            summary = action["summary"]
        elif intent == "media_control":
            from handlers_system import run_media_action
            result = await run_media_action(action["action"])
            if not result.ok:
                logger.warning("Tier-0 media command failed: %s", result.error)
                return False
            summary = action["summary"]
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
            if intent == "flash_note":
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak("Note saved."))
            elif intent == "engine_switch":
                speak = getattr(tts, "speak", None)
                if speak:
                    await _await_if_needed(speak("Switched to OpenCode." if action["engine"] == "opencode" else "Switched to Antigravity."))
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
            await _await_if_needed(bus.set_state("conversing", "🧠 LLM: Thinking…"))
        await _await_if_needed(engine.send_prompt(text))
    return False
