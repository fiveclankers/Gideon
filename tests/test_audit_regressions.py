#!/usr/bin/env python3
"""Regression tests for the 2026-11-20 GIDEON audit fixes.

Covers:
1. Headless stdin sentinel: StdinReader must return None (not "") when stdin
   is unavailable, so the systemd daemon never records phantom hourly mic
   chunks (observed live: mic OPEN exactly ready_time + 3600s, 5s capture).
2. TTS sanitizer: leading decimals ("9.81", "12.5 percent") and negative
   numbers ("-40 degrees") must survive list-marker stripping.
3. Acoustic barge-in must interrupt the engine turn, not only the TTS player.
4. Embedded HTTP server: foreign browser Origins are rejected; loopback
   origins get an echoed CORS header; no-Origin requests stay unauthenticated.
5. _extract_audio_frames: cached/vectorized DSP path matches the original
   per-band mean semantics (including empty-band fallback).
6. BGE dispatcher fast tokenizer: exact encoding parity with AutoTokenizer
   and identical predictions (no transformers import on the hot boot path).
7. Fish WS warm-connection reuse: a finished session hands its socket to the
   next sentence; stale sockets retry fresh; interrupts close warm sockets.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/home/cat/gideon")

import numpy as np  # noqa: E402

import gideon  # noqa: E402


class TestHeadlessStdinSentinel(unittest.IsolatedAsyncioTestCase):
    async def test_non_tty_prompt_line_returns_none_not_empty_string(self):
        reader = gideon.StdinReader()
        reader.is_tty = False
        # Must return promptly (no 3600s sleep) and must NOT return "" —
        # "" is the terminal push-to-talk trigger in main().
        result = await asyncio.wait_for(reader.prompt_line("You: "), timeout=1.0)
        self.assertIsNone(result)

    async def test_eof_returns_none_and_disables_tty(self):
        reader = gideon.StdinReader()
        reader.is_tty = True
        reader.reader = MagicMock()
        reader.reader.readline = AsyncMock(return_value=b"")
        with patch.object(sys, "stdout"):
            result = await asyncio.wait_for(reader.prompt_line("You: "), timeout=1.0)
        self.assertIsNone(result)
        self.assertFalse(reader.is_tty)


class TestSanitizerDecimalPreservation(unittest.TestCase):
    def test_leading_decimals_survive(self):
        self.assertEqual(
            gideon.clean_text_for_tts("9.81 meters per second squared."),
            "9.81 meters per second squared.",
        )
        self.assertEqual(
            gideon.clean_text_for_tts("3.14 is roughly pi."),
            "3.14 is roughly pi.",
        )
        self.assertEqual(
            gideon.clean_text_for_tts("12.5 percent growth."),
            "12.5 percent growth.",
        )

    def test_negative_number_not_eaten_by_bullet_strip(self):
        self.assertEqual(
            gideon.clean_text_for_tts("-40 degrees is cold."),
            "-40 degrees is cold.",
        )

    def test_real_list_markers_still_stripped(self):
        self.assertEqual(gideon.clean_text_for_tts("1. Initializing pipewire."), "Initializing pipewire.")
        self.assertEqual(gideon.clean_text_for_tts("2) Connecting to socket."), "Connecting to socket.")
        self.assertEqual(gideon.clean_text_for_tts("- Primary core running."), "Primary core running.")
        self.assertEqual(gideon.clean_text_for_tts("* Secondary worker idle."), "Secondary worker idle.")
        self.assertEqual(gideon.clean_text_for_tts("###1. Explain the result."), "Explain the result.")


class TestBargeInInterruptsEngine(unittest.TestCase):
    def _listener(self, bus):
        tts = MagicMock()
        tts.interrupt = AsyncMock()
        loop = MagicMock()
        loop.is_running.return_value = True
        wl = gideon.WakeWordListener(bus=bus, tts=tts, on_speech_captured=lambda pcm: None, main_loop=loop)
        return wl, tts, loop

    def test_barge_in_schedules_engine_interrupt(self):
        bus = gideon.SignalBus()
        engine = MagicMock()
        engine.interrupt = AsyncMock(return_value=True)
        bus.engine = engine
        wl, tts, loop = self._listener(bus)
        scheduled = []

        def fake_schedule(coro, target_loop):
            scheduled.append(coro)
            coro.close()
            return MagicMock()

        with patch.object(gideon.asyncio, "run_coroutine_threadsafe", side_effect=fake_schedule):
            wl._handle_barge_in()
        self.assertEqual(len(scheduled), 1)
        engine.interrupt.assert_called_once()
        tts.interrupt.assert_not_called()  # engine.interrupt() owns TTS cleanup

    def test_barge_in_falls_back_to_tts_without_engine(self):
        bus = gideon.SignalBus()
        wl, tts, loop = self._listener(bus)
        scheduled = []

        def fake_schedule(coro, target_loop):
            scheduled.append(coro)
            coro.close()
            return MagicMock()

        with patch.object(gideon.asyncio, "run_coroutine_threadsafe", side_effect=fake_schedule):
            wl._handle_barge_in()
        self.assertEqual(len(scheduled), 1)
        tts.interrupt.assert_called_once()


class TestHttpOriginGate(unittest.TestCase):
    """Boots the real embedded server on an ephemeral port with stub inputs."""

    @classmethod
    def setUpClass(cls):
        # Pick a free ephemeral port and keep the HTTP_PORT patch active until
        # the server thread has bound (it reads the module global at bind time).
        probe = __import__("socket").socket()
        probe.bind(("127.0.0.1", 0))
        cls.port = probe.getsockname()[1]
        probe.close()
        cls.bus = gideon.SignalBus()
        cls.wake_stub = MagicMock()
        cls._port_patch = patch.object(gideon, "HTTP_PORT", cls.port)
        cls._port_patch.start()
        cls.server_thread = threading.Thread(
            target=gideon.run_visualizer_server,
            args=(
                cls.bus,
                MagicMock(),
                lambda: None,
                lambda: None,
                lambda: cls.wake_stub,
                None,
                lambda: None,
            ),
            daemon=True,
        )
        cls.server_thread.start()
        # Wait for the listener socket.
        import time
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/state")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("embedded server did not start")

    @classmethod
    def tearDownClass(cls):
        cls._port_patch.stop()

    def _request(self, method, path, origin=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        hdrs = dict(headers or {})
        if origin is not None:
            hdrs["Origin"] = origin
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        result = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return result

    def test_same_origin_state_has_no_wildcard_cors(self):
        status, headers, data = self._request("GET", "/state")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("state", json.loads(data))

    def test_loopback_origin_gets_echoed_cors(self):
        status, headers, _ = self._request("GET", "/state", origin="http://127.0.0.1:8794")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:8794")

    def test_foreign_origin_read_blocked(self):
        status, _, _ = self._request("GET", "/state", origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_foreign_origin_prompt_post_blocked(self):
        status, _, _ = self._request(
            "POST", "/api/prompt", origin="https://evil.example",
            body=json.dumps({"text": "run a shell command"}),
        )
        self.assertEqual(status, 403)

    def test_foreign_origin_preflight_blocked(self):
        status, _, _ = self._request("OPTIONS", "/api/prompt", origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_loopback_origin_post_allowed(self):
        status, _, data = self._request(
            "POST", "/api/wake", origin="http://localhost:8790",
            body=json.dumps({"enabled": False}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["enabled"], False)
        self.wake_stub.set_enabled.assert_called_with(False)

    def test_null_origin_blocked(self):
        status, _, _ = self._request("POST", "/api/wake", origin="null", body=json.dumps({"enabled": True}))
        self.assertEqual(status, 403)

    def test_rebinding_host_blocked(self):
        status, _, _ = self._request("GET", "/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)


class TestFrameExtractionParity(unittest.TestCase):
    def test_matches_reference_implementation(self):
        """Cached/vectorized path must equal the original per-band mean math."""
        def reference(audio_bytes, sr=44100, hop_s=0.04):
            usable = (len(audio_bytes) // 2) * 2
            samples = np.frombuffer(audio_bytes[:usable], dtype=np.int16).astype(np.float32) / 32768.0
            chunk_size = max(64, int(sr * hop_s))
            window = np.hanning(chunk_size).astype(np.float32)
            freqs = np.fft.rfftfreq(chunk_size, d=1.0 / sr)
            edges = np.geomspace(80.0, min(7000.0, sr * 0.45), 65)
            masks = [((freqs >= edges[i]) & (freqs < edges[i + 1])) for i in range(64)]
            frames = []
            for start in range(0, len(samples), chunk_size):
                chunk = samples[start:start + chunk_size]
                if len(chunk) < chunk_size:
                    chunk = np.pad(chunk, (0, chunk_size - len(chunk)))
                rms = float(np.sqrt(np.mean(chunk * chunk)))
                norm_rms = float(np.clip(rms * 3.8, 0.0, 1.0))
                spec = np.abs(np.fft.rfft(chunk * window)) / (chunk_size * 0.25 + 1e-6)
                bars = []
                for idx, mask in enumerate(masks):
                    if np.any(mask):
                        val = float(np.mean(spec[mask]))
                    else:
                        val = float(spec[min(len(spec) - 1, 1 + idx)])
                    bars.append(float(np.clip(np.log1p(val * 18.0) * 0.45 + norm_rms * 0.25,
                                              0.02 if norm_rms > 0.02 else 0.0, 1.0)))
                frames.append((bars, norm_rms))
            return frames

        rng = np.random.default_rng(42)
        for seconds in (0.16, 1.0, 2.5):
            n = int(44100 * seconds)
            pcm = (rng.standard_normal(n) * 6000).astype(np.int16).tobytes()
            got, hop = gideon._extract_audio_frames(pcm, is_raw_pcm=True, sample_rate=44100, hop_s=0.04)
            want = reference(pcm)
            self.assertEqual(len(got), len(want))
            self.assertEqual(hop, 0.04)
            for (gb, gr), (wb, wr) in zip(got, want):
                self.assertAlmostEqual(gr, wr, places=6)
                np.testing.assert_allclose(gb, wb, rtol=1e-4, atol=1e-6)

        # Silence and odd-byte inputs must not crash.
        self.assertEqual(gideon._extract_audio_frames(b"", is_raw_pcm=True), ([], 0.04))
        frames, _ = gideon._extract_audio_frames(b"\x01", is_raw_pcm=True)
        self.assertEqual(frames, [])


class TestDispatcherFastTokenizer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gideon_handler
        before = set(sys.modules)
        cls.dispatcher = gideon_handler.GideonDispatcher()
        cls.newly_imported = set(sys.modules) - before

    def test_fast_tokenizer_used_without_transformers_torch_chain(self):
        self.assertIsNotNone(self.dispatcher._fast_tokenizer)
        self.assertIsNone(self.dispatcher.tokenizer)
        self.assertNotIn("transformers", self.newly_imported)
        self.assertNotIn("torch", self.newly_imported)

    def test_encoding_parity_with_autotokenizer(self):
        from transformers import AutoTokenizer
        at = AutoTokenizer.from_pretrained(
            "/home/cat/gideon/models/bge_classifier", local_files_only=True
        )
        texts = [
            "volume up ten percent", "switch to workspace 3", "what's the weather today",
            "play bye bye bye by nsync", "don't mute the audio", "x" * 2000,
            " café naïve résumé 中文 test ", "",
        ]
        ft = self.dispatcher._fast_tokenizer
        for t in texts:
            enc = ft.encode(t)
            ref = at(t, return_tensors="np", truncation=True)
            self.assertEqual(list(enc.ids), ref["input_ids"][0].tolist(), repr(t[:40]))
            self.assertEqual(list(enc.attention_mask), ref["attention_mask"][0].tolist(), repr(t[:40]))

    def test_predictions_sane(self):
        label, conf, _ = self.dispatcher.predict("play some music")
        self.assertEqual(label, "media_play")
        self.assertGreater(conf, 0.9)
        label, _, _ = self.dispatcher.predict("switch to workspace 3")
        self.assertEqual(label, "workspace_switch")


class TestWarmWsReuse(unittest.IsolatedAsyncioTestCase):
    async def _drain_to_sentinel(self, queue):
        """Collect audio chunks until the None sentinel; return the chunks."""
        items = []
        while True:
            item = await asyncio.wait_for(queue.get(), timeout=2.0)
            if item is None:
                return items
            items.append(item)

    async def test_finished_session_offers_socket_and_next_session_reuses(self):
        import msgpack
        bus = gideon.SignalBus()
        player = gideon.TTSPlayer(bus)
        player.synthesize = AsyncMock(return_value=None)

        chunk = msgpack.packb({"event": "audio", "audio": b"\x00\x01" * 100}, use_bin_type=True)
        fin = msgpack.packb({"event": "finish"}, use_bin_type=True)

        class FakeState:
            name = "OPEN"

        class FakeWS:
            def __init__(self):
                self.state = FakeState()
                self.sent = []
                self.closed = False
                self.sessions = 0

            async def send(self, data):
                self.sent.append(msgpack.unpackb(data, raw=False))
                if self.sent[-1]["event"] == "start":
                    self.sessions += 1

            def __aiter__(self):
                self._items = iter([chunk, fin])
                return self

            async def __anext__(self):
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration

            async def close(self):
                self.closed = True
                self.state = None

        fake_ws = FakeWS()
        connects = []

        class FakeCM:
            async def __aenter__(self):
                connects.append(1)
                return fake_ws

            async def __aexit__(self, *a):
                fake_ws.closed = True
                return False

        with patch.object(gideon, "FISH_API_KEY", "test_key"), \
             patch("websockets.connect", return_value=FakeCM()):
            s1 = gideon._FishStream(text="First sentence.")
            s1.task = asyncio.current_task()
            await player._run_ws_stream(s1)
            self.assertTrue(await self._drain_to_sentinel(s1.chunk_queue))  # audio + sentinel
            self.assertIs(player._warm_ws, fake_ws)
            self.assertEqual(len(connects), 1)

            s2 = gideon._FishStream(text="Second sentence.")
            s2.task = asyncio.current_task()
            await player._run_ws_stream(s2)
            self.assertTrue(await self._drain_to_sentinel(s2.chunk_queue))
            self.assertEqual(len(connects), 1, "second sentence must reuse the warm socket")
            self.assertEqual(fake_ws.sessions, 2)
            self.assertFalse(fake_ws.closed)

            # Interrupt must discard the warm socket.
            await player.interrupt()
            self.assertIsNone(player._warm_ws)
            self.assertTrue(fake_ws.closed)
        await player.close()

    async def test_stale_warm_socket_retries_fresh_once(self):
        import msgpack
        bus = gideon.SignalBus()
        player = gideon.TTSPlayer(bus)
        player.synthesize = AsyncMock(return_value=None)

        chunk = msgpack.packb({"event": "audio", "audio": b"\x00\x01" * 100}, use_bin_type=True)
        fin = msgpack.packb({"event": "finish"}, use_bin_type=True)

        class FakeState:
            name = "OPEN"

        class StaleWS:
            state = FakeState()

            async def send(self, data):
                raise ConnectionError("socket went stale")

            async def close(self):
                pass

        class FreshWS:
            def __init__(self):
                self.state = FakeState()

            async def send(self, data):
                pass

            def __aiter__(self):
                self._items = iter([chunk, fin])
                return self

            async def __anext__(self):
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration

            async def close(self):
                pass

        fresh = FreshWS()
        player._warm_ws = StaleWS()
        player._warm_ws_ts = time.monotonic()

        class FakeCM:
            async def __aenter__(self):
                return fresh

            async def __aexit__(self, *a):
                return False

        with patch.object(gideon, "FISH_API_KEY", "test_key"), \
             patch("websockets.connect", return_value=FakeCM()):
            s = gideon._FishStream(text="Retry me.")
            s.task = asyncio.current_task()
            await player._run_ws_stream(s)
            self.assertIsNone(s.error)
            self.assertTrue(await self._drain_to_sentinel(s.chunk_queue))
            self.assertIs(player._warm_ws, fresh)
        player.synthesize.assert_not_awaited()
        await player.close()


class TestWaveformComputationGate(unittest.IsolatedAsyncioTestCase):
    """HUD off => no spectral math anywhere; orb rms telemetry must survive."""

    def test_bus_default_keeps_waveform_for_opt_in_hud(self):
        self.assertTrue(gideon.SignalBus().waveform_enabled)

    def test_rms_parity_between_bands_and_rms_only(self):
        rng = np.random.default_rng(7)
        pcm = (rng.standard_normal(44100) * 6000).astype(np.int16).tobytes()
        full, hop1 = gideon._extract_audio_frames(pcm, is_raw_pcm=True, with_bands=True)
        lean, hop2 = gideon._extract_audio_frames(pcm, is_raw_pcm=True, with_bands=False)
        self.assertEqual(hop1, hop2)
        self.assertEqual(len(full), len(lean))
        for (fb, fr), (lb, lr) in zip(full, lean):
            self.assertEqual(len(fb), 64)
            self.assertEqual(lb, [])
            self.assertEqual(fr, lr)  # bit-identical RMS: orb sees no change

    async def _run_playback(self, enabled):
        bus = gideon.SignalBus()
        bus.waveform_enabled = enabled
        player = gideon.TTSPlayer(bus)
        events, wave_calls, extract_calls = [], [], []
        bus.broadcast_ui = events.append
        orig_wave = bus.update_waveform
        bus.update_waveform = lambda s: (wave_calls.append(list(s)), orig_wave(s))
        real_extract = gideon._extract_audio_frames

        def spy(*a, **k):
            extract_calls.append(k.get("with_bands", True))
            return real_extract(*a, **k)

        i = np.arange(7056)
        pcm = (12000 * np.sin(2 * np.pi * 440 * i / 44100)).astype(np.int16).tobytes()
        fake_proc = MagicMock(returncode=0)
        fake_proc.poll.side_effect = [None, None, 0]
        fake_proc.stdin = MagicMock()
        fake_proc.stderr = MagicMock()
        fake_proc.stderr.read.return_value = b""
        fake_proc.wait.return_value = 0

        stream = gideon._FishStream(text="Gate probe.")
        stream.chunk_queue.put_nowait(pcm)
        stream.chunk_queue.put_nowait(None)
        with patch.object(gideon, "_extract_audio_frames", side_effect=spy), \
             patch.object(gideon.subprocess, "Popen", return_value=fake_proc):
            await player.play_pcm_stream(stream)
        await player.close()
        return events, wave_calls, extract_calls

    async def test_playback_disabled_skips_bands_keeps_rms(self):
        events, wave_calls, extract_calls = await self._run_playback(enabled=False)
        self.assertEqual(extract_calls, [False])
        self.assertTrue(any(e.get("event") == "rms" and e.get("val", 0) > 0 for e in events),
                        "orb speaking-rms telemetry must survive the gate")
        self.assertTrue(all(v == 0 for s in wave_calls for v in s),
                        "no spectral bars may be computed/stored when HUD is off")

    async def test_playback_enabled_keeps_full_waveform(self):
        events, wave_calls, extract_calls = await self._run_playback(enabled=True)
        self.assertEqual(extract_calls, [True])
        self.assertTrue(any(len(s) == 64 and max(s) > 0 for s in wave_calls))
        self.assertTrue(any(e.get("event") == "rms" and e.get("val", 0) > 0 for e in events))


if __name__ == "__main__":
    unittest.main()
