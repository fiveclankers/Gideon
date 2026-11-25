"""ACP engine for Antigravity's ``agy acp`` stdio server.

This is GIDEON's brain backend for ``--engine agy`` (engine id ``agy``).
:func:`gideon._make_engine` selects it for ``--engine agy``; the legacy
headless NDJSON engine keeps the id ``agy-headless`` as a fallback path.

Design notes
------------
* Transport: :class:`acp_transport.ACPTransport` owns JSON-RPC framing,
  correlation, and the "exactly one response per inbound request" guarantee.
  This module never re-implements framing.
* Two independent state machines:

  =============  ===========================================================
  connection     ``STOPPED -> STARTING -> READY -> RECOVERING``, terminal
                 ``CLOSING -> CLOSED``
  turn           ``IDLE -> PROMPTING -> DRAINING_TTS -> IDLE`` with
                 ``CANCELLING`` in between
  =============  ===========================================================

  Protocol idle and audible idle are different: a prompt can return while its
  sentences are still being spoken.
* ``session/cancel`` is a notification with **no id**. Its acknowledgement is
  the terminal response to the *original* ``session/prompt`` request. That is
  why :meth:`ACPAntigravityEngine.interrupt` invalidates the audible generation
  *before* it sends anything.
* The engine never replays an uncertain user prompt after a reconnect: its
  tools may already have run. A fresh session gets an explicit context-loss
  notice instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from acp_transport import ACPEOF, ACPError, ACPTransport

__all__ = [
    "ACPAntigravityEngine",
    "THOUGHT_LEVEL_CONFIG_ID",
    "MODEL_CONFIG_ID",
    "DEFAULT_ACP_MODEL",
    "DEFAULT_ACP_EFFORT",
    "DEFAULT_AGY_PROFILE_DIR",
    "DEFAULT_AGY_APP_DATA_DIR",
    "CURATED_VOICE_SKILLS",
    "EXCLUDED_SKILLS",
    "ensure_gideon_agy_home",
]

logger = logging.getLogger("gideon.acp_antigravity")

DEFAULT_AGY_PROFILE_DIR = Path.home() / ".local" / "share" / "gideon" / "agy_home"
DEFAULT_AGY_APP_DATA_DIR = "antigravity-cli"

CURATED_VOICE_SKILLS: tuple[str, ...] = (
    "gideon-doctor",
    "gideon-music",
    "omarchy",
    "diagnose-crash",
    "ask-muse",
)

EXCLUDED_SKILLS: frozenset[str] = frozenset({
    "frontend-design",
    "impeccable",
    "d3-viz",
    "motion-design",
    "thinking-orbs",
    "visual-explainer",
    "omarchy-app",
    "swarm-delegator",
    "graphify",
    "humanizer",
    "unslop",
    "agent-reach",
    "web-scrape",
    "adversarial-personal-inference",
    "study-ticktick-sync",
})

SKILL_SEARCH_DIRS: tuple[Path, ...] = (
    Path.home() / ".agents" / "skills",
    Path("/usr/share/omarchy/default/agents/skills"),
    Path.home() / ".gemini" / "config" / "skills",
    Path.home() / ".gemini" / "skills",
)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Symlink ``src`` -> ``dst`` atomically, updating stale symlinks cleanly."""
    if not src.exists():
        return
    try:
        if dst.is_symlink() and os.readlink(dst) == str(src):
            return
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        try:
            tmp.symlink_to(src)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except OSError as exc:
        logger.warning("Could not stage %s into %s: %s", src, dst, exc)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON data to path atomically using a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ensure_gideon_agy_home(
    profile_dir: Optional[Path | str] = None,
    *,
    app_data_dir: str = DEFAULT_AGY_APP_DATA_DIR,
    source_gemini_dir: Optional[Path | str] = None,
    curated_skills: Optional[tuple[str, ...] | list[str]] = None,
    search_dirs: Optional[tuple[Path, ...] | list[Path]] = None,
) -> Path:
    """Prepare Gideon's isolated, lightweight Antigravity ACP profile.

    Idempotent, runs before spawn (mirroring codex_engine.py's ensure_gideon_codex_home):
    - Default profile dir: ~/.local/share/gideon/agy_home (or GIDEON_AGY_GEMINI_DIR)
    - Symlinks OS keyring / auth credentials (oauth_creds.json, google_accounts.json,
      installation_id, state.json) from source_gemini_dir (default: ~/.gemini)
    - Writes minimal, clean settings.json in profile root and app_data_dir
    - Symlinks ONLY curated voice-essential skills (gideon-doctor, gideon-music,
      omarchy, diagnose-crash, ask-muse), pruning any bloat or excluded skills.
    - Minimal MCP set: mirrors codex_engine.py with second-brain and agent-browser if present.
    """
    home_dir = Path(
        profile_dir
        or os.environ.get("GIDEON_AGY_GEMINI_DIR")
        or DEFAULT_AGY_PROFILE_DIR
    ).expanduser().resolve()
    home_dir.mkdir(parents=True, exist_ok=True)

    app_data_path = home_dir / app_data_dir
    app_data_path.mkdir(parents=True, exist_ok=True)

    # 1. Symlink minimal auth/credential files if present
    src_dir = Path(source_gemini_dir or (Path.home() / ".gemini")).expanduser().resolve()
    for fname in ("oauth_creds.json", "google_accounts.json", "installation_id", "state.json"):
        src_file = src_dir / fname
        if src_file.exists():
            _link_or_copy(src_file, home_dir / fname)

    # 2. Write minimal settings.json in profile_dir and app_data_dir
    root_settings = {
        "security": {
            "auth": {
                "selectedType": "oauth-personal"
            }
        },
        "hooks": {
            "BeforeTool": []
        }
    }
    _atomic_write_json(home_dir / "settings.json", root_settings)

    app_settings = {
        "allowNonWorkspaceAccess": True,
        "artifactReviewPolicy": "agent-decides",
        "model": "Gemini 4 Hydrogen (Medium)",
        "notifications": False,
        "queuedMessages": "send-immediately",
        "toolPermission": "always-proceed",
        "trustedWorkspaces": [
            "/home/cat/gideon"
        ]
    }
    _atomic_write_json(app_data_path / "settings.json", app_settings)

    # 3. Clean and populate curated voice skills
    skills_dir = home_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    config_skills_dir = home_dir / "config" / "skills"
    config_skills_dir.mkdir(parents=True, exist_ok=True)

    target_skills = tuple(curated_skills if curated_skills is not None else CURATED_VOICE_SKILLS)
    source_roots = tuple(search_dirs if search_dirs is not None else SKILL_SEARCH_DIRS)

    # Prune excluded or un-curated skills from profile's skills directories
    for s_dir in (skills_dir, config_skills_dir):
        if s_dir.is_dir():
            for item in list(s_dir.iterdir()):
                if item.name in EXCLUDED_SKILLS or (target_skills and item.name not in target_skills):
                    try:
                        if item.is_symlink() or item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)
                    except OSError as exc:
                        logger.warning("Could not remove excluded skill %s: %s", item, exc)

    # Symlink only existing curated skills
    for skill_name in target_skills:
        if skill_name in EXCLUDED_SKILLS:
            continue
        found_src = None
        for root in source_roots:
            cand = root / skill_name
            if cand.exists():
                found_src = cand
                break
        if found_src is not None:
            _link_or_copy(found_src, skills_dir / skill_name)
            _link_or_copy(found_src, config_skills_dir / skill_name)

    # 4. Minimal MCP configuration (second-brain and agent-browser if present)
    second_brain_server = Path("/home/cat/Documents/Me/agent-inference/retrieval/mcp_server.py")
    second_brain_python = Path("/home/cat/jarvis/.venv/bin/python")
    agent_browser_runner = Path("/home/cat/.local/share/antigravity-browser-mcp/run_mcp.sh")

    mcp_servers: dict[str, Any] = {}
    if second_brain_server.is_file() and second_brain_python.is_file():
        mcp_servers["second-brain"] = {
            "command": str(second_brain_python),
            "args": [str(second_brain_server)],
            "disabled": False,
        }
    if agent_browser_runner.is_file():
        mcp_servers["agent-browser"] = {
            "command": str(agent_browser_runner),
            "disabled": False,
        }

    config_dir = home_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if mcp_servers:
        _atomic_write_json(config_dir / "mcp_config.json", {"mcpServers": mcp_servers})

    return home_dir

#: Connection state machine values.
STOPPED = "stopped"
STARTING = "starting"
READY = "ready"
RECOVERING = "recovering"
CLOSING = "closing"
CLOSED = "closed"

#: Turn state machine values. Protocol idle and audible idle are distinct.
IDLE = "idle"
PROMPTING = "prompting"
DRAINING_TTS = "draining_tts"
CANCELLING = "cancelling"

ACP_PROTOCOL_VERSION = 1
CLIENT_NAME = "gideon"
CLIENT_VERSION = "0.1.0"

#: Extension config ids Antigravity exposes.
THOUGHT_LEVEL_CONFIG_ID = "thought_level"
MODEL_CONFIG_ID = "model"

#: Levels GIDEON knows about.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: Default ACP model and effort.
DEFAULT_ACP_MODEL = "gemini-4-hydrogen"
DEFAULT_ACP_EFFORT = "medium"

#: Default Flash models mapped by effort (legacy fallback mapping).
_DEFAULT_FLASH_MODELS = {
    "low": "gemini-3.8-flash-low",
    "medium": "gemini-3.8-flash-medium",
    "high": "gemini-3.8-flash-high",
    "xhigh": "gemini-3.8-flash-high",
    "max": "gemini-3.8-flash-high",
}


def _resolve_agy_model(effort: str) -> str:
    """Returns the configured AGY model slug, defaulting to Gemini 4 Hydrogen."""
    override = os.environ.get("GIDEON_AGY_MODEL", "").strip()
    if override:
        return override
    return DEFAULT_ACP_MODEL


#: How long interrupt() waits for the original prompt's terminal response.
DEFAULT_CANCEL_SETTLE_TIMEOUT_S = 10.0
WATCHDOG_INTERVAL_S = 45.0
MONITOR_INTERVAL_S = 0.5

# Abbreviation-safe sentence boundary regex (mirrors gideon.py).
_SENTENCE_END = re.compile(
    r"(?<!\b(?:e\.g|i\.e)\.)"
    r"(?<!\b(?:etc|mrs)\.)"
    r"(?<!\b(?:dr|mr|ms|vs)\.)"
    r"(?<!\bv\d\.\d)"
    r"(?<!\b\d\.\d)"
    r"(?<=[.!?])\s+",
    re.IGNORECASE,
)


@dataclass
class _PromptRecord:
    """One in-flight ``session/prompt`` turn."""

    generation: int
    text: str
    request_id: Optional[int] = None
    suppress_output: bool = False
    acknowledged: bool = False
    stop_reason: Optional[str] = None
    result: dict[str, Any] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class ACPAntigravityEngine:
    """GIDEON voice engine speaking ACP to ``agy acp``.

    Implements the BaseEngine protocol. ``preamble`` is injected by the host
    instead of imported from ``gideon`` so this module has no hard module-level
    dependency on the daemon.
    """

    name = "agy"

    def __init__(
        self,
        bus: Any,
        tts: Any,
        preamble: str,
        cwd: str,
        effort: str = DEFAULT_ACP_EFFORT,
        agy_bin: str = "agy",
        extra_args: list[str] | None = None,
        clean_text_fn: Optional[Callable[[str], Optional[str]]] = None,
        sentence_end_re: Optional[re.Pattern] = None,
        directive_classifier: Optional[Callable[[str], Optional[str]]] = None,
        hot_mic_window_s: float = 45.0,
        profile_dir: Optional[Path | str] = None,
        app_data_dir: Optional[str] = None,
    ) -> None:
        self.bus = bus
        self.tts = tts
        self.preamble = preamble
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.effort = effort
        self.agy_bin = agy_bin
        self.extra_args = list(extra_args or [])
        self.clean_text_fn = clean_text_fn
        self.sentence_end_re = sentence_end_re or _SENTENCE_END
        self.directive_classifier = directive_classifier
        self.hot_mic_window_s = hot_mic_window_s
        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else None
        self.app_data_dir = app_data_dir

        # BaseEngine surface.
        self.is_turn_active = False
        self.current_tool: Optional[dict[str, Any]] = None
        self.turn_start_time = 0.0

        # Host-injected collaborator; set by the caller once wired up.
        self.wake_listener: Any = None

        # Connection state.
        self._conn_state = STOPPED
        self._epoch = 0
        self._transport: Optional[ACPTransport] = None
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._agent_info: dict[str, Any] = {}
        self._agent_capabilities: dict[str, Any] = {}
        self._session_id: Optional[str] = None
        self._config_options: list[dict[str, Any]] = []

        # Turn state.
        self._turn_state = IDLE
        self._turn_generation = 0
        self._prompt_record: Optional[_PromptRecord] = None
        self._prompt_task: Optional[asyncio.Task[None]] = None
        self._finish_task: Optional[asyncio.Task[None]] = None
        self._watchdog_task: Optional[asyncio.Task[None]] = None
        self._sentence_buffer = ""
        self.accumulated_reply = ""

        # Tool bookkeeping, keyed by ``toolCallId``.
        self._tools: dict[str, dict[str, Any]] = {}
        self._tool_seq = 0

        # Inbound permission requests.
        self._pending_permissions: dict[str, dict[str, Any]] = {}
        self._resolved_permissions: set[str] = set()
        self._permission_seq = 0

        self._restart_lock = asyncio.Lock()
        self._cancel_settle_timeout = DEFAULT_CANCEL_SETTLE_TIMEOUT_S
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Spawn ``agy acp``, initialize, create (or resume) a session."""
        if self._closed:
            raise RuntimeError("ACPAntigravityEngine is closed")
        if self._conn_state in (STARTING, READY):
            return
        await self._connect()

    def _build_argv(self) -> list[str]:
        """Return the agent command line. Tests override this seam.

        Uses an isolated, lightweight profile directory with minimal customizations:
        - --gemini-dir (default: ~/.local/share/gideon/agy_home or GIDEON_AGY_GEMINI_DIR)
        - --app-data-dir (default: antigravity-cli or GIDEON_AGY_APP_DATA_DIR)
        - --include-default-customizations=false (unless GIDEON_AGY_DEFAULT_CUSTOMIZATIONS=1)
        - --dangerously-skip-permissions only if GIDEON_AGY_ACP_SKIP_PERMISSIONS=1
        """
        argv = [self.agy_bin, "acp"]
        if os.environ.get("GIDEON_AGY_ACP_SKIP_PERMISSIONS") == "1":
            argv.append("--dangerously-skip-permissions")

        gemini_dir = str(
            self.profile_dir
            or os.environ.get("GIDEON_AGY_GEMINI_DIR")
            or DEFAULT_AGY_PROFILE_DIR
        )
        argv.extend(["--gemini-dir", gemini_dir])

        app_data_dir = (
            self.app_data_dir
            or os.environ.get("GIDEON_AGY_APP_DATA_DIR")
            or DEFAULT_AGY_APP_DATA_DIR
        )
        argv.extend(["--app-data-dir", app_data_dir])

        if os.environ.get("GIDEON_AGY_DEFAULT_CUSTOMIZATIONS") != "1":
            argv.append("--include-default-customizations=false")

        argv.extend(self.extra_args)
        return argv

    def _make_transport(self) -> ACPTransport:
        self.profile_dir = ensure_gideon_agy_home(
            self.profile_dir,
            app_data_dir=self.app_data_dir or os.environ.get("GIDEON_AGY_APP_DATA_DIR") or DEFAULT_AGY_APP_DATA_DIR,
        )
        return ACPTransport(self._build_argv(), cwd=self.cwd, log=logger)

    async def _connect(self, resume_session_id: Optional[str] = None) -> None:
        """Create a transport, initialize it, and establish a session."""
        self._conn_state = STARTING
        transport = self._make_transport()
        self._transport = transport
        transport.on_notification("session/update", self._on_session_update)
        transport.on_request("session/request_permission", self._on_permission_request)

        try:
            # The transport starts its stdout reader before any request is
            # written, so no early frame can land in an unread pipe.
            await transport.start()

            init_result = await transport.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
                timeout=30.0,
            )
            version = init_result.get("protocolVersion")
            if version != ACP_PROTOCOL_VERSION:
                raise RuntimeError(
                    "ACP protocol mismatch: agent speaks "
                    f"{version!r}, client requires {ACP_PROTOCOL_VERSION}"
                )
            self._agent_info = init_result.get("agentInfo") or {}
            self._agent_capabilities = init_result.get("agentCapabilities") or {}
            logger.info("ACP agent: %s", self._agent_info)
            logger.info("ACP agent capabilities: %s", self._agent_capabilities)

            self._epoch += 1
            self._session_id = None
            context_lost = False
            resumed = False

            if resume_session_id:
                try:
                    resumed_result = await transport.request(
                        "session/resume",
                        {
                            "sessionId": resume_session_id,
                            "cwd": self.cwd,
                            "mcpServers": [],
                        },
                        timeout=60.0,
                    )
                    self._session_id = (
                        resumed_result.get("sessionId") or resume_session_id
                    )
                    self._config_options = list(
                        resumed_result.get("configOptions") or []
                    )
                    resumed = True
                    logger.info("ACP resumed session %s", self._session_id)
                except (ACPError, ACPEOF, asyncio.TimeoutError) as exc:
                    logger.warning(
                        "ACP session/resume failed (%s); starting a fresh session",
                        exc,
                    )
                    context_lost = True

            if not resumed:
                session = await transport.request(
                    "session/new",
                    {
                        "cwd": self.cwd,
                        "mcpServers": [],
                        "_meta": {
                            "antigravity": {
                                "systemPrompt": {"append": self.preamble}
                            }
                        },
                    },
                    timeout=60.0,
                )
                self._session_id = session.get("sessionId")
                if not self._session_id:
                    raise RuntimeError("ACP session/new returned no sessionId")
                self._config_options = list(session.get("configOptions") or [])
                logger.info("ACP session %s", self._session_id)

            await self._apply_model()
            await self._apply_effort(self.effort)

            self._conn_state = READY
            self._start_monitor()
            logger.info("ACP engine connected and ready.")
            if context_lost:
                self._engine_notice(
                    "I'm back, but I had to start a fresh conversation."
                )
        except BaseException:
            self._conn_state = STOPPED
            try:
                await transport.close()
            except BaseException:  # noqa: BLE001 - cleanup must not mask the cause
                logger.debug("ACP transport cleanup failed", exc_info=True)
            if self._transport is transport:
                self._transport = None
            raise

    async def close(self) -> None:
        """Close the session (when advertised) and reap the agent. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._conn_state = CLOSING
        self._turn_generation += 1

        tasks: list[asyncio.Task[None]] = []
        for attr in ("_watchdog_task", "_finish_task", "_monitor_task", "_prompt_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.tts.interrupt()
        except Exception:  # noqa: BLE001 - shutdown must proceed
            logger.debug("TTS interrupt failed during close", exc_info=True)

        transport = self._transport
        if transport is not None:
            if (
                self._session_id
                and self._session_capability("close")
                and not transport.is_closed
            ):
                try:
                    await transport.request(
                        "session/close",
                        {"sessionId": self._session_id},
                        timeout=5.0,
                    )
                except (ACPError, ACPEOF, asyncio.TimeoutError) as exc:
                    logger.debug("ACP session/close failed: %s", exc)
            try:
                await transport.close()
            except Exception:  # noqa: BLE001 - already dying
                logger.debug("ACP transport close failed", exc_info=True)
        self._transport = None
        self._prompt_record = None
        self._reset_turn()
        self._conn_state = CLOSED
        logger.info("ACP engine closed.")

    # ---------------------------------------------------------------- prompts

    async def send_prompt(
        self, text: str, *, display_text: Optional[str] = None
    ) -> None:
        """Submit one prompt and return after submission, not completion.

        The console must stay available for interruption, so the terminal
        response is handled by a tracked task.
        """
        if not text.strip():
            return

        # Never have two prompts in flight: cancel and settle the old one.
        if self._prompt_record is not None or self._turn_state != IDLE:
            await self.interrupt()
        else:
            await self.tts.interrupt()

        # Overlap Fish live WS handshake with LLM thinking time so the first
        # spoken sentence does not pay the connect cost.
        prewarm = getattr(self.tts, "prewarm_ws", None)
        if asyncio.iscoroutinefunction(prewarm):
            asyncio.create_task(prewarm())

        if not await self._ensure_ready():
            return

        self._turn_generation += 1
        generation = self._turn_generation
        self._sentence_buffer = ""
        self.accumulated_reply = ""
        self.current_tool = None
        self.bus.current_tool = None
        self._tools.clear()
        self._resolved_permissions.clear()
        self._pending_permissions.clear()

        shown_text = (
            display_text.strip()
            if isinstance(display_text, str) and display_text.strip()
            else text
        )
        self.bus.set_user_transcript(shown_text)
        self.bus.current_reply = ""
        print(f"\nYou: {shown_text}\n", flush=True)

        self.turn_start_time = time.time()
        self.is_turn_active = True
        self._turn_state = PROMPTING
        self.bus.set_state("conversing", "LLM: Thinking…")
        self._start_watchdog()

        # Directives (e.g. terse mode, precision mode)
        payload_text = text
        if self.directive_classifier is not None:
            directive_source = display_text if display_text is not None else text
            try:
                directive = self.directive_classifier(directive_source)
            except Exception:
                directive = None
            if (
                directive
                and directive != self.preamble
                and "[Directive: Voice Operating Mode" not in directive
            ):
                payload_text = f"{directive}\n\nUser: {text}"

        record = _PromptRecord(generation=generation, text=payload_text)
        self._prompt_record = record
        self._prompt_task = asyncio.create_task(
            self._submit_prompt(record), name="acp-prompt"
        )
        # Yield once so the prompt task starts and registers its request.
        await asyncio.sleep(0)

    async def _submit_prompt(self, record: _PromptRecord) -> None:
        transport = self._transport
        if transport is None:
            await self._handle_connection_loss(ACPEOF("ACP transport missing"))
            return
        params = {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": record.text}],
        }
        try:
            result = await transport.request("session/prompt", params)
        except asyncio.CancelledError:
            raise
        except ACPEOF as exc:
            await self._handle_connection_loss(exc)
            return
        except ACPError as exc:
            logger.warning("ACP prompt failed: %s", exc)
            record.acknowledged = True
            record.stop_reason = "error"
            self._settle_prompt(record)
            record.done.set()
            self._engine_notice("My engine rejected that request.")
            return
        except Exception as exc:  # noqa: BLE001 - never leave the turn stuck
            logger.exception("ACP prompt crashed: %s", exc)
            record.acknowledged = True
            record.stop_reason = "error"
            self._settle_prompt(record)
            record.done.set()
            self._engine_notice("Something went wrong inside my engine.")
            return
        self._on_prompt_terminal(record, result or {})

    def _on_prompt_terminal(
        self, record: _PromptRecord, result: dict[str, Any]
    ) -> None:
        record.acknowledged = True
        record.stop_reason = result.get("stopReason")
        record.result = result
        self._mark_running_tools_done()
        self._stop_watchdog()
        if self._is_current_audible_turn(record):
            self._turn_state = DRAINING_TTS
            self._finish_task = asyncio.create_task(
                self._finish_turn(record), name="acp-finish"
            )
        else:
            self._settle_prompt(record)
        record.done.set()

    def _is_current_audible_turn(self, record: _PromptRecord) -> bool:
        return (
            record.generation == self._turn_generation
            and not record.suppress_output
        )

    async def _finish_turn(self, record: _PromptRecord) -> None:
        """Flush the tail, let TTS drain, then re-arm the hot mic."""
        try:
            if self._is_current_audible_turn(record):
                self._flush_tail()
            await self.tts.wait_until_done()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a TTS failure must not wedge the turn
            logger.exception("ACP turn drain failed")
        if record.generation != self._turn_generation:
            return
        if self._prompt_record is record:
            self._prompt_record = None
        self._reset_turn()
        wake_listener = getattr(self, "wake_listener", None)
        if wake_listener is not None:
            wake_listener.arm_hot_mic(timeout_s=self.hot_mic_window_s)
        else:
            self.bus.set_state("idle")
        logger.info("[Turn Complete:%s] Gideon is idle.", record.stop_reason)

    async def interrupt(self, hard: bool = False) -> bool:
        """Cancel the active turn and settle it on the prompt's response."""
        # 1. Any chunk that arrives from here on belongs to a dead turn.
        self._turn_generation += 1
        record = self._prompt_record
        if record is not None:
            record.suppress_output = True

        # 2. Silence and reset before touching the wire.
        self._stop_watchdog()
        finish = self._finish_task
        self._finish_task = None
        if finish is not None and finish is not asyncio.current_task():
            finish.cancel()
            await asyncio.gather(finish, return_exceptions=True)
        self._sentence_buffer = ""
        self.accumulated_reply = ""
        self.bus.current_reply = ""
        try:
            await self.tts.interrupt()
        except Exception:  # noqa: BLE001 - interruption must continue
            logger.debug("TTS interrupt failed", exc_info=True)
        self.bus.clear_transcript()
        self._reset_turn(turn_state=CANCELLING)

        # 3. Local tool bookkeeping is cancelled even if the agent lags.
        for tool in self._tools.values():
            if tool.get("status") == "running":
                tool["status"] = "cancelled"
        self.current_tool = None
        self.bus.current_tool = None

        if record is None:
            self._turn_state = IDLE
            return True

        # 4. Cancel is a notification. It has no id and gets no direct reply.
        transport = self._transport
        if transport is not None and not transport.is_closed:
            try:
                await transport.notify(
                    "session/cancel", {"sessionId": self._session_id}
                )
            except ACPEOF as exc:
                logger.debug("ACP cancel write failed: %s", exc)

        # 5. A racing permission decision must resolve as cancelled.
        await self._cancel_pending_permissions()

        # 6. The acknowledgement is the original prompt's terminal response.
        try:
            await asyncio.wait_for(
                record.done.wait(), timeout=self._cancel_settle_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "ACP cancel was not acknowledged within %.1fs; "
                "suppressing output and marking the connection for recovery",
                self._cancel_settle_timeout,
            )
            record.suppress_output = True
            self._turn_state = IDLE
            if not self._closed:
                self._conn_state = RECOVERING
            return False

        self._turn_state = IDLE
        return record.acknowledged

    # ----------------------------------------------------------------- models

    def _pick_model(self, advertised_values: list[str]) -> Optional[str]:
        """Resolve the model to use from advertised model choices."""
        explicit = os.environ.get("GIDEON_AGY_MODEL", "").strip()
        if explicit:
            if explicit in advertised_values:
                return explicit
            for val in advertised_values:
                if val.lower() == explicit.lower():
                    return val
            logger.warning(
                "Requested GIDEON_AGY_MODEL=%r not in advertised models: %s",
                explicit,
                advertised_values,
            )
            return None

        preferred = _resolve_agy_model(self.effort)
        if preferred in advertised_values:
            return preferred
        for val in advertised_values:
            if preferred.lower() in val.lower():
                return val

        # Fallback candidates matching Hydrogen, then Flash
        for candidate in (
            "gemini-4-hydrogen",
            "gemini-4-hydrogen-medium",
            "gemini-4-hydrogen-high",
            "gemini-4-hydrogen-low",
            "gemini-3.8-flash",
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-2.5-flash",
        ):
            if candidate in advertised_values:
                return candidate
        for val in advertised_values:
            if "hydrogen" in val.lower():
                return val
        for val in advertised_values:
            if "3.8-flash" in val.lower():
                return val
        for val in advertised_values:
            if "flash" in val.lower():
                return val
        return None

    async def _apply_model(self) -> bool:
        """Configure model via session/set_config_option if advertised."""
        option = self._find_config_option(MODEL_CONFIG_ID)
        if option is None:
            logger.debug("ACP server does not advertise 'model' config option")
            return False
        values = self._advertised_values(option)
        chosen = self._pick_model(values)
        if not chosen:
            logger.info("No matching model found in ACP advertised models: %s", values)
            return False
        current = self._config_option_value(MODEL_CONFIG_ID)
        if current == chosen:
            logger.info("ACP model already set to %s", chosen)
            return True
        transport = self._transport
        if transport is None:
            return False
        try:
            result = await transport.request(
                "session/set_config_option",
                {
                    "sessionId": self._session_id,
                    "configId": MODEL_CONFIG_ID,
                    "value": chosen,
                },
                timeout=15.0,
            )
        except (ACPError, ACPEOF, asyncio.TimeoutError) as exc:
            logger.warning("ACP session/set_config_option for model failed: %s", exc)
            return False
        returned = result.get("configOptions")
        if isinstance(returned, list):
            self._config_options = list(returned)
        confirmed = self._config_option_value(MODEL_CONFIG_ID) or chosen
        logger.info("ACP model -> %s", confirmed)
        return True

    # ----------------------------------------------------------------- effort

    async def set_effort(self, new_effort: str) -> bool:
        """Select a reasoning level the server actually advertises."""
        if new_effort not in EFFORT_LEVELS:
            logger.warning("Unknown effort level %r", new_effort)
            return False

        option = self._find_config_option(THOUGHT_LEVEL_CONFIG_ID)
        advertised = self._advertised_values(option) if option else []

        if new_effort in ("xhigh", "max") and new_effort not in advertised:
            logger.warning(
                "Effort %r is not advertised over ACP (available: %s); "
                "the headless engine is required for that level.",
                new_effort,
                advertised or "none",
            )
            self._engine_notice(
                "The headless engine is required for xhigh or max effort. "
                "Over ACP I can go up to high."
            )
            return False

        if new_effort == self.effort:
            return True
        if self._conn_state != READY or self._transport is None:
            logger.warning("Cannot set effort while ACP is %s", self._conn_state)
            return False
        if option is None:
            logger.warning("ACP does not advertise %s", THOUGHT_LEVEL_CONFIG_ID)
            return False
        if new_effort not in advertised:
            logger.warning(
                "ACP does not offer effort %r; advertised: %s",
                new_effort,
                advertised,
            )
            return False
        return await self._apply_effort(new_effort)

    async def _apply_effort(self, effort: str) -> bool:
        option = self._find_config_option(THOUGHT_LEVEL_CONFIG_ID)
        if option is None:
            logger.warning(
                "ACP server does not advertise %s; effort left at %r",
                THOUGHT_LEVEL_CONFIG_ID,
                self.effort,
            )
            return False
        values = self._advertised_values(option)
        if effort not in values:
            logger.warning(
                "Requested effort %r is not offered (advertised: %s)", effort, values
            )
            confirmed = self._config_option_value(THOUGHT_LEVEL_CONFIG_ID)
            if confirmed:
                self.effort = confirmed
                self.bus.current_effort = confirmed
            return False
        transport = self._transport
        if transport is None:
            return False
        try:
            result = await transport.request(
                "session/set_config_option",
                {
                    "sessionId": self._session_id,
                    "configId": THOUGHT_LEVEL_CONFIG_ID,
                    "value": effort,
                },
                timeout=15.0,
            )
        except (ACPError, ACPEOF, asyncio.TimeoutError) as exc:
            logger.warning("ACP set_config_option failed: %s", exc)
            return False
        returned = result.get("configOptions")
        if isinstance(returned, list):
            self._config_options = list(returned)
        accepted = self._config_option_value(THOUGHT_LEVEL_CONFIG_ID) or effort
        self.effort = accepted
        self.bus.current_effort = accepted
        logger.info("ACP effort -> %s", accepted)
        return True

    def _find_config_option(self, config_id: str) -> Optional[dict[str, Any]]:
        for option in self._config_options:
            if not isinstance(option, dict):
                continue
            if option.get("id") == config_id or option.get("configId") == config_id:
                return option
        return None

    def _advertised_values(self, option: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for entry in option.get("options") or []:
            if isinstance(entry, dict) and entry.get("value") is not None:
                values.append(str(entry["value"]))
        return values

    def _config_option_value(self, config_id: str) -> Optional[str]:
        option = self._find_config_option(config_id)
        if option is None:
            return None
        value = option.get("currentValue")
        return str(value) if value is not None else None

    # ---------------------------------------------------------- notifications

    async def _on_session_update(self, params: dict[str, Any]) -> None:
        """Route one ``session/update``. Runs inline in the transport reader."""
        session_id = params.get("sessionId")
        if session_id and self._session_id and session_id != self._session_id:
            logger.debug("ACP: dropping update for foreign session %s", session_id)
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._on_message_chunk(update)
        elif kind in ("tool_call", "tool_call_update"):
            self._on_tool_update(update)
        elif kind == "agent_thought_chunk":
            # Reasoning is never spoken.
            logger.debug("ACP thought chunk (not spoken)")
        elif kind == "config_option_update":
            options = update.get("configOptions")
            if isinstance(options, list):
                self._config_options = list(options)
        elif kind in (
            "user_message_chunk",
            "plan",
            "available_commands_update",
            "current_mode_update",
        ):
            logger.debug("ACP: ignoring %s update", kind)
        else:
            logger.debug("ACP: unknown sessionUpdate %r", kind)

    def _on_message_chunk(self, update: dict[str, Any]) -> None:
        record = self._prompt_record
        if record is None:
            logger.debug("ACP: dropping agent_message_chunk outside a turn")
            return
        if not self._is_current_audible_turn(record):
            return
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") != "text":
            logger.debug("ACP: ignoring non-text agent_message_chunk")
            return
        text = content.get("text") or ""
        if not text:
            return

        self._sentence_buffer += text
        self.accumulated_reply += text
        self.bus.current_reply = self.accumulated_reply

        regex = self.sentence_end_re or _SENTENCE_END
        while True:
            match = regex.search(self._sentence_buffer)
            if not match:
                break
            sentence = self._sentence_buffer[: match.end()].strip()
            self._sentence_buffer = self._sentence_buffer[match.end() :]
            if sentence:
                self._speak_sentence(sentence)

    def _speak_sentence(self, sentence: str) -> None:
        sentence = sentence.strip()
        if not sentence:
            return
        clean_fn = self.clean_text_fn
        if clean_fn is None:
            from gideon import clean_text_for_tts as clean_fn
        clean = clean_fn(sentence)
        if not clean:
            return
        logger.info("Gideon: %s", clean)
        print(f"Gideon: {clean}", flush=True)
        if not self.tts._is_speaking and self.bus.current_state != "speaking":
            self.bus.set_state("conversing", "LLM: Synthesizing voice…")
        self.tts.queue_sentence(clean)

    def _flush_tail(self) -> None:
        tail = self._sentence_buffer.strip()
        self._sentence_buffer = ""
        if tail:
            self._speak_sentence(tail)

    # ------------------------------------------------------------------- tools

    def _on_tool_update(self, update: dict[str, Any]) -> None:
        if self._prompt_record is None and not self.is_turn_active:
            logger.debug("ACP: dropping tool update outside a turn")
            return
        tool_id = update.get("toolCallId") or update.get("tool_call_id")
        if not tool_id:
            logger.debug("ACP: tool update without toolCallId")
            return
        tool_id = str(tool_id)

        record = self._tools.get(tool_id)
        if record is None:
            record = {"id": tool_id, "name": "tool", "status": "pending"}
            self._tools[tool_id] = record
        self._tool_seq += 1
        record["_seq"] = self._tool_seq

        for key in ("title", "kind", "status", "content", "locations", "_meta"):
            if key in update and update[key] is not None:
                record[key] = update[key]
        for key in ("rawInput", "rawOutput"):
            value = update.get(key)
            if not isinstance(value, dict):
                if value is not None:
                    record[key] = value
                continue
            existing = record.get(key)
            if isinstance(existing, dict):
                existing.update(value)
            else:
                record[key] = dict(value)

        record["status"] = self._map_tool_status(record.get("status"))
        record["name"] = (
            record.get("title") or record.get("kind") or record.get("name") or "tool"
        )
        record["target"] = self._tool_target(
            f"{record['name']} {record.get('kind') or ''}"
        )
        record["label"] = self._tool_label(record["name"], record.get("rawInput"))

        self._refresh_current_tool()
        logger.info("[Tool %s] %s", record["status"], record["label"])

    @staticmethod
    def _map_tool_status(status: Any) -> str:
        mapping = {
            "pending": "pending",
            "in_progress": "running",
            "completed": "done",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        return mapping.get(str(status), str(status or "pending"))

    @staticmethod
    def _tool_target(name: str) -> str:
        lowered = name.lower()
        if "command" in lowered or "exec" in lowered:
            return "shell"
        if "browser" in lowered or "url" in lowered:
            return "web"
        return "core"

    @staticmethod
    def _tool_label(name: str, args: Any) -> str:
        if not isinstance(args, dict):
            return name
        if "CommandLine" in args:
            return f"{name}: {str(args['CommandLine']).strip()[:40]}"
        if "AbsolutePath" in args:
            return f"{name}: {Path(str(args['AbsolutePath'])).name}"
        if "TargetFile" in args:
            return f"{name}: {Path(str(args['TargetFile'])).name}"
        if "query" in args:
            return f"{name}: {str(args['query'])[:35]}"
        if "command" in args:
            return f"{name}: {str(args['command']).strip()[:40]}"
        return name

    def _refresh_current_tool(self) -> None:
        running = [
            tool for tool in self._tools.values() if tool.get("status") == "running"
        ]
        if not running:
            if self.current_tool is not None:
                self.current_tool = None
                self.bus.current_tool = None
            return
        latest = max(running, key=lambda tool: tool.get("_seq", 0))
        tool = {
            "id": latest["id"],
            "name": latest["name"],
            "target": latest["target"],
            "label": latest["label"],
            "status": "running",
        }
        changed = self.current_tool != tool
        self.current_tool = tool
        self.bus.current_tool = dict(tool)
        if changed:
            self.bus.set_state("toolcall", detail=tool["label"])

    def _mark_running_tools_done(self) -> None:
        for tool in self._tools.values():
            if tool.get("status") == "running":
                tool["status"] = "done"
        self.current_tool = None
        self.bus.current_tool = None

    # ------------------------------------------------------------- watchdog

    def _start_watchdog(self) -> None:
        self._stop_watchdog()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="acp-watchdog"
        )

    def _stop_watchdog(self) -> None:
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _watchdog_loop(self) -> None:
        """Calm periodic check-ins during genuinely running tools."""
        try:
            while self.is_turn_active:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if (
                    not self.is_turn_active
                    or not self.current_tool
                    or self.current_tool.get("status") != "running"
                ):
                    break
                if (
                    self.tts._is_speaking
                    or not self.tts._queue.empty()
                    or self.bus.current_state in ("speaking", "listening", "hotmic")
                ):
                    continue
                elapsed = int(time.time() - self.turn_start_time)
                label = self.current_tool.get("label") or "working on the solution"
                checkin = f"Still running {label}, {elapsed} seconds in."
                logger.info("[Watchdog Check-in] %s", checkin)
                self.tts.queue_sentence(checkin)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------ permissions

    async def _on_permission_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer a ``session/request_permission`` inbound request exactly once.

        Picks allow_always / allow_once if the turn is actively running, or returns
        cancelled if interrupted or outside a turn.
        """
        key = self._permission_key(params)
        generation = self._turn_generation
        self._pending_permissions[key] = params
        try:
            # Yield once so a cancel racing this decision can claim the request.
            await asyncio.sleep(0)
            if key in self._resolved_permissions:
                return {"outcome": {"outcome": "cancelled"}}
            if (
                self._closed
                or self._turn_state == CANCELLING
                or not self.is_turn_active
                or self._prompt_record is None
                or generation != self._turn_generation
            ):
                result: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
            else:
                option_id = self._pick_allow_option(params.get("options") or [])
                if option_id is None:
                    logger.warning(
                        "ACP permission request advertised no allow option"
                    )
                    result = {"outcome": {"outcome": "cancelled"}}
                else:
                    result = {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option_id,
                        }
                    }
            self._resolved_permissions.add(key)
            logger.info("ACP permission %s -> %s", key, result["outcome"]["outcome"])
            return result
        finally:
            self._pending_permissions.pop(key, None)

    def _permission_key(self, params: dict[str, Any]) -> str:
        tool_call = params.get("toolCall")
        tool_id = None
        if isinstance(tool_call, dict):
            tool_id = tool_call.get("toolCallId")
        if not tool_id:
            tool_id = params.get("toolCallId")
        self._permission_seq += 1
        return f"{self._turn_generation}:{tool_id or 'unknown'}:{self._permission_seq}"

    @staticmethod
    def _pick_allow_option(options: list[Any]) -> Optional[str]:
        for preferred in ("allow_always", "allow_once"):
            for option in options:
                if isinstance(option, dict) and option.get("kind") == preferred:
                    option_id = option.get("optionId")
                    if option_id:
                        return str(option_id)
        for option in options:
            if isinstance(option, dict) and str(option.get("kind", "")).startswith("allow"):
                option_id = option.get("optionId")
                if option_id:
                    return str(option_id)
        return None

    async def _cancel_pending_permissions(self) -> None:
        """Resolve in-flight permission requests as cancelled."""
        for key in list(self._pending_permissions):
            self._resolved_permissions.add(key)
            self._pending_permissions.pop(key, None)

    # ---------------------------------------------------------------- recovery

    def _session_capability(self, name: str) -> bool:
        caps = self._agent_capabilities.get("sessionCapabilities")
        if isinstance(caps, dict) and caps.get(name):
            return True
        if name == "load":
            return bool(self._agent_capabilities.get("loadSession"))
        return False

    async def _ensure_ready(self) -> bool:
        if self._closed:
            self._engine_notice("My engine is shut down.")
            return False
        transport = self._transport
        if (
            self._conn_state == READY
            and transport is not None
            and not transport.is_closed
        ):
            return True
        return await self._recover()

    async def _recover(self) -> bool:
        """One serialized reconnect path."""
        async with self._restart_lock:
            if self._closed:
                return False
            transport = self._transport
            if (
                self._conn_state == READY
                and transport is not None
                and not transport.is_closed
            ):
                return True

            session_id = self._session_id
            old = self._transport
            self._transport = None
            if old is not None:
                try:
                    await asyncio.wait_for(old.close(), timeout=10.0)
                except Exception:  # noqa: BLE001
                    logger.debug("ACP old transport close failed", exc_info=True)

            try:
                await self._connect(resume_session_id=session_id)
                return True
            except (ACPError, ACPEOF, asyncio.TimeoutError, OSError, RuntimeError):
                logger.warning("ACP reconnect failed", exc_info=True)
                self._conn_state = RECOVERING
                self._reset_turn()
                self._engine_notice(
                    "I couldn't reconnect. I'll try again when you next speak."
                )
                return False

    async def _handle_connection_loss(self, exc: BaseException) -> bool:
        """Fail pending state, silence stale speech, then reconnect."""
        if self._closed or self._conn_state in (CLOSING, CLOSED, RECOVERING):
            return False
        self._conn_state = RECOVERING
        logger.warning("ACP connection lost: %s", exc)

        self._turn_generation += 1
        self._stop_watchdog()
        finish = self._finish_task
        self._finish_task = None
        if finish is not None and finish is not asyncio.current_task():
            finish.cancel()
            await asyncio.gather(finish, return_exceptions=True)

        record = self._prompt_record
        if record is not None:
            record.suppress_output = True
            record.acknowledged = False
            record.stop_reason = "connection_lost"
        self._prompt_record = None
        self._prompt_task = None
        self._sentence_buffer = ""
        self.accumulated_reply = ""
        self._mark_running_tools_done()
        try:
            await self.tts.interrupt()
        except Exception:  # noqa: BLE001
            logger.debug("TTS interrupt failed after connection loss", exc_info=True)
        self._reset_turn()
        self._engine_notice("My engine dropped. I'm reconnecting.")
        if record is not None:
            record.done.set()
        return await self._recover()

    def _start_monitor(self) -> None:
        old = self._monitor_task
        if old is not None and old is not asyncio.current_task() and not old.done():
            old.cancel()
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(), name="acp-monitor"
        )

    async def _monitor_loop(self) -> None:
        """Detect an idle EOF; the transport has no close callback."""
        try:
            while not self._closed:
                await asyncio.sleep(MONITOR_INTERVAL_S)
                transport = self._transport
                if transport is None:
                    continue
                if (
                    self._conn_state in (READY, STARTING)
                    and transport.is_closed
                ):
                    await self._handle_connection_loss(
                        ACPEOF("ACP agent process exited")
                    )
                    return
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------ state

    def _reset_turn(self, turn_state: str = IDLE) -> None:
        self.is_turn_active = False
        self.current_tool = None
        self.bus.current_tool = None
        self._stop_watchdog()
        self._turn_state = turn_state
        self.bus.set_state("idle")

    def _settle_prompt(self, record: _PromptRecord) -> None:
        """Settle a terminal that no longer owns the audible turn."""
        if self._prompt_record is record:
            self._prompt_record = None
            self._reset_turn()
        elif self._prompt_record is None and self._turn_state == CANCELLING:
            self._turn_state = IDLE

    def _engine_notice(self, text: str) -> None:
        self.bus.current_reply = text
        self.bus.set_state("idle", text)
        self.tts.queue_sentence(text)
