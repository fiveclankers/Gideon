"""Persistent Codex `app-server` voice engine for GIDEON.

Speaks newline-delimited JSON-RPC 2.0 over stdio to `codex app-server --listen stdio://`
using an isolated ``CODEX_HOME`` so Codex Desktop plugin/config edits and global hooks
cannot pollute voice-daemon latency or break startup.

Implements :class:`engine_base.BaseEngine`.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("gideon.codex")

CODEX_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CODEX_MODEL = "gpt-6-luna"
# MUST stay xhigh — Luna is genuinely dumb/unusable at lower effort (owner
# directive, explicit). Do not "optimize" this down for latency.
DEFAULT_CODEX_EFFORT = "xhigh"

DEFAULT_FRAME_LIMIT = 8 * 1024 * 1024
STDERR_TAIL_LINES = 100
DEFAULT_DRAIN_TIMEOUT_S = 5.0
WATCHDOG_INTERVAL_S = 45.0
DEFAULT_HOT_MIC_WINDOW_S = 45.0

SECOND_BRAIN_PYTHON = "/home/cat/jarvis/.venv/bin/python"
SECOND_BRAIN_SERVER = "/home/cat/Documents/Me/agent-inference/retrieval/mcp_server.py"
AGENT_BROWSER_VENV_PYTHON = "/home/cat/.local/share/antigravity-browser-mcp/.venv/bin/python"
AGENT_BROWSER_SERVER = "/home/cat/.local/share/antigravity-browser-mcp/server.py"
AGENT_BROWSER_FALLBACK_RUNNER = "/home/cat/.local/share/antigravity-browser-mcp/run_mcp.sh"

_DEFAULT_SENTENCE_END = re.compile(
    r"(?<!\b(?:e\.g|i\.e)\.)"
    r"(?<!\b(?:etc|mrs)\.)"
    r"(?<!\b(?:dr|mr|ms|vs)\.)"
    r"(?<!\bv\d\.\d)"
    r"(?<!\b\d\.\d)"
    r"(?<=[.!?])\s+",
    re.IGNORECASE,
)

_IGNORED_ITEM_TYPES = frozenset({
    "userMessage",
    "hookPrompt",
    "reasoning",
    "plan",
    "agentMessage",
    "contextCompaction",
})


def _resolve_codex_bin(codex_bin: str = "codex") -> str:
    """Resolve the Codex CLI path even under stripped systemd user PATH."""
    candidate = (codex_bin or "codex").strip()
    if "/" in candidate:
        return candidate
    found = shutil.which(candidate)
    if found:
        return found
    if candidate == "codex":
        home = Path.home()
        for path in (
            home / ".local" / "share" / "mise" / "installs" / "codex" / "latest" / "bin" / "codex",
            home / ".local" / "share" / "mise" / "shims" / "codex",
            home / ".local" / "share" / "mise" / "command-wrappers" / "bin" / "codex",
            home / ".local" / "bin" / "codex",
        ):
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return candidate


def _build_codex_env(codex_home: Path) -> dict[str, str]:
    """Build subprocess env with isolated CODEX_HOME and mise/local tool dirs."""
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    home = Path.home()
    extra_bins = [
        str(home / ".local" / "share" / "mise" / "installs" / "codex" / "latest" / "bin"),
        str(home / ".local" / "share" / "mise" / "shims"),
        str(home / ".local" / "bin"),
    ]
    current_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    merged = [p for p in extra_bins if Path(p).is_dir() and p not in current_parts] + current_parts
    env["PATH"] = os.pathsep.join(merged)
    return env


def _toml_str(value: str) -> str:
    """Serialize a Python string as a single-line TOML double-quoted string."""
    return json.dumps(value, ensure_ascii=False)


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


def ensure_gideon_codex_home(
    codex_home: Optional[Path] = None,
    *,
    cwd: Optional[str] = None,
    model: str = DEFAULT_CODEX_MODEL,
    effort: str = DEFAULT_CODEX_EFFORT,
    source_codex_dir: Optional[Path] = None,
) -> Path:
    """Prepare Gideon's isolated ``CODEX_HOME`` with lean MCPs only.

    Symlinks ChatGPT credentials (``auth.json``, ``models_cache.json``) from
    ``~/.codex`` and writes a dedicated ``config.toml`` with hooks/apps disabled
    and only ``second-brain``, ``agent-browser``, and live ``web_search`` enabled.
    """
    home_dir = Path(
        codex_home
        or os.environ.get("GIDEON_CODEX_HOME")
        or (Path.home() / ".local" / "share" / "gideon" / "codex_home")
    ).expanduser()
    home_dir.mkdir(parents=True, exist_ok=True)

    src_dir = (source_codex_dir or (Path.home() / ".codex")).expanduser()
    for fname in ("auth.json", "models_cache.json"):
        _link_or_copy(src_dir / fname, home_dir / fname)

    trusted_cwd = str(Path(cwd or "/home/cat/gideon").expanduser().resolve())
    lines = [
        f"model = {_toml_str(model)}",
        f"model_reasoning_effort = {_toml_str(effort)}",
        'model_reasoning_summary = "none"',
        'model_verbosity = "low"',
        'web_search = "live"',
        "",
        f"[projects.{_toml_str(trusted_cwd)}]",
        'trust_level = "trusted"',
        "",
        "[features]",
        "hooks = false",
        "apps = false",
        "",
    ]

    if Path(SECOND_BRAIN_SERVER).is_file():
        lines.extend([
            "[mcp_servers.second-brain]",
            f"command = {_toml_str(SECOND_BRAIN_PYTHON)}",
            f"args = [{_toml_str(SECOND_BRAIN_SERVER)}]",
            "",
        ])

    if Path(AGENT_BROWSER_VENV_PYTHON).is_file() and Path(AGENT_BROWSER_SERVER).is_file():
        lines.extend([
            "[mcp_servers.agent-browser]",
            f"command = {_toml_str(AGENT_BROWSER_VENV_PYTHON)}",
            f"args = [{_toml_str(AGENT_BROWSER_SERVER)}]",
            "",
        ])
    elif Path(AGENT_BROWSER_FALLBACK_RUNNER).is_file():
        lines.extend([
            "[mcp_servers.agent-browser]",
            f"command = {_toml_str(AGENT_BROWSER_FALLBACK_RUNNER)}",
            "",
        ])

    cfg_path = home_dir / "config.toml"
    tmp_cfg = home_dir / f".config.toml.{os.getpid()}.tmp"
    tmp_cfg.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp_cfg, cfg_path)
    return home_dir


def _unwrap_shell_command(raw_cmd: str) -> str:
    """Strip `/bin/bash -lc '...'` wrapper so tool HUD badges stay concise."""
    text = raw_cmd.strip()
    if not text:
        return ""
    try:
        tokens = shlex.split(text)
    except ValueError:
        return text
    if len(tokens) >= 3 and Path(tokens[0]).name in {"bash", "sh", "zsh"} and tokens[1] in {"-lc", "-c"}:
        return " ".join(tokens[2:]).strip()
    return text


def _format_tool_meta(item: dict[str, Any]) -> Optional[dict[str, str]]:
    """Convert a Codex `ThreadItem` payload to Gideon's tool UI metadata."""
    item_type = str(item.get("type") or "")
    if not item_type or item_type in _IGNORED_ITEM_TYPES:
        return None

    if item_type == "commandExecution":
        raw_cmd = str(item.get("command") or "")
        inner = _unwrap_shell_command(raw_cmd)
        label = f"shell: {inner[:42]}" if inner else "shell"
        return {"name": "commandExecution", "target": "shell", "label": label}

    if item_type == "mcpToolCall":
        server = str(item.get("server") or "").strip()
        tool = str(item.get("tool") or "").strip() or "tool"
        name = f"{server}:{tool}" if server else tool
        lowered = name.lower()
        target = "web" if ("browser" in lowered or "url" in lowered) else "core"
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        hint = args.get("query") or args.get("url") or args.get("text")
        label = f"{tool}: {str(hint)[:32]}" if hint else name
        return {"name": name, "target": target, "label": label}

    if item_type == "webSearch":
        query = str(item.get("query") or "").strip()
        label = f"web_search: {query[:35]}" if query else "web_search"
        return {"name": "web_search", "target": "web", "label": label}

    if item_type == "fileChange":
        return {"name": "file_change", "target": "core", "label": "file_change"}

    if item_type in ("collabAgentToolCall", "dynamicToolCall"):
        tool = str(item.get("tool") or item_type)
        return {"name": tool, "target": "core", "label": tool}

    return {"name": item_type, "target": "core", "label": item_type}


def _default_clean_text(raw: str) -> Optional[str]:
    text = " ".join(raw.split()).strip()
    return text if len(text) >= 3 else None


class _JsonRpcStdioTransport:
    """Async newline-delimited JSON-RPC 2.0 stdio transport."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        *,
        on_notification: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_disconnect: Callable[[Optional[int], str], Awaitable[None]] | None = None,
        frame_limit: int = DEFAULT_FRAME_LIMIT,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_notification = on_notification
        self._on_disconnect = on_disconnect
        self._frame_limit = frame_limit

        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return bool(
            not self._closed
            and self.proc is not None
            and self.proc.returncode is None
            and self.proc.stdin is not None
        )

    @property
    def stderr_summary(self) -> str:
        return " | ".join(list(self._stderr_tail)[-5:])

    async def start(self) -> None:
        self._closed = False
        self.proc = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._frame_limit,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        if isinstance(getattr(self.proc, "stderr", None), asyncio.StreamReader):
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> Any:
        if not self.is_alive or self.proc is None or self.proc.stdin is None:
            raise ConnectionError("Codex transport is not running")
        loop = asyncio.get_running_loop()
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            await self._write_frame(frame)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()

    async def _write_frame(self, payload: dict[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise ConnectionError("Codex stdin is unavailable")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            proc.stdin.write(raw)
            await asyncio.wait_for(proc.stdin.drain(), timeout=DEFAULT_DRAIN_TIMEOUT_S)

    async def _drain_stderr(self) -> None:
        proc = self.proc
        stderr = getattr(proc, "stderr", None)
        if not isinstance(stderr, asyncio.StreamReader):
            return
        try:
            while not self._closed and proc is self.proc:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").strip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("Codex stderr: %s", text)
        except asyncio.CancelledError:
            pass
        except (OSError, ValueError):
            pass

    async def _read_loop(self) -> None:
        proc = self.proc
        stdout = getattr(proc, "stdout", None)
        if proc is None or stdout is None:
            return
        rc: Optional[int] = None
        try:
            while not self._closed and proc is self.proc:
                try:
                    raw = await stdout.readline()
                except ValueError as exc:
                    logger.warning("Codex stdout frame exceeded limit (%s); recycling transport", exc)
                    break
                except OSError as exc:
                    logger.warning("Codex stdout read error: %s", exc)
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch_message(msg)
        except asyncio.CancelledError:
            return
        finally:
            if proc.returncode is None and not self._closed:
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=0.5)
                except (asyncio.TimeoutError, OSError):
                    rc = proc.returncode
            else:
                rc = proc.returncode

            err = ConnectionError(f"Codex app-server exited (code={rc})")
            for req_id in list(self._pending):
                fut = self._pending.pop(req_id, None)
                if fut and not fut.done():
                    fut.set_exception(err)

            if not self._closed and self._on_disconnect is not None:
                try:
                    await self._on_disconnect(rc, self.stderr_summary)
                except Exception:
                    logger.debug("Disconnect handler failed", exc_info=True)

    async def _dispatch_message(self, msg: dict[str, Any]) -> None:
        if msg.get("id") is not None and type(msg["id"]) not in (int, str):
            return
        has_id = "id" in msg and msg["id"] is not None
        method = msg.get("method")
        if has_id and method:
            # Unexpected inbound server request: decline cleanly so server never stalls.
            try:
                await self._write_frame({
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32601, "message": f"Unsupported client request: {method}"},
                })
            except (OSError, ConnectionError, asyncio.TimeoutError):
                pass
            return

        if has_id:
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                if "error" in msg and msg["error"] is not None:
                    err = msg["error"]
                    detail = err.get("message") if isinstance(err, dict) else str(err)
                    fut.set_exception(RuntimeError(f"Codex RPC error: {detail}"))
                else:
                    fut.set_result(msg.get("result"))
            return

        if isinstance(method, str) and self._on_notification is not None:
            params = msg.get("params")
            await self._on_notification(method, params if isinstance(params, dict) else {})

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task and task is not current and not task.done():
                task.cancel()
        tasks = [t for t in (self._reader_task, self._stderr_task) if t and t is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

        for req_id in list(self._pending):
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                fut.cancel()

        proc = self.proc
        self.proc = None
        if proc is not None:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except (ProcessLookupError, asyncio.TimeoutError):
                        pass


class CodexEngine:
    """Manages one persistent ``codex app-server --listen stdio://`` subprocess."""

    name = "codex"

    def __init__(
        self,
        bus: Any,
        tts: Any,
        *,
        preamble: str = "",
        cwd: str = "/home/cat/gideon",
        model: Optional[str] = None,
        effort: str = DEFAULT_CODEX_EFFORT,
        codex_bin: Optional[str] = None,
        codex_home: Optional[Path] = None,
        clean_text_fn: Optional[Callable[[str], Optional[str]]] = None,
        sentence_end_re: Optional[re.Pattern[str]] = None,
        directive_classifier: Optional[Callable[[str], Optional[str]]] = None,
        hot_mic_window_s: float = DEFAULT_HOT_MIC_WINDOW_S,
    ) -> None:
        self.bus = bus
        self.tts = tts
        self.preamble = preamble
        self.cwd = str(Path(cwd).expanduser().resolve())
        self.model = (model or os.environ.get("GIDEON_CODEX_MODEL") or DEFAULT_CODEX_MODEL).strip()
        self.effort = effort if effort in CODEX_EFFORT_LEVELS else DEFAULT_CODEX_EFFORT
        self.codex_bin = (codex_bin or os.environ.get("GIDEON_CODEX_BIN") or "codex").strip()
        self.codex_home = Path(codex_home).expanduser() if codex_home else None
        self._clean_text_fn = clean_text_fn or _default_clean_text
        self._sentence_end_re = sentence_end_re or _DEFAULT_SENTENCE_END
        self._directive_classifier = directive_classifier
        self._hot_mic_window_s = hot_mic_window_s

        # BaseEngine & host bindings.
        self.is_turn_active = False
        self.current_tool: Optional[dict[str, Any]] = None
        self.turn_start_time = 0.0
        self.wake_listener: Any = None
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self._needs_item_space = False

        self._transport: Optional[_JsonRpcStdioTransport] = None
        self._thread_id: Optional[str] = None
        self._active_turn_id: Optional[str] = None
        self._cancelled_turn_ids: deque[str] = deque(maxlen=32)
        self._turn_generation = 0
        self._active_turn_gen = 0
        self._tool_seq = 0
        self._tools: dict[str, dict[str, Any]] = {}
        self._item_has_delta: set[str] = set()
        self._item_phases: dict[str, str] = {}
        self._commentary_buffers: dict[str, str] = {}
        self._watchdog_task: Optional[asyncio.Task[None]] = None
        self._finish_task: Optional[asyncio.Task[None]] = None
        self._restart_lock = asyncio.Lock()
        self._last_start_time = 0.0
        self._consecutive_start_failures = 0
        self._closed = False

    @property
    def proc(self):
        return self._transport.proc if self._transport is not None else None

    @property
    def conversation_id(self) -> Optional[str]:
        return self._thread_id

    def _make_transport(self) -> _JsonRpcStdioTransport:
        home_dir = ensure_gideon_codex_home(
            self.codex_home,
            cwd=self.cwd,
            model=self.model,
            effort=self.effort,
        )
        env = _build_codex_env(home_dir)
        argv = [_resolve_codex_bin(self.codex_bin), "app-server", "--listen", "stdio://"]
        return _JsonRpcStdioTransport(
            argv,
            cwd=self.cwd,
            env=env,
            on_notification=self._on_notification,
            on_disconnect=self._on_disconnect,
        )

    async def start(self, effort: Optional[str] = None) -> None:
        self._closed = False
        if effort in CODEX_EFFORT_LEVELS:
            self.effort = effort
        self.bus.current_effort = self.effort

        if self._transport is not None and self._transport.is_alive and self._thread_id:
            return

        logger.info(
            "Initializing Gideon's Codex Engine (model=%s, effort=%s)...",
            self.model,
            self.effort,
        )
        await self._discard_transport()
        transport = self._make_transport()
        self._transport = transport
        try:
            await transport.start()
            await transport.request(
                "initialize",
                {"clientInfo": {"name": "gideon", "version": "0.2.0"}, "capabilities": None},
                timeout=20.0,
            )
            thread_params: dict[str, Any] = {
                "model": self.model,
                "cwd": self.cwd,
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
            }
            if self.preamble:
                thread_params["developerInstructions"] = self.preamble
            thread_resp = await transport.request("thread/start", thread_params, timeout=20.0)
            thread_obj = thread_resp.get("thread") if isinstance(thread_resp, dict) else None
            thread_id = thread_obj.get("id") if isinstance(thread_obj, dict) else None
            if not thread_id:
                raise RuntimeError(f"Codex thread/start returned unexpected response: {thread_resp!r}")
            self._thread_id = str(thread_id)
            self._last_start_time = time.monotonic()
            logger.info("Codex Engine connected and ready. (thread=%s)", self._thread_id)
        except BaseException:
            await self._discard_transport()
            raise

    async def _discard_transport(self) -> None:
        transport = self._transport
        self._transport = None
        self._thread_id = None
        self._active_turn_id = None
        if transport is not None:
            await transport.close()

    def _engine_alive(self) -> bool:
        return bool(
            not self._closed
            and self._transport is not None
            and self._transport.is_alive
            and self._thread_id
        )

    def _engine_notice(self, text: str) -> None:
        self.bus.current_reply = text
        self.bus.set_state("idle", text)
        self.tts.queue_sentence(text)

    async def _reconnect_backoff(self, attempt: int) -> None:
        delay = min(1.0 * (2 ** max(0, attempt - 1)), 4.0)
        await asyncio.sleep(delay)

    async def _ensure_engine_alive(self) -> bool:
        if self._engine_alive():
            return True
        if self._closed:
            return False

        async with self._restart_lock:
            if self._engine_alive():
                return True
            if self._closed:
                return False

            if self._last_start_time > 0 and (time.monotonic() - self._last_start_time) < 15.0:
                self._consecutive_start_failures += 1
            else:
                self._consecutive_start_failures = 0

            if self._consecutive_start_failures >= 3:
                logger.error("Codex dropped %d times right after startup; waiting for next prompt", self._consecutive_start_failures)
                self._engine_notice("Codex keeps dropping. Please try again shortly.")
                return False

            if self._consecutive_start_failures > 0:
                await self._reconnect_backoff(self._consecutive_start_failures)
                if self._closed:
                    return False

            try:
                await self.start(effort=self.effort)
                return self._engine_alive()
            except Exception as exc:
                logger.warning("Codex reconnect failed (%s); will retry on next prompt", exc)
                self._reset_turn()
                self._engine_notice("Couldn't reconnect Codex right now. Please try again.")
                return False

    async def _on_disconnect(self, returncode: Optional[int], stderr_summary: str) -> None:
        if self._closed:
            return
        was_active = self.is_turn_active
        logger.warning(
            "Codex app-server disconnected (code=%s, active=%s)%s",
            returncode,
            was_active,
            f": {stderr_summary}" if stderr_summary else "",
        )
        self._thread_id = None
        self._active_turn_id = None
        if was_active:
            self._turn_generation += 1
            await self.tts.interrupt()
            self._reset_turn()
            self._engine_notice("Codex disconnected mid-response. Please repeat that.")

    async def set_effort(self, new_effort: str) -> bool:
        if new_effort not in CODEX_EFFORT_LEVELS:
            logger.warning("Unknown Codex effort level %r", new_effort)
            return False
        if new_effort == self.effort:
            return True
        logger.info("Switching Codex effort: %s -> %s (in-thread, 0ms respawn)", self.effort, new_effort)
        self.effort = new_effort
        self.bus.current_effort = new_effort
        return True

    async def _cancel_inflight_turn(self, *, clear_transcript: bool) -> bool:
        self._turn_generation += 1
        self._stop_watchdog()
        if self._finish_task and self._finish_task is not asyncio.current_task():
            self._finish_task.cancel()
            await asyncio.gather(self._finish_task, return_exceptions=True)
        self._finish_task = None
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self._needs_item_space = False
        self.bus.current_reply = ""
        self._tools.clear()
        self._item_has_delta.clear()
        self._item_phases.clear()
        self._commentary_buffers.clear()
        await self.tts.interrupt()
        if clear_transcript:
            self.bus.clear_transcript()

        thread_id = self._thread_id
        turn_id = self._active_turn_id
        if turn_id:
            self._cancelled_turn_ids.append(turn_id)
        self._active_turn_id = None
        self._reset_turn()

        if thread_id and turn_id and self._transport and self._transport.is_alive:
            try:
                await self._transport.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    timeout=2.0,
                )
                return True
            except Exception as exc:
                logger.warning("Codex turn/interrupt failed: %s", exc)
                return False
        return True

    async def send_prompt(self, text: str, *, display_text: Optional[str] = None) -> None:
        if not text.strip():
            return

        if self.is_turn_active or self._active_turn_id:
            await self._cancel_inflight_turn(clear_transcript=False)
        else:
            await self.tts.interrupt()

        # Overlap the Fish live WS handshake with LLM thinking time so the
        # first spoken sentence does not pay the connect cost.
        prewarm = getattr(self.tts, "prewarm_ws", None)
        if asyncio.iscoroutinefunction(prewarm):
            asyncio.create_task(prewarm())

        if not await self._ensure_engine_alive():
            return

        shown_text = display_text.strip() if isinstance(display_text, str) and display_text.strip() else text
        self.sentence_buffer = ""
        self.accumulated_reply = ""
        self._needs_item_space = False
        self._tools.clear()
        self._item_has_delta.clear()
        self._item_phases.clear()
        self._commentary_buffers.clear()
        self.bus.set_user_transcript(shown_text)
        self.bus.current_reply = ""
        print(f"\nYou: {shown_text}\n", flush=True)

        self._turn_generation += 1
        gen = self._turn_generation
        self._active_turn_gen = gen
        if self._finish_task and self._finish_task is not asyncio.current_task():
            self._finish_task.cancel()
            self._finish_task = None

        self.turn_start_time = time.time()
        self.is_turn_active = True
        self.bus.set_state("conversing", "LLM: Thinking…")
        self._stop_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        payload_text = text
        if self._directive_classifier is not None:
            directive_source = display_text if display_text is not None else text
            try:
                directive = self._directive_classifier(directive_source)
            except Exception:
                directive = None
            if directive and directive != self.preamble and "[Directive: Voice Operating Mode" not in directive:
                payload_text = f"{directive}\n\nUser: {text}"

        transport = self._transport
        thread_id = self._thread_id
        if transport is None or not thread_id:
            self._reset_turn()
            self._engine_notice("Codex engine is unavailable. Please try again.")
            return

        try:
            resp = await transport.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": payload_text}],
                    "effort": self.effort,
                    "summary": "none",
                },
                timeout=30.0,
            )
            if gen != self._turn_generation:
                return
            turn_obj = resp.get("turn") if isinstance(resp, dict) else None
            turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
            if turn_id and self._active_turn_id is None:
                self._active_turn_id = str(turn_id)
        except Exception as exc:
            logger.warning("Codex turn/start failed: %s", exc)
            if gen == self._turn_generation:
                self._active_turn_id = None
                self._reset_turn()
                self._engine_notice("My Codex connection failed. Please try that again.")

    async def interrupt(self, hard: bool = False) -> bool:
        ok = await self._cancel_inflight_turn(clear_transcript=True)
        if hard and not ok and not self._closed:
            await self._discard_transport()
        return ok

    def _stop_watchdog(self) -> None:
        if self._watchdog_task and self._watchdog_task is not asyncio.current_task():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def _reset_turn(self) -> None:
        self.is_turn_active = False
        self.current_tool = None
        self.bus.current_tool = None
        self._stop_watchdog()
        self.bus.set_state("idle")

    async def _watchdog_loop(self) -> None:
        try:
            while self.is_turn_active:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if not self.is_turn_active or not self.current_tool or self.current_tool.get("status") != "running":
                    break
                if (
                    self.tts._is_speaking
                    or not self.tts._queue.empty()
                    or self.bus.current_state in ("speaking", "listening", "hotmic")
                ):
                    continue
                elapsed = int(time.time() - self.turn_start_time)
                t_label = self.current_tool.get("label") or "working on the solution"
                checkin = f"Still running {t_label}, {elapsed} seconds in."
                logger.info("[Watchdog Check-in] %s", checkin)
                self.tts.queue_sentence(checkin)
        except asyncio.CancelledError:
            pass

    def _speak_sentence(self, sentence: str) -> None:
        clean = self._clean_text_fn(sentence)
        if not clean:
            return
        print(f"Gideon: {clean}", flush=True)
        if not self.tts._is_speaking and self.bus.current_state != "speaking":
            self.bus.set_state("conversing", "LLM: Synthesizing voice…")
        self.tts.queue_sentence(clean)

    def _append_commentary_text(self, item_id: str, text: str) -> None:
        if not item_id or not text:
            return
        buffer = self._commentary_buffers.get(item_id, "") + text
        while True:
            match = self._sentence_end_re.search(buffer)
            if not match:
                break
            sentence = buffer[:match.end()].strip()
            buffer = buffer[match.end():]
            if sentence:
                self._speak_sentence(sentence)
        self._commentary_buffers[item_id] = buffer

    def _flush_commentary_tail(self, item_id: str) -> None:
        tail = self._commentary_buffers.pop(item_id, "").strip()
        if tail:
            self._speak_sentence(tail)

    def _append_spoken_text(self, text: str) -> None:
        if not text:
            return
        if self.current_tool and self.current_tool.get("status") == "running":
            self.current_tool["status"] = "done"
            self.bus.current_tool = dict(self.current_tool)
        if not self.tts._is_speaking and self.bus.current_state != "speaking":
            self.bus.set_state("conversing", "LLM: Formulating speech…")

        self.sentence_buffer += text
        if (
            self._needs_item_space
            and self.accumulated_reply
            and not self.accumulated_reply[-1].isspace()
            and not text[0].isspace()
        ):
            self.accumulated_reply += " "
        self._needs_item_space = False
        self.accumulated_reply += text
        self.bus.current_reply = self.accumulated_reply

        while True:
            match = self._sentence_end_re.search(self.sentence_buffer)
            if not match:
                break
            sentence = self.sentence_buffer[:match.end()].strip()
            self.sentence_buffer = self.sentence_buffer[match.end():]
            if sentence:
                self._speak_sentence(sentence)

    def _flush_tail(self) -> None:
        tail = self.sentence_buffer.strip()
        self.sentence_buffer = ""
        if tail:
            clean = self._clean_text_fn(tail)
            if self.accumulated_reply and not self.accumulated_reply.endswith(tail):
                if not self.accumulated_reply.endswith(" "):
                    self.accumulated_reply += " "
                self.accumulated_reply += tail
            elif not self.accumulated_reply:
                self.accumulated_reply = tail
            self.bus.current_reply = self.accumulated_reply
            if clean:
                print(f"Gideon: {clean}", flush=True)
                self.tts.queue_sentence(clean)

    def _matches_active_turn(self, params: dict[str, Any]) -> bool:
        if not self.is_turn_active or self._active_turn_gen != self._turn_generation:
            return False
        thread_id = params.get("threadId")
        if thread_id and self._thread_id and thread_id != self._thread_id:
            return False
        turn_id = params.get("turnId")
        if not turn_id and isinstance(params.get("turn"), dict):
            turn_id = params["turn"].get("id")
        if turn_id and str(turn_id) in self._cancelled_turn_ids:
            return False
        if turn_id and self._active_turn_id and str(turn_id) != self._active_turn_id:
            return False
        return True

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            if self._matches_active_turn(params):
                turn_obj = params.get("turn")
                tid = str(turn_obj.get("id")) if isinstance(turn_obj, dict) and turn_obj.get("id") else None
                if tid and tid not in self._cancelled_turn_ids and self._active_turn_id is None:
                    self._active_turn_id = tid
            return

        if not self._matches_active_turn(params):
            return

        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            if isinstance(item_id, str) and item_id:
                self._item_has_delta.add(item_id)
                if self._item_phases.get(item_id) == "commentary":
                    self._append_commentary_text(item_id, str(params.get("delta") or ""))
                    return
            delta = str(params.get("delta") or "")
            self._append_spoken_text(delta)
            return

        if method == "item/started":
            item = params.get("item")
            if isinstance(item, dict):
                if item.get("type") == "agentMessage":
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id:
                        self._item_phases[item_id] = str(item.get("phase") or "")
                    return
                self._on_tool_started(item)
            return

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                self._on_item_completed(item)
            return

        if method == "turn/completed":
            self._on_turn_completed(params)

    def _on_tool_started(self, item: dict[str, Any]) -> None:
        meta = _format_tool_meta(item)
        if meta is None:
            return
        self._tool_seq += 1
        item_id = str(item.get("id") or f"item_{self._tool_seq}")
        record = {
            "id": f"t_{self._tool_seq}",
            "name": meta["name"],
            "target": meta["target"],
            "label": meta["label"],
            "status": "running",
        }
        self._tools[item_id] = record
        self.current_tool = record
        self.bus.current_tool = dict(record)
        self.bus.set_state("toolcall", detail=record["label"])
        logger.info("[Tool Execution] %s", record["label"])

    def _on_item_completed(self, item: dict[str, Any]) -> None:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if item_type == "agentMessage":
            phase = str(item.get("phase") or self._item_phases.get(item_id) or "")
            if phase == "commentary":
                if item_id and item_id not in self._item_has_delta:
                    text = str(item.get("text") or "")
                    if text:
                        self._item_has_delta.add(item_id)
                        self._append_commentary_text(item_id, text)
                self._flush_commentary_tail(item_id)
                return
            if item_id and item_id not in self._item_has_delta:
                text = str(item.get("text") or "")
                if text:
                    self._append_spoken_text(text)
            self._flush_tail()
            self._needs_item_space = bool(self.accumulated_reply)
            return

        record = self._tools.get(item_id) if item_id else None
        if record is None:
            return
        exit_code = item.get("exitCode")
        failed = (
            item.get("status") in ("failed", "error")
            or bool(item.get("error"))
            or (isinstance(exit_code, int) and exit_code != 0)
        )
        record["status"] = "error" if failed else "done"
        if self.current_tool and self.current_tool.get("id") == record["id"]:
            self.current_tool = record
            self.bus.current_tool = dict(record)
        if not self.tts._is_speaking and self.bus.current_state == "toolcall":
            self.bus.set_state("conversing", "LLM: Thinking…")

    def _on_turn_completed(self, params: dict[str, Any]) -> None:
        turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        status = str(turn_obj.get("status") or "completed").lower()
        gen = self._turn_generation
        self._active_turn_id = None

        if status == "interrupted":
            return

        if self.current_tool and self.current_tool.get("status") == "running":
            self.current_tool["status"] = "error" if status == "failed" else "done"
            self.bus.current_tool = dict(self.current_tool)

        if status == "failed" and not self.accumulated_reply.strip():
            err = turn_obj.get("error")
            logger.error("Codex turn failed: %s", err)
            self._append_spoken_text("That request failed on Codex. Please try again.")

        self._flush_tail()
        if self._finish_task and self._finish_task is not asyncio.current_task():
            self._finish_task.cancel()
        self._finish_task = asyncio.create_task(self._finish_turn(gen))

    async def _finish_turn(self, generation: int) -> None:
        try:
            await self.tts.wait_until_done()
        except asyncio.CancelledError:
            return
        if generation != self._turn_generation or not self.is_turn_active:
            return
        self._reset_turn()
        if getattr(self, "wake_listener", None):
            self.wake_listener.arm_hot_mic(timeout_s=self._hot_mic_window_s)
        print("\n[Turn Complete] Gideon is ready.\n", flush=True)

    async def close(self) -> None:
        self._closed = True
        self._turn_generation += 1
        current = asyncio.current_task()
        for task in (self._watchdog_task, self._finish_task):
            if task and task is not current and not task.done():
                task.cancel()
        tasks = [t for t in (self._watchdog_task, self._finish_task) if t and t is not current]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._watchdog_task = None
        self._finish_task = None
        try:
            await self.tts.interrupt()
        except Exception:
            pass
        self._reset_turn()
        await self._discard_transport()
