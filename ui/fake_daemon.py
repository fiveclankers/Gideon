#!/usr/bin/env python3
"""Stand-in for gideon.py: hosts /tmp/gideon-ui.sock and broadcasts
newline-delimited JSON testing all GIDEON states (idle, listening,
toolcall with dynamic labels, thinking, speaking with streamed transcripts, hotmic).
"""
import asyncio
import json
import math
import os
import random

SOCK = "/tmp/gideon-ui.sock"
clients: set[asyncio.StreamWriter] = set()
current_state = "idle"
FAKE_ENGINE = os.environ.get("GIDEON_FAKE_ENGINE", "agy")


def send(obj: dict) -> None:
    if obj.get("event") == "state" and "engine" not in obj:
        obj["engine"] = FAKE_ENGINE
    line = (json.dumps(obj) + "\n").encode()
    for w in list(clients):
        try:
            w.write(line)
        except Exception:
            clients.discard(w)


async def on_connect(_reader, writer):
    clients.add(writer)
    send({"event": "state", "val": current_state})
    try:
        await writer.wait_closed()
    finally:
        clients.discard(writer)


async def stream_rms(state: str, duration: float):
    steps = int(duration * 25)
    for i in range(steps):
        t = i / 25.0
        if state == "listening":
            env = 0.5 + 0.5 * math.sin(t * 2.6)
            val = max(0.0, min(1.0, env * (0.25 + 0.6 * random.random())))
        elif state == "speaking":
            env = abs(math.sin(t * 7.2)) * (0.55 + 0.45 * math.sin(t * 1.4))
            val = max(0.0, min(1.0, 0.18 + 0.7 * env + 0.1 * random.random()))
        else:
            val = 0.0
        send({"event": "rms", "val": round(val, 3)})
        await asyncio.sleep(1 / 25)


async def stream_gideon_sentence(sentence: str, audio_duration: float):
    """Simulates sliding window subtitle streaming synced to voice audio duration."""
    words = sentence.split()
    interval = audio_duration / max(1, len(words))
    for i in range(len(words)):
        start = max(0, i - 6)
        window = " ".join(words[start : i + 1])
        send({"event": "transcript", "speaker": "gideon", "text": window})
        await stream_rms("speaking", interval)


async def scenario_loop():
    global current_state
    while True:
        # 1. Listening to user
        current_state = "listening"
        send({"event": "state", "val": "listening"})
        await stream_rms("listening", 2.2)

        # Complete user query emitted in single packet at end of speech
        send({
            "event": "transcript",
            "speaker": "user",
            "text": "Can you check the current GPU memory usage and optimize?"
        })
        await asyncio.sleep(0.8)

        # 2. Thinking
        current_state = "thinking"
        send({"event": "state", "val": "thinking"})
        await stream_rms("thinking", 1.8)

        # 3. Tool Call execution (Amber Canopy / strict toolcall binding)
        current_state = "toolcall"
        send({"event": "state", "val": "toolcall", "detail": "run_command: nvidia-smi --query-gpu"})
        await stream_rms("toolcall", 2.6)

        # 4. Thinking post-tool
        current_state = "thinking"
        send({"event": "state", "val": "thinking"})
        await stream_rms("thinking", 1.4)

        # 5. Speaking with sliding window synced to audio duration
        current_state = "speaking"
        send({"event": "state", "val": "speaking"})
        await stream_gideon_sentence(
            "GPU memory is currently operating at normal parameters with 3.2 gigabytes allocated.",
            audio_duration=3.4,
        )

        # 6. Idle (triggers smooth dock and 2400ms subtitle fade)
        current_state = "idle"
        send({"event": "state", "val": "idle"})
        await stream_rms("idle", 4.0)

        # 7. Hotmic test
        current_state = "hotmic"
        send({"event": "state", "val": "hotmic"})
        await stream_rms("hotmic", 2.0)


async def main():
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    server = await asyncio.start_unix_server(on_connect, path=SOCK)
    print("Gideon fake daemon listening on", SOCK, flush=True)
    async with server:
        await scenario_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(SOCK):
            os.unlink(SOCK)
