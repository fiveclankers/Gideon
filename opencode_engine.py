#!/usr/bin/env python3
"""OpenCode sidecar engine for Gideon. Antigravity path is untouched.

Mirrors AntigravityEngine's observable behavior (sentence-streaming TTS,
45s watchdog, turn flags, SignalBus states) but talks to the local
OpenCode background service instead of `agy` NDJSON.

Verified against opencode v2.0.29:
- GET /api/event SSE (live-only, filter by sessionID)
- POST /api/session, POST /api/session/{id}/prompt {delivery: queue}
- POST /api/session/{id}/interrupt -> {"interrupted": true}
- POST /experimental/session/{id}/wait -> 204 on idle
- POST /api/session/{id}/model, /agent for engine switching
Terminal events are session.execution.succeeded/interrupted (not session.idle).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gideon.opencode")

SERVICE_FILE = Path.home() / ".local" / "state" / "opencode" / "service.json"

# Abbreviation-safe sentence boundary regex (mirrors gideon.py so both engines split identically).
_SENTENCE_END = re.compile(
    r'(?<!\b(?:e\.g|i\.e)\.)'
    r'(?<!\b(?:etc|mrs)\.)'
    r'(?<!\b(?:dr|mr|ms|vs)\.)'
    r'(?<!\bv\d\.\d)'
    r'(?<!\b\d\.\d)'
    r'(?<=[.!?])\s+',
    re.IGNORECASE
)


def _auth_headers() -> tuple[str, dict]:
    try:
        info = json.loads(SERVICE_FILE.read_text())
    except Exception as e:
        raise RuntimeError(f"OpenCode service file unreadable: {SERVICE_FILE}: {e}")
    url = info.get("url")
    password = info.get("password", "")
    if not url:
        raise RuntimeError("OpenCode service file has no url")
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    return url.rstrip("/"), {"authorization": f"Basic {token}"}


class OpenCodeEngine:
    """Drop-in alternative to AntigravityEngine."""

    name = "opencode"

    def __init__(self, bus, tts, directory: Optional[str] = None,
                 model: Optional[dict] = None, agent: str = "build"):
        self.bus = bus
        self.tts = tts
        self.directory = directory or str(Path(__file__).resolve().parent)
        self.model = model or {"providerID": "ifm", "id": "k2-horizon"}
        self.agent = agent
        self.session_id: Optional[str] = None
        self.is_turn_active = False
        self.current_tool: Optional[dict] = None
        self.turn_start_time = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._client = None
        self._base_url = ""
        self._headers: dict = {}
        self._sentence_buffer = ""

    async def start(self) -> None:
        import httpx

        logger.info("Initializing Gideon's OpenCode engine...")
        self._base_url, self._headers = _auth_headers()
        self._client = httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers, timeout=httpx.Timeout(300.0))
        # Create one persistent session for this run.
        r = await self._client.post("/api/session", json={"location": {"directory": self.directory}})
        r.raise_for_status()
        self.session_id = r.json()["data"]["id"]
        logger.info(f"OpenCode session {self.session_id}")
        # Engine selection (best-effort; keep server default on failure).
        try:
            await self._client.post(f"/api/session/{self.session_id}/model",
                                    json={"model": self.model})
            logger.info(f"OpenCode model -> {self.model}")
        except Exception as e:
            logger.warning(f"switchModel failed, keeping default: {e}")
        try:
            await self._client.post(f"/api/session/{self.session_id}/agent",
                                    json={"agent": self.agent})
        except Exception as e:
            logger.warning(f"switchAgent failed: {e}")
        self._sse_task = asyncio.create_task(self._subscribe_loop())
        # Give SSE a moment to connect before first prompt.
        await asyncio.sleep(0.5)
        logger.info("OpenCode engine connected and ready.")

    async def send_prompt(self, text: str) -> None:
        if not text.strip():
            return
        # A new turn always clears any stale tool telemetry from the previous turn.
        self.current_tool = None
        if not self._client or not self.session_id:
            raise RuntimeError("OpenCodeEngine not started")
        logger.info(f"User: {text}")
        self.turn_start_time = time.time()
        self.is_turn_active = True
        self._sentence_buffer = ""
        self.bus.current_reply = ""
        self.bus.set_state("conversing", "🧠 LLM: Thinking…")
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        # queue delivery = priority intervention without SIGINT, like agy directly-sent.
        r = await self._client.post(f"/api/session/{self.session_id}/prompt",
                                    json={"text": text, "delivery": "queue"})
        r.raise_for_status()

    async def interrupt(self, hard: bool = False) -> bool:
        # Mirror AntigravityEngine.interrupt(): silence TTS, cancel the
        # watchdog, and reset turn state before asking the service to stop.
        await self.tts.interrupt()
        self._sentence_buffer = ""
        self.is_turn_active = False
        self.current_tool = None
        self.bus.current_tool = None
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        self.bus.set_state("idle")
        if not self._client or not self.session_id:
            return False
        try:
            r = await self._client.post(f"/api/session/{self.session_id}/interrupt")
            r.raise_for_status()
            try:
                return bool(r.json().get("interrupted", True))
            except Exception:
                return True
        except Exception as e:
            logger.warning(f"OpenCode interrupt failed: {e}")
            return False

    async def wait(self, timeout: float = 120.0) -> bool:
        import httpx

        try:
            r = await self._client.post(
                f"/api/experimental/session/{self.session_id}/wait",
                timeout=httpx.Timeout(timeout + 5.0))
            return r.status_code in (200, 204)
        except Exception as e:
            logger.warning(f"OpenCode wait failed: {e}")
            return False

    async def close(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
            self._sse_task = None
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _watchdog_loop(self):
        """Speaks a calm check-in periodically during long turns, with collision protection."""
        try:
            while self.is_turn_active:
                await asyncio.sleep(45.0)

                # Strict Status Gate: exit if the turn ended or the tool is no longer actively running
                if not self.is_turn_active or not self.current_tool or self.current_tool.get("status") != "running":
                    break

                # Acoustic Collision Guard: Never interrupt user speech or ongoing/queued TTS playback
                if self.tts._is_speaking or not self.tts._queue.empty() or self.bus.current_state in ("speaking", "listening", "hotmic"):
                    continue

                elapsed = int(time.time() - self.turn_start_time)
                t_label = self.current_tool.get("label") or "working on the solution"
                checkin_text = f"Still running {t_label}, {elapsed} seconds in."
                logger.info(f"[Watchdog Check-in] {checkin_text}")
                self.tts.queue_sentence(checkin_text)
        except asyncio.CancelledError:
            pass

    def _speak_sentence(self, sentence: str):
        from gideon import clean_text_for_tts

        sentence = sentence.strip()
        if not sentence:
            return
        clean = clean_text_for_tts(sentence)
        if not clean:
            return
        logger.info(f"Gideon: {clean}")
        if not self.tts._is_speaking and self.bus.current_state != "speaking":
            self.bus.set_state("conversing", "LLM: Synthesizing voice…")
        self.tts.queue_sentence(clean)

    def _flush_tail(self):
        tail = self._sentence_buffer.strip()
        self._sentence_buffer = ""
        if tail:
            self._speak_sentence(tail)

    async def _finish_turn(self, how: str):
        self._flush_tail()
        await self.tts.wait_until_done()
        self.is_turn_active = False
        self.current_tool = None
        if self._watchdog_task:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        # Same follow-up window as the agy engine: keep listening briefly
        # without the wake word after a completed turn.
        wake_listener = getattr(self, "wake_listener", None)
        if wake_listener:
            from gideon import HOT_MIC_WINDOW_S
            wake_listener.arm_hot_mic(timeout_s=HOT_MIC_WINDOW_S)
        else:
            self.bus.set_state("idle")
        logger.info(f"[Turn Complete:{how}] Gideon is idle.\n")

    def _handle_event(self, ev_type: str, data: dict):
        # Returns a coroutine to await, or None.
        if not isinstance(data, dict):
            return None
        sid = data.get("sessionID") or data.get("sessionId")
        if sid and sid != self.session_id:
            return None
        if ev_type == "session.text.delta":
            if self.current_tool and self.current_tool.get("status") == "running":
                self.current_tool["status"] = "done"
                self.bus.set_state("thinking")
            delta = data.get("delta", "")
            if delta:
                self._sentence_buffer += delta
                self.bus.current_reply = getattr(self.bus, "current_reply", "") + delta
            return "buffered"
        if ev_type == "session.step.started":
            # OpenCode has no per-tool name here; keep agent for watchdog context.
            self.current_tool = {
                "name": data.get("agent") or "tool",
                "label": data.get("agent") or "working on the solution",
                "status": "running",
            }
            self.bus.set_state("toolcall", detail=self.current_tool["label"])
            return None
        if ev_type in ("session.execution.succeeded", "session.execution.interrupted"):
            return "finish"
        if ev_type == "session.step.failed":
            # A failed step may still be followed by retry; only finish on execution.*.
            logger.info(f"[Step Failed] {str(data)[:200]}")
            return None
        return None

    async def _subscribe_loop(self):
        import httpx

        backoff = 1.0
        while True:
            try:
                async with self._client.stream("GET", "/api/event",
                                               headers={"Accept": "text/event-stream"}) as resp:
                    resp.raise_for_status()
                    backoff = 1.0
                    cur_event = "message"
                    cur_data_lines: list[str] = []
                    async for line in resp.aiter_lines():
                        if line.startswith(":"):
                            continue
                        if line == "":
                            if cur_data_lines:
                                raw = "\n".join(cur_data_lines)
                                cur_data_lines = []
                                try:
                                    payload = json.loads(raw)
                                except Exception:
                                    cur_event = "message"
                                    continue
                                # Payload shape: {id, type, data, ...}
                                ev_type = payload.get("type", cur_event)
                                data = payload.get("data", {})
                                action = self._handle_event(ev_type, data)
                                if action == "finish":
                                    await self._finish_turn(ev_type)
                                elif action == "buffered":
                                    while True:
                                        m = _SENTENCE_END.search(self._sentence_buffer)
                                        if not m:
                                            break
                                        sent = self._sentence_buffer[:m.end()].strip()
                                        self._sentence_buffer = self._sentence_buffer[m.end():]
                                        if sent:
                                            self._speak_sentence(sent)
                                cur_event = "message"
                            continue
                        if line.startswith("event:"):
                            cur_event = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            cur_data_lines.append(line[len("data:"):].strip())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"OpenCode SSE disconnected ({e}), retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)
