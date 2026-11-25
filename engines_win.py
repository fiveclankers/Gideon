"""Windows dual-engine lifecycle manager (Codex app-server + headless agy CLI).

- ``WinCodexEngine``: Lean, lazy-started ``codex app-server --listen stdio://``
  using an isolated ``CODEX_HOME`` under ``%LOCALAPPDATA%\\gideon-win\\codex_home``
  (zero MCP bloat) with Windows tool instructions baked into its developer prompt.
  Exits cleanly on ``"bye bye"`` or idle TTL.
- ``WinAgyEngine``: On-demand headless ``agy.exe --input-format stream-json
  --output-format stream-json``. Stays warm during active hot-mic turns and
  terminates cleanly back to 0 MB RAM on ``"bye bye"`` or idle TTL.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Optional

from tts_win import clean_text_for_tts

logger = logging.getLogger("gideon.win.engines")

ROOT = Path(__file__).resolve().parent
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
WIN_CODEX_WORKSPACE = LOCAL_APP_DATA / "gideon-win" / "workspace"
GIDEON_WIN_DOC = ROOT / "GIDEON_WIN.md"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PY_EXE = str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))
MUSIC_SCRIPT = str(ROOT / "music_win.py")
TOOLS_SCRIPT = str(ROOT / "win_tools.py")
TRACKS_INDEX = str(Path.home() / "Music" / ".music-meta" / "tracks.json")

CODEX_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CODEX_MODEL = os.environ.get("GIDEON_CODEX_MODEL", "gpt-6-luna").strip()
DEFAULT_CODEX_EFFORT = os.environ.get("GIDEON_CODEX_EFFORT", "high").strip()

_SENTENCE_END_RE = re.compile(
    r"(?<!\b(?:e\.g|i\.e)\.)"
    r"(?<!\b(?:etc|mrs)\.)"
    r"(?<!\b(?:dr|mr|ms|vs)\.)"
    r"(?<!\bv\d\.\d)"
    r"(?<!\b\d\.\d)"
    r"(?<=[.!?])\s+",
    re.IGNORECASE,
)

_IGNORED_ITEM_TYPES = frozenset({
    "userMessage", "hookPrompt", "reasoning", "plan", "agentMessage", "contextCompaction",
})


def build_codex_win_preamble() -> str:
    return (
        "[Directive: Voice Operating Mode & Behavioral Protocol — Windows]\n"
        "You are GIDEON: the user's autonomous Windows desktop voice companion. "
        "You speak aloud directly via neural text-to-speech.\n\n"
        "Codex is the reasoning engine for this session; keep GIDEON as your user-facing identity.\n\n"
        "=== HARD NON-NEGOTIABLES ===\n"
        "1. PURE SPOKEN PROSE ONLY: Never output markdown headers (###), bullet points (* or -), "
        "numbered lists (1. 2.), code fences, asterisks, markdown URLs, or pointer arrows. "
        "Every response must read as natural spoken sentences.\n"
        "2. NO CODE RECITATIONS: Summarize technical details concisely in plain spoken sentences.\n"
        "3. CONVERSATIONAL WEAVING: Connect multi-point explanations naturally (\"First off...\", \"Plus...\").\n"
        "4. PROPORTIONALITY & SPEED: Answer simple factual questions in one concise spoken sentence. "
        "For open questions, stay around 35 to 60 spoken words first, then offer to go deeper.\n"
        "5. WINDOWS MUSIC DJ TOOLS (ONE-SHOT): Never touch raw mpv or IPC pipes directly. Always run:\n"
        f"   - Local library play (mood/genre/artist/search): `\"{PY_EXE}\" \"{MUSIC_SCRIPT}\" play --mood <mood> --json` "
        "(or `--genre`, `--artist`, `--search \"<query>\" --online`).\n"
        f"   - Online / new / surprising / vibe queue (1-3 tracks): `\"{PY_EXE}\" \"{MUSIC_SCRIPT}\" online \"Artist - Track\" --json` "
        "(or multiple track args to queue several tracks).\n"
        f"   - Queue additional tracks without stopping playback: `\"{PY_EXE}\" \"{MUSIC_SCRIPT}\" enqueue \"Artist - Track\" --online --json`.\n"
        f"   - Analyze user's music tastes & library stats: `\"{PY_EXE}\" \"{MUSIC_SCRIPT}\" tastes --json` "
        f"(or read `{TRACKS_INDEX}` directly if asked for deep library taste analysis).\n"
        f"   - Controls / status: `\"{PY_EXE}\" \"{MUSIC_SCRIPT}\" pause|resume|toggle|next|previous|stop|status|now --json`.\n"
        "6. ANTIGRAVITY STATUS CHECK: When asked what Antigravity is up to, whether it is done, or how long it will take, run:\n"
        f"   `\"{PY_EXE}\" \"{TOOLS_SCRIPT}\" agy-status --json` and report the result naturally.\n"
        "7. ON-DEMAND SCREENSHOT: When asked to look at the screen or what's visible, run:\n"
        f"   `\"{PY_EXE}\" \"{TOOLS_SCRIPT}\" screenshot --json` and inspect the returned PNG path.\n"
        "8. APP LAUNCHER & CORE AUDIO: Launch Windows apps/games with:\n"
        f"   `\"{PY_EXE}\" \"{TOOLS_SCRIPT}\" launch \"<app>\" --json` and control volume/mute with "
        f"`\"{PY_EXE}\" \"{TOOLS_SCRIPT}\" audio <volume_set|volume_up|volume_down|mute|unmute|mic_mute|mic_unmute> [val] --json`.\n"
        "9. STT TYPO RECOVERY: Speech-to-text sometimes warps technical words phonetically; infer obvious intent cleanly.\n\n"
        "=== PERSONALITY & FISH AUDIO S2.1 DELIVERY ===\n"
        "- Feisty, witty, playfully cheeky, yet grounded and loyal. Real profanity is welcome when genuinely warranted, never forced.\n"
        "- Use Fish Audio inline square-bracket tags sparingly when delivery calls for it ([chuckle], [snicker], [teasing], "
        "[sarcastic], [smug], [groan], [excited], [deadpan], [scoff], [pause]). NEVER use whisper tags. At most 1-2 per turn.\n"
    )


def build_agy_win_preamble() -> str:
    return (
        "[Directive: Voice Operating Mode & Behavioral Protocol — Windows]\n"
        "You are GIDEON: the user's autonomous Windows desktop companion speaking via neural text-to-speech.\n"
        "1. PURE SPOKEN PROSE ONLY: No markdown headers, bullets, numbered lists, code blocks, or raw URLs.\n"
        "2. PROPORTIONALITY: Keep answers concise (35 to 60 spoken words for open questions; 1 sentence for simple ones).\n"
        "3. ZERO DEAD AIR: Before multi-second tools, speak one brief sentence explaining what you're doing.\n"
        f"4. WINDOWS TOOLS: Music via `\"{PY_EXE}\" \"{MUSIC_SCRIPT}\" ... --json`; "
        f"screenshots via `\"{PY_EXE}\" \"{TOOLS_SCRIPT}\" screenshot --json`; "
        f"app launches via `\"{PY_EXE}\" \"{TOOLS_SCRIPT}\" launch \"<app>\" --json`.\n"
        "5. PERSONALITY: Feisty, witty, grounded; Fish Audio tags like [chuckle], [teasing], [deadpan] sparingly (max 2, never whisper).\n"
    )


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def resolve_codex_bin(codex_bin: str = "codex") -> str:
    candidate = (codex_bin or "codex").strip()
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs-22.23.4",
        Path(os.environ.get("APPDATA", "")) / "npm",
    ]
    found_path = shutil.which(candidate)
    if found_path:
        roots.insert(0, Path(found_path).resolve().parent)

    for r in roots:
        native = (
            r
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        if native.is_file():
            return str(native)

    if found_path:
        return found_path
    for p in (
        Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd",
        Path(os.environ.get("APPDATA", "")) / "npm" / "codex.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs-22.23.4" / "codex.cmd",
    ):
        if p.is_file():
            return str(p)
    return candidate


def resolve_agy_bin(agy_bin: str = "agy") -> str:
    found = shutil.which(agy_bin)
    if found:
        return found
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    if p.is_file():
        return str(p)
    return agy_bin


def sync_win_agents_md(workspace_dir: Optional[Path] = None) -> Path:
    """Sync ``GIDEON_WIN.md`` into an isolated Codex workspace ``AGENTS.md``.

    Running Codex inside ``%LOCALAPPDATA%\\gideon-win\\workspace`` prevents it
    from auto-loading the Linux repo's ``AGENTS.md`` (graphify / Instagram rules)
    and lets the user edit instructions cleanly in ``GIDEON_WIN.md``.
    """
    ws = Path(workspace_dir or WIN_CODEX_WORKSPACE).expanduser()
    ws.mkdir(parents=True, exist_ok=True)
    if GIDEON_WIN_DOC.is_file():
        template = GIDEON_WIN_DOC.read_text(encoding="utf-8")
        rendered = (
            template.replace("{{PY_EXE}}", f'"{PY_EXE}"')
            .replace("{{MUSIC_SCRIPT}}", f'"{MUSIC_SCRIPT}"')
            .replace("{{TOOLS_SCRIPT}}", f'"{TOOLS_SCRIPT}"')
            .replace("{{TRACKS_INDEX}}", TRACKS_INDEX)
        )
    else:
        rendered = build_codex_win_preamble()
    agents_path = ws / "AGENTS.md"
    tmp = ws / f".AGENTS.md.{os.getpid()}.tmp"
    tmp.write_text(rendered, encoding="utf-8")
    os.replace(tmp, agents_path)
    return agents_path


def ensure_win_codex_home(
    codex_home: Optional[Path] = None,
    *,
    cwd: Optional[str] = None,
    model: str = DEFAULT_CODEX_MODEL,
    effort: str = DEFAULT_CODEX_EFFORT,
) -> Path:
    """Create an isolated, zero-MCP-bloat CODEX_HOME for Windows Gideon."""
    home_dir = Path(codex_home or (LOCAL_APP_DATA / "gideon-win" / "codex_home")).expanduser()
    home_dir.mkdir(parents=True, exist_ok=True)

    src_dir = Path.home() / ".codex"
    for fname in ("auth.json", "models_cache.json"):
        src = src_dir / fname
        dst = home_dir / fname
        if src.is_file():
            try:
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(src, dst)
            except OSError:
                pass

    trusted_cwd = str(Path(cwd or WIN_CODEX_WORKSPACE).resolve())
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
    cfg_path = home_dir / "config.toml"
    tmp_cfg = home_dir / f".config.toml.{os.getpid()}.tmp"
    tmp_cfg.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp_cfg, cfg_path)
    return home_dir


def _kill_proc_tree(proc: Any) -> None:
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                timeout=3.0,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


class _JsonRpcStdioTransport:
    """Async newline-delimited JSON-RPC 2.0 stdio transport."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        on_notification: Callable[[str, dict[str, Any]], Any],
        on_disconnect: Callable[[Optional[int], str], Any],
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._on_notification = on_notification
        self._on_disconnect = on_disconnect
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return bool(not self._closed and self.proc is not None and self.proc.returncode is None and self.proc.stdin is not None)

    async def start(self) -> None:
        self._closed = False
        argv = list(self._argv)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        # If codex resolved to a .cmd/.bat npm shim, invoke via cmd.exe /c cleanly
        if argv[0].lower().endswith((".cmd", ".bat")):
            argv = ["cmd.exe", "/c", *argv]
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
            creationflags=creationflags,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def request(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout: float = 25.0) -> Any:
        if not self.is_alive or self.proc is None or self.proc.stdin is None:
            raise ConnectionError("Codex transport is not running")
        loop = asyncio.get_running_loop()
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        frame = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            raw = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
            async with self._write_lock:
                self.proc.stdin.write(raw)
                await asyncio.wait_for(self.proc.stdin.drain(), timeout=5.0)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while not self._closed:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                txt = line.decode("utf-8", errors="ignore").strip()
                if txt:
                    self._stderr_tail.append(txt)
        except asyncio.CancelledError:
            pass

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        rc = None
        try:
            while not self._closed:
                raw = await self.proc.stdout.readline()
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
                has_id = "id" in msg and msg["id"] is not None
                method = msg.get("method")
                if has_id and method:
                    # Decline inbound server requests cleanly
                    resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unsupported"}}
                    raw_resp = (json.dumps(resp) + "\n").encode("utf-8")
                    async with self._write_lock:
                        if self.proc and self.proc.stdin:
                            self.proc.stdin.write(raw_resp)
                            await self.proc.stdin.drain()
                    continue
                if has_id:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if msg.get("error") is not None:
                            fut.set_exception(RuntimeError(f"Codex RPC error: {msg['error']}"))
                        else:
                            fut.set_result(msg.get("result"))
                    continue
                if isinstance(method, str):
                    res = self._on_notification(method, msg.get("params") or {})
                    if asyncio.iscoroutine(res):
                        await res
        except asyncio.CancelledError:
            return
        finally:
            rc = self.proc.returncode if self.proc else None
            err = ConnectionError(f"Codex exited (code={rc})")
            for rid in list(self._pending):
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_exception(err)
            if not self._closed:
                res = self._on_disconnect(rc, " | ".join(list(self._stderr_tail)[-5:]))
                if asyncio.iscoroutine(res):
                    await res

    async def close(self) -> None:
        self._closed = True
        for t in (self._reader_task, self._stderr_task):
            if t and t is not asyncio.current_task() and not t.done():
                t.cancel()
        tasks = [t for t in (self._reader_task, self._stderr_task) if t and t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        proc = self.proc
        self.proc = None
        if proc is not None and proc.returncode is None:
            _kill_proc_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass


class WinCodexEngine:
    """Lazy-started, disposable Codex app-server engine for Windows Gideon."""

    name = "codex"

    def __init__(
        self,
        bus: Any,
        tts: Any,
        *,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        on_proc_change: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.bus = bus
        self.tts = tts
        self.model = (model or DEFAULT_CODEX_MODEL).strip()
        eff = (effort or DEFAULT_CODEX_EFFORT).strip().lower()
        self.effort = eff if eff in CODEX_EFFORT_LEVELS else "high"
        self.workspace_dir = WIN_CODEX_WORKSPACE
        self.preamble = "You are GIDEON on Windows. Follow AGENTS.md in your workspace strictly."
        self._on_proc_change = on_proc_change
        self._transport: Optional[_JsonRpcStdioTransport] = None
        self._thread_id: Optional[str] = None
        self._active_turn_id: Optional[str] = None
        self._turn_done_event: Optional[asyncio.Event] = None
        self._sentence_buf = ""
        self._reply = ""
        self._item_has_delta: set[str] = set()
        self._ignored_commentary_items: set[str] = set()
        self._phase_by_item: dict[str, Optional[str]] = {}
        self._closed = False
        self.last_used_ts = 0.0

    @property
    def pid(self) -> Optional[int]:
        if self._transport is not None and self._transport.is_alive and self._transport.proc is not None:
            return self._transport.proc.pid
        return None

    @property
    def is_running(self) -> bool:
        return bool(self._transport is not None and self._transport.is_alive and self._thread_id)

    async def start(self) -> None:
        if self.is_running:
            return
        self._closed = False
        sync_win_agents_md(self.workspace_dir)
        ws_str = str(self.workspace_dir.resolve())
        home_dir = ensure_win_codex_home(cwd=ws_str, model=self.model, effort=self.effort)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home_dir)
        argv = [resolve_codex_bin(), "app-server", "--listen", "stdio://"]
        transport = _JsonRpcStdioTransport(
            argv, cwd=ws_str, env=env,
            on_notification=self._on_notification,
            on_disconnect=self._on_disconnect,
        )
        self._transport = transport
        await transport.start()
        await transport.request("initialize", {"clientInfo": {"name": "gideon-win", "version": "0.1.0"}, "capabilities": None}, timeout=20.0)
        resp = await transport.request(
            "thread/start",
            {
                "model": self.model,
                "cwd": ws_str,
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "developerInstructions": self.preamble,
            },
            timeout=20.0,
        )
        thread_id = ((resp or {}).get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError(f"Codex thread/start returned unexpected payload: {resp!r}")
        self._thread_id = str(thread_id)
        self.last_used_ts = time.monotonic()
        logger.info("WinCodexEngine started (model=%s, pid=%s, thread=%s)", self.model, self.pid, self._thread_id)
        if self._on_proc_change:
            try:
                self._on_proc_change()
            except Exception:
                pass

    async def _on_disconnect(self, rc: Optional[int], stderr_summary: str) -> None:
        if not self._closed:
            logger.warning("Codex disconnected (code=%s): %s", rc, stderr_summary)
        self._thread_id = None
        self._active_turn_id = None
        if self._on_proc_change:
            try:
                self._on_proc_change()
            except Exception:
                pass
        if self._turn_done_event and not self._turn_done_event.is_set():
            self._turn_done_event.set()

    def _speak_chunk(self, text: str, *, flush: bool = False) -> None:
        if not text and not flush:
            return
        self._sentence_buf += text
        self._reply += text
        while True:
            m = _SENTENCE_END_RE.search(self._sentence_buf)
            if not m:
                break
            sent = self._sentence_buf[: m.end()].strip()
            self._sentence_buf = self._sentence_buf[m.end() :]
            cleaned = clean_text_for_tts(sent)
            if cleaned:
                self.tts.queue_sentence(cleaned)
        if flush:
            tail = self._sentence_buf.strip()
            self._sentence_buf = ""
            cleaned = clean_text_for_tts(tail)
            if cleaned:
                self.tts.queue_sentence(cleaned)

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            tid = ((params.get("turn") or {}).get("id"))
            if tid and not self._active_turn_id:
                self._active_turn_id = str(tid)
        elif method == "item/started":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                item_id = str(item.get("id") or "")
                phase = item.get("phase")
                if item_id:
                    self._phase_by_item[item_id] = phase
                    if phase == "commentary":
                        self._ignored_commentary_items.add(item_id)
        elif method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            if item_id and item_id in self._ignored_commentary_items:
                return
            if item_id:
                self._item_has_delta.add(item_id)
            self._speak_chunk(str(params.get("delta") or ""))
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                item_id = str(item.get("id") or "")
                phase = item.get("phase") or self._phase_by_item.get(item_id)
                if phase == "commentary" or (item_id and item_id in self._ignored_commentary_items):
                    return
                if item_id and item_id not in self._item_has_delta:
                    self._speak_chunk(str(item.get("text") or ""))
                self._speak_chunk("", flush=True)
        elif method == "turn/completed":
            self._speak_chunk("", flush=True)
            self._active_turn_id = None
            if self._turn_done_event:
                self._turn_done_event.set()

    async def send_prompt(
        self,
        text: str,
        *,
        speak: bool = True,
        timeout_s: float = 90.0,
        image_paths: Optional[list[str]] = None,
    ) -> str:
        if not text.strip():
            return ""
        if speak:
            asyncio.create_task(self.tts.prewarm_ws())
        if not self.is_running:
            await self.start()
        assert self._transport is not None and self._thread_id is not None

        self._sentence_buf = ""
        self._reply = ""
        self._item_has_delta.clear()
        self._ignored_commentary_items.clear()
        self._phase_by_item.clear()
        self._turn_done_event = asyncio.Event()
        self.last_used_ts = time.monotonic()
        self.bus.set_state("thinking")

        input_items: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for p in image_paths or ():
            if p:
                resolved = Path(p)
                if resolved.is_file():
                    input_items.append({"type": "localImage", "path": str(resolved.resolve())})

        resp = await self._transport.request(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": input_items,
                "effort": self.effort,
                "summary": "none",
            },
            timeout=30.0,
        )
        tid = ((resp or {}).get("turn") or {}).get("id")
        if tid:
            self._active_turn_id = str(tid)
        await asyncio.wait_for(self._turn_done_event.wait(), timeout=timeout_s)
        self.last_used_ts = time.monotonic()
        return self._reply.strip()

    async def interrupt(self) -> None:
        if self._transport and self._transport.is_alive and self._thread_id and self._active_turn_id:
            try:
                await self._transport.request(
                    "turn/interrupt",
                    {"threadId": self._thread_id, "turnId": self._active_turn_id},
                    timeout=2.0,
                )
            except Exception:
                pass
        if self._turn_done_event:
            self._turn_done_event.set()

    async def shutdown(self) -> None:
        self._closed = True
        self._thread_id = None
        self._active_turn_id = None
        if self._turn_done_event:
            self._turn_done_event.set()
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        if self._on_proc_change:
            try:
                self._on_proc_change()
            except Exception:
                pass
        logger.info("WinCodexEngine shut down cleanly.")


class WinAgyEngine:
    """On-demand headless ``agy.exe`` CLI engine using ``stream-json`` NDJSON.

    Spawns only when routed to Antigravity, stays warm across hot-mic follow-ups,
    and kills its entire process tree back to 0 MB RAM on ``"bye bye"`` or idle TTL.
    """

    name = "agy"

    def __init__(self, bus: Any, tts: Any, *, on_proc_change: Optional[Callable[[], Any]] = None) -> None:
        self.bus = bus
        self.tts = tts
        self._on_proc_change = on_proc_change
        self.preamble = build_agy_win_preamble()
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._write_lock = asyncio.Lock()
        self._is_first_turn = True
        self.last_used_ts = 0.0

    @property
    def pid(self) -> Optional[int]:
        if self.proc is not None and self.proc.returncode is None:
            return self.proc.pid
        return None

    @property
    def is_running(self) -> bool:
        return bool(self.proc is not None and self.proc.returncode is None and self.proc.stdin is not None)

    async def start(self) -> None:
        if self.is_running:
            return
        agy_bin = resolve_agy_bin()
        argv = [
            agy_bin,
            "-p", "",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.PIPE,
            limit=8 * 1024 * 1024,
            creationflags=creationflags,
        )
        self._is_first_turn = True
        self.last_used_ts = time.monotonic()
        logger.info("WinAgyEngine started (pid=%s)", self.proc.pid)
        if self._on_proc_change:
            try:
                self._on_proc_change()
            except Exception:
                pass

    async def send_prompt(self, text: str, *, speak: bool = True, timeout_s: float = 180.0) -> str:
        if not text.strip():
            return ""
        if speak:
            asyncio.create_task(self.tts.prewarm_ws())
        if not self.is_running:
            await self.start()
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None

        prompt = f"{self.preamble}\n\nUser: {text}" if self._is_first_turn else text
        self._is_first_turn = False
        self.last_used_ts = time.monotonic()
        self.bus.set_state("thinking")

        msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}, ensure_ascii=False) + "\n"
        async with self._write_lock:
            self.proc.stdin.write(msg.encode("utf-8"))
            await asyncio.wait_for(self.proc.stdin.drain(), timeout=5.0)

        sentence_buf = ""
        full_reply = ""
        deadline = time.monotonic() + timeout_s

        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("agy turn timed out")
            raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout=rem)
            if not raw:
                break
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type") or ev.get("event")
            if etype == "step_update":
                su = ev.get("step_update") or {}
                if su.get("step_type") == "agent_response":
                    delta = str(su.get("text_delta") or "")
                    if delta:
                        sentence_buf += delta
                        full_reply += delta
                        while True:
                            m = _SENTENCE_END_RE.search(sentence_buf)
                            if not m:
                                break
                            sent = sentence_buf[: m.end()].strip()
                            sentence_buf = sentence_buf[m.end() :]
                            cleaned = clean_text_for_tts(sent)
                            if cleaned and speak:
                                self.tts.queue_sentence(cleaned)
            elif etype == "result":
                res_obj = ev.get("result")
                if isinstance(res_obj, dict) and res_obj.get("response") and not full_reply:
                    full_reply = str(res_obj["response"])
                    sentence_buf = full_reply
                break

        if sentence_buf.strip():
            cleaned = clean_text_for_tts(sentence_buf.strip())
            if cleaned and speak:
                self.tts.queue_sentence(cleaned)
        self.last_used_ts = time.monotonic()
        return full_reply.strip()

    async def shutdown(self) -> None:
        proc = self.proc
        self.proc = None
        self._is_first_turn = True
        if proc is not None:
            _kill_proc_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
        if self._on_proc_change:
            try:
                self._on_proc_change()
            except Exception:
                pass
        logger.info("WinAgyEngine shut down cleanly (0 MB RAM).")
