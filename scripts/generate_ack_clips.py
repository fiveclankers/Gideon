#!/usr/bin/env python3
"""Pre-generate and peak-normalize Gideon voice ack WAV clips via Fish Audio.

Uses Gideon's exact Fish Audio voice (reference_id 98655a12fa944e26b274c535e5e03842,
model s2.1-pro-free) to synthesize non-repetitive acknowledgments into
sounds/acks/<category>/, peak-normalizes all clips for crisp playback, and
generates soft chimes sounds/empty_soft.wav and sounds/heard_soft.wav.
"""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import os
from pathlib import Path
import sys
import wave

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path.home() / ".config" / "gideon" / "gideon.env"
SOUNDS_DIR = ROOT / "sounds"
ACKS_DIR = SOUNDS_DIR / "acks"

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_REF_ID = "98655a12fa944e26b274c535e5e03842"
FISH_MODEL = "s2.1-pro-free"

CLIP_SETS: dict[str, list[tuple[str, str]]] = {
    "listen_start": [
        ("listen_start_01.wav", "What?"),
        ("listen_start_02.wav", "Mmhmm?"),
        ("listen_start_03.wav", "Yeah?"),
        ("listen_start_04.wav", "Go ahead."),
        ("listen_start_05.wav", "Listening."),
        ("listen_start_06.wav", "Talk to me."),
        ("listen_start_07.wav", "Sup?"),
        ("listen_start_08.wav", "Shoot."),
        ("listen_start_09.wav", "I'm here."),
        ("listen_start_10.wav", "Hit me."),
        ("listen_start_11.wav", "Yo."),
        ("listen_start_12.wav", "What's up?"),
        ("listen_start_13.wav", "Yes?"),
        ("listen_start_14.wav", "Speak."),
        ("listen_start_15.wav", "Alright, go."),
        ("listen_start_16.wav", "Mmm?"),
        ("listen_start_17.wav", "What now?"),
        ("listen_start_18.wav", "Yeah, what's good?"),
        ("listen_start_19.wav", "Hm?"),
        ("listen_start_20.wav", "Go on."),
        ("listen_start_21.wav", "Ready."),
        ("listen_start_22.wav", "Yeah, go."),
        ("listen_start_23.wav", "Tell me."),
        ("listen_start_24.wav", "What's the move?"),
        ("listen_start_25.wav", "Lay it on me."),
        ("listen_start_26.wav", "Ears open."),
        ("listen_start_27.wav", "Mm, yeah?"),
        ("listen_start_28.wav", "Hit it."),
        ("listen_start_29.wav", "Talk."),
        ("listen_start_30.wav", "I'm listening."),
        ("listen_start_31.wav", "Whatcha need?"),
        ("listen_start_32.wav", "Yep?"),
        ("listen_start_33.wav", "Fire away."),
        ("listen_start_34.wav", "Mmhm, go."),
        ("listen_start_35.wav", "Sure, go on."),
    ],
    "ask_agy": [
        ("ask_agy_01.wav", "Passing that over to Antigravity now."),
        ("ask_agy_02.wav", "Handing it off to Antigravity, give it a sec."),
        ("ask_agy_03.wav", "Alright, spinning up Antigravity for you."),
        ("ask_agy_04.wav", "Sending that straight to Antigravity."),
        ("ask_agy_05.wav", "Waking Antigravity up, hang tight."),
        ("ask_agy_06.wav", "Let's get Antigravity on this."),
        ("ask_agy_07.wav", "On it, looping in Antigravity now."),
        ("ask_agy_08.wav", "Throwing that to Antigravity."),
        ("ask_agy_09.wav", "Antigravity's taking this one."),
        ("ask_agy_10.wav", "Routing that to Antigravity, one sec."),
        ("ask_agy_11.wav", "Alright, asking Antigravity now."),
        ("ask_agy_12.wav", "Pinging Antigravity with that."),
        ("ask_agy_13.wav", "Let's hand that over to Antigravity."),
        ("ask_agy_14.wav", "Booting Antigravity up for this."),
        ("ask_agy_15.wav", "Sending it to Antigravity, hang on."),
    ],
    "check_agy": [
        ("check_agy_01.wav", "Checking on Antigravity right now."),
        ("check_agy_02.wav", "Let me peek at what Antigravity's up to."),
        ("check_agy_03.wav", "Hold on, checking Antigravity's status."),
        ("check_agy_04.wav", "Let's see where Antigravity's at."),
        ("check_agy_05.wav", "Taking a quick look at Antigravity."),
        ("check_agy_06.wav", "Checking in on it right now."),
        ("check_agy_07.wav", "Let's check the logs real quick."),
        ("check_agy_08.wav", "Peeking at Antigravity's progress."),
        ("check_agy_09.wav", "Seeing how far along Antigravity is."),
        ("check_agy_10.wav", "One sec, checking Antigravity."),
        ("check_agy_11.wav", "Let me see if it's wrapped up."),
        ("check_agy_12.wav", "Checking what Antigravity's doing."),
    ],
    "bye_bye": [
        ("bye_bye_01.wav", "Catch you later."),
        ("bye_bye_02.wav", "Going dark. Holler if you need me."),
        ("bye_bye_03.wav", "Shutting it down. Peace."),
        ("bye_bye_04.wav", "Alright, stepping back."),
        ("bye_bye_05.wav", "Done and dusted. Later."),
        ("bye_bye_06.wav", "Cool, I'll get out of your hair."),
        ("bye_bye_07.wav", "Unloading everything. See ya."),
        ("bye_bye_08.wav", "Later, going quiet."),
        ("bye_bye_09.wav", "Shutting down the engines. Peace."),
        ("bye_bye_10.wav", "Alright, talk later."),
    ],
    "music_play": [
        ("music_play_01.wav", "Putting something on."),
        ("music_play_02.wav", "Spinning up a track."),
        ("music_play_03.wav", "Got you, playing something from your library."),
        ("music_play_04.wav", "Rolling some music."),
        ("music_play_05.wav", "Alright, queuing something up."),
        ("music_play_06.wav", "Dropping a track now."),
        ("music_play_07.wav", "Let's get some tunes going."),
        ("music_play_08.wav", "Cueing that up."),
        ("music_play_09.wav", "Putting on a vibe."),
        ("music_play_10.wav", "Alright, music coming up."),
        ("music_play_11.wav", "Shuffling something good."),
        ("music_play_12.wav", "On it, firing up a track."),
        ("music_play_13.wav", "Gotcha, hitting play."),
        ("music_play_14.wav", "Let's spin something."),
        ("music_play_15.wav", "Coming right up."),
    ],
}


def _load_fish_key() -> str:
    env_key = os.environ.get("FISH_API_KEY", "").strip()
    if env_key:
        return env_key
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            if k.lstrip("\ufeff").strip() == "FISH_API_KEY":
                return v.strip().strip('"').strip("'")
    return ""


def _validate_wav(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 2048:
        return False
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() > 0 and wf.getframerate() > 0
    except Exception:
        return False


def _normalize_wav_file(path: Path, *, target_peak: float = 0.88, max_gain: float = 5.0) -> bool:
    """Peak-normalize a 16-bit PCM WAV file in place if its peak is below target."""
    if not _validate_wav(path):
        return False
    with wave.open(str(path), "rb") as wf:
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if sw != 2 or not pcm:
        return False

    arr = array.array("h")
    arr.frombytes(pcm[: len(pcm) & ~1])
    if not arr:
        return False
    peak = max(abs(x) for x in arr)
    if peak <= 0:
        return False
    current_peak = peak / 32768.0
    if current_peak >= target_peak * 0.92:
        return False

    gain = min(max_gain, target_peak / current_peak)
    if gain <= 1.05:
        return False

    for i, s in enumerate(arr):
        val = int(s * gain)
        if val > 32767:
            val = 32767
        elif val < -32768:
            val = -32768
        arr[i] = val

    tmp = path.with_suffix(".norm.tmp")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(ch)
        out.setsampwidth(2)
        out.setframerate(sr)
        out.writeframes(arr.tobytes())
    os.replace(tmp, path)
    return True


def _write_two_note_chime(filename: str, notes: list[tuple[float, float, float]]) -> Path:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SOUNDS_DIR / filename
    sr = 44100
    samples = array.array("h")
    for freq, dur_s, amp in notes:
        total = int(sr * dur_s)
        attack = max(1, int(sr * 0.012))
        for i in range(total):
            t = i / sr
            if i < attack:
                env = i / attack
            else:
                env = math.exp(-5.0 * (i - attack) / max(1, total - attack))
            tone = math.sin(2.0 * math.pi * freq * t) + 0.15 * math.sin(4.0 * math.pi * freq * t)
            val = int(max(-32767, min(32767, 32767 * amp * env * tone)))
            samples.append(val)
    tmp = out_path.with_suffix(".wav.tmp")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    os.replace(tmp, out_path)
    return out_path


def _generate_empty_soft_chime() -> Path:
    """Create a subtle, warm two-note descending chime for empty/silent turns."""
    return _write_two_note_chime("empty_soft.wav", [(520.0, 0.09, 0.22), (390.0, 0.13, 0.18)])


def _generate_heard_soft_chime() -> Path:
    """Create a gentle ascending two-note chime confirming a voice turn was heard."""
    return _write_two_note_chime("heard_soft.wav", [(660.0, 0.07, 0.18), (880.0, 0.11, 0.18)])


def _generate_listen_soft_chime() -> Path:
    """Create a brief, warm soft chime for follow-up PTT turns within a back-and-forth."""
    return _write_two_note_chime("listen_soft.wav", [(587.3, 0.055, 0.16), (784.0, 0.085, 0.14)])


def _synth_one(client: httpx.Client, headers: dict[str, str], category: str, filename: str, text: str) -> tuple[str, str, str]:
    out_path = ACKS_DIR / category / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "text": text,
        "reference_id": FISH_REF_ID,
        "format": "wav",
        "sample_rate": 44100,
        "latency": "balanced",
    }
    try:
        resp = client.post(FISH_TTS_URL, headers=headers, json=payload)
        if resp.status_code == 200 and len(resp.content) > 2048:
            tmp = out_path.with_suffix(".wav.tmp")
            tmp.write_bytes(resp.content)
            os.replace(tmp, out_path)
            if _validate_wav(out_path):
                _normalize_wav_file(out_path)
                return ("ok", category, f"{filename} ({out_path.stat().st_size} bytes)")
            out_path.unlink(missing_ok=True)
            return ("err", category, f"{filename}: invalid WAV")
        return ("err", category, f"{filename}: HTTP {resp.status_code} {resp.text[:120]}")
    except Exception as exc:
        return ("err", category, f"{filename}: {exc}")


def main() -> int:
    force = "--force" in sys.argv[1:]
    empty_chime = _generate_empty_soft_chime()
    heard_chime = _generate_heard_soft_chime()
    listen_chime = _generate_listen_soft_chime()
    print(f"[CHIME] {empty_chime} ({empty_chime.stat().st_size} bytes)")
    print(f"[CHIME] {heard_chime} ({heard_chime.stat().st_size} bytes)")
    print(f"[CHIME] {listen_chime} ({listen_chime.stat().st_size} bytes)")

    api_key = _load_fish_key()
    if not api_key:
        print("FISH_API_KEY missing from env and ~/.config/gideon/gideon.env", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": FISH_MODEL,
    }

    todo: list[tuple[str, str, str]] = []
    skipped = 0
    normalized_existing = 0

    for category, items in CLIP_SETS.items():
        cat_dir = ACKS_DIR / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        for filename, text in items:
            out_path = cat_dir / filename
            if not force and _validate_wav(out_path):
                skipped += 1
                if _normalize_wav_file(out_path):
                    normalized_existing += 1
                continue
            todo.append((category, filename, text))

    generated = 0
    failed = 0
    if todo:
        with httpx.Client(timeout=30.0) as client:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(_synth_one, client, headers, cat, fn, txt) for cat, fn, txt in todo]
                for fut in as_completed(futs):
                    status, cat, msg = fut.result()
                    if status == "ok":
                        generated += 1
                        print(f"[OK] {cat}/{msg}")
                    else:
                        failed += 1
                        print(f"[ERR] {cat}/{msg}", file=sys.stderr)

    print(
        f"Summary: generated={generated}, skipped={skipped}, "
        f"normalized_existing={normalized_existing}, failed={failed}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
