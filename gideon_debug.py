"""Scrubbed diagnostic bundle generator for GIDEON.

Collects system resources, systemd service state, PipeWire audio sink info,
UI IPC/HTTP socket health, journald logs, and the latest Antigravity CLI daemon
log tail while stripping secrets (API keys, Bearer tokens, credentials, emails).
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import Optional

from focus_mode import get_focus_manager
from reflex_router import ensure_hyprland_env

DEFAULT_ENV_FILE = Path.home() / ".config" / "gideon" / "gideon.env"
DEFAULT_AGY_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
DEFAULT_DOWNLOADS_DIR = Path.home() / "Downloads"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(r"(Authorization\s*:\s*Bearer\s+|Bearer\s+)[A-Za-z0-9._\-+/=]{8,}", re.I)
_KV_SECRET_RE = re.compile(
    r"""(\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|AUTH|COOKIE|CREDENTIALS)[A-Z0-9_]*\b\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
    re.I,
)
_KNOWN_KEY_PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{12,}|AIza[0-9A-Za-z_\-]{16,}|ghp_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9\-]{10,}|ya29\.[0-9A-Za-z_\-.]+)\b"
)


def scrub_sensitive_text(text: str) -> str:
    """Redact credentials, API keys, Bearer tokens, cookies, and emails."""
    if not text:
        return ""
    out = _BEARER_RE.sub(r"\1[REDACTED_TOKEN]", text)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    out = _KNOWN_KEY_PREFIX_RE.sub("[REDACTED_KEY]", out)
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)
    return out


def _run_text(argv: list[str], *, timeout: float = 5.0) -> str:
    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        combined = "\n".join(part for part in (res.stdout.strip(), res.stderr.strip()) if part)
        return combined or f"(exit {res.returncode}, no output)"
    except FileNotFoundError:
        return f"(command not found: {argv[0]})"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout}s)"
    except Exception as exc:
        return f"(error: {exc})"


def _read_env_key_inventory(env_path: Path = DEFAULT_ENV_FILE) -> str:
    """List variable names and byte sizes in gideon.env without ever exposing values."""
    if not env_path.is_file():
        return f"(missing: {env_path})"
    lines = []
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val_clean = val.strip().strip('"').strip("'")
        lines.append(f"{key} (<set, {len(val_clean)} chars>)")
    return "\n".join(lines) if lines else "(empty env file)"


def _read_latest_agy_log_tail(log_dir: Path = DEFAULT_AGY_LOG_DIR, max_lines: int = 150) -> tuple[str, str]:
    """Return `(filename, tail_text)` for the newest CLI log in `log_dir`."""
    if not log_dir.is_dir():
        return "(none)", f"(directory missing: {log_dir})"
    candidates = sorted(
        (p for p in log_dir.glob("cli-*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return "(none)", f"(no cli-*.log files in {log_dir})"
    newest = candidates[0]
    try:
        all_lines = newest.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(all_lines[-max_lines:])
        return newest.name, tail
    except Exception as exc:
        return newest.name, f"(failed reading {newest.name}: {exc})"


def _check_tcp_port(host: str = "127.0.0.1", port: int = 8790, timeout: float = 0.5) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return f"listening on {host}:{port}"
    except Exception as exc:
        return f"closed ({exc})"


def collect_debug_bundle(
    output_path: Optional[str | Path] = None,
    *,
    journal_lines: int = 250,
    agy_lines: int = 150,
    env_file: Path = DEFAULT_ENV_FILE,
    agy_log_dir: Path = DEFAULT_AGY_LOG_DIR,
) -> tuple[Path, str]:
    """Assemble and save a scrubbed GIDEON diagnostic bundle to disk."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if output_path is not None:
        dest = Path(output_path).expanduser()
    else:
        target_dir = DEFAULT_DOWNLOADS_DIR if DEFAULT_DOWNLOADS_DIR.is_dir() else Path.home() / ".local" / "state" / "gideon"
        dest = target_dir / f"gideon-debug-{stamp}.log"
    dest.parent.mkdir(parents=True, exist_ok=True)

    hypr_sig = ensure_hyprland_env()
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    hypr_sock = Path(xdg_runtime) / "hypr" / hypr_sig / ".socket.sock" if hypr_sig else None
    ui_sock = Path("/tmp/gideon-ui.sock")
    focus_status = get_focus_manager().status()
    agy_name, agy_tail = _read_latest_agy_log_tail(agy_log_dir, max_lines=agy_lines)

    sections = [
        ("GIDEON DEBUG BUNDLE", "\n".join([
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Kernel: {_run_text(['uname', '-srm'])}",
            f"Python: {sys.executable} ({sys.version.split()[0]})",
            f"STT_BACKEND: {os.environ.get('GIDEON_STT_BACKEND', 'parakeet (default)')}",
            f"FISH_WS: {os.environ.get('GIDEON_FISH_WS', '1 (default)')}",
            f"HYPR_SOCKET: {'present' if (hypr_sock and hypr_sock.exists()) else 'missing'}",
            f"UI_UNIX_SOCK: {'present' if ui_sock.exists() else 'missing'} ({ui_sock})",
            f"HTTP_8790: {_check_tcp_port('127.0.0.1', 8790)}",
        ])),
        ("MEMORY & GPU", "\n".join([
            _run_text(["free", "-h"]),
            "",
            "GPU VRAM:",
            _run_text(["nvidia-smi", "--query-gpu=name,memory.used,memory.free,memory.total", "--format=csv,noheader"]),
        ])),
        ("ENV INVENTORY (VALUES OMITTED)", _read_env_key_inventory(env_file)),
        ("FOCUS MODE STATUS", json.dumps(focus_status, indent=2)),
        ("SYSTEMD SERVICES", _run_text(["systemctl", "--user", "status", "gideon.service", "gideon-ui.service", "--no-pager", "-l"])),
        ("AUDIO ROUTING (WPCTL)", "\n".join([
            "Default Sink: " + _run_text(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"]).splitlines()[0],
            "Default Source: " + _run_text(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"]).splitlines()[0],
        ])),
        (f"JOURNALD TAIL (LAST {journal_lines} LINES)", _run_text([
            "journalctl", "--user", "-u", "gideon.service", "-u", "gideon-ui.service",
            "-n", str(journal_lines), "--no-pager",
        ], timeout=8.0)),
        (f"ANTIGRAVITY CLI LOG TAIL ({agy_name}, LAST {agy_lines} LINES)", agy_tail),
    ]

    rendered = "\n\n".join(
        f"==================== {title} ====================\n{scrub_sensitive_text(body)}"
        for title, body in sections
    ) + "\n"

    dest.write_text(rendered, encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    summary = f"Saved scrubbed debug bundle to {dest}"
    return dest, summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export a scrubbed GIDEON status and log bundle.")
    parser.add_argument("-o", "--output", default=None, help="Output file path (default: ~/Downloads/gideon-debug-<ts>.log)")
    parser.add_argument("--journal-lines", type=int, default=250, help="Number of systemd journal lines to include")
    parser.add_argument("--agy-lines", type=int, default=150, help="Number of AGY CLI log lines to include")
    args = parser.parse_args(argv)
    path, summary = collect_debug_bundle(
        args.output,
        journal_lines=args.journal_lines,
        agy_lines=args.agy_lines,
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
