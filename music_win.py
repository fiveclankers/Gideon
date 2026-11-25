#!/usr/bin/env python3
"""Windows music controller for the tagged lossless library + YouTube Music.

`C:\\Users\\cat\\Music\\.music-meta\\tracks.json` is treated as authoritative and
read-only; Linux paths like ``/home/cat/Music/...`` are transparently mapped to
the local Windows ``Music`` folder at read time without mutating the index.
Playback is delegated to one detached headless ``mpv`` daemon controlled over
Windows Named Pipe JSON IPC (``\\\\.\\pipe\\gideon-mpv``).
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
import difflib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
MUSIC_DIR = Path(os.environ.get("GIDEON_MUSIC_DIR", str(Path.home() / "Music"))).expanduser()
INDEX_PATH = MUSIC_DIR / ".music-meta" / "tracks.json"
PLAYLISTS_DIR = MUSIC_DIR / ".music-meta" / "playlists"
RUNTIME_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "gideon-win" / "music"
MPV_PIPE_NAME = r"\\.\pipe\gideon-mpv"
QUEUE_FILE = RUNTIME_DIR / "music-queue.m3u"
MPV_LOG = RUNTIME_DIR / "music-mpv.log"
MPV_PID_FILE = RUNTIME_DIR / "music-mpv.pid"
_ONLINE_CACHE_DIR = RUNTIME_DIR / "online-cache"
_PLAYBACK_GEN_PATH = RUNTIME_DIR / "music-mpv.gen"
DEFAULT_LIMIT = 50
MPV_VOLUME = int(os.environ.get("GIDEON_MPV_VOLUME", "100"))

GENRE_LABELS = {
    "rock": "Rock",
    "alternative": "Alternative",
    "pop": "Pop",
    "hip-hop": "Hip-Hop",
    "rnb-soul": "R&B/Soul",
    "electronic": "Electronic",
    "jazz": "Jazz",
    "funk-disco": "Funk/Disco",
    "folk-acoustic": "Folk/Acoustic",
    "blues": "Blues",
    "metal": "Metal",
    "soundtrack": "Soundtrack",
    "world": "World",
    "ambient-chill": "Ambient/Chill",
}
GENRE_ALIASES = {label.casefold(): slug for slug, label in GENRE_LABELS.items()}

MOODS = [
    "chill",
    "focus",
    "energetic",
    "uplifting",
    "late-night",
    "nostalgic",
    "romantic",
    "melancholy",
]

_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav"}


class MusicError(Exception):
    """A user-facing error, with the process exit code to use."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code


class MusicArgumentParser(argparse.ArgumentParser):
    """argparse that emits a JSON error when --json was requested."""

    def error(self, message: str) -> None:
        if "--json" in sys.argv[1:]:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
            raise SystemExit(1)
        super().error(message)


def as_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def number(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f.is_integer() else round(f, 2)


def format_time(seconds) -> str:
    if seconds is None:
        return "--:--"
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def resolve_track_path(raw_path: str | None) -> str:
    """Translate Linux Music paths (``/home/cat/Music/...``) to Windows paths."""
    if not raw_path:
        return ""
    text = str(raw_path).strip()
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("file://"):
        from urllib.parse import unquote, urlparse
        text = unquote(urlparse(text).path)

    normalized_fwd = text.replace("\\", "/")
    m = re.match(r"^/home/[^/]+/Music/(.*)$", normalized_fwd, re.IGNORECASE)
    if m:
        rel = m.group(1).lstrip("/")
        candidate = MUSIC_DIR / Path(rel)
        return os.path.normpath(str(candidate))
    if normalized_fwd.lower().startswith("/music/"):
        rel = normalized_fwd[len("/music/"):].lstrip("/")
        return os.path.normpath(str(MUSIC_DIR / Path(rel)))
    return os.path.normpath(text)


def path_key(path: str | None) -> str:
    resolved = resolve_track_path(path)
    if not resolved:
        return ""
    if resolved.startswith(("http://", "https://")):
        return resolved
    return os.path.normcase(os.path.normpath(resolved))


def _build_disk_track(p: Path) -> dict:
    artist = p.parent.parent.name if p.parent.parent != MUSIC_DIR else p.parent.name
    if artist in ("Lossless", MUSIC_DIR.name):
        artist = ""
    title = re.sub(r"^\d+\s+", "", p.stem).strip() or p.stem
    album = p.parent.name if p.parent != MUSIC_DIR else ""
    raw_genres: list[str] = []
    length_sec = None

    try:
        import mutagen

        mf = mutagen.File(str(p), easy=True)
        if mf is not None:
            tags = getattr(mf, "tags", None) or mf
            if hasattr(tags, "get"):
                def _first(key: str) -> str:
                    val = tags.get(key)
                    if isinstance(val, (list, tuple)) and val:
                        return str(val[0]).strip()
                    return str(val).strip() if val else ""

                t_artist = _first("artist") or _first("albumartist")
                t_title = _first("title")
                t_album = _first("album")
                t_genre = _first("genre")
                if t_artist:
                    artist = t_artist
                if t_title:
                    title = t_title
                if t_album:
                    album = t_album
                if t_genre:
                    raw_genres = [t_genre]
            info = getattr(mf, "info", None)
            if info is not None and getattr(info, "length", None):
                length_sec = round(float(info.length), 2)
    except Exception:
        pass

    return {
        "id": None,
        "artist": artist,
        "title": title,
        "album": album,
        "canonical_genre": None,
        "moods": [],
        "raw_genres": raw_genres,
        "path": os.path.normpath(str(p)),
        "length_sec": length_sec,
        "source": "disk-scan",
    }


def _scan_music_dir_fallback(exclude_keys: set[str] | None = None) -> list[dict]:
    if not MUSIC_DIR.is_dir():
        return []
    tracks: list[dict] = []
    seen = exclude_keys or set()
    for p in sorted(MUSIC_DIR.rglob("*")):
        if not p.is_file() or p.suffix.casefold() not in _AUDIO_EXTS:
            continue
        if ".music-meta" in p.parts:
            continue
        if seen and path_key(str(p)) in seen:
            continue
        tracks.append(_build_disk_track(p))
    return tracks


def load_library() -> tuple[dict, list[dict]]:
    if INDEX_PATH.is_file():
        try:
            data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MusicError(f"Could not read index {INDEX_PATH}: {exc}")
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
            raise MusicError(f"Malformed index: {INDEX_PATH}")
        tracks: list[dict] = []
        existing_keys: set[str] = set()
        for item in data["tracks"]:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            copy = dict(item)
            copy["path"] = resolve_track_path(item["path"])
            tracks.append(copy)
            k = path_key(copy["path"])
            if k:
                existing_keys.add(k)
        tracks.extend(_scan_music_dir_fallback(exclude_keys=existing_keys))
        return data, tracks

    scanned = _scan_music_dir_fallback()
    if not scanned:
        raise MusicError(f"Index not found ({INDEX_PATH}) and no audio files in {MUSIC_DIR}")
    return {"genres": [], "moods": MOODS, "tracks": scanned}, scanned


def path_map(tracks: list[dict]) -> dict[str, dict]:
    return {path_key(t["path"]): t for t in tracks if t.get("path")}


def track_summary(track: dict) -> dict:
    genre = track.get("canonical_genre")
    return {
        "id": track.get("id"),
        "artist": track.get("artist") or "",
        "title": track.get("title") or Path(track.get("path", "")).stem,
        "album": track.get("album") or "",
        "genre": genre,
        "genre_label": GENRE_LABELS.get(genre, genre),
        "moods": list(track.get("moods") or []),
        "path": track.get("path"),
        "duration": number(track.get("length_sec")),
    }


def resolve_mood(value: str) -> str:
    key = value.strip().casefold().replace("_", "-").replace(" ", "-")
    if key in MOODS:
        return key
    raise MusicError(f"Unknown mood {value!r}. Valid moods: {', '.join(MOODS)}", code=2)


def resolve_genre(value: str) -> str:
    raw = value.strip()
    if raw.casefold() in GENRE_ALIASES:
        return GENRE_ALIASES[raw.casefold()]
    key = raw.casefold().replace("_", "-").replace(" ", "-")
    if key in GENRE_LABELS:
        return key
    valid = ", ".join(f"{slug} ({label})" for slug, label in GENRE_LABELS.items())
    raise MusicError(f"Unknown genre {value!r}. Valid genres: {valid}", code=2)


def _listify(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _matches(text: str | None, query: str) -> bool:
    return bool(text) and query.casefold() in str(text).casefold()


def track_matches(
    track: dict,
    moods: list[str],
    genres: list[str],
    artists: list[str],
    albums: list[str],
    searches: list[str],
) -> bool:
    track_moods = set(track.get("moods") or [])
    if moods and not track_moods.intersection(moods):
        return False
    if genres and track.get("canonical_genre") not in genres:
        return False
    if artists and not any(_matches(track.get("artist"), q) for q in artists):
        return False
    if albums and not any(_matches(track.get("album"), q) for q in albums):
        return False
    if searches:
        haystacks = (track.get("title"), track.get("artist"), track.get("album"))
        if not any(_matches(haystack, query) for query in searches for haystack in haystacks):
            return False
    return True


# ---------------------------------------------------------------------------
# Windows Named Pipe JSON IPC for mpv
# ---------------------------------------------------------------------------

_UNSET = object()

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_ERROR_PIPE_BUSY = 231
_ERROR_BROKEN_PIPE = 109

_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_kernel32.WaitNamedPipeW.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
]
_kernel32.PeekNamedPipe.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class MPVClient:
    """Synchronous Win32 Named Pipe client for mpv's ``--input-ipc-server``."""

    def __init__(self, pipe_name: str = MPV_PIPE_NAME, timeout: float = 1.0):
        self.pipe_name = pipe_name
        self.timeout = timeout
        self.handle = None
        self.buf = b""
        self.request_id = 0

    def connect(self) -> bool:
        if self.handle is not None:
            return True
        deadline = time.monotonic() + min(self.timeout, 0.5)
        while True:
            h = _kernel32.CreateFileW(
                self.pipe_name,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                0,
                None,
            )
            if h != _INVALID_HANDLE_VALUE:
                self.handle = h
                self.buf = b""
                return True
            err = ctypes.get_last_error()
            if err == _ERROR_PIPE_BUSY and time.monotonic() < deadline:
                _kernel32.WaitNamedPipeW(self.pipe_name, 100)
                continue
            return False

    def close(self) -> None:
        if self.handle is not None:
            try:
                _kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
        self.buf = b""

    def _read_chunk(self, remaining_s: float) -> bytes:
        deadline = time.monotonic() + max(0.02, remaining_s)
        avail = wintypes.DWORD(0)
        while True:
            if self.handle is None:
                raise MusicError("mpv is not running")
            ok = _kernel32.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(avail), None)
            if not ok:
                err = ctypes.get_last_error()
                self.close()
                raise MusicError(f"mpv IPC pipe closed ({err})")
            if avail.value > 0:
                to_read = min(65536, avail.value)
                buf = ctypes.create_string_buffer(to_read)
                read_n = wintypes.DWORD(0)
                if not _kernel32.ReadFile(self.handle, buf, to_read, ctypes.byref(read_n), None):
                    err = ctypes.get_last_error()
                    self.close()
                    raise MusicError(f"mpv IPC read error ({err})")
                if read_n.value == 0:
                    self.close()
                    raise MusicError("mpv IPC connection closed")
                return buf.raw[: read_n.value]
            if time.monotonic() >= deadline:
                raise MusicError("mpv IPC timed out")
            time.sleep(0.01)

    def _recv_response(self, rid: int):
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if message.get("request_id") != rid:
                    continue
                if message.get("error") != "success":
                    raise MusicError(f"mpv: {message.get('error')}")
                return message.get("data")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MusicError("mpv IPC timed out")
            self.buf += self._read_chunk(remaining)

    def command(self, command: str, *args):
        if self.handle is None and not self.connect():
            raise MusicError("mpv is not running")
        self.request_id += 1
        rid = self.request_id
        raw = (json.dumps({"command": [command, *args], "request_id": rid}, ensure_ascii=False) + "\n").encode("utf-8")
        written = wintypes.DWORD(0)
        ok = _kernel32.WriteFile(self.handle, raw, len(raw), ctypes.byref(written), None)
        if not ok or written.value != len(raw):
            err = ctypes.get_last_error()
            self.close()
            raise MusicError(f"mpv IPC write error ({err})")
        return self._recv_response(rid)

    def get(self, name: str, default=_UNSET):
        try:
            return self.command("get_property", name)
        except MusicError:
            if default is not _UNSET:
                return default
            raise

    def set(self, name: str, value):
        return self.command("set_property", name, value)


def find_mpv_bin() -> str | None:
    env_bin = os.environ.get("GIDEON_MPV_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    for candidate in (
        ROOT / "bin" / "mpv" / "mpv.exe",
        ROOT / "bin" / "mpv" / "mpv.com",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "mpv.exe",
        Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims" / "mpv.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("mpv")


def mpv_is_available() -> bool:
    client = MPVClient(timeout=0.35)
    try:
        return client.connect()
    finally:
        client.close()


def _kill_orphan_bundled_mpv() -> None:
    """Terminate any orphaned bundled mpv.exe/mpv.com processes inside bin/mpv."""
    bundled_dir = str((ROOT / "bin" / "mpv").resolve()).casefold()
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
    except Exception:
        return

    psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    psapi.EnumProcesses.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

    pids = (wintypes.DWORD * 2048)()
    bytes_ret = wintypes.DWORD(0)
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(bytes_ret)):
        return
    count = bytes_ret.value // ctypes.sizeof(wintypes.DWORD)
    access = 0x1000 | 0x0001  # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE
    for i in range(count):
        pid = int(pids[i])
        if pid <= 4:
            continue
        hproc = _kernel32.OpenProcess(access, False, pid)
        if not hproc:
            continue
        try:
            buf = ctypes.create_unicode_buffer(520)
            sz = wintypes.DWORD(len(buf))
            if _kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                img = str(Path(buf.value).resolve()).casefold()
                name = Path(img).name
                if img.startswith(bundled_dir) and name in ("mpv.exe", "mpv.com"):
                    _kernel32.TerminateProcess(hproc, 1)
        except Exception:
            pass
        finally:
            _kernel32.CloseHandle(hproc)


def ensure_mpv() -> None:
    if mpv_is_available():
        return
    mpv_bin = find_mpv_bin()
    if not mpv_bin:
        raise MusicError("mpv binary not found (install portable mpv into bin/mpv/ or PATH)")

    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel32.ReleaseMutex.restype = wintypes.BOOL

    hmutex = _kernel32.CreateMutexW(None, False, r"Local\GideonMpvLaunch")
    acquired = False
    if hmutex:
        wait_rc = _kernel32.WaitForSingleObject(hmutex, 5000)
        acquired = wait_rc in (0x00000000, 0x00000080)  # WAIT_OBJECT_0 | WAIT_ABANDONED

    try:
        if mpv_is_available():
            return
        _kill_orphan_bundled_mpv()

        MPV_LOG.parent.mkdir(parents=True, exist_ok=True)
        rg_preamp = os.environ.get("GIDEON_MPV_RG_PREAMP", "4").strip() or "4"
        rg_fallback = os.environ.get("GIDEON_MPV_RG_FALLBACK", "-2").strip() or "-2"
        command = [
            mpv_bin,
            "--idle=yes",
            "--no-video",
            "--force-window=no",
            "--no-terminal",
            "--ytdl=yes",
            "--replaygain=track",
            f"--replaygain-preamp={rg_preamp}",
            "--replaygain-clip=no",
            f"--replaygain-fallback={rg_fallback}",
            "--volume-max=150",
            f"--input-ipc-server={MPV_PIPE_NAME}",
            f"--volume={MPV_VOLUME}",
        ]
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
        try:
            with MPV_LOG.open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    close_fds=True,
                )
        except OSError as exc:
            raise MusicError(f"Could not start mpv: {exc}")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if mpv_is_available():
                try:
                    MPV_PID_FILE.write_text(str(process.pid), encoding="utf-8")
                except OSError:
                    pass
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)

        tail = ""
        try:
            tail = MPV_LOG.read_text(encoding="utf-8", errors="replace")[-800:].strip()
        except OSError:
            pass
        detail = f": {tail}" if tail else ""
        raise MusicError(f"mpv did not start within 3 seconds{detail}")
    finally:
        if hmutex:
            if acquired:
                try:
                    _kernel32.ReleaseMutex(hmutex)
                except Exception:
                    pass
            _kernel32.CloseHandle(hmutex)


def mpv_client(start: bool = True) -> MPVClient:
    if start:
        ensure_mpv()
    client = MPVClient(timeout=1.2)
    if not client.connect():
        raise MusicError("mpv is not running")
    return client


def mpv_has_active_media() -> bool:
    """Return True if mpv is connected and currently has loaded/playing media."""
    client = MPVClient(timeout=0.25)
    try:
        if not client.connect():
            return False
        idle = bool(client.get("idle-active", True))
        return not idle
    except Exception:
        return False
    finally:
        client.close()


def mpv_media_key_action(action: str) -> bool:
    """Dispatch a hardware media-key action directly to running mpv IPC."""
    cmd_map = {
        "play_pause": ("cycle", "pause"),
        "next": ("playlist-next", "weak"),
        "previous": ("playlist-prev", "weak"),
        "stop": ("stop",),
    }
    args = cmd_map.get(action)
    if not args:
        return False
    client = MPVClient(timeout=0.45)
    try:
        if not client.connect():
            return False
        client.command(*args)
        return True
    except Exception:
        return False
    finally:
        client.close()


def quit_mpv() -> bool:
    """Ask any running gideon-mpv instance to quit cleanly over Named Pipe IPC."""
    client = MPVClient(timeout=0.5)
    try:
        if not client.connect():
            return False
        client.command("quit")
        try:
            MPV_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return True
    except Exception:
        return False
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Selection & playlists
# ---------------------------------------------------------------------------

def playlist_files() -> list[Path]:
    if not PLAYLISTS_DIR.is_dir():
        return []
    files = [
        p for p in PLAYLISTS_DIR.iterdir()
        if p.is_file() and p.suffix.casefold() in {".json", ".m3u", ".m3u8"}
    ]
    return sorted(files, key=lambda p: p.name.casefold())


def playlist_names() -> list[str]:
    names: list[str] = []
    for path in playlist_files():
        name = path.stem
        if path.suffix.casefold() == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("name"):
                    name = str(data["name"])
            except (OSError, json.JSONDecodeError):
                pass
        if name not in names:
            names.append(name)
    return names


def _find_playlist(name: str) -> Path:
    wanted = name.strip()
    if not wanted or "/" in wanted or "\\" in wanted or wanted in {".", ".."}:
        raise MusicError(f"Invalid playlist name: {name!r}", code=2)
    files = playlist_files()
    matches: list[Path] = []
    for path in files:
        aliases = {path.name.casefold(), path.stem.casefold()}
        if path.suffix.casefold() == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("name"):
                    aliases.add(str(data["name"]).casefold())
            except (OSError, json.JSONDecodeError):
                pass
        if wanted.casefold() in aliases:
            matches.append(path)
    if not matches:
        available = ", ".join(playlist_names()) or "(none)"
        raise MusicError(f"Playlist {name!r} not found. Available playlists: {available}", code=2)
    if len(matches) > 1:
        raise MusicError(f"Playlist name {name!r} is ambiguous", code=2)
    return matches[0]


def _pseudo_track(path: str) -> dict:
    stem = Path(path).stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
    else:
        artist, title = "", stem
    return {
        "id": None,
        "artist": artist,
        "title": title,
        "album": "",
        "canonical_genre": None,
        "moods": [],
        "path": path,
        "length_sec": None,
        "source": "playlist",
    }


def _playlist_tracks(paths: list[str], by_path: dict[str, dict]) -> list[dict]:
    selected: list[dict] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = resolve_track_path(raw.strip())
        if not os.path.isabs(path):
            path = str((PLAYLISTS_DIR / path).resolve())
        if not os.path.exists(path):
            raise MusicError(f"Playlist references missing file: {path}", code=2)
        selected.append(by_path.get(path_key(path)) or _pseudo_track(path))
    return selected


def load_playlist(name: str, by_path: dict[str, dict]) -> dict:
    file = _find_playlist(name)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise MusicError(f"Could not read playlist {file}: {exc}")

    if file.suffix.casefold() in {".m3u", ".m3u8"}:
        paths = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        return {
            "name": file.stem,
            "file": file,
            "tracks": _playlist_tracks(paths, by_path),
            "moods": [],
            "genres": [],
            "artists": [],
            "limit": None,
        }

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MusicError(f"Invalid JSON playlist {file}: {exc}", code=2)
    if not isinstance(data, dict):
        raise MusicError(f"Invalid JSON playlist {file}: expected an object", code=2)

    mood_values = [resolve_mood(str(v)) for v in _listify(data.get("moods"))]
    genre_values = [resolve_genre(str(v)) for v in _listify(data.get("genres"))]
    artist_values = [str(v) for v in _listify(data.get("artists")) if str(v).strip()]
    track_values = [v for v in _listify(data.get("tracks")) if isinstance(v, str)]
    limit = data.get("limit")
    if limit is not None:
        limit = int(limit)
    return {
        "name": str(data.get("name") or file.stem),
        "file": file,
        "tracks": _playlist_tracks(track_values, by_path) if track_values else None,
        "moods": mood_values,
        "genres": genre_values,
        "artists": artist_values,
        "limit": limit,
    }


def _find_track(tracks: list[dict], query: str) -> dict:
    needle = query.strip().casefold()
    if not needle:
        raise MusicError("--track needs a non-empty search string", code=2)
    scored = []
    for position, track in enumerate(tracks):
        artist = track.get("artist") or ""
        title = track.get("title") or ""
        combined = f"{artist} - {title}"
        combined_em = f"{artist} — {title}"
        exact = combined.casefold() == needle or combined_em.casefold() == needle
        found = needle in combined.casefold() or needle in combined_em.casefold() or needle in title.casefold()
        if found:
            scored.append((0 if exact else 1, position, track))
    if not scored:
        raise MusicError(f"No track matched --track {query!r}", code=2)
    scored.sort(key=lambda item: (item[0], item[1]))
    return scored[0][2]


def _norm_find_text(value: str | None) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", (value or "").casefold())
    return " ".join(cleaned.split())


def find_tracks(query: str, limit: int = 8) -> list[dict]:
    """Fuzzy-search local library tracks, tolerant of STT misspellings."""
    q = _norm_find_text(query)
    if not q:
        return []
    _, tracks = load_library()
    q_tokens = [tok for tok in q.split() if len(tok) >= 2]
    scored: list[tuple[float, float, dict]] = []

    for track in tracks:
        artist = track.get("artist") or ""
        title = track.get("title") or ""
        album = track.get("album") or ""
        raw_genres = " ".join(str(g) for g in (track.get("raw_genres") or []))

        fields = [
            _norm_find_text(f"{artist} {title}"),
            _norm_find_text(title),
            _norm_find_text(artist),
            _norm_find_text(album),
            _norm_find_text(f"{title} {artist} {album}"),
        ]
        best_ratio = max((difflib.SequenceMatcher(None, q, fld).ratio() for fld in fields if fld), default=0.0)

        c_tokens = _norm_find_text(f"{artist} {title} {album} {raw_genres}").split()
        if q_tokens and c_tokens:
            hits = sum(
                1
                for qt in q_tokens
                if any(qt in ct or difflib.SequenceMatcher(None, qt, ct).ratio() >= 0.8 for ct in c_tokens)
            )
            token_score = hits / len(q_tokens)
        else:
            token_score = 0.0

        score = 0.5 * best_ratio + 0.5 * token_score
        scored.append((score, best_ratio, track))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [dict(track_summary(t), score=round(sc, 3)) for sc, _, t in scored[: max(1, limit)]]


def select_tracks(args) -> tuple[list[dict], dict]:
    data, tracks = load_library()
    by_path = path_map(tracks)

    raw_paths = getattr(args, "path", None)
    if raw_paths:
        resolved = [resolve_track_path(p) for p in raw_paths if p and str(p).strip()]
        if not resolved:
            raise MusicError("--path requires a non-empty file path", code=2)
        selected: list[dict] = []
        for p in resolved:
            if not os.path.exists(p):
                raise MusicError(f"Track file not found: {p}", code=2)
            selected.append(by_path.get(path_key(p)) or _pseudo_track(p))
        return selected, {"data": data, "playlist": None, "shuffle": False, "limit": len(selected)}

    arg_track = getattr(args, "track", None)
    arg_playlist = getattr(args, "playlist", None)
    if arg_track and arg_playlist:
        raise MusicError("Use --track or --playlist, not both", code=2)

    playlist = None
    candidates = list(tracks)
    if arg_playlist:
        playlist = load_playlist(arg_playlist, by_path)
        if playlist["tracks"] is not None:
            candidates = list(playlist["tracks"])

    moods = [resolve_mood(v) for value in (getattr(args, "mood", None) or []) for v in str(value).split(",") if v.strip()]
    genres = [resolve_genre(v) for value in (getattr(args, "genre", None) or []) for v in str(value).split(",") if v.strip()]
    artists = [str(v).strip() for v in (getattr(args, "artist", None) or []) if str(v).strip()]
    albums = [str(v).strip() for v in (getattr(args, "album", None) or []) if str(v).strip()]
    searches = [str(v).strip() for v in (getattr(args, "search", None) or []) if str(v).strip()]

    if playlist:
        moods.extend(playlist["moods"])
        genres.extend(playlist["genres"])
        artists.extend(playlist["artists"])

    moods = list(dict.fromkeys(moods))
    genres = list(dict.fromkeys(genres))
    artists = list(dict.fromkeys(artists))
    albums = list(dict.fromkeys(albums))
    searches = list(dict.fromkeys(searches))

    candidates = [t for t in candidates if track_matches(t, moods, genres, artists, albums, searches)]
    if arg_track:
        candidates = [_find_track(candidates, arg_track)]
    if not candidates:
        raise MusicError("No tracks matched the selection", code=2)

    arg_shuffle = getattr(args, "shuffle", None)
    shuffle = not (arg_playlist or arg_track) if arg_shuffle is None else bool(arg_shuffle)
    arg_limit = getattr(args, "limit", None)
    if arg_limit is not None:
        limit = arg_limit
    elif playlist and playlist.get("limit") is not None:
        limit = playlist["limit"]
    else:
        limit = DEFAULT_LIMIT

    if arg_track:
        limit = 1
        shuffle = False

    if shuffle:
        random.shuffle(candidates)
    selected = candidates if limit == 0 else candidates[:limit]
    return selected, {"data": data, "playlist": playlist, "shuffle": shuffle, "limit": limit}


def write_queue(tracks: list[dict]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for track in tracks:
        path = track.get("path")
        if not path or "\n" in path or "\r" in path:
            raise MusicError("Refusing to queue a track with an invalid path")
        lines.append(path)
    temp = RUNTIME_DIR / f".music-queue.{os.getpid()}.m3u"
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temp, QUEUE_FILE)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise MusicError(f"Could not write queue file {QUEUE_FILE}: {exc}")


def selection_payload(selected: list[dict], meta: dict, dry_run: bool) -> dict:
    return {
        "ok": True,
        "dry_run": dry_run,
        "count": len(selected),
        "shuffle": meta["shuffle"],
        "limit": meta["limit"],
        "playlist": meta["playlist"]["name"] if meta.get("playlist") else None,
        "tracks": [track_summary(t) for t in selected],
        "paths": [t["path"] for t in selected],
    }


def describe_selection(selected: list[dict], meta: dict, dry_run: bool) -> str:
    prefix = "Would play" if dry_run else "Playing"
    details = []
    if meta.get("playlist"):
        details.append(f"playlist {meta['playlist']['name']}")
    details.append("shuffle on" if meta["shuffle"] else "shuffle off")
    names = [f"{t.get('artist', '')} - {t.get('title', '')}" for t in selected[:3]]
    more = f"; +{len(selected) - 3} more" if len(selected) > 3 else ""
    return f"{prefix} {len(selected)} track{'' if len(selected) == 1 else 's'} ({', '.join(details)}): " + "; ".join(names) + more


# ---------------------------------------------------------------------------
# Status & queue
# ---------------------------------------------------------------------------

def _metadata_dict(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {str(key).casefold(): value for key, value in raw.items()}


def _is_online_cache_path(path: str | None) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().parent == _ONLINE_CACHE_DIR.resolve()
    except OSError:
        return False


def _split_display_title(raw: str | None) -> tuple[str | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return None, None
    for sep in (" — ", " - "):
        if sep in text:
            artist, title = text.split(sep, 1)
            return artist.strip() or None, title.strip() or text
    return None, text


def get_status() -> dict:
    client = MPVClient(timeout=0.6)
    if not client.connect():
        client.close()
        return {"ok": True, "mpv": False, "playing": False}

    try:
        idle = bool(client.get("idle-active", True))
        path = client.get("path", None) or None
        pause = bool(client.get("pause", False)) if path else False
        playlist_pos = client.get("playlist-pos", -1)
        playlist_count = client.get("playlist-count", 0) or 0
        position = client.get("time-pos", None) if path else None
        duration = client.get("duration", None) if path else None
        volume = client.get("volume", None)
        metadata = _metadata_dict(client.get("metadata", {}))
        media_title = client.get("media-title", None) if path else None
    except MusicError:
        client.close()
        return {"ok": True, "mpv": False, "playing": False}
    finally:
        client.close()

    by_path: dict[str, dict] = {}
    try:
        _, tracks = load_library()
        by_path = path_map(tracks)
    except MusicError:
        pass
    indexed = by_path.get(path_key(path)) if path else None

    artist = indexed.get("artist") if indexed else None
    title = indexed.get("title") if indexed else None
    album = indexed.get("album") if indexed else None
    genre = indexed.get("canonical_genre") if indexed else None
    moods = list(indexed.get("moods") or []) if indexed else []

    is_remote = bool(path and path.startswith(("http://", "https://")))
    is_online_src = is_remote or _is_online_cache_path(path)
    if not artist:
        artist = metadata.get("artist") or metadata.get("album_artist") or ""
    if not title:
        title = metadata.get("title") or ("" if is_online_src else (Path(path).stem if path else ""))
    if (not title or (not artist and is_online_src)) and media_title:
        parsed_artist, parsed_title = _split_display_title(str(media_title))
        if not artist and parsed_artist:
            artist = parsed_artist
        if (not title or title == "videoplayback") and parsed_title:
            title = parsed_title
    if not title and path and not is_online_src:
        title = Path(path).stem
    if not album:
        album = metadata.get("album") or ""
    if not genre:
        genre = metadata.get("genre") or ""

    if duration is None and indexed:
        duration = indexed.get("length_sec")

    queue_index = int(playlist_pos) + 1 if isinstance(playlist_pos, (int, float)) and playlist_pos >= 0 else None
    queue_count = int(playlist_count or 0)
    playing = bool(path) and not idle and not pause
    paused = bool(path) and not idle and pause

    return {
        "ok": True,
        "mpv": True,
        "playing": playing,
        "paused": paused,
        "path": path,
        "artist": artist or None,
        "title": title or None,
        "album": album or None,
        "genre": genre or None,
        "genre_label": GENRE_LABELS.get(genre, genre) if genre else None,
        "moods": moods,
        "position": number(position),
        "duration": number(duration),
        "volume": number(volume),
        "queue_index": queue_index,
        "queue_count": queue_count,
    }


def status_line(status: dict) -> str:
    if not status.get("mpv"):
        return "mpv not running"
    if not status.get("path"):
        return "Stopped (mpv idle)"
    state = "Paused" if status.get("paused") else "Playing"
    artist = status.get("artist") or "Unknown artist"
    title = status.get("title") or Path(status.get("path", "")).stem
    index = status.get("queue_index")
    count = status.get("queue_count")
    place = f" [{index}/{count}]" if index and count else ""
    return f"{state}: {artist} - {title}{place} ({format_time(status.get('position'))} / {format_time(status.get('duration'))})"


def get_queue() -> list[dict]:
    client = MPVClient(timeout=0.5)
    if not client.connect():
        client.close()
        return []
    try:
        entries = client.get("playlist", [])
    except MusicError:
        entries = []
    finally:
        client.close()

    by_path: dict[str, dict] = {}
    try:
        _, tracks = load_library()
        by_path = path_map(tracks)
    except MusicError:
        pass

    result = []
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        filename = ""
        if isinstance(entry, dict):
            filename = entry.get("filename") or entry.get("url") or ""
        elif isinstance(entry, str):
            filename = entry
        track = by_path.get(path_key(filename))
        if track is None:
            entry_title = entry.get("title") if isinstance(entry, dict) else None
            if entry_title and (filename.startswith(("http://", "https://")) or _is_online_cache_path(filename)):
                p_artist, p_title = _split_display_title(str(entry_title))
                track = {"artist": p_artist or "", "title": p_title or str(entry_title)}
            else:
                track = _pseudo_track(filename)
        result.append({
            "index": index + 1,
            "path": filename,
            "artist": track.get("artist") or "",
            "title": track.get("title") or "",
        })
    return result


# ---------------------------------------------------------------------------
# Online search & streaming helpers (YouTube Music + yt-dlp fallback)
# ---------------------------------------------------------------------------

_YTM_SEARCH_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
_YTM_SONGS_PARAMS = "EgWKAQIIAWoKEAMQBBAJEAoQBQ=="
_YTM_CLIENT = {"clientName": "WEB_REMIX", "clientVersion": "1.20261101.01.00"}
_ONLINE_UNFILTERED_HINTS = frozenset({
    "live", "remix", "cover", "edit", "mix", "session", "version", "ost",
    "instrumental", "slowed", "reverb", "nightcore", "bootleg", "hour",
    "concert", "acoustic", "mashup", "radio", "podcast", "talk",
})


def _read_playback_gen() -> int:
    try:
        return int(_PLAYBACK_GEN_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _bump_playback_gen() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    next_gen = max(int(time.time_ns()), _read_playback_gen() + 1)
    tmp = RUNTIME_DIR / f".music-mpv.gen.{os.getpid()}"
    try:
        tmp.write_text(str(next_gen), encoding="utf-8")
        os.replace(tmp, _PLAYBACK_GEN_PATH)
    except OSError:
        pass
    return next_gen


def _ytm_parse_items(data: dict) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()

    def walk(obj) -> None:
        if isinstance(obj, dict):
            renderer = obj.get("musicResponsiveListItemRenderer")
            if isinstance(renderer, dict):
                vid = (renderer.get("playlistItemData") or {}).get("videoId")
                if not vid:
                    vid = (
                        (renderer.get("overlay") or {})
                        .get("musicItemThumbnailOverlayRenderer", {})
                        .get("content", {})
                        .get("musicPlayButtonRenderer", {})
                        .get("playNavigationEndpoint", {})
                        .get("watchEndpoint", {})
                        .get("videoId")
                    )
                if isinstance(vid, str) and len(vid) == 11 and vid not in seen:
                    cols = []
                    for col in renderer.get("flexColumns", []):
                        runs = ((col.get("musicResponsiveListItemFlexColumnRenderer") or {}).get("text") or {}).get("runs") or []
                        cols.append("".join(str(x.get("text", "")) for x in runs))
                    title = cols[0].strip() if cols else ""
                    artist = ""
                    if len(cols) > 1 and cols[1]:
                        parts = [p.strip() for p in cols[1].split("•") if p.strip()]
                        if parts and parts[0].casefold() in {"song", "video", "single", "ep", "album"}:
                            parts = parts[1:]
                        if parts:
                            artist = parts[0]
                    seen.add(vid)
                    items.append({
                        "id": vid,
                        "watch_url": f"https://www.youtube.com/watch?v={vid}",
                        "title": title,
                        "artist": artist,
                    })
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for entry in obj:
                walk(entry)

    walk(data)
    return items


def _ytm_post_search(query: str, params: str | None) -> list[dict]:
    payload: dict = {"context": {"client": _YTM_CLIENT}, "query": query}
    if params:
        payload["params"] = params
    req = urllib.request.Request(
        _YTM_SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return _ytm_parse_items(data)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
        return []


def _online_search(query: str, count: int = 1) -> list[dict]:
    words = {w.strip(".,!?()[]{}\"'").casefold() for w in query.split()}
    prefer_songs = not bool(words.intersection(_ONLINE_UNFILTERED_HINTS))
    items: list[dict] = []
    if prefer_songs:
        items = _ytm_post_search(query, _YTM_SONGS_PARAMS)
    if not items:
        items = _ytm_post_search(query, None)
    if items:
        return items[:count]

    try:
        import yt_dlp
    except ImportError as exc:
        raise MusicError(f"yt_dlp not available for online search: {exc}")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "socket_timeout": 8,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max(1, count)}:{query}", download=False)
    entries = (info.get("entries") or []) if isinstance(info, dict) else []
    results = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("id")
        watch_url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
        if not watch_url:
            continue
        results.append({
            "id": vid or "",
            "watch_url": watch_url if watch_url.startswith("http") else f"https://www.youtube.com/watch?v={watch_url}",
            "title": str(entry.get("title") or "").strip(),
            "artist": str(entry.get("uploader") or entry.get("channel") or "").strip(),
        })
    if not results:
        raise MusicError(f"No online results found for {query!r}", code=2)
    return results[:count]


def _format_display_title(artist: str, title: str, watch_url: str) -> str:
    if artist and title:
        if artist.casefold() in title.casefold():
            return title
        return f"{artist} — {title}"
    return title or artist or watch_url


def _extract_online_stream(item: dict, *, client_list: list[str] | None = None) -> dict:
    try:
        import yt_dlp
    except ImportError as exc:
        raise MusicError(f"yt_dlp not available for online extraction: {exc}")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "socket_timeout": 8,
    }
    if client_list is not None:
        opts["extractor_args"] = {"youtube": {"player_client": client_list}}

    watch_url = item["watch_url"]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(watch_url, download=False)
    if isinstance(info, dict) and "entries" in info:
        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            raise MusicError(f"No playable stream extracted for {watch_url}")
        info = entries[0]
    stream_url = (info or {}).get("url")
    if not isinstance(stream_url, str) or not stream_url:
        raise MusicError(f"No direct audio URL returned for {watch_url}")

    artist = item.get("artist") or str(info.get("artist") or info.get("uploader") or "").strip()
    title = item.get("title") or str(info.get("title") or "").strip()
    return {
        "id": item.get("id") or str(info.get("id") or ""),
        "watch_url": watch_url,
        "stream_url": stream_url,
        "artist": artist or None,
        "title": title or None,
        "display": _format_display_title(artist, title, watch_url),
        "duration": info.get("duration"),
    }


def _cleanup_online_cache(max_age_s: float = 3600.0) -> None:
    if not _ONLINE_CACHE_DIR.exists():
        return
    now = time.time()
    for p in _ONLINE_CACHE_DIR.iterdir():
        try:
            if p.is_file() and (now - p.stat().st_mtime) > max_age_s:
                p.unlink()
        except OSError:
            pass


def _download_fallback_audio(item: dict) -> dict:
    try:
        import yt_dlp
    except ImportError as exc:
        raise MusicError(f"yt_dlp not available for fallback stream: {exc}")

    _ONLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_online_cache()
    vid = re.sub(r"[^A-Za-z0-9_-]", "", item.get("id") or "") or f"track_{int(time.time())}"
    out_tpl = str(_ONLINE_CACHE_DIR / f"{vid}.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": out_tpl,
        "overwrites": True,
        "socket_timeout": 10,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(item["watch_url"], download=True)
        file_path = ydl.prepare_filename(info)
    if not file_path or not Path(file_path).exists():
        matches = sorted(_ONLINE_CACHE_DIR.glob(f"{vid}.*"))
        if not matches:
            raise MusicError(f"Fallback download produced no file for {item['watch_url']}")
        file_path = str(matches[0])

    artist = item.get("artist") or str((info or {}).get("artist") or (info or {}).get("uploader") or "").strip()
    title = item.get("title") or str((info or {}).get("title") or "").strip()
    return {
        "id": vid,
        "watch_url": item["watch_url"],
        "stream_url": file_path,
        "artist": artist or None,
        "title": title or None,
        "display": _format_display_title(artist, title, item["watch_url"]),
        "duration": (info or {}).get("duration"),
    }


def _verify_mpv_online_playback(
    client: MPVClient,
    target_path: str,
    timeout_s: float = 4.0,
    *,
    append: bool = False,
    expected_gen: int | None = None,
) -> str:
    deadline = time.monotonic() + timeout_s
    saw_loading = False
    while time.monotonic() < deadline:
        if expected_gen is not None and _read_playback_gen() != expected_gen:
            return "superseded"
        idle = bool(client.get("idle-active", True))
        cur_path = client.get("path", None)
        if append and not idle and cur_path and cur_path != target_path:
            playlist = client.get("playlist", []) or []
            if any(isinstance(e, dict) and e.get("filename") == target_path for e in playlist):
                return "ok"
        paused = bool(client.get("pause", False)) if cur_path else False
        time_pos = client.get("time-pos", None)
        cache_dur = client.get("demuxer-cache-duration", None)
        if not idle and cur_path == target_path:
            saw_loading = True
            if not paused and (
                (isinstance(time_pos, (int, float)) and time_pos > 0.03)
                or (isinstance(cache_dur, (int, float)) and cache_dur > 0.25)
            ):
                return "ok"
        elif saw_loading and idle:
            return "failed"
        time.sleep(0.06)
    return "failed"


def _load_and_verify_online(item: dict, *, append: bool = False, expected_gen: int | None = None) -> dict:
    extracted: dict | None = None
    last_err: Exception | None = None
    superseded = False

    def _is_superseded() -> bool:
        return expected_gen is not None and _read_playback_gen() != expected_gen

    client = mpv_client()
    try:
        if _is_superseded():
            raise MusicError("Playback stopped or superseded")
        try:
            extracted = _extract_online_stream(item, client_list=["android"])
            if _is_superseded():
                superseded = True
                raise MusicError("Playback stopped or superseded")
            was_idle = bool(client.get("idle-active", True))
            mode = "append-play" if append else "replace"
            client.command("loadfile", extracted["stream_url"], mode, -1, {"force-media-title": extracted["display"]})
            if not append or was_idle:
                client.set("pause", False)
            status = _verify_mpv_online_playback(client, extracted["stream_url"], timeout_s=4.0, append=append, expected_gen=expected_gen)
            if status == "ok":
                return extracted
            if status == "superseded":
                superseded = True
                raise MusicError("Playback stopped or superseded")
        except Exception as exc:
            last_err = exc

        if superseded or _is_superseded():
            raise MusicError("Playback stopped or superseded")

        try:
            downloaded = _download_fallback_audio(item)
            if _is_superseded():
                raise MusicError("Playback stopped or superseded")
            was_idle = bool(client.get("idle-active", True))
            mode = "append-play" if append else "replace"
            client.command("loadfile", downloaded["stream_url"], mode, -1, {"force-media-title": downloaded["display"]})
            if not append or was_idle:
                client.set("pause", False)
            status = _verify_mpv_online_playback(client, downloaded["stream_url"], timeout_s=2.5, append=append, expected_gen=expected_gen)
            if status == "ok":
                return downloaded
            if status == "superseded":
                raise MusicError("Playback stopped or superseded")
        except Exception as exc:
            last_err = exc
    finally:
        client.close()

    detail = f": {last_err}" if last_err else ""
    raise MusicError(f"Could not start online stream for {item.get('title') or item['watch_url']}{detail}")


def _play_online(queries: list[str], *, count: int = 1, append: bool = False) -> tuple[dict, int]:
    cleaned = [q.strip() for q in queries if q and q.strip()]
    if not cleaned:
        raise MusicError("online needs at least one search query", code=2)

    expected_gen = _read_playback_gen() if append else _bump_playback_gen()
    candidates: list[dict] = []
    if len(cleaned) == 1:
        candidates = _online_search(cleaned[0], count=max(1, min(count, 8)))
    else:
        for q in cleaned[:6]:
            if _read_playback_gen() != expected_gen:
                raise MusicError("Playback stopped or superseded")
            candidates.extend(_online_search(q, count=1)[:1])

    if not candidates:
        raise MusicError(f"No online results matched {cleaned[0]!r}", code=2)

    first_started: dict | None = None
    remaining: list[dict] = []
    last_error: MusicError | None = None
    for idx, cand in enumerate(candidates):
        if _read_playback_gen() != expected_gen:
            raise MusicError("Playback stopped or superseded")
        try:
            first_started = _load_and_verify_online(cand, append=append, expected_gen=expected_gen)
            remaining = candidates[idx + 1 :]
            break
        except MusicError as exc:
            last_error = exc
            if "superseded" in exc.message:
                break
    if first_started is None:
        raise last_error or MusicError("Online playback failed")

    if remaining and _read_playback_gen() == expected_gen:
        try:
            payload_bytes = json.dumps({"gen": expected_gen, "items": remaining}).encode("utf-8")
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            )
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "_append-online"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
            if proc.stdin is not None:
                proc.stdin.write(payload_bytes)
                proc.stdin.close()
        except OSError:
            pass

    return first_started, 1 + len(remaining)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_online(args) -> int:
    append = bool(getattr(args, "append", False) or getattr(args, "queue", False))
    started, total_count = _play_online(args.queries, count=args.count, append=append)
    payload = {
        "ok": True,
        "source": "online",
        "playing": True,
        "title": started.get("title"),
        "artist": started.get("artist"),
        "display": started.get("display"),
        "duration": number(started.get("duration")),
        "watch_url": started.get("watch_url"),
        "queued_count": total_count,
    }
    if args.json:
        as_json(payload)
    else:
        extra = f" (+{total_count - 1} queued)" if total_count > 1 else ""
        print(f"Playing online: {started['display']}{extra}")
    return 0


def cmd__append_online(args) -> int:
    raw = getattr(args, "items_json", None) or sys.stdin.read()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return 1
    expected_gen = parsed.get("gen") if isinstance(parsed, dict) else None
    items = parsed.get("items") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return 1
    for item in items:
        if expected_gen is not None and _read_playback_gen() != expected_gen:
            break
        if not isinstance(item, dict) or not item.get("watch_url"):
            continue
        try:
            extracted = _extract_online_stream(item, client_list=["android"])
        except Exception:
            if expected_gen is not None and _read_playback_gen() != expected_gen:
                break
            try:
                extracted = _download_fallback_audio(item)
            except Exception:
                continue
        if expected_gen is not None and _read_playback_gen() != expected_gen:
            break
        client = MPVClient(timeout=0.8)
        if not client.connect():
            client.close()
            break
        try:
            if (expected_gen is not None and _read_playback_gen() != expected_gen) or bool(client.get("idle-active", True)):
                break
            client.command("loadfile", extracted["stream_url"], "append-play", -1, {"force-media-title": extracted["display"]})
        except MusicError:
            pass
        finally:
            client.close()
    return 0


def _online_query_from_play_args(args) -> str:
    parts: list[str] = []
    if getattr(args, "track", None):
        parts.append(str(args.track).strip())
    for attr in ("search", "artist", "album", "genre", "mood"):
        for val in getattr(args, attr, None) or []:
            parts.append(str(val).strip())
    return " ".join(p for p in parts if p)


def cmd_play(args) -> int:
    append = bool(getattr(args, "append", False))
    try:
        selected, meta = select_tracks(args)
        if not args.dry_run:
            selected = [t for t in selected if Path(str(t.get("path") or "")).is_file()]
            if not selected:
                raise MusicError("No playable local files found on disk", code=2)
    except MusicError as exc:
        if getattr(args, "online", False) and not getattr(args, "dry_run", False) and exc.code == 2:
            fallback_query = _online_query_from_play_args(args)
            if fallback_query:
                limit = args.limit if args.limit and args.limit > 0 else (1 if args.track or args.search else 3)
                started, total_count = _play_online([fallback_query], count=limit, append=append)
                payload = {
                    "ok": True,
                    "source": "online",
                    "playing": True,
                    "title": started.get("title"),
                    "artist": started.get("artist"),
                    "display": started.get("display"),
                    "duration": number(started.get("duration")),
                    "watch_url": started.get("watch_url"),
                    "queued_count": total_count,
                }
                if args.json:
                    as_json(payload)
                else:
                    extra = f" (+{total_count - 1} queued)" if total_count > 1 else ""
                    print(f"Playing online: {started['display']}{extra}")
                return 0
        raise

    payload = selection_payload(selected, meta, dry_run=bool(args.dry_run))
    if args.dry_run:
        if args.json:
            as_json(payload)
        else:
            print(describe_selection(selected, meta, True))
        return 0

    if not append:
        _bump_playback_gen()
    client = mpv_client()
    active_path = None
    idle_active = True
    try:
        if append:
            for t in selected:
                client.command("loadfile", t["path"], "append-play")
            client.set("pause", False)
            queue_count = int(client.get("playlist-count", 0) or 0)
            active_path = client.get("path", None)
            idle_active = bool(client.get("idle-active", True))
        else:
            write_queue(selected)
            client.command("loadfile", str(QUEUE_FILE), "replace")
            client.set("pause", False)
            deadline = time.monotonic() + 2.5
            queue_count = 0
            while time.monotonic() < deadline:
                queue_count = int(client.get("playlist-count", 0) or 0)
                active_path = client.get("path", None)
                idle_active = bool(client.get("idle-active", True))
                if queue_count >= len(selected) and active_path and not idle_active:
                    break
                time.sleep(0.05)
            client.set("pause", False)
            active_path = client.get("path", None)
            idle_active = bool(client.get("idle-active", True))
    finally:
        client.close()

    if queue_count == 0:
        raise MusicError("mpv did not load the queue")
    if not active_path or idle_active:
        raise MusicError("mpv loaded the queue but could not start playback", code=2)
    payload["queue_count"] = queue_count
    if args.json:
        as_json(payload)
    else:
        print(describe_selection(selected, meta, False))
    return 0


def cmd_enqueue(args) -> int:
    queries = [q.strip() for q in args.queries if q and q.strip()]
    if not queries:
        raise MusicError("enqueue needs at least one query", code=2)
    if args.online:
        started, total_count = _play_online(queries, count=1, append=True)
        payload = {"ok": True, "action": "enqueue", "source": "online", "queued_count": total_count, "display": started.get("display")}
        if args.json:
            as_json(payload)
        else:
            print(f"Queued online: {started.get('display')}")
        return 0

    _, tracks = load_library()
    matched: list[dict] = []
    for q in queries:
        hits = [t for t in tracks if track_matches(t, [], [], [], [], [q]) and Path(str(t.get("path") or "")).is_file()]
        if hits:
            matched.append(hits[0])
    if not matched:
        started, total_count = _play_online(queries, count=1, append=True)
        payload = {"ok": True, "action": "enqueue", "source": "online", "queued_count": total_count, "display": started.get("display")}
        if args.json:
            as_json(payload)
        else:
            print(f"Queued online: {started.get('display')}")
        return 0

    client = mpv_client()
    try:
        for t in matched:
            client.command("loadfile", t["path"], "append-play")
        client.set("pause", False)
        qcount = int(client.get("playlist-count", 0) or 0)
    finally:
        client.close()
    payload = {"ok": True, "action": "enqueue", "source": "local", "queued_count": len(matched), "queue_count": qcount, "tracks": [track_summary(t) for t in matched]}
    if args.json:
        as_json(payload)
    else:
        print(f"Queued {len(matched)} track(s)")
    return 0


def _require_playing(client: MPVClient) -> str:
    path = client.get("path", None)
    if not path:
        raise MusicError("Nothing is playing", code=2)
    return path


def cmd_pause(args) -> int:
    client = mpv_client()
    try:
        _require_playing(client)
        client.set("pause", True)
    finally:
        client.close()
    if args.json:
        as_json({"ok": True, "action": "pause", "paused": True})
    else:
        print("Paused")
    return 0


def cmd_resume(args) -> int:
    client = mpv_client()
    try:
        _require_playing(client)
        client.set("pause", False)
    finally:
        client.close()
    if args.json:
        as_json({"ok": True, "action": "resume", "paused": False})
    else:
        print("Resumed")
    return 0


def cmd_toggle(args) -> int:
    client = mpv_client()
    try:
        _require_playing(client)
        paused = bool(client.get("pause", False))
        client.set("pause", not paused)
        new_state = not paused
    finally:
        client.close()
    if args.json:
        as_json({"ok": True, "action": "toggle", "paused": new_state})
    else:
        print("Paused" if new_state else "Resumed")
    return 0


def _skip(direction: str) -> None:
    client = mpv_client()
    try:
        count = int(client.get("playlist-count", 0) or 0)
        if count == 0:
            raise MusicError("The queue is empty", code=2)
        client.command("playlist-next" if direction == "next" else "playlist-prev")
    finally:
        client.close()


def cmd_next(args) -> int:
    _skip("next")
    if args.json:
        as_json({"ok": True, "action": "next"})
    else:
        print("Skipped to next track")
    return 0


def cmd_previous(args) -> int:
    _skip("previous")
    if args.json:
        as_json({"ok": True, "action": "previous"})
    else:
        print("Skipped to previous track")
    return 0


def cmd_stop(args) -> int:
    _bump_playback_gen()
    _cleanup_online_cache()
    client = mpv_client(start=False)
    if not client.connect():
        if args.json:
            as_json({"ok": True, "action": "stop", "playing": False, "paused": False, "queue_count": 0})
        else:
            print("Already stopped")
        return 0
    try:
        client.command("stop")
        try:
            client.command("playlist-clear")
        except MusicError:
            pass
        try:
            client.set("pause", False)
        except MusicError:
            pass
    finally:
        client.close()
    if args.json:
        as_json({"ok": True, "action": "stop", "playing": False, "paused": False, "queue_count": 0})
    else:
        print("Stopped; queue cleared")
    return 0


def cmd_shuffle(args) -> int:
    client = mpv_client()
    try:
        count = int(client.get("playlist-count", 0) or 0)
        if count == 0:
            raise MusicError("The queue is empty", code=2)
        client.command("playlist-shuffle")
    finally:
        client.close()
    if args.json:
        as_json({"ok": True, "action": "shuffle", "queue_count": count})
    else:
        print(f"Shuffled {count} queued track{'' if count == 1 else 's'}")
    return 0


_VOLUME_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def cmd_volume(args) -> int:
    client = mpv_client()
    try:
        current = float(client.get("volume", MPV_VOLUME) or MPV_VOLUME)
        if args.value is None:
            new_volume = current
        else:
            delta = args.value.strip()
            if not _VOLUME_RE.match(delta):
                raise MusicError(f"Invalid volume: {args.value!r}", code=2)
            amount = float(delta)
            new_volume = max(0.0, min(100.0, (current + amount) if delta[0] in "+-" else amount))
            client.set("volume", new_volume)
    finally:
        client.close()
    shown = number(new_volume)
    if args.json:
        as_json({"ok": True, "volume": shown})
    else:
        print(f"Volume: {shown}%")
    return 0


def cmd_seek(args) -> int:
    client = mpv_client()
    try:
        _require_playing(client)
        current = client.get("time-pos", None)
        duration = client.get("duration", None)
        if args.value is None:
            position = current
        else:
            raw = args.value.strip()
            if not _VOLUME_RE.match(raw):
                raise MusicError(f"Invalid seek amount: {args.value!r}", code=2)
            client.command("seek", float(raw), "relative" if raw[0] in "+-" else "absolute")
            position = client.get("time-pos", None)
    finally:
        client.close()
    if args.json:
        as_json({"ok": True, "position": number(position), "duration": number(duration), "position_text": format_time(position), "duration_text": format_time(duration)})
    else:
        print(f"{format_time(position)} / {format_time(duration)}")
    return 0


def cmd_status(args) -> int:
    status = get_status()
    if args.json:
        as_json(status)
    else:
        print(status_line(status))
    return 0


def cmd_now(args) -> int:
    status = get_status()
    if status.get("mpv") and (status.get("artist") or status.get("title")):
        artist = status.get("artist") or "Unknown artist"
        title = status.get("title") or ""
        text = f"{artist} — {title}" if title else artist
    else:
        text = "Nothing playing"
    if args.json:
        payload = dict(status)
        payload["now"] = text
        as_json(payload)
    else:
        print(text)
    return 0


def cmd_queue(args) -> int:
    queue = get_queue()
    if args.json:
        as_json(queue)
    elif not queue:
        print("Queue is empty")
    else:
        for item in queue:
            print(f"{item['index']}. {item['artist']} - {item['title']}")
    return 0


def cmd_genres(args) -> int:
    _, tracks = load_library()
    counts = Counter(t.get("canonical_genre") for t in tracks if t.get("canonical_genre"))
    items = [{"slug": slug, "label": GENRE_LABELS.get(slug, slug), "count": int(cnt)} for slug, cnt in counts.most_common()]
    if args.json:
        as_json({"ok": True, "genres": items, "total": len(tracks)})
    else:
        print("Genres: " + ", ".join(f"{i['label']} ({i['count']})" for i in items))
    return 0


def cmd_moods(args) -> int:
    _, tracks = load_library()
    counts = Counter(m for t in tracks for m in (t.get("moods") or []))
    if args.json:
        as_json({"ok": True, "moods": MOODS, "counts": {m: int(counts.get(m, 0)) for m in MOODS}, "total": len(tracks)})
    else:
        print("Moods: " + ", ".join(f"{m} ({counts.get(m, 0)})" for m in MOODS))
    return 0


def cmd_playlists(args) -> int:
    names = playlist_names()
    if args.json:
        as_json({"ok": True, "playlists": names})
    else:
        print("Playlists: " + (", ".join(names) if names else "(none)"))
    return 0


def cmd_find(args) -> int:
    matches = find_tracks(args.query, limit=args.limit)
    if args.json:
        as_json({"ok": True, "query": args.query, "matches": matches})
    elif not matches:
        print("No matching tracks")
    else:
        for m in matches:
            print(f"{m['score']:.3f}  {m['artist']} - {m['title']}  ({m['album']})")
    return 0


def cmd_tastes(args) -> int:
    """Summarize the local FLAC library's top genres, moods, and artists."""
    _, tracks = load_library()
    genres = Counter(t.get("canonical_genre") for t in tracks if t.get("canonical_genre"))
    moods = Counter(m for t in tracks for m in (t.get("moods") or []))
    artists = Counter(t.get("artist") for t in tracks if t.get("artist"))
    payload = {
        "ok": True,
        "total_tracks": len(tracks),
        "top_genres": [{"genre": g, "label": GENRE_LABELS.get(g, g), "count": c} for g, c in genres.most_common(8)],
        "top_moods": [{"mood": m, "count": c} for m, c in moods.most_common(8)],
        "top_artists": [{"artist": a, "count": c} for a, c in artists.most_common(15)],
    }
    if args.json:
        as_json(payload)
    else:
        print(f"Library: {len(tracks)} tracks")
        print("Top genres: " + ", ".join(f"{x['label']} ({x['count']})" for x in payload["top_genres"]))
        print("Top moods: " + ", ".join(f"{x['mood']} ({x['count']})" for x in payload["top_moods"]))
        print("Top artists: " + ", ".join(f"{x['artist']} ({x['count']})" for x in payload["top_artists"][:10]))
    return 0


def _non_negative_int(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return result


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON")

    parser = MusicArgumentParser(prog="music_win", description="Windows FLAC + YouTube Music controller via mpv Named Pipe IPC.")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    play = sub.add_parser("play", parents=[common], help="select and play local tracks")
    play.add_argument("--mood", action="append")
    play.add_argument("--genre", action="append")
    play.add_argument("--artist", action="append")
    play.add_argument("--album", action="append")
    play.add_argument("--search", action="append")
    play.add_argument("--playlist")
    play.add_argument("--track")
    play.add_argument("--path", action="append", default=None)
    play.add_argument("--limit", type=_non_negative_int, default=None)
    shuffle = play.add_mutually_exclusive_group()
    shuffle.add_argument("--shuffle", dest="shuffle", action="store_true", default=None)
    shuffle.add_argument("--no-shuffle", dest="shuffle", action="store_false")
    play.add_argument("--dry-run", action="store_true")
    play.add_argument("--online", action="store_true")
    play.add_argument("--append", action="store_true")

    find_p = sub.add_parser("find", parents=[common], help="fuzzy search the local music library")
    find_p.add_argument("query")
    find_p.add_argument("--limit", type=int, default=8)

    online = sub.add_parser("online", parents=[common], help="play or queue tracks from YouTube Music")
    online.add_argument("queries", nargs="+")
    online.add_argument("--count", type=int, default=1)
    online.add_argument("--append", action="store_true")
    online.add_argument("--queue", action="store_true")

    enqueue = sub.add_parser("enqueue", parents=[common], help="append matching local or online tracks to queue")
    enqueue.add_argument("queries", nargs="+")
    enqueue.add_argument("--online", action="store_true")

    internal_append = sub.add_parser("_append-online", parents=[common])
    internal_append.add_argument("items_json", nargs="?")

    for name in ("pause", "resume", "toggle", "next", "previous", "stop", "clear", "shuffle", "status", "now", "queue", "genres", "moods", "playlists", "tastes"):
        sub.add_parser(name, parents=[common])

    vol = sub.add_parser("volume", parents=[common])
    vol.add_argument("value", nargs="?")
    seek = sub.add_parser("seek", parents=[common])
    seek.add_argument("value", nargs="?")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    handlers = {
        "play": cmd_play,
        "find": cmd_find,
        "online": cmd_online,
        "enqueue": cmd_enqueue,
        "_append-online": cmd__append_online,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "toggle": cmd_toggle,
        "next": cmd_next,
        "previous": cmd_previous,
        "stop": cmd_stop,
        "clear": cmd_stop,
        "shuffle": cmd_shuffle,
        "status": cmd_status,
        "now": cmd_now,
        "queue": cmd_queue,
        "genres": cmd_genres,
        "moods": cmd_moods,
        "playlists": cmd_playlists,
        "tastes": cmd_tastes,
        "volume": cmd_volume,
        "seek": cmd_seek,
    }
    try:
        return handlers[args.command](args) or 0
    except MusicError as exc:
        if getattr(args, "json", False):
            as_json({"ok": False, "error": exc.message})
        else:
            print(f"music_win: {exc.message}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
