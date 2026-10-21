"""
Sound Effects Utility for GIDEON.
Plays non-blocking low-latency UI chimes and auditory feedback via PipeWire.
"""
from pathlib import Path
import subprocess

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"


def play_sound(sound_name: str) -> None:
    """Fire-and-forget audio chime playback (<1ms dispatch)."""
    sound_path = SOUNDS_DIR / sound_name
    if sound_path.exists():
        try:
            subprocess.Popen(
                [
                    "pw-play",
                    "--media-role", "Notification",
                    "-P", "application.name=GIDEON",
                    str(sound_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
