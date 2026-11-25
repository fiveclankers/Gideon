#!/usr/bin/env python3
"""Live microphone diagnostic for Gideon's Silero VAD + Faster-Whisper wake engine.

Usage:
    /home/cat/jarvis/.venv/bin/python scripts/test_wake_mic.py
"""
from __future__ import annotations

import os
import sys
import asyncio
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from audio.wake import AudioListener, DEFAULT_WAKE_MODEL, DEFAULT_WAKE_THRESHOLD, VadWhisperWakeEngine


async def _listen(engine) -> None:
    hits = 0

    def on_wake():
        nonlocal hits
        hits += 1
        print(f"Wake #{hits}; recording the command.", flush=True)

    listener = AudioListener(wake_threshold=engine.vad_threshold, on_wake_detected=on_wake)
    listener.wakeword_model = engine
    try:
        while True:
            audio = await listener.listen_for_command()
            if audio is not None:
                rms = float(np.sqrt(np.mean((audio.astype(np.float32) / 32768.0) ** 2)))
                print(f"Captured {len(audio) / listener.sample_rate:.2f}s of command audio, rms={rms:.4f}", flush=True)
    finally:
        listener.close()


def main() -> None:
    vad_thresh = float(os.environ.get("GIDEON_WAKE_THRESHOLD", str(DEFAULT_WAKE_THRESHOLD)))
    model_name = os.environ.get("GIDEON_WAKE_WHISPER_MODEL", DEFAULT_WAKE_MODEL)
    engine = VadWhisperWakeEngine(model_name=model_name, vad_threshold=vad_thresh)
    print(f"Wake engine ready: Silero VAD (thresh={vad_thresh:.2f}) + Whisper '{model_name}' ({engine.device})")
    print("Say 'Hey Gideon', 'Hi Gideon', 'Okay Gideon', or anything else (Ctrl+C to stop)...\n", flush=True)

    try:
        asyncio.run(_listen(engine))
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
