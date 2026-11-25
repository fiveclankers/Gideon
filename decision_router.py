"""Gemini Decision 1 Tier-1 intent router for GIDEON.

Drop-in replacement for the local BGE-small dispatcher. The ONNX session is
never created until a Decision 1 call actually fails, so in normal operation
BGE-small stays completely cold (no model load, no tokenizer load).

Wire contract (no training or calibration involved)::

    POST https://generativelanguage.googleapis.com/v1beta/decisions
    x-goog-api-key: <key from GEMINI_API_KEY / GEMINI_API_KEYS>
    {"model": "gemini-decision-1", "input": <text or typed blocks>,
     "questions": {"intent": {"type": "choice", "instructions": "...",
                              "criteria": {<intent_id>: <description>, ...}}}}

Secrets are read from the environment only. Nothing is logged except status.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx

logger = logging.getLogger("gideon.decision_router")

DECISIONS_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
DECISION_MODEL = "gemini-decision-1"
DEFAULT_TIMEOUT_S = 2.0
SCHEMA_PATH = Path(__file__).resolve().parent / "intents_schema.json"
_INTENT_INSTRUCTIONS = "Pick the best intent for the voice command."

# Hot-reloadable key pool file (one key per line; ``#`` starts a comment).
DEFAULT_KEYS_FILE = Path.home() / ".config" / "gideon" / "gemini_keys.txt"

# Rate-limit backoff. 62s covers a rolling 1-minute TPM window plus margin.
DEFAULT_COOLDOWN_S = 62.0
RETRY_AFTER_MIN_S = 5.0
RETRY_AFTER_MAX_S = 3600.0
_COOLDOWN_ESCALATION_S = (62.0, 180.0, 600.0)
_MAX_COMPACT_CRITERIA_CHARS = 55

# One shared BGE-small instance across every Decision 1 dispatcher in-process.
_shared_bge: Any = None
_shared_bge_lock = threading.Lock()


class DecisionAPIError(RuntimeError):
    """Decision 1 request failed or returned an unusable payload."""


def gemini_keys_file_path() -> Optional[Path]:
    """Resolve the optional key-pool file, or ``None`` when disabled."""
    raw = os.environ.get("GIDEON_GEMINI_KEYS_FILE")
    if raw is not None:
        cleaned = raw.strip()
        if not cleaned:  # explicitly disabled
            return None
        return Path(cleaned).expanduser()
    return DEFAULT_KEYS_FILE


def _read_keys_file(path: Path) -> Tuple[str, ...]:
    """Parse one-key-per-line file; blank lines and ``#`` comments are ignored."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    keys: list[str] = []
    for line in text.splitlines():
        # ``#`` may start a full-line comment or trail a key inline.
        candidate = line.split("#", 1)[0].strip()
        if candidate:
            keys.append(candidate)
    return tuple(keys)


def gemini_api_keys() -> Tuple[str, ...]:
    """Read API keys from the key file and the environment; preserve order.

    Sources are merged as key file -> ``GEMINI_API_KEY`` -> ``GEMINI_API_KEYS``
    with duplicates dropped so a key listed twice is only rotated once.
    """
    candidates: list[str] = []
    keys_file = gemini_keys_file_path()
    if keys_file is not None:
        candidates.extend(_read_keys_file(keys_file))
    single = os.environ.get("GEMINI_API_KEY", "")
    if single.strip():
        candidates.append(single.strip())
    for part in os.environ.get("GEMINI_API_KEYS", "").split(","):
        if part.strip():
            candidates.append(part.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for key in candidates:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return tuple(unique)


def compact_criteria_enabled() -> bool:
    """Resolve ``GIDEON_DECISION_COMPACT_CRITERIA`` (default: enabled)."""
    raw = os.environ.get("GIDEON_DECISION_COMPACT_CRITERIA")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# Hand-tuned telegraphic labels. Each keeps the distinction that separates it
# from its closest sibling (up/down/set, mute/unmute/toggle, prev/next/switch).
_COMPACT_CRITERIA: Dict[str, str] = {
    "audio_volume_set": "Volume exact %",
    "audio_volume_up": "Volume up step",
    "audio_volume_down": "Volume down step",
    "audio_mute": "Mute audio",
    "audio_unmute": "Unmute audio",
    "audio_mute_toggle": "Toggle mute",
    "audio_mic_mute": "Mute mic",
    "audio_mic_unmute": "Unmute mic",
    "audio_mic_mute_toggle": "Toggle mic mute",
    "audio_output_headphones": "Headphones out",
    "audio_output_speakers": "Speakers out",
    "media_play": "Resume playback",
    "media_pause": "Pause playback",
    "media_play_pause": "Play/pause toggle",
    "media_next": "Next track",
    "media_previous": "Previous track",
    "media_stop": "Stop playback",
    "brightness_up": "Brightness up",
    "brightness_down": "Brightness down",
    "brightness_set": "Brightness exact %",
    "window_close": "Close window",
    "window_fullscreen_toggle": "Toggle fullscreen",
    "window_focus_left": "Focus left",
    "window_focus_right": "Focus right",
    "window_focus_up": "Focus up",
    "window_focus_down": "Focus down",
    "window_floating_toggle": "Toggle floating",
    "workspace_switch": "Workspace 1-10",
    "workspace_previous": "Last workspace",
    "workspace_next": "Next workspace",
    "workspace_prev": "Prior workspace",
    "launch_browser": "Launch Chrome",
    "launch_brave": "Launch Brave",
    "launch_spotify": "Launch Spotify",
    "launch_terminal": "Launch Kitty",
    "launch_obsidian": "Launch Obsidian",
    "launch_code": "Launch VS Code",
    "launch_file_manager": "Launch Thunar",
    "omarchy_theme_list": "List themes",
    "omarchy_theme_set": "Set theme",
    "omarchy_theme_next": "Next theme",
    "omarchy_theme_dark": "Dark mode theme",
    "omarchy_theme_light": "Light mode theme",
    "omarchy_wallpaper_next": "Next wallpaper",
    "omarchy_wallpaper_set": "Wallpaper path",
    "omarchy_keybindings": "Keybindings",
    "omarchy_screenshot": "Region screenshot",
    "omarchy_screenshot_full": "Full screenshot",
    "omarchy_update": "Update system",
    "system_status": "CPU, mem, uptime",
    "system_lock": "Lock session",
    "system_logout": "Log out",
    "system_reboot": "Reboot",
    "system_shutdown": "Shutdown system",
    "study_queue_view": "Due queue",
    "study_status_view": "Study burndown",
    "study_doctor": "Verify ledgers",
    "study_traps_view": "Chapter traps",
    "study_sprint_start": "Start study sprint",
    "flash_note_add": "Note to vault",
    "engine_switch": "LLM engine",
    "conversation": "Chat, chit-chat, science",
    "hard_goal": "Complex coding, scripts",
    "web_search": "Live news, scores",
    "browser_use_passthrough": "Click, fill forms",
}



def _generic_compact(description: str) -> str:
    """Fallback shortening for intents without a hand-tuned one-liner."""
    text = re.sub(r"\([^)]*\)", " ", description)
    text = re.sub(r"^\s*pass-?through\s*:\s*", "", text, flags=re.I)
    text = text.split("->")[0]
    text = " ".join(text.split()).strip(" -;,")
    if len(text) > _MAX_COMPACT_CRITERIA_CHARS:
        text = text[: _MAX_COMPACT_CRITERIA_CHARS + 1].rsplit(" ", 1)[0].rstrip(" ,;-")
    return text or description.strip()[:_MAX_COMPACT_CRITERIA_CHARS]


def _compact_description(intent_id: str, description: str) -> str:
    return _COMPACT_CRITERIA.get(intent_id) or _generic_compact(description)



def select_tier1_mode() -> str:
    """Resolve ``GIDEON_TIER1_ROUTER`` to ``"bge"`` or ``"decision"``.

    ``auto`` (the default) picks Decision 1 when any API key is configured
    (environment or the hot-reloadable keys file) and the local BGE-small
    router otherwise. Unknown values fall back to ``auto``.
    Explicit ``decision`` without a key is allowed: the dispatcher cold-loads
    BGE-small on its first prediction.
    """
    mode = os.environ.get("GIDEON_TIER1_ROUTER", "auto").strip().lower()
    if mode not in {"bge", "decision", "auto"}:
        logger.warning("Unknown GIDEON_TIER1_ROUTER=%r; using 'auto'", mode)
        mode = "auto"
    if mode == "auto":
        return "decision" if gemini_api_keys() else "bge"
    return mode


def load_intent_criteria(
    schema_path: Path | str = SCHEMA_PATH, compact: Optional[bool] = None
) -> Dict[str, str]:
    """Load all intent descriptions from ``intents_schema.json``.

    ``compact`` (default from ``GIDEON_DECISION_COMPACT_CRITERIA``) shortens
    every description into a high-signal phrase, cutting the serialized
    ``questions`` payload roughly in half for extra TPM headroom.
    """
    if compact is None:
        compact = compact_criteria_enabled()
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    criteria: Dict[str, str] = {}
    for intent_id, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        description = str(meta.get("description", "")).strip()
        key = str(intent_id)
        criteria[key] = _compact_description(key, description) if compact else description
    return criteria


class GeminiDecisionDispatcher:
    """Decision 1 classifier with the BGE-small dispatcher as cold fallback."""

    def __init__(
        self,
        api_keys: Optional[Iterable[str]] = None,
        schema_path: Path | str = SCHEMA_PATH,
        timeout_s: Optional[float] = None,
        client: Optional[httpx.Client] = None,
        compact_criteria: Optional[bool] = None,
    ) -> None:
        # Explicit keys pin the pool; otherwise the keys file is hot-reloadable.
        self._explicit_keys = api_keys is not None
        self._keys_file: Optional[Path] = None if self._explicit_keys else gemini_keys_file_path()
        if api_keys is None:
            api_keys = gemini_api_keys()
        self.api_keys: Tuple[str, ...] = tuple(k.strip() for k in api_keys if k and k.strip())
        self._key_index = 0
        self._key_lock = threading.Lock()
        self._cooldown_until: Dict[str, float] = {}
        self._rate_limit_strikes: Dict[str, int] = {}
        self._keys_file_sig = self._keys_file_signature()
        self._bge_lock = threading.Lock()
        self.schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        self.criteria = load_intent_criteria(schema_path, compact=compact_criteria)
        raw_timeout = os.environ.get("GIDEON_DECISION_TIMEOUT_S")
        self.timeout_s = float(timeout_s if timeout_s is not None else (raw_timeout or DEFAULT_TIMEOUT_S))
        self._client = client if client is not None else httpx.Client(
            timeout=httpx.Timeout(self.timeout_s),
            headers={"Content-Type": "application/json"},
        )
        self._owns_client = client is None
        self._last_prediction: Optional[Tuple[str, str, float, float]] = None
        self._bge: Any = None
        self.fallback_count = 0

    def __repr__(self) -> str:  # never leak keys via logs/reprs
        return (
            f"GeminiDecisionDispatcher(keys={len(self.api_keys)}, "
            f"intents={len(self.criteria)}, timeout_s={self.timeout_s}, "
            f"fallbacks={self.fallback_count})"
        )

    def close(self) -> None:
        """Close the persistent keep-alive client (no-op for injected clients)."""
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # Hot-reloadable key pool
    # ------------------------------------------------------------------
    def _keys_file_signature(self) -> Optional[Tuple[bool, int, int]]:
        """Cheap ``(exists, mtime_ns, size)`` stamp used to detect file edits."""
        path = self._keys_file
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return (False, 0, 0)
        return (True, stat.st_mtime_ns, stat.st_size)

    def _refresh_keys(self) -> None:
        """Reload the key pool when the keys file was created, edited or deleted."""
        path = self._keys_file
        if path is None:
            return
        if self._keys_file_signature() == self._keys_file_sig:
            return
        with self._key_lock:
            signature = self._keys_file_signature()
            if signature == self._keys_file_sig:
                return
            self._keys_file_sig = signature
            fresh = gemini_api_keys()
            self.api_keys = fresh
            live = set(fresh)
            # Preserve cooldowns/strikes for surviving keys, prune removed ones.
            for tracked in list(self._cooldown_until):
                if tracked not in live:
                    del self._cooldown_until[tracked]
            for tracked in list(self._rate_limit_strikes):
                if tracked not in live:
                    del self._rate_limit_strikes[tracked]
            self._key_index = self._key_index % len(fresh) if fresh else 0
        logger.info("Reloaded Gemini key pool from disk (%d key(s))", len(fresh))

    def key_pool_status(self) -> Dict[str, Any]:
        """Return aggregate pool health. Never exposes key material."""
        self._refresh_keys()
        with self._key_lock:
            now = time.monotonic()
            cooling = [
                key for key in self.api_keys if self._cooldown_until.get(key, 0.0) > now
            ]
            total = len(self.api_keys)
            next_recovery = min(
                (self._cooldown_until[key] - now for key in cooling), default=0.0
            )
        return {
            "total_keys": total,
            "available_keys": total - len(cooling),
            "cooling_down_keys": len(cooling),
            "next_recovery_s": round(max(next_recovery, 0.0), 3),
        }

    def _take_key(self, attempted: set[str]) -> Optional[str]:
        """Pick the next usable key, skipping cooled-down ones for this request."""
        with self._key_lock:
            keys = self.api_keys
            if not keys:
                return None
            now = time.monotonic()
            start = self._key_index % len(keys)
            for offset in range(len(keys)):
                index = (start + offset) % len(keys)
                key = keys[index]
                if key in attempted:
                    continue
                if self._cooldown_until.get(key, 0.0) > now:
                    continue
                self._key_index = (index + 1) % len(keys)
                return key
        return None

    def _record_success(self, key: str) -> None:
        with self._key_lock:
            self._rate_limit_strikes.pop(key, None)
            self._cooldown_until.pop(key, None)

    def _record_rate_limit(self, key: str, response: Any) -> float:
        """Cool ``key`` down, honouring ``Retry-After`` and escalating streaks."""
        header: Any = None
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                header = headers.get("Retry-After")
            except Exception:  # exotic/mocked header objects
                header = None
        with self._key_lock:
            strikes = self._rate_limit_strikes.get(key, 0) + 1
            self._rate_limit_strikes[key] = strikes
            retry_after: Optional[float] = None
            if header is not None and str(header).strip():
                try:
                    retry_after = float(str(header).strip())
                    if not math.isfinite(retry_after):
                        retry_after = None
                except (TypeError, ValueError):
                    retry_after = None
            if retry_after is not None:
                cooldown = min(max(retry_after, RETRY_AFTER_MIN_S), RETRY_AFTER_MAX_S)
            else:
                step = min(strikes - 1, len(_COOLDOWN_ESCALATION_S) - 1)
                cooldown = _COOLDOWN_ESCALATION_S[step]
            self._cooldown_until[key] = time.monotonic() + cooldown
        return cooldown

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Post with key rotation; cooled-down keys are never re-hit."""
        self._refresh_keys()
        attempted: set[str] = set()
        last_error: Optional[BaseException] = None
        while True:
            key = self._take_key(attempted)
            if key is None:
                break
            attempted.add(key)
            try:
                response = self._client.post(
                    DECISIONS_URL,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=payload,
                )
            except Exception as exc:  # timeouts, DNS/connect failures, mocked errors
                last_error = exc
                logger.warning("Decision 1 transport error (%s)", type(exc).__name__)
                continue
            status = getattr(response, "status_code", None)
            if status == 200:
                self._record_success(key)
                try:
                    data = response.json()
                except Exception as exc:
                    last_error = exc
                    logger.warning("Decision 1 returned unreadable JSON: %s", exc)
                    continue
                if not isinstance(data, dict):
                    last_error = DecisionAPIError("Decision 1 response is not an object")
                    continue
                return data
            if status == 429:
                cooldown = self._record_rate_limit(key, response)
                last_error = DecisionAPIError("Decision 1 HTTP 429")
                logger.warning("Decision 1 HTTP 429; key cooling down %.0fs", cooldown)
                continue
            if isinstance(status, int) and 500 <= status < 600:
                last_error = DecisionAPIError(f"Decision 1 HTTP {status}")
                logger.warning("Decision 1 HTTP %s; rotating key", status)
                continue
            raise DecisionAPIError(f"Decision 1 HTTP {status}")
        if not self.api_keys:
            raise DecisionAPIError("no Gemini API key configured")
        if not attempted:
            raise DecisionAPIError("all Gemini API keys are in cooldown")
        raise DecisionAPIError("all Gemini API keys failed") from last_error


    # ------------------------------------------------------------------
    # Drop-in dispatcher interface
    # ------------------------------------------------------------------
    def _get_bge(self) -> Any:
        """Cold-load BGE-small on first fallback only (shared across instances)."""
        global _shared_bge
        with self._bge_lock, _shared_bge_lock:
            if self._bge is None:
                if _shared_bge is None:
                    from gideon_handler import GideonDispatcher

                    _shared_bge = GideonDispatcher()
                    logger.info("Cold-loaded Tier-1 BGE-small fallback dispatcher")
                self._bge = _shared_bge
            return self._bge

    @staticmethod
    def _probability(value: Any) -> float:
        probability = float(value)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise DecisionAPIError("invalid probability")
        return probability

    @staticmethod
    def _as_probability_dict(raw: Any, labels: Any = None) -> Dict[str, float]:
        if isinstance(raw, dict):
            return {str(k): GeminiDecisionDispatcher._probability(v) for k, v in raw.items()}
        probs: Dict[str, float] = {}
        for index, value in enumerate([] if raw is None else raw):
            label = getattr(labels, "get", lambda _i, _d=None: None)(index)
            probs[str(label if label is not None else index)] = GeminiDecisionDispatcher._probability(value)
        return probs

    def _fallback_predict(self, text: str) -> Tuple[str, float, Dict[str, float]]:
        self.fallback_count += 1
        bge = self._get_bge()
        result = bge.predict(text)
        intent, confidence = str(result[0]), float(result[1])
        probs = self._as_probability_dict(result[2], getattr(bge, "id2label", None))
        self._last_prediction = (text, intent, confidence, time.monotonic())
        return intent, confidence, probs

    @staticmethod
    def _choice_payload(text: str) -> Dict[str, Any]:
        return {
            "model": DECISION_MODEL,
            "input": text,
            "questions": {
                "intent": {
                    "type": "choice",
                    "instructions": _INTENT_INSTRUCTIONS,
                    "criteria": {},  # filled per instance from intents_schema.json
                }
            },
        }

    def predict(self, text: str) -> Tuple[str, float, Dict[str, float]]:
        """Return ``(intent_id, confidence, probabilities)`` like BGE-small."""
        payload = self._choice_payload(text)
        payload["questions"]["intent"]["criteria"] = dict(self.criteria)
        try:
            data = self._post(payload)
            answer = data["answers"]["intent"]
            if answer.get("type") != "choice":
                raise DecisionAPIError(f"unexpected answer type: {answer.get('type')!r}")
            intent = str(answer["choice"])
            if intent not in self.criteria:
                raise DecisionAPIError("choice is not a configured intent")
            confidence = self._probability(answer.get("confidence", 0.0))
            probs = self._as_probability_dict(answer.get("probabilities") or {})
        except Exception as exc:
            logger.warning("Decision 1 prediction failed (%s); using BGE-small fallback", exc)
            return self._fallback_predict(text)
        self._last_prediction = (text, intent, confidence, time.monotonic())
        return intent, confidence, probs

    def peek_cached_intent(self, text: str, max_age_s: float = 10.0) -> Optional[str]:
        """Return the last predicted intent when ``text`` matches and is fresh."""
        cached = self._last_prediction
        if not cached:
            return None
        cached_text, intent, _confidence, ts = cached
        if cached_text == text and (time.monotonic() - ts) <= max_age_s:
            return intent
        return None

    def _shell(self) -> Any:
        """Wrap the shared BGE dispatch pipeline with Decision 1 prediction.

        Reuses ``GideonDispatcher.dispatch`` / ``dispatch_confirmed`` verbatim
        (negation guard, risk gate, slots, handlers) without ever constructing
        a GideonDispatcher with an ONNX session.
        """
        from gideon_handler import GideonDispatcher

        owner = self

        class _DecisionShell(GideonDispatcher):
            def __init__(self) -> None:
                self.schema = owner.schema
                self._last_prediction = None

            def predict(self, text: str) -> Tuple[str, float, Any]:
                return owner.predict(text)

            def peek_cached_intent(self, text: str, max_age_s: float = 10.0) -> Optional[str]:
                return owner.peek_cached_intent(text, max_age_s)

        return _DecisionShell()

    async def dispatch(
        self, utterance: str, dry_run: bool = False, confirmed: bool = False
    ) -> Any:
        """BGE-compatible end-to-end dispatch using Decision 1 classification."""
        return await self._shell().dispatch(utterance, dry_run=dry_run, confirmed=confirmed)

    async def dispatch_confirmed(
        self,
        intent_id: str,
        params: Optional[Dict[str, Any]] = None,
        utterance: str = "",
        risk_tier: Optional[str] = None,
    ) -> Any:
        """Execute a confirmed intent without re-running classification."""
        return await self._shell().dispatch_confirmed(
            intent_id, params=params, utterance=utterance, risk_tier=risk_tier
        )

    # ------------------------------------------------------------------
    # Browser step demonstration: one pass, text + image + three questions
    # ------------------------------------------------------------------
    def decide_browser_step(
        self,
        goal: str,
        dom_snippet: str,
        candidates: Dict[str, str],
        screenshot_png_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Choose an action and check modal/goal state in a single Decision 1 call.

        Returns a dict with ``candidate_action``, its confidence/probabilities,
        ``has_blocking_modal``, ``goal_complete`` and raw usage. Unlike intent
        prediction, this has no local BGE equivalent, so API errors propagate.
        """
        if not candidates:
            raise ValueError("candidates must contain at least one action")
        blocks: list[Dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Goal: {goal}\n\nDOM snippet:\n{dom_snippet}\n\n"
                    "Candidate actions (id: description):\n"
                    + json.dumps({k: str(v) for k, v in candidates.items()}, ensure_ascii=False)
                ),
            }
        ]
        if screenshot_png_bytes:
            blocks.append(
                {
                    "type": "image",
                    "data": base64.b64encode(bytes(screenshot_png_bytes)).decode("ascii"),
                    "mime_type": "image/png",
                }
            )
        payload = {
            "model": DECISION_MODEL,
            "input": blocks,
            "questions": {
                "candidate_action": {
                    "type": "choice",
                    "instructions": (
                        "Select the candidate action that best advances the goal "
                        "given the screenshot and DOM; avoid actions blocked by a modal."
                    ),
                    "criteria": {str(k): str(v) for k, v in candidates.items()},
                },
                "has_blocking_modal": {
                    "type": "noul",
                    "instructions": (
                        "Return the probability that a blocking modal dialog is "
                        "visible and prevents normal interaction with the page."
                    ),
                },
                "goal_complete": {
                    "type": "noul",
                    "instructions": (
                        "Return the probability that the stated goal is already "
                        "fully achieved in the screenshot and DOM."
                    ),
                },
            },
        }
        data = self._post(payload)
        answers = data.get("answers") or {}
        if not isinstance(answers, dict):
            raise DecisionAPIError("Decision 1 answers is not an object")

        def _noul(name: str) -> float:
            answer = answers.get(name) or {}
            if not isinstance(answer, dict):
                raise DecisionAPIError(f"invalid {name} answer")
            return self._probability(answer.get("noul", answer.get("confidence", 0.0)))

        choice = answers.get("candidate_action") or {}
        if not isinstance(choice, dict) or not choice.get("choice"):
            raise DecisionAPIError("Decision 1 returned no candidate_action choice")
        if str(choice["choice"]) not in {str(key) for key in candidates}:
            raise DecisionAPIError("Decision 1 choice is not a candidate action")
        return {
            "candidate_action": str(choice["choice"]),
            "confidence": self._probability(choice.get("confidence", 0.0)),
            "probabilities": self._as_probability_dict(choice.get("probabilities") or {}),
            "has_blocking_modal": _noul("has_blocking_modal"),
            "goal_complete": _noul("goal_complete"),
            "usage": data.get("usage") or {},
        }
