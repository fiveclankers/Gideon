from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable when pytest is invoked without ``python -m``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acp_transport import ACPEOF, ACPError, ACPTransport  # noqa: E402

_FAKE_AGENT = '''\
import json
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "echo"


def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\\n")
    sys.stdout.flush()


def read_msg():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except ValueError:
        return {}


def respond(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def fail(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    send({"jsonrpc": "2.0", "id": rid, "error": err})


def is_request(msg):
    return "method" in msg and "id" in msg


def wait_ready():
    while True:
        msg = read_msg()
        if msg is None:
            return False
        if msg.get("method") == "client/ready" and "id" not in msg:
            return True


def await_response(rid):
    while True:
        msg = read_msg()
        if msg is None:
            return None
        if "method" not in msg and "id" in msg:
            return msg


def forward(tag, msg):
    send({"jsonrpc": "2.0", "method": "test/inbound_response",
          "params": {"tag": tag,
                     "id": msg.get("id") if msg else None,
                     "result": msg.get("result") if msg else None,
                     "error": msg.get("error") if msg else None}})


def tail():
    while True:
        msg = read_msg()
        if msg is None:
            return
        if is_request(msg):
            respond(msg["id"], {"ok": True})
        elif msg.get("method") == "test/ping":
            send({"jsonrpc": "2.0", "method": "test/pong", "params": {}})


def main():
    if MODE == "echo":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                respond(msg["id"], {"method": msg["method"], "echo": msg.get("params")})
    elif MODE == "out_of_order":
        buffered = []
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                buffered.append(msg)
                if len(buffered) == 2:
                    respond(buffered[1]["id"], {"which": "second"})
                    respond(buffered[0]["id"], {"which": "first"})
                    buffered = []
    elif MODE == "notify_then_result":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                for i in range(5):
                    send({"jsonrpc": "2.0", "method": "session/update",
                          "params": {"index": i}})
                respond(msg["id"], {"stopReason": "end_turn"})
    elif MODE == "collision":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if not is_request(msg):
                continue
            rid = msg["id"]
            if rid == 5:
                send({"jsonrpc": "2.0", "id": 5, "method": "sampling/createMessage",
                      "params": {"prompt": "hello"}})
                inbound = await_response(5)
                respond(rid, {"outbound": True,
                              "inbound_result": inbound.get("result") if inbound else None})
            else:
                respond(rid, {"outbound": True, "id": rid})
    elif MODE == "inbound_zero":
        if not wait_ready():
            return
        send({"jsonrpc": "2.0", "id": 0, "method": "custom/ping", "params": {"n": 0}})
        forward("zero", await_response(0))
        tail()
    elif MODE == "unknown_method":
        if not wait_ready():
            return
        send({"jsonrpc": "2.0", "id": 7, "method": "nope/method", "params": {}})
        forward("unknown", await_response(7))
        tail()
    elif MODE == "handler_raises":
        if not wait_ready():
            return
        send({"jsonrpc": "2.0", "id": 9, "method": "boom", "params": {}})
        forward("boom", await_response(9))
        send({"jsonrpc": "2.0", "id": 10, "method": "ok/method", "params": {}})
        forward("ok", await_response(10))
        tail()
    elif MODE == "error_response":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                fail(msg["id"], -32602, "bad params", {"field": "prompt"})
    elif MODE == "die_mid":
        read_msg()
        sys.exit(1)
    elif MODE == "big_frame":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                respond(msg["id"], {"payload": "x" * 200000})
    elif MODE == "garbage":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                sys.stdout.write("this is not json\\n")
                sys.stdout.flush()
                respond(msg["id"], {"after_garbage": True})
    elif MODE == "stray_id":
        while True:
            msg = read_msg()
            if msg is None:
                return
            if is_request(msg):
                send({"jsonrpc": "2.0", "id": 9999, "result": {"stray": True}})
                respond(msg["id"], {"real": True})
    else:
        fail(0, -32601, "unknown mode " + MODE)


main()
'''


class ACPTransportTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.agent_dir = Path(cls._tmpdir.name)
        cls.agent_path = cls.agent_dir / "fake_agent.py"
        cls.agent_path.write_text(_FAKE_AGENT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    async def _start(self, mode: str, **kwargs) -> ACPTransport:
        transport = ACPTransport(
            [sys.executable, "-u", str(self.agent_path), mode],
            cwd=str(self.agent_dir),
            log=logging.getLogger("test.acp"),
            **kwargs,
        )
        await asyncio.wait_for(transport.start(), timeout=10)

        async def _cleanup() -> None:
            try:
                await asyncio.wait_for(transport.close(), timeout=15)
            except BaseException:  # noqa: BLE001 - cleanup must never mask a failure
                pass

        self.addAsyncCleanup(_cleanup)
        return transport

    def _channel(self, transport: ACPTransport, name: str, expect: int = 1):
        event = asyncio.Event()
        box: dict = {"items": []}

        async def handler(params: dict) -> None:
            box["items"].append(params)
            if len(box["items"]) >= expect:
                event.set()

        transport.on_notification(name, handler)
        return event, box

    async def test_request_returns_result(self) -> None:
        transport = await self._start("echo")
        result = await asyncio.wait_for(
            transport.request("session/new", {"cwd": "/tmp"}), timeout=10
        )
        self.assertEqual(result["method"], "session/new")
        self.assertEqual(result["echo"], {"cwd": "/tmp"})

    async def test_concurrent_requests_resolved_out_of_order(self) -> None:
        transport = await self._start("out_of_order")
        first, second = await asyncio.wait_for(
            asyncio.gather(
                transport.request("alpha", {"n": 1}),
                transport.request("beta", {"n": 2}),
            ),
            timeout=10,
        )
        self.assertEqual(first, {"which": "first"})
        self.assertEqual(second, {"which": "second"})

    async def test_notification_handlers_fire_in_receive_order(self) -> None:
        transport = await self._start("notify_then_result")
        seen: list[int] = []
        finished = asyncio.Event()

        async def handler(params: dict) -> None:
            # Deliberately slow: with per-notification tasks the response
            # would overtake this handler.
            await asyncio.sleep(0.02)
            seen.append(params["index"])
            if len(seen) == 5:
                finished.set()

        transport.on_notification("session/update", handler)

        result = await asyncio.wait_for(
            transport.request("session/prompt", {"prompt": "hi"}), timeout=10
        )
        self.assertEqual(result, {"stopReason": "end_turn"})
        self.assertTrue(finished.is_set())
        self.assertEqual(seen, [0, 1, 2, 3, 4])

    async def test_inbound_request_id_does_not_collide_with_outbound_id(self) -> None:
        transport = await self._start("collision")
        inbound: list[dict] = []

        async def handler(params: dict) -> dict:
            inbound.append(params)
            return {"accepted": True}

        transport.on_request("sampling/createMessage", handler)

        # Consume outbound ids 1..4 so the next request is exactly id 5.
        for index in range(4):
            reply = await asyncio.wait_for(
                transport.request("ping", {"n": index}), timeout=10
            )
            self.assertEqual(reply, {"outbound": True, "id": index + 1})

        result = await asyncio.wait_for(
            transport.request("session/prompt", {"prompt": "x"}), timeout=10
        )
        # The peer sent its own request with id 5; our id 5 must still resolve
        # to the peer's *response*, not its request.
        self.assertTrue(result["outbound"])
        self.assertEqual(result["inbound_result"], {"accepted": True})
        self.assertEqual(inbound, [{"prompt": "hello"}])

    async def test_inbound_request_id_zero_is_handled(self) -> None:
        transport = await self._start("inbound_zero")

        async def handler(params: dict) -> dict:
            return {"handled": True, "n": params["n"]}

        transport.on_request("custom/ping", handler)
        event, box = self._channel(transport, "test/inbound_response")

        await asyncio.wait_for(transport.notify("client/ready"), timeout=10)
        await asyncio.wait_for(event.wait(), timeout=10)

        payload = box["items"][0]
        self.assertEqual(payload["id"], 0)
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["result"], {"handled": True, "n": 0})

    async def test_unregistered_inbound_method_gets_method_not_found(self) -> None:
        transport = await self._start("unknown_method")
        event, box = self._channel(transport, "test/inbound_response")

        await asyncio.wait_for(transport.notify("client/ready"), timeout=10)
        await asyncio.wait_for(event.wait(), timeout=10)

        payload = box["items"][0]
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["error"]["code"], -32601)
        self.assertIsNone(payload["result"])

    async def test_error_response_raises_acp_error(self) -> None:
        transport = await self._start("error_response")
        with self.assertRaises(ACPError) as caught:
            await asyncio.wait_for(
                transport.request("session/prompt", {"prompt": "x"}), timeout=10
            )
        self.assertEqual(caught.exception.code, -32602)
        self.assertEqual(caught.exception.message, "bad params")
        self.assertEqual(caught.exception.data, {"field": "prompt"})

    async def test_agent_exit_fails_pending_request_with_eof(self) -> None:
        transport = await self._start("die_mid")
        with self.assertRaises(ACPEOF):
            await asyncio.wait_for(
                transport.request("session/prompt", {"prompt": "x"}), timeout=10
            )
        self.assertTrue(transport.is_closed)

    async def test_frame_larger_than_64k_is_read(self) -> None:
        transport = await self._start("big_frame")
        result = await asyncio.wait_for(transport.request("big", {}), timeout=10)
        self.assertEqual(len(result["payload"]), 200000)
        self.assertEqual(set(result["payload"]), {"x"})

    async def test_handler_exception_returns_internal_error_and_keeps_working(
        self,
    ) -> None:
        transport = await self._start("handler_raises")

        async def boom(params: dict) -> dict:
            raise RuntimeError("handler exploded")

        async def ok(params: dict) -> dict:
            return {"ok": True}

        transport.on_request("boom", boom)
        transport.on_request("ok/method", ok)
        event, box = self._channel(transport, "test/inbound_response", expect=2)
        pong = asyncio.Event()

        async def on_pong(params: dict) -> None:
            pong.set()

        transport.on_notification("test/pong", on_pong)

        await asyncio.wait_for(transport.notify("client/ready"), timeout=10)
        await asyncio.wait_for(event.wait(), timeout=10)

        by_tag = {item["tag"]: item for item in box["items"]}
        self.assertEqual(by_tag["boom"]["id"], 9)
        self.assertEqual(by_tag["boom"]["error"]["code"], -32603)
        self.assertIsNone(by_tag["boom"]["result"])
        self.assertEqual(by_tag["ok"]["id"], 10)
        self.assertIsNone(by_tag["ok"]["error"])
        self.assertEqual(by_tag["ok"]["result"], {"ok": True})

        # Connection is still usable after the handler blew up.
        await asyncio.wait_for(transport.notify("test/ping"), timeout=10)
        await asyncio.wait_for(pong.wait(), timeout=10)

    async def test_close_is_idempotent_and_reaps_the_process(self) -> None:
        transport = await self._start("echo")
        process = transport._proc
        self.assertIsNotNone(process)
        await asyncio.wait_for(transport.request("ping", {}), timeout=10)

        await asyncio.wait_for(transport.close(), timeout=15)
        await asyncio.wait_for(transport.close(), timeout=15)

        self.assertTrue(transport.is_closed)
        self.assertIsNotNone(process.returncode)

    async def test_malformed_stdout_line_does_not_kill_the_connection(self) -> None:
        transport = await self._start("garbage")
        result = await asyncio.wait_for(transport.request("ping", {}), timeout=10)
        self.assertEqual(result, {"after_garbage": True})
        self.assertFalse(transport.is_closed)

    async def test_stray_response_id_is_discarded_not_treated_as_fifo(self) -> None:
        transport = await self._start("stray_id")
        result = await asyncio.wait_for(transport.request("ping", {}), timeout=10)
        self.assertEqual(result, {"real": True})


if __name__ == "__main__":
    unittest.main()
