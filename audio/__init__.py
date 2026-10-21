"""
GIDEON Audio Package.
Self-contained audio capture (wake word listener) and sound playback,
decoupled from JARVIS.
"""
from .sounds import play_sound
from .wake import AudioListener

__all__ = ["AudioListener", "play_sound"]
