"""Agent Client Protocol (ACP) stdio transport.

ACP speaks **newline-delimited JSON-RPC 2.0 over stdio, encoded as UTF-8**.
This is *not* LSP framing: there is no ``Content-Length`` header. Every
message is exactly one compact JSON object followed by a single ``\\n`` byte.
Newlines that occur inside strings are JSON-escaped, so a line break is always
a message boundary. Because of that, the stdout ``StreamReader`` must be given
a large ``limit=`` -- asyncio's 64 KiB default silently truncates a single
``session/update`` frame that carries a big diff or tool result.

Envelope kinds
--------------
Every inbound object is one of exactly three things, classified in this order:

1. ``method`` **and** an ``id`` key  -> a server->client **request**. We must
   reply exactly once, using the *same* id.
2. ``method`` and **no** ``id`` key  -> a **notification**. Fire and forget;
   nothing is sent back.
3. no ``method`` and an ``id`` key   -> a **response** to one of our outbound
   requests.

Id presence is tested with ``"id" in msg``, never with truthiness: JSON-RPC
allows ``0`` as an id, and ``if msg.get("id"):`` would misclassify it.

Two id namespaces
-----------------
Inbound ids (allocated by the peer) and outbound ids (allocated by us, from a
monotonic counter starting at 1) are completely separate. A server request
with id ``5`` must never resolve our outbound request ``5``. Correlation is by
exact id inside ``_pending``, and anything unknown is logged and dropped --
never treated as "the next response in the queue".

Ordering guarantees
-------------------
Responses and notifications are dispatched **in receive order**. Notification
handlers are awaited *inline* in the reader loop, one at a time, so a
``session/update`` can never overtake the ``session/prompt`` response that
logically follows it.

Reader must never block on human interaction
--------------------------------------------
Because the reader awaits notification handlers inline, a handler that needs
to do slow work (TTS playback, asking a human to approve a tool call, network
calls) MUST schedule that work itself -- e.g. ``asyncio.create_task(...)`` or
``loop.call_soon`` -- and return immediately. If a handler blocks, the whole
transport stalls: no further notifications, no responses, no EOF detection.
For the same reason a notification handler must not ``await request(...)`` on
this same transport: the response it waits for can only be read by the very
coroutine it is blocking. Schedule the request as a task instead.

Inbound requests are the opposite case: their handlers run in a tracked task
so the reader keeps reading, and exactly one response is written per id.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from typing import Any, Awaitable, Callable

__all__ = [
    "ACPError",
    "ACPEOF",
    "ACPTransport",
    "DEFAULT_FRAME_LIMIT",
    "STDERR_TAIL_LINES",
]

#: Default stdout frame limit. ACP messages have no length prefix, so a single
#: line may be arbitrarily large; 8 MiB comfortably covers a big tool result.
DEFAULT_FRAME_LIMIT = 8 * 1024 * 1024

#: How many trailing stderr lines to keep for diagnostics.
STDERR_TAIL_LINES = 200

#: Bounded wait for ``stdin.drain()`` before declaring the pipe wedged.
DEFAULT_DRAIN_TIMEOUT_S = 5.0

RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class ACPError(Exception):
    """A JSON-RPC ``error`` object returned by the peer."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ACPEOF(Exception):
    """The ACP connection died (EOF, broken pipe, or process exit)."""


def _consume_future_exception(fut: asyncio.Future) -> None:
    """Retrieve a future's exception so abandoned futures don't warn."""
    if fut.cancelled():
        return
    try:
        fut.exception()
    except BaseException:  # noqa: BLE001 - purely a warning suppressor
        pass


class ACPTransport:
    """Newline-delimited JSON-RPC 2.0 transport for an ACP agent subprocess."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        frame_limit: int = DEFAULT_FRAME_LIMIT,
        log: logging.Logger | None = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._frame_limit = frame_limit
        self._drain_timeout = DEFAULT_DRAIN_TIMEOUT_S
        self.log = log if log is not None else logging.getLogger(__name__)

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None

        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._next_id = 1

        self._write_lock = asyncio.Lock()
        self._request_handlers: dict[str, RequestHandler] = {}
        self._notification_handlers: dict[str, NotificationHandler] = {}
        self._inbound_tasks: set[asyncio.Task[None]] = set()
        self._answered_inbound: set[int | str] = set()

        self._stderr_tail: collections.deque[str] = collections.deque(
            maxlen=STDERR_TAIL_LINES
        )
        self._closed = False
        self._closing = False
        self._close_done: asyncio.Future[None] | None = None

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        """Spawn the agent and start the stdout/stderr pumps.

        The reader task is started here, before any request can be issued, so
        no early response can land in an unread pipe.
        """
        if self._proc is not None:
            raise RuntimeError("ACPTransport.start() called twice")
        if self._closed:
            raise RuntimeError("ACPTransport is already closed")

        kwargs: dict[str, Any] = {}
        if self._env is not None:
            kwargs["env"] = self._env

        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._frame_limit,
            **kwargs,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="acp-stdout-reader"
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="acp-stderr-reader"
        )

    # -------------------------------------------------------------- outbound

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a request and return its ``result`` object.

        Raises :class:`ACPError` for a JSON-RPC error response,
        :class:`ACPEOF` if the connection dies, and ``asyncio.TimeoutError``
        if ``timeout`` elapses first.
        """
        proc = self._require_proc()
        if self._closed:
            raise ACPEOF("ACP transport is closed")
        if proc.stdin is None or proc.stdin.is_closing():
            raise ACPEOF("ACP stdin is closed")

        loop = asyncio.get_running_loop()
        req_id = self._next_id
        self._next_id += 1

        # Register before writing: a fast peer may answer before drain() even
        # returns, and an unregistered id would be dropped as "late".
        fut: asyncio.Future[Any] = loop.create_future()
        fut.add_done_callback(_consume_future_exception)
        self._pending[req_id] = fut

        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        try:
            await self._send(message)
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def notify(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """Send a notification (no ``id``, no response expected)."""
        if self._closed:
            raise ACPEOF("ACP transport is closed")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    # --------------------------------------------------------------- handlers

    def on_request(self, method: str, handler: RequestHandler) -> None:
        """Register an async handler for inbound server->client requests.

        The handler receives ``params`` and returns the ``result`` object. It
        runs in its own task, so it may block; the reader keeps reading. Exactly
        one response is written per inbound id. If the handler raises, a
        ``-32603`` internal error is returned. With no handler registered the
        peer gets ``-32601`` method not found.
        """
        self._request_handlers[method] = handler

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        """Register an async handler for inbound notifications.

        Handlers are awaited **inline and sequentially** by the reader loop to
        preserve receive order. A handler that needs to do slow work (TTS,
        network, human approval) MUST schedule it itself -- e.g.
        ``asyncio.create_task(...)`` -- and return immediately. Blocking here
        stalls the entire transport, including response correlation and EOF
        detection. Never ``await request(...)`` on this transport from inside a
        notification handler; schedule the request as a task instead.
        """
        self._notification_handlers[method] = handler

    # ------------------------------------------------------------------ state

    @property
    def is_closed(self) -> bool:
        """True once the connection is closed or the process has exited."""
        if self._closed:
            return True
        proc = self._proc
        return proc is not None and proc.returncode is not None

    @property
    def stderr_tail(self) -> list[str]:
        """The last :data:`STDERR_TAIL_LINES` stderr lines, oldest first."""
        return list(self._stderr_tail)

    # ---------------------------------------------------------------- reading

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - defensive
            self._fail_all(ACPEOF("ACP stdout is unavailable"))
            return
        stdout = proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except ValueError:
                    # Line longer than frame_limit: StreamReader clears its
                    # buffer before raising, so we can safely keep going.
                    self.log.warning(
                        "acp: dropping oversized stdout frame (limit=%d bytes)",
                        self._frame_limit,
                    )
                    continue
                except (ConnectionResetError, BrokenPipeError):
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # One bad line must not kill a chatty session/update stream.
                    self.log.warning(
                        "acp: dropping malformed stdout line (%d bytes)", len(line)
                    )
                    continue
                if not isinstance(message, dict):
                    self.log.warning(
                        "acp: dropping non-object stdout frame (%s)",
                        type(message).__name__,
                    )
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reader must never crash silently
            self.log.exception("acp: stdout reader crashed")
        finally:
            self._fail_all(ACPEOF("ACP connection closed"))

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:  # pragma: no cover - defensive
            return
        try:
            while True:
                try:
                    line = await proc.stderr.readline()
                except ValueError:
                    continue
                except (ConnectionResetError, BrokenPipeError):
                    break
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    self._stderr_tail.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.log.debug("acp: stderr reader stopped", exc_info=True)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        """Classify one inbound envelope and route it.

        Order matters: request, then notification, then response.
        """
        if "method" in message:
            method = message["method"]
            if not isinstance(method, str):
                self.log.warning("acp: dropping frame with non-string method")
                return
            params = message.get("params")
            if not isinstance(params, dict):
                params = {}

            if "id" in message:
                self._start_inbound_request(message["id"], method, params)
                return

            handler = self._notification_handlers.get(method)
            if handler is None:
                self.log.debug("acp: unhandled notification %s", method)
                return
            try:
                # Inline await: receive order is the whole point.
                await handler(params)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a handler bug is not fatal
                self.log.exception("acp: notification handler %s raised", method)
            return

        if "id" in message:
            self._resolve_response(message)
            return

        self.log.warning("acp: dropping frame with neither method nor id")

    def _resolve_response(self, message: dict[str, Any]) -> None:
        req_id = message["id"]
        try:
            fut = self._pending.pop(req_id, None)
        except TypeError:  # unhashable id, e.g. a list
            self.log.warning("acp: dropping response with unhashable id %r", req_id)
            return
        if fut is None:
            self.log.warning(
                "acp: discarding response for unknown/late id %r", req_id
            )
            return
        if fut.done():
            self.log.debug("acp: response for already-settled id %r", req_id)
            return

        error = message.get("error")
        if error is not None:
            if isinstance(error, dict):
                code = error.get("code", -32603)
                text = error.get("message", "")
                data = error.get("data")
            else:
                code, text, data = -32603, str(error), None
            fut.set_exception(ACPError(code, text, data))
            return

        result = message.get("result")
        fut.set_result(result if isinstance(result, dict) else {})

    # ------------------------------------------------------ inbound requests

    def _start_inbound_request(
        self, req_id: int | str, method: str, params: dict[str, Any]
    ) -> None:
        task = asyncio.create_task(
            self._handle_inbound(req_id, method, params),
            name=f"acp-inbound-{method}",
        )
        self._inbound_tasks.add(task)
        task.add_done_callback(self._inbound_tasks.discard)

    async def _handle_inbound(
        self, req_id: int | str, method: str, params: dict[str, Any]
    ) -> None:
        if self._closed:
            self.log.debug(
                "acp: connection closed before inbound %r was answered", req_id
            )
            return

        handler = self._request_handlers.get(method)
        if handler is None:
            await self._respond_error(
                req_id, -32601, f"Method not found: {method}"
            )
            return

        try:
            result = await handler(params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            self.log.exception("acp: inbound handler %s raised", method)
            await self._respond_error(req_id, -32603, f"Internal error: {exc}")
            return

        if result is None:
            result = {}
        if not isinstance(result, dict):
            await self._respond_error(
                req_id, -32603, "Handler returned a non-object result"
            )
            return
        await self._respond(req_id, result)

    async def _respond(self, req_id: int | str, result: dict[str, Any]) -> None:
        if not self._claim_inbound_id(req_id):
            return
        await self._send_safe({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _respond_error(
        self, req_id: int | str, code: int, message: str
    ) -> None:
        if not self._claim_inbound_id(req_id):
            return
        await self._send_safe(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
        )

    def _claim_inbound_id(self, req_id: int | str) -> bool:
        """Reserve an inbound id; False means it was already answered."""
        try:
            if req_id in self._answered_inbound:
                self.log.warning(
                    "acp: refusing duplicate response for inbound id %r", req_id
                )
                return False
            self._answered_inbound.add(req_id)
        except TypeError:
            self.log.warning("acp: inbound id %r is unhashable", req_id)
            return False
        return True

    # ---------------------------------------------------------------- writing

    async def _send(self, message: dict[str, Any]) -> None:
        """Serialize one frame under the write lock and drain it."""
        proc = self._require_proc()
        stdin = proc.stdin
        if stdin is None or stdin.is_closing():
            raise ACPEOF("ACP stdin is closed")

        data = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )

        async with self._write_lock:
            if self._closed or stdin.is_closing():
                raise ACPEOF("ACP stdin is closed")
            try:
                stdin.write(data)
                # Bounded: a wedged peer must not hang a request forever.
                await asyncio.wait_for(stdin.drain(), timeout=self._drain_timeout)
            except asyncio.TimeoutError as exc:
                self._fail_all(ACPEOF("timed out draining ACP stdin"))
                raise ACPEOF("timed out draining ACP stdin") from exc
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                self._fail_all(ACPEOF(f"ACP stdin write failed: {exc}"))
                raise ACPEOF(f"ACP stdin write failed: {exc}") from exc

    async def _send_safe(self, message: dict[str, Any]) -> None:
        try:
            await self._send(message)
        except ACPEOF as exc:
            self.log.debug("acp: dropping write after connection loss: %s", exc)

    def _require_proc(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise ACPEOF("ACP transport has not been started")
        return self._proc

    # --------------------------------------------------------------- teardown

    def _fail_all(self, exc: BaseException) -> None:
        """Fail every pending request and mark the transport closed."""
        self._closed = True
        pending = list(self._pending.items())
        self._pending.clear()
        for req_id, fut in pending:
            if not fut.done():
                fut.set_exception(exc)
            self.log.debug("acp: failing pending request %r: %s", req_id, exc)

        reader = self._reader_task
        if (
            reader is not None
            and not reader.done()
            and reader is not asyncio.current_task()
        ):
            reader.cancel()

    async def close(self, graceful: bool = True) -> None:
        """Stop the transport and reap the subprocess. Idempotent."""
        if self._closing:
            done = self._close_done
            if done is not None and not done.done():
                try:
                    await asyncio.wait_for(asyncio.shield(done), timeout=15.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            return

        self._closing = True
        self._close_done = asyncio.get_running_loop().create_future()
        try:
            await self._close_impl(graceful)
        finally:
            done = self._close_done
            if done is not None and not done.done():
                done.set_result(None)

    async def _close_impl(self, graceful: bool) -> None:
        proc = self._proc
        self._fail_all(ACPEOF("ACP transport closed"))

        for task in list(self._inbound_tasks):
            task.cancel()
        if self._inbound_tasks:
            await asyncio.gather(*self._inbound_tasks, return_exceptions=True)
        self._inbound_tasks.clear()

        reader = self._reader_task
        if reader is not None and not reader.done():
            reader.cancel()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        self._reader_task = None

        if proc is None:
            await self._stop_stderr_task()
            return

        stdin = proc.stdin
        if stdin is not None and not stdin.is_closing():
            try:
                stdin.close()
            except (BrokenPipeError, OSError):
                pass

        if graceful:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

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
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:  # pragma: no cover - pathological
                    self.log.warning("acp: agent survived SIGKILL")

        await self._stop_stderr_task()

    async def _stop_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
