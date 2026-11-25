"""Zero-idle-RAM Speech-to-Text for Windows Gideon.

Supports two backends selected by ``GIDEON_STT_BACKEND=auto|groq|whispercpp``:
1. ``groq``: Cloud Groq Whisper (``whisper-large-v3``) when ``GROQ_API_KEY`` is set.
2. ``whispercpp``: On-demand ``bin\\whisper\\whisper-cli.exe`` with ``ggml-base.en-q5_1.bin``
   spawned only during transcription and exiting immediately to 0 MB RAM.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Optional
import wave

import httpx

logger = logging.getLogger("gideon.win.stt")

ROOT = Path(__file__).resolve().parent
WHISPER_DIR = ROOT / "bin" / "whisper"
WHISPER_MODEL_DEFAULT = WHISPER_DIR / "ggml-base.en-q5_1.bin"

WHISPER_HALLUCINATION_PHRASES: frozenset[str] = frozenset({
    "thank you",
    "thank you.",
    "thanks",
    "thanks.",
    "thanks for watching",
    "thanks for watching.",
    "thank you for watching",
    "thank you for watching.",
    "bye",
    "bye.",
    "you",
    "you.",
    "the end",
    "the end.",
    "subtitles by",
    "amara.org",
    "silence",
})

_NON_SPEECH_TAG_RE = re.compile(r"^\s*(?:\[[^\]]+\]|\([^)]+\))\s*$")


def _find_whisper_cli() -> Optional[Path]:
    env_bin = os.environ.get("GIDEON_WHISPER_CLI", "").strip()
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)
    for p in WHISPER_DIR.rglob("whisper-cli.exe"):
        return p
    for p in WHISPER_DIR.rglob("main.exe"):
        return p
    return None


def _find_whisper_model() -> Optional[Path]:
    env_model = os.environ.get("GIDEON_WHISPER_MODEL", "").strip()
    if env_model and Path(env_model).is_file():
        return Path(env_model)
    if WHISPER_MODEL_DEFAULT.is_file():
        return WHISPER_MODEL_DEFAULT
    for p in WHISPER_DIR.rglob("ggml-*.bin"):
        return p
    return None


def is_hallucination(text: str | None) -> bool:
    if not text:
        return True
    stripped = text.strip()
    if not stripped or _NON_SPEECH_TAG_RE.match(stripped):
        return True
    norm = re.sub(r"\s+", " ", stripped.lower()).strip()
    if norm in WHISPER_HALLUCINATION_PHRASES:
        return True
    if not any(ch.isalnum() for ch in norm):
        return True
    return False


def pcm16_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class WinSTT:
    """Stateless, zero-idle-RAM speech transcriber."""

    def __init__(self, backend: Optional[str] = None) -> None:
        self.backend = (backend or os.environ.get("GIDEON_STT_BACKEND") or "auto").strip().lower()
        self._http: Optional[httpx.Client] = None

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None

    def active_backend(self) -> str:
        if self.backend in {"groq", "whispercpp"}:
            return self.backend
        if os.environ.get("GROQ_API_KEY", "").strip():
            return "groq"
        return "whispercpp"

    def transcribe_wav_bytes(self, wav_bytes: bytes) -> Optional[str]:
        if not wav_bytes or len(wav_bytes) < 1024:
            return None
        mode = self.active_backend()
        text: Optional[str] = None
        if mode == "groq":
            text = self._transcribe_groq(wav_bytes)
            if text is None and _find_whisper_cli() is not None:
                logger.warning("Groq STT failed; falling back to whisper.cpp")
                text = self._transcribe_whispercpp_bytes(wav_bytes)
        else:
            text = self._transcribe_whispercpp_bytes(wav_bytes)

        if is_hallucination(text):
            logger.info("[STT] Filtered raw transcript: %r", text)
            return None
        return text.strip() if text else None

    def transcribe_wav_file(self, wav_path: Path | str) -> Optional[str]:
        path = Path(wav_path)
        if not path.is_file():
            return None
        mode = self.active_backend()
        if mode == "groq":
            return self.transcribe_wav_bytes(path.read_bytes())
        text = self._run_whispercpp_file(path)
        if is_hallucination(text):
            logger.info("[STT] Filtered raw transcript: %r", text)
            return None
        return text.strip() if text else None

    def _transcribe_groq(self, wav_bytes: bytes) -> Optional[str]:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            return None
        if self._http is None or self._http.is_closed:
            self._http = httpx.Client(timeout=15.0)
        try:
            resp = self._http.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data={
                    "model": os.environ.get("GIDEON_GROQ_STT_MODEL", "whisper-large-v3"),
                    "language": "en",
                    "response_format": "json",
                },
                files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            )
            if resp.status_code == 200:
                return str(resp.json().get("text") or "").strip()
            logger.warning("Groq STT HTTP %s: %s", resp.status_code, resp.text[:150])
        except Exception as exc:
            logger.warning("Groq STT error: %s", exc)
        return None

    def _transcribe_whispercpp_bytes(self, wav_bytes: bytes) -> Optional[str]:
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tf.write(wav_bytes)
                tmp_path = Path(tf.name)
            return self._run_whispercpp_file(tmp_path)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _run_whispercpp_file(self, wav_path: Path) -> Optional[str]:
        cli = _find_whisper_cli()
        model = _find_whisper_model()
        if cli is None or model is None:
            logger.error("whisper.cpp binary or model missing under %s", WHISPER_DIR)
            return None
        threads = str(min(4, os.cpu_count() or 4))
        cmd = [
            str(cli),
            "-m", str(model),
            "-f", str(wav_path),
            "-l", "en",
            "-t", threads,
            "-nt",
            "--no-prints",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30.0,
                creationflags=creationflags,
            )
        except Exception as exc:
            logger.error("whisper-cli failed: %s", exc)
            return None
        if proc.returncode != 0:
            logger.warning("whisper-cli exited %s: %s", proc.returncode, (proc.stderr or "")[:200])
            return None
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return " ".join(lines).strip() or None
