"""Windows per-session audio ducking using pycaw on a dedicated COM thread."""
from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Optional

logger = logging.getLogger("gideon.win.ducking")


def _parse_duck_level() -> float:
    try:
        val = float(os.environ.get("GIDEON_DUCK_LEVEL", "0.05"))
    except ValueError:
        val = 0.05
    return max(0.0, min(1.0, val))


DUCK_LEVEL = _parse_duck_level()


class AudioDucker:
    """Ducks other Windows audio sessions while Gideon listens/speaks and restores cleanly."""

    def __init__(self, duck_level: Optional[float] = None) -> None:
        self.duck_level = DUCK_LEVEL if duck_level is None else max(0.0, min(1.0, float(duck_level)))
        self._own_pid = os.getpid()
        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=32)
        self._lock = threading.Lock()
        self._is_ducked = False
        # (session_id, pid, proc_name, idx) -> (original_volume, target_ducked_volume)
        self._saved: dict[tuple[str, int, str, int], tuple[float, float]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="gideon-audio-duck", daemon=True)
        self._thread.start()

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return self._is_ducked

    def duck(self) -> None:
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait("duck")
        except queue.Full:
            pass

    def restore(self) -> None:
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait("restore")
        except queue.Full:
            pass

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._queue.put_nowait("restore")
        except queue.Full:
            pass
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _worker(self) -> None:
        com_inited = False
        try:
            import comtypes

            comtypes.CoInitialize()
            com_inited = True
        except Exception as exc:
            logger.debug("AudioDucker CoInitialize skipped: %s", exc)

        try:
            while True:
                cmd = self._queue.get()
                if cmd is None:
                    break
                # Coalesce rapid back-to-back requests to the latest state
                while not self._queue.empty():
                    try:
                        nxt = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        cmd = "restore_and_exit"
                        break
                    cmd = nxt

                if cmd == "duck":
                    self._do_duck()
                elif cmd == "restore":
                    self._do_restore()
                elif cmd == "restore_and_exit":
                    self._do_restore()
                    break
        finally:
            if com_inited:
                try:
                    import comtypes

                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def _iter_sessions(self):
        import comtypes
        from pycaw.pycaw import AudioSession, AudioUtilities, IAudioSessionControl2, IAudioSessionManager2

        raw_sessions = []
        try:
            enum = AudioUtilities.GetDeviceEnumerator()
            # eRender = 0, DEVICE_STATE_ACTIVE = 1
            col = enum.EnumAudioEndpoints(0, 1)
            dev_count = col.GetCount()
            for dev_idx in range(dev_count):
                try:
                    dev = col.Item(dev_idx)
                    iface = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                    mgr = iface.QueryInterface(IAudioSessionManager2)
                    se = mgr.GetSessionEnumerator()
                    for i in range(se.GetCount()):
                        ctl = se.GetSession(i)
                        if ctl is None:
                            continue
                        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                        if ctl2 is not None:
                            raw_sessions.append(AudioSession(ctl2))
                except Exception:
                    continue
        except Exception:
            raw_sessions = []

        if not raw_sessions:
            raw_sessions = AudioUtilities.GetAllSessions()

        for idx, session in enumerate(raw_sessions):
            proc = getattr(session, "Process", None)
            if not proc:
                continue
            pid = getattr(session, "ProcessId", None)
            if pid is None:
                try:
                    pid = proc.pid
                except Exception:
                    continue
            if int(pid) == self._own_pid:
                continue
            try:
                name = proc.name() or ""
            except Exception:
                name = ""
            vol = getattr(session, "SimpleAudioVolume", None)
            if vol is None:
                continue
            sid = str(getattr(session, "SessionInstanceIdentifier", None) or "")
            yield (sid, int(pid), name, idx), vol

    def _do_duck(self) -> None:
        with self._lock:
            if self._is_ducked:
                return
            self._is_ducked = True

        saved: dict[tuple[str, int, str, int], tuple[float, float]] = {}
        try:
            for key, vol in self._iter_sessions():
                try:
                    orig = float(vol.GetMasterVolume())
                    if orig <= 0.0:
                        continue
                    target = max(0.0, min(1.0, orig * self.duck_level))
                    vol.SetMasterVolume(target, None)
                    saved[key] = (orig, target)
                except Exception as exc:
                    logger.debug("AudioDucker failed to duck %s: %s", key, exc)
        except Exception as exc:
            logger.debug("AudioDucker duck scan failed: %s", exc)

        with self._lock:
            self._saved = saved
        if saved:
            logger.info("[Duck] Ducked %d audio session(s) -> %.0f%%", len(saved), self.duck_level * 100)

    def _do_restore(self) -> None:
        with self._lock:
            saved = dict(self._saved)
            self._saved.clear()
            self._is_ducked = False

        if not saved:
            return
        restored = 0
        # Map by session instance id when non-empty, else (pid, proc_name)
        by_sid = {k[0]: (k, v) for k, v in saved.items() if k[0]}
        by_proc = {(k[1], k[2]): (k, v) for k, v in saved.items()}
        try:
            for key, vol in self._iter_sessions():
                entry = by_sid.get(key[0]) if key[0] else None
                if entry is None:
                    entry = by_proc.get((key[1], key[2]))
                if entry is None:
                    continue
                _, (orig, target) = entry
                try:
                    cur = float(vol.GetMasterVolume())
                    if abs(cur - target) < 0.03:
                        vol.SetMasterVolume(max(0.0, min(1.0, orig)), None)
                        restored += 1
                except Exception as exc:
                    logger.debug("AudioDucker failed to restore %s: %s", key, exc)
        except Exception as exc:
            logger.debug("AudioDucker restore scan failed: %s", exc)
        if restored:
            logger.info("[Duck] Restored %d audio session(s)", restored)
