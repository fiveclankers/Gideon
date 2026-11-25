#!/home/cat/jarvis/.venv/bin/python
"""Live end-to-end latency & failure-mode benchmark for the running GIDEON daemon.

Measures real wall-clock timings via GET /state polling (50ms) and correlates
every turn against newly emitted journalctl (gideon.service), Antigravity CLI
logs (~/.gemini/antigravity-cli/log/cli-*.log), read-only AGY conversation DB
steps, and /tmp/music-mpv.{sock,log}. Never restarts gideon.service or edits
GIDEON core files.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "http://127.0.0.1:8790"
DEFAULT_JSON_OUT = Path("/tmp/gideon_live_latency_report.json")
DEFAULT_MD_OUT = Path("/tmp/gideon_live_latency_report.md")
CLI_LOG_DIR = Path.home() / ".gemini" / "antigravity-cli" / "log"
CONVERSATIONS_DIR = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
MUSIC_BIN = Path.home() / ".local" / "bin" / "music"
MPV_SOCK = Path("/tmp/music-mpv.sock")
MPV_LOG = Path("/tmp/music-mpv.log")
BENCH_NOTE_PATH = Path("/tmp/gideon_bench_note.md")


@dataclass(frozen=True)
class BenchCase:
    case_id: str
    category: str
    prompt: str
    effort: str = "low"
    timeout_s: float = 45.0
    budget_s: Optional[float] = None
    cleanup: Optional[str] = None
    notes: str = ""


CASES: list[BenchCase] = [
    BenchCase(
        case_id="tier0_focus_status",
        category="tier0",
        prompt="focus status",
        effort="low",
        timeout_s=30.0,
        budget_s=30.0,
    ),
    BenchCase(
        case_id="small_sky_blue",
        category="small",
        prompt="Why is the sky blue? Keep it brief.",
        effort="low",
        timeout_s=90.0,
        budget_s=45.0,
    ),
    BenchCase(
        case_id="small_big_o",
        category="small",
        prompt="What is the difference between O of N log N and O of N squared in plain words?",
        effort="low",
        timeout_s=90.0,
        budget_s=45.0,
    ),
    BenchCase(
        case_id="small_euphonium",
        category="small",
        prompt="Why does a euphonium sound warmer than a trumpet acoustically?",
        effort="low",
        timeout_s=90.0,
        budget_s=45.0,
    ),
    BenchCase(
        case_id="medium_dijkstra_astar_low",
        category="medium",
        prompt=(
            "Compare Dijkstra's algorithm and A-star pathfinding, especially when "
            "heuristic choices slow A-star down or hurt optimality."
        ),
        effort="low",
        timeout_s=130.0,
        budget_s=75.0,
    ),
    BenchCase(
        case_id="medium_lc_qfactor_low",
        category="medium",
        prompt="Explain intuitively how an LC circuit resonates and what Q-factor actually controls.",
        effort="low",
        timeout_s=130.0,
        budget_s=75.0,
    ),
    BenchCase(
        case_id="medium_parakeet_whisper_medium",
        category="medium",
        prompt="Compare Parakeet TDT ONNX versus Faster-Whisper small on a CPU for live voice assistants.",
        effort="medium",
        timeout_s=130.0,
        budget_s=75.0,
    ),
    BenchCase(
        case_id="retrieve_focus_stt",
        category="retrieve_note",
        prompt=(
            f"Check {REPO_ROOT / 'focus_mode.py'} and tell me the default poll interval "
            f"and grace hits, and also check what STT backend {REPO_ROOT / 'gideon.py'} defaults to."
        ),
        effort="low",
        timeout_s=90.0,
        budget_s=75.0,
    ),
    BenchCase(
        case_id="note_stt_bench",
        category="retrieve_note",
        prompt=(
            "Write a two-line summary of the STT backend and port from "
            f"{REPO_ROOT / 'gideon.py'} into /tmp/gideon_bench_note.md and confirm."
        ),
        effort="low",
        timeout_s=90.0,
        budget_s=75.0,
        cleanup="remove_bench_note",
    ),
    BenchCase(
        case_id="online_music_natural",
        category="online_music",
        prompt="Play Hotel California by Eagles, it is not in my local library.",
        effort="low",
        timeout_s=90.0,
        budget_s=90.0,
        cleanup="music_stop",
        notes="Substituted Eagles - Hotel California after verifying Radiohead - Creep is present locally.",
    ),
    BenchCase(
        case_id="online_music_stream",
        category="online_music",
        prompt="Stream Never Gonna Give You Up by Rick Astley from YouTube, it is not in my local library.",
        effort="low",
        timeout_s=90.0,
        budget_s=90.0,
        cleanup="music_stop",
        notes="Second probe with mpv daemon warm/checked after Prompt 1.",
    ),
    BenchCase(
        case_id="sparse_web_python313",
        category="sparse_web",
        prompt=(
            "Look up the latest stable Python 3.13 patch version via web search and "
            "give me a two-sentence summary of what's notable in it."
        ),
        effort="low",
        timeout_s=140.0,
        budget_s=110.0,
    ),
]


def _round_opt(val: Optional[float], digits: int = 3) -> Optional[float]:
    return None if val is None else round(val, digits)


def get_journal_cursor() -> Optional[str]:
    proc = subprocess.run(
        ["journalctl", "--user", "-u", "gideon.service", "-n", "1", "-o", "json", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if "__CURSOR" in obj:
                return str(obj["__CURSOR"])
        except json.JSONDecodeError:
            continue
    return None


def read_journal_after(cursor: Optional[str]) -> list[dict[str, Any]]:
    cmd = ["journalctl", "--user", "-u", "gideon.service", "-o", "json", "--no-pager"]
    if cursor:
        cmd.append(f"--after-cursor={cursor}")
    else:
        cmd.extend(["-n", "200"])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    entries: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_us = obj.get("__REALTIME_TIMESTAMP")
        ts_s = int(ts_us) / 1_000_000.0 if ts_us is not None else None
        entries.append(
            {
                "ts_epoch": ts_s,
                "message": str(obj.get("MESSAGE") or ""),
                "cursor": obj.get("__CURSOR"),
            }
        )
    return entries


def snapshot_cli_logs() -> dict[str, int]:
    offsets: dict[str, int] = {}
    if not CLI_LOG_DIR.is_dir():
        return offsets
    for path in sorted(CLI_LOG_DIR.glob("cli-*.log")):
        try:
            offsets[str(path)] = path.stat().st_size
        except OSError:
            pass
    return offsets


def read_cli_log_deltas(before: dict[str, int]) -> dict[str, list[str]]:
    deltas: dict[str, list[str]] = {}
    if not CLI_LOG_DIR.is_dir():
        return deltas
    for path in sorted(CLI_LOG_DIR.glob("cli-*.log")):
        pstr = str(path)
        start_off = before.get(pstr, 0)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= start_off:
            continue
        try:
            with path.open("rb") as fh:
                fh.seek(start_off)
                chunk = fh.read(size - start_off)
            text = chunk.decode("utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if lines:
                deltas[path.name] = lines
        except OSError:
            continue
    return deltas


def find_active_conversation_id(journal_entries: list[dict[str, Any]] | None = None) -> Optional[str]:
    if journal_entries:
        for entry in reversed(journal_entries):
            m = re.search(r"conv=([0-9a-f-]{36})", entry.get("message", ""))
            if m:
                return m.group(1)
    proc = subprocess.run(
        ["journalctl", "--user", "-u", "gideon.service", "-b", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    matches = re.findall(r"conv=([0-9a-f-]{36})", proc.stdout)
    return matches[-1] if matches else None


def snapshot_conv_max_idx(conv_id: Optional[str]) -> Optional[int]:
    if not conv_id:
        return None
    db_path = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not db_path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("SELECT COALESCE(MAX(idx), -1) FROM steps").fetchone()
        con.close()
        return int(row[0]) if row else -1
    except Exception:
        return None


def read_conv_steps_after(conv_id: Optional[str], after_idx: Optional[int]) -> list[dict[str, Any]]:
    if not conv_id:
        return []
    db_path = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not db_path.is_file():
        return []
    min_idx = -1 if after_idx is None else after_idx
    steps: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT idx, step_type, status, step_payload, error_details FROM steps WHERE idx > ? ORDER BY idx ASC",
            (min_idx,),
        ).fetchall()
        con.close()
    except Exception:
        return []

    for idx, step_type, status, payload, err in rows:
        raw_strings = re.findall(rb"[ -~]{4,}", payload or b"")
        err_strings = re.findall(rb"[ -~]{4,}", err or b"")
        decoded = [s.decode("utf-8", "replace") for s in raw_strings]
        decoded_err = [s.decode("utf-8", "replace") for s in err_strings]
        cmd_jsons: list[dict[str, Any]] = []
        output_text: Optional[str] = None
        for s in decoded:
            if any(k in s for k in ('"CommandLine"', '"query"', '"AbsolutePath"', '"Path"')):
                start = s.find("{")
                end = s.rfind("}")
                if 0 <= start < end:
                    try:
                        cmd_jsons.append(json.loads(s[start : end + 1]))
                    except json.JSONDecodeError:
                        pass
            m_out = re.search(r"file://([^\s\"']+?/steps/\d+/output\.txt)", s)
            if m_out:
                out_path = Path(m_out.group(1))
                if out_path.is_file():
                    try:
                        output_text = out_path.read_text(encoding="utf-8", errors="replace")[:4000]
                    except OSError:
                        pass
        steps.append(
            {
                "idx": int(idx),
                "step_type": int(step_type),
                "status": int(status),
                "tool_args": cmd_jsons,
                "output_text": output_text,
                "strings_preview": decoded[:15],
                "error_strings": decoded_err[:10],
            }
        )
    return steps


def snapshot_mpv_log() -> int:
    try:
        return MPV_LOG.stat().st_size if MPV_LOG.exists() else 0
    except OSError:
        return 0


def read_mpv_log_delta(start_off: int) -> list[str]:
    if not MPV_LOG.exists():
        return []
    try:
        size = MPV_LOG.stat().st_size
        if size <= start_off:
            return []
        with MPV_LOG.open("rb") as fh:
            fh.seek(start_off)
            return fh.read(size - start_off).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def wait_until_ready(client: httpx.Client, max_wait_s: float = 45.0) -> dict[str, Any]:
    """Wait until GIDEON /state reaches idle (dismissing hotmic with 'bye bye' if needed)."""
    deadline = time.monotonic() + max_wait_s
    dismissed = False
    while True:
        resp = client.get("/state")
        resp.raise_for_status()
        st = resp.json()
        state = st.get("state")
        loading = bool(st.get("loading"))
        if state == "idle" and not loading:
            return st
        if state == "hotmic" and not dismissed:
            dismissed = True
            client.post("/api/prompt", json={"text": "bye bye"})
        if time.monotonic() >= deadline:
            return st
        time.sleep(0.05)


def parse_cli_timestamp(line: str, ref_year: int) -> Optional[float]:
    # Format: I1115 18:15:43.015845 ...
    m = re.match(r"^[IWEF](\d{2})(\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d+)", line)
    if not m:
        return None
    mm, dd, hms = m.groups()
    try:
        dt = datetime.strptime(f"{ref_year}-{mm}-{dd} {hms}", "%Y-%m-%d %H:%M:%S.%f")
        return dt.timestamp()
    except ValueError:
        return None


def analyze_forensics(
    t0_epoch: float,
    journal_entries: list[dict[str, Any]],
    cli_deltas: dict[str, list[str]],
    conv_steps: list[dict[str, Any]],
    first_speaking_s: Optional[float],
) -> dict[str, Any]:
    bge_events: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    gideon_sentences: list[dict[str, Any]] = []
    fish_http_requests: list[dict[str, Any]] = []
    fish_ws_warnings: list[str] = []
    fish_errors: list[str] = []
    watchdog_events: list[str] = []
    turn_complete_s: Optional[float] = None
    effort_switch_start_s: Optional[float] = None
    effort_switch_ready_s: Optional[float] = None
    errors_or_warnings: list[str] = []

    for entry in journal_entries:
        msg = entry["message"]
        ts = entry["ts_epoch"]
        rel_s = (ts - t0_epoch) if ts is not None else None

        if "Switching Antigravity Engine effort:" in msg:
            effort_switch_start_s = rel_s
        elif "Antigravity Engine connected and ready" in msg and effort_switch_start_s is not None:
            effort_switch_ready_s = rel_s

        m_t0 = re.search(r"\[Tier-0 Reflex\]\s+(\S+)\s+\(([\d.]+)ms\):\s*(.*)", msg)
        if m_t0:
            bge_events.append(
                {
                    "tier": "tier0",
                    "intent": m_t0.group(1),
                    "latency_ms": float(m_t0.group(2)),
                    "detail": m_t0.group(3),
                    "rel_s": _round_opt(rel_s),
                }
            )
        m_t1 = re.search(r"\[Tier-1 Neural Reflex\]\s+(\S+)\s+\(([\d.]+)ms\):\s*(.*)", msg)
        if m_t1:
            bge_events.append(
                {
                    "tier": "tier1_neural",
                    "intent": m_t1.group(1),
                    "latency_ms": float(m_t1.group(2)),
                    "detail": m_t1.group(3),
                    "rel_s": _round_opt(rel_s),
                }
            )
        if "[Tier-1 bypass]" in msg or "Escalating" in msg or "Tier-0 music play failed" in msg:
            bge_events.append(
                {
                    "tier": "bypass_or_escalate",
                    "detail": msg,
                    "rel_s": _round_opt(rel_s),
                }
            )

        m_tool = re.search(r"\[Tool Execution\]\s+(.*)", msg)
        if m_tool:
            tool_events.append({"label": m_tool.group(1), "rel_s": _round_opt(rel_s)})

        if msg.startswith("Gideon:"):
            gideon_sentences.append({"text": msg[len("Gideon:") :].strip(), "rel_s": _round_opt(rel_s)})

        if "POST https://api.fish.audio/v1/tts" in msg:
            fish_http_requests.append({"rel_s": _round_opt(rel_s), "message": msg})
        if "Fish WS" in msg:
            fish_ws_warnings.append(msg)
        if "Fish TTS" in msg or "Fish HTTP fallback error" in msg:
            fish_errors.append(msg)
        if "[Watchdog Check-in]" in msg:
            watchdog_events.append(msg)
        if "[Turn Complete]" in msg:
            turn_complete_s = rel_s
        if "[WARNING]" in msg or "[ERROR]" in msg or "failed" in msg.lower():
            errors_or_warnings.append(msg)

    # Determine Fish TTS path empirically
    if fish_http_requests and fish_ws_warnings:
        fish_path = "http_fallback"
    elif fish_http_requests:
        fish_path = "http"
    elif first_speaking_s is not None or gideon_sentences:
        fish_path = "ws_live"
    else:
        fish_path = "none"

    # Extract CLI log round-trips, hooks, and errors
    ref_year = datetime.fromtimestamp(t0_epoch).year
    cli_sse_calls: list[dict[str, Any]] = []
    cli_hooks_or_errors: list[str] = []
    cli_send_latency_ms: Optional[int] = None
    for fname, lines in cli_deltas.items():
        for ln in lines:
            ts = parse_cli_timestamp(ln, ref_year)
            rel_s = _round_opt(ts - t0_epoch) if ts is not None else None
            if "streamGenerateContent" in ln:
                m_rid = re.search(r"ResponseID:\s*(\S+)", ln)
                cli_sse_calls.append(
                    {
                        "file": fname,
                        "rel_s": rel_s,
                        "response_id": m_rid.group(1) if m_rid else None,
                    }
                )
            m_lat = re.search(r"SEND_USER_CASCADE_MESSAGE_LATENCY.*total_latency_ms:(\d+)", ln)
            if m_lat:
                cli_send_latency_ms = int(m_lat.group(1))
            if (
                ln.startswith(("E", "W", "Caught panic"))
                or "hook" in ln.lower()
                or "error" in ln.lower()
                or "failed" in ln.lower()
                or "timeout" in ln.lower()
            ):
                cli_hooks_or_errors.append(f"[{fname}] {ln}")

    effort_switch_s = (
        round(effort_switch_ready_s - effort_switch_start_s, 3)
        if effort_switch_start_s is not None and effort_switch_ready_s is not None
        else (round(effort_switch_ready_s, 3) if effort_switch_ready_s is not None else None)
    )

    tool_steps_from_db = sum(1 for s in conv_steps if s.get("step_type") == 132)
    return {
        "bge_events": bge_events,
        "tool_events_journal": tool_events,
        "tool_steps_from_db": tool_steps_from_db,
        "conv_steps_delta": conv_steps,
        "gideon_sentences": gideon_sentences,
        "first_sentence_s": gideon_sentences[0]["rel_s"] if gideon_sentences else None,
        "fish_path": fish_path,
        "fish_http_count": len(fish_http_requests),
        "fish_ws_warnings": fish_ws_warnings,
        "fish_errors": fish_errors,
        "watchdog_events": watchdog_events,
        "turn_complete_journal_s": _round_opt(turn_complete_s),
        "effort_switch_s": effort_switch_s,
        "effort_switch_ready_s": _round_opt(effort_switch_ready_s),
        "cli_sse_roundtrips": len(cli_sse_calls),
        "cli_sse_calls": cli_sse_calls,
        "cli_send_latency_ms": cli_send_latency_ms,
        "cli_hooks_or_errors": cli_hooks_or_errors[:30],
        "journal_warnings_or_errors": errors_or_warnings,
    }


def run_case(client: httpx.Client, case: BenchCase, poll_interval_s: float, timeout_override: Optional[float]) -> dict[str, Any]:
    timeout_s = timeout_override if timeout_override is not None else case.timeout_s
    pre_state = wait_until_ready(client, max_wait_s=45.0)

    if case.case_id == "note_stt_bench" and BENCH_NOTE_PATH.exists():
        BENCH_NOTE_PATH.unlink()

    cursor = get_journal_cursor()
    cli_before = snapshot_cli_logs()
    conv_id = find_active_conversation_id()
    conv_idx_before = snapshot_conv_max_idx(conv_id)
    mpv_sock_before = MPV_SOCK.exists()
    mpv_log_before = snapshot_mpv_log()

    t0_epoch = time.time()
    t0 = time.monotonic()
    resp = client.post("/api/prompt", json={"text": case.prompt, "effort": case.effort})
    resp.raise_for_status()
    post_ack_s = time.monotonic() - t0

    timeline: list[dict[str, Any]] = []
    last_state_key: Any = None
    last_state_change_t = t0
    last_reply = ""
    last_reply_change_t = t0

    ttft_s: Optional[float] = None
    first_speaking_s: Optional[float] = None
    done_s: Optional[float] = None
    completion_reason = ""
    timed_out = False
    left_initial_idle = False
    turn_started = False
    switching_in_progress = False
    last_journal_check_t = 0.0
    final_state_payload: dict[str, Any] = {}

    while True:
        now = time.monotonic()
        elapsed = now - t0
        if elapsed >= timeout_s:
            timed_out = True
            completion_reason = "timeout"
            break

        st = client.get("/state").json()
        final_state_payload = st
        state = st.get("state")
        loading = bool(st.get("loading"))
        tool = st.get("tool")
        reply = str(st.get("reply") or "")
        effort_now = st.get("effort")

        tool_sig = (tool.get("id"), tool.get("status"), tool.get("label")) if isinstance(tool, dict) else None
        state_key = (state, tool_sig, effort_now)
        if state_key != last_state_key:
            last_state_key = state_key
            last_state_change_t = now
            timeline.append(
                {
                    "elapsed_s": round(elapsed, 3),
                    "state": state,
                    "effort": effort_now,
                    "tool": tool.get("label") if isinstance(tool, dict) else None,
                    "tool_status": tool.get("status") if isinstance(tool, dict) else None,
                    "reply_len": len(reply),
                }
            )

        if state != "idle" or loading or bool(reply.strip()):
            left_initial_idle = True
        if state in ("thinking", "bge_reflex", "conversing") or reply == "":
            turn_started = True

        if turn_started and ttft_s is None and reply.strip():
            ttft_s = elapsed
            timeline.append(
                {
                    "elapsed_s": round(elapsed, 3),
                    "state": f"{state}:reply_nonempty",
                    "effort": effort_now,
                    "tool": tool.get("label") if isinstance(tool, dict) else None,
                    "tool_status": tool.get("status") if isinstance(tool, dict) else None,
                    "reply_len": len(reply),
                }
            )

        if reply != last_reply:
            last_reply = reply
            last_reply_change_t = now

        if turn_started and first_speaking_s is None and state == "speaking":
            first_speaking_s = elapsed

        if left_initial_idle:
            if state == "hotmic":
                done_s = elapsed
                completion_reason = "hotmic"
                break
            if state == "idle" and not loading:
                if (now - last_journal_check_t) >= 0.2:
                    last_journal_check_t = now
                    switching_in_progress = False
                    j_probe = read_journal_after(cursor)
                    msgs = [e["message"] for e in j_probe]
                    sw_idx = max((i for i, m in enumerate(msgs) if "Switching Antigravity Engine effort:" in m), default=-1)
                    ready_idx = max((i for i, m in enumerate(msgs) if "Antigravity Engine connected and ready" in m), default=-1)
                    if sw_idx >= 0 and ready_idx < sw_idx:
                        switching_in_progress = True
                        last_state_change_t = now
                    has_complete = any("[Turn Complete]" in m for m in msgs)
                    has_reflex = any(
                        "[Tier-0 Reflex]" in m or "[Tier-1 Neural Reflex]" in m for m in msgs
                    )
                    if not switching_in_progress and (has_complete or has_reflex):
                        if not reply or (now - last_reply_change_t) >= 0.1:
                            done_s = elapsed
                            completion_reason = "idle_journal_complete" if has_complete else "idle_reflex"
                            break
                if (
                    not switching_in_progress
                    and (now - last_state_change_t) >= 65.0
                    and (now - last_reply_change_t) >= 2.0
                    and (reply.strip() or elapsed >= 65.0)
                ):
                    done_s = elapsed
                    completion_reason = "idle_settled_fallback"
                    break

        time.sleep(poll_interval_s)

    # Give journal/stdout ~100ms to flush trailing log lines
    time.sleep(0.1)
    journal_entries = read_journal_after(cursor)
    cli_deltas = read_cli_log_deltas(cli_before)
    # Re-resolve conversation id in case effort switch restarted AGY
    conv_id_after = find_active_conversation_id(journal_entries) or conv_id
    conv_steps = read_conv_steps_after(
        conv_id_after,
        conv_idx_before if conv_id_after == conv_id else -1,
    )

    mpv_sock_after = MPV_SOCK.exists()
    mpv_log_delta = read_mpv_log_delta(mpv_log_before)
    music_status_after = None
    if case.category == "online_music":
        ms = subprocess.run(
            [str(MUSIC_BIN), "status", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            music_status_after = json.loads(ms.stdout.strip()) if ms.stdout.strip() else ms.stderr.strip()
        except json.JSONDecodeError:
            music_status_after = ms.stdout.strip() or ms.stderr.strip()

    bench_note_content = None
    if case.case_id == "note_stt_bench" and BENCH_NOTE_PATH.exists():
        bench_note_content = BENCH_NOTE_PATH.read_text(encoding="utf-8", errors="replace")

    cleanup_result = None
    if case.cleanup == "remove_bench_note":
        if BENCH_NOTE_PATH.exists():
            BENCH_NOTE_PATH.unlink()
            cleanup_result = "removed /tmp/gideon_bench_note.md"
        else:
            cleanup_result = "/tmp/gideon_bench_note.md was not created"
    elif case.cleanup == "music_stop":
        stop_proc = subprocess.run(
            [str(MUSIC_BIN), "stop", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        cleanup_result = stop_proc.stdout.strip() or stop_proc.stderr.strip()

    forensics = analyze_forensics(
        t0_epoch=t0_epoch,
        journal_entries=journal_entries,
        cli_deltas=cli_deltas,
        conv_steps=conv_steps,
        first_speaking_s=first_speaking_s,
    )

    pure_done_s = (
        round(done_s - forensics["effort_switch_s"], 3)
        if done_s is not None and forensics["effort_switch_s"] is not None
        else _round_opt(done_s)
    )
    pure_ttft_s = (
        round(ttft_s - forensics["effort_switch_s"], 3)
        if ttft_s is not None and forensics["effort_switch_s"] is not None
        else _round_opt(ttft_s)
    )
    pure_speaking_s = (
        round(first_speaking_s - forensics["effort_switch_s"], 3)
        if first_speaking_s is not None and forensics["effort_switch_s"] is not None
        else _round_opt(first_speaking_s)
    )

    return {
        "case_id": case.case_id,
        "category": case.category,
        "prompt": case.prompt,
        "requested_effort": case.effort,
        "pre_state": pre_state.get("state"),
        "pre_effort": pre_state.get("effort"),
        "post_ack_s": round(post_ack_s, 4),
        "ttft_s": _round_opt(ttft_s),
        "first_speaking_s": _round_opt(first_speaking_s),
        "done_s": _round_opt(done_s),
        "pure_ttft_s": pure_ttft_s,
        "pure_first_speaking_s": pure_speaking_s,
        "pure_done_s": pure_done_s,
        "timed_out": timed_out,
        "budget_s": case.budget_s,
        "exceeded_budget": (done_s > case.budget_s) if (done_s is not None and case.budget_s is not None) else timed_out,
        "completion_reason": completion_reason,
        "reply_text": final_state_payload.get("reply") or "",
        "state_timeline": timeline,
        "mpv_sock_before": mpv_sock_before,
        "mpv_sock_after": mpv_sock_after,
        "mpv_log_delta": mpv_log_delta,
        "music_status_after": music_status_after,
        "bench_note_content": bench_note_content,
        "cleanup_result": cleanup_result,
        "notes": case.notes,
        "forensics": forensics,
        "journal_lines": [
            {"rel_s": _round_opt((e["ts_epoch"] - t0_epoch) if e["ts_epoch"] else None), "message": e["message"]}
            for e in journal_entries
        ],
    }


def _stat_summary(values: list[float]) -> dict[str, Optional[float]]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"min": None, "median": None, "max": None, "count": 0}
    return {
        "min": round(min(clean), 3),
        "median": round(statistics.median(clean), 3),
        "max": round(max(clean), 3),
        "count": len(clean),
    }


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    cat_stats: dict[str, Any] = {}
    for cat, items in by_cat.items():
        cat_stats[cat] = {
            "ttft_s": _stat_summary([i["ttft_s"] for i in items if i["ttft_s"] is not None]),
            "first_speaking_s": _stat_summary(
                [i["first_speaking_s"] for i in items if i["first_speaking_s"] is not None]
            ),
            "done_s": _stat_summary([i["done_s"] for i in items if i["done_s"] is not None]),
            "pure_done_s": _stat_summary([i["pure_done_s"] for i in items if i["pure_done_s"] is not None]),
        }

    fish_counts: dict[str, int] = {}
    for r in results:
        fp = r["forensics"]["fish_path"]
        fish_counts[fp] = fish_counts.get(fp, 0) + 1

    return {"categories": cat_stats, "fish_paths": fish_counts}


def fmt(v: Any) -> str:
    return "—" if v is None else f"{v:.3f}s" if isinstance(v, float) else str(v)


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# GIDEON Live Latency & Failure-Mode Benchmark Report")
    lines.append("")
    lines.append(f"- Timestamp: `{report['timestamp_iso']}`")
    lines.append(f"- Target Daemon: `{report['base_url']}`")
    lines.append(f"- Poll Interval: `{report['poll_ms']} ms`")
    lines.append(f"- Final Effort Restored: `{report['final_effort']}` (`state={report['final_state']}`)")
    lines.append("")
    lines.append("## Per-Prompt Timing Breakdown")
    lines.append("")
    lines.append(
        "| Case ID | Cat | Effort | TTFT | 1st Speak | Done | Effort Sw | Pure Done | Budget | Fish Path | SSE Turns | Tools (DB/Jrn) | Status |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|")
    for r in report["results"]:
        f = r["forensics"]
        budget_str = f"{r['budget_s']:.0f}s" if r.get("budget_s") else "—"
        if r.get("exceeded_budget"):
            budget_str += " (OVER)"
        lines.append(
            f"| `{r['case_id']}` | `{r['category']}` | `{r['requested_effort']}` | "
            f"{fmt(r['ttft_s'])} | {fmt(r['first_speaking_s'])} | {fmt(r['done_s'])} | "
            f"{fmt(f['effort_switch_s'])} | {fmt(r['pure_done_s'])} | {budget_str} | "
            f"`{f['fish_path']}` (http={f['fish_http_count']}) | {f['cli_sse_roundtrips']} | "
            f"{f.get('tool_steps_from_db', 0)}/{len(f['tool_events_journal'])} | `{r['completion_reason']}` |"
        )

    lines.append("")
    lines.append("## Category Aggregate Timings (Min / Median / Max)")
    lines.append("")
    lines.append("| Category | Count | TTFT (min / med / max) | 1st Speak (min / med / max) | Done (min / med / max) | Pure Done (min / med / max) |")
    lines.append("|---|---:|---|---|---|---|")
    for cat, st in report["summary"]["categories"].items():
        def _mm(d: dict[str, Any]) -> str:
            if not d["count"]:
                return "—"
            return f"{d['min']:.3f}s / {d['median']:.3f}s / {d['max']:.3f}s"

        lines.append(
            f"| `{cat}` | {st['done_s']['count']} | {_mm(st['ttft_s'])} | "
            f"{_mm(st['first_speaking_s'])} | {_mm(st['done_s'])} | {_mm(st['pure_done_s'])} |"
        )

    lines.append("")
    lines.append("## Fish Audio TTS Path Summary")
    lines.append("")
    lines.append(f"- Path counts across benchmark turns: `{json.dumps(report['summary']['fish_paths'])}`")
    lines.append("")
    lines.append("## Detailed Turn Forensics")
    lines.append("")
    for r in report["results"]:
        f = r["forensics"]
        lines.append(f"### `{r['case_id']}` (`{r['category']}`, effort=`{r['requested_effort']}`)")
        lines.append(f"- **Prompt:** {r['prompt']}")
        if r["notes"]:
            lines.append(f"- **Note:** {r['notes']}")
        lines.append(
            f"- **Timings:** `post_ack={r['post_ack_s']}s`, `ttft={fmt(r['ttft_s'])}`, "
            f"`first_sentence={fmt(f['first_sentence_s'])}`, `first_speaking={fmt(r['first_speaking_s'])}`, "
            f"`done={fmt(r['done_s'])}` (`{r['completion_reason']}`), `effort_switch={fmt(f['effort_switch_s'])}`, "
            f"`pure_done={fmt(r['pure_done_s'])}`"
        )
        lines.append(f"- **BGE / Reflex:** `{json.dumps(f['bge_events'], ensure_ascii=False)}`")
        lines.append(
            f"- **Fish TTS:** path=`{f['fish_path']}`, http_requests={f['fish_http_count']}, "
            f"ws_warnings={f['fish_ws_warnings']}, errors={f['fish_errors']}"
        )
        if f["tool_events_journal"]:
            lines.append(f"- **Journal Tool Events:** `{json.dumps(f['tool_events_journal'], ensure_ascii=False)}`")
        if f["conv_steps_delta"]:
            lines.append("- **AGY Conversation Steps (Tools/Prompts):**")
            for step in f["conv_steps_delta"]:
                if step["tool_args"] or step["error_strings"] or step.get("output_text"):
                    lines.append(
                        f"  - idx={step['idx']} type={step['step_type']} status={step['status']}: "
                        f"args=`{json.dumps(step['tool_args'], ensure_ascii=False)}` "
                        f"err=`{step['error_strings']}`"
                    )
                    if step.get("output_text"):
                        snippet = step["output_text"].replace("\n", "\\n")[:600]
                        lines.append(f"    - output: `{snippet}`")
        if r["category"] == "online_music":
            lines.append(
                f"- **mpv State:** sock_before=`{r['mpv_sock_before']}`, sock_after=`{r['mpv_sock_after']}`, "
                f"music_status_after=`{json.dumps(r['music_status_after'], ensure_ascii=False)}`"
            )
            if r["mpv_log_delta"]:
                lines.append(f"- **mpv Log Delta:** `{r['mpv_log_delta']}`")
        if r["bench_note_content"] is not None:
            lines.append(f"- **Bench Note Created:** `{r['bench_note_content'].strip()}`")
        if r["cleanup_result"]:
            lines.append(f"- **Cleanup:** `{r['cleanup_result']}`")
        if f["cli_hooks_or_errors"]:
            lines.append(f"- **CLI Warnings/Hooks:** `{f['cli_hooks_or_errors'][:10]}`")
        lines.append(f"- **Reply:** {r['reply_text'] or '(none in /state; see Gideon TTS sentences below)'}")
        if f["gideon_sentences"]:
            lines.append(
                "- **Gideon Spoken Sentences:** "
                + " | ".join(f"[{s['rel_s']}s] {s['text']}" for s in f["gideon_sentences"])
            )
        lines.append(
            "- **State Timeline:** "
            + " -> ".join(f"{t['elapsed_s']}s:{t['state']}" for t in r["state_timeline"])
        )
        lines.append("")

    return "\n".join(lines)


def restore_environment(client: httpx.Client) -> dict[str, Any]:
    if BENCH_NOTE_PATH.exists():
        BENCH_NOTE_PATH.unlink()
    if MPV_SOCK.exists():
        subprocess.run([str(MUSIC_BIN), "stop"], capture_output=True, text=True, check=False)
    st = wait_until_ready(client, max_wait_s=10.0)
    if st.get("effort") != "low":
        client.post("/api/effort", json={"effort": "low"})
        # Wait up to 30s for effort switch AGY respawn + idle
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            st = client.get("/state").json()
            if st.get("effort") == "low" and st.get("state") == "idle" and not st.get("loading"):
                break
            time.sleep(0.25)
        time.sleep(2.0)
    st = wait_until_ready(client, max_wait_s=10.0)
    return st


def main() -> int:
    parser = argparse.ArgumentParser(description="Live GIDEON latency & breakdown benchmark.")
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="GIDEON HTTP base URL")
    parser.add_argument("--only", action="append", default=[], help="Filter cases by id/category substring")
    parser.add_argument("--timeout", type=float, default=None, help="Override per-case timeout (seconds)")
    parser.add_argument("--poll-ms", type=int, default=50, help="/state poll interval in ms")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--skip-cleanup", action="store_true", help="Skip final cleanup/effort restore")
    args = parser.parse_args()

    filters: list[str] = []
    for item in args.only:
        filters.extend(part.strip().lower() for part in item.split(",") if part.strip())

    selected = [
        c
        for c in CASES
        if not filters
        or any(flt in c.case_id.lower() or flt in c.category.lower() for flt in filters)
    ]
    if not selected:
        print(f"No benchmark cases matched --only={filters}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.url, timeout=15.0) as client:
        st0 = wait_until_ready(client, max_wait_s=10.0)
        print(
            f"[Preflight] Daemon state={st0.get('state')} effort={st0.get('effort')} "
            f"loading={st0.get('loading')} | Running {len(selected)} cases...",
            flush=True,
        )
        for idx, case in enumerate(selected, 1):
            print(f"[{idx}/{len(selected)}] Running {case.case_id} ({case.effort})...", flush=True)
            res = run_case(client, case, poll_interval_s=args.poll_ms / 1000.0, timeout_override=args.timeout)
            results.append(res)
            print(
                f"    -> ttft={fmt(res['ttft_s'])} speak={fmt(res['first_speaking_s'])} "
                f"done={fmt(res['done_s'])} (pure={fmt(res['pure_done_s'])}) "
                f"fish={res['forensics']['fish_path']} reason={res['completion_reason']}",
                flush=True,
            )

        if args.skip_cleanup:
            final_st = client.get("/state").json()
        else:
            final_st = restore_environment(client)

    report = {
        "timestamp_iso": datetime.now().isoformat(timespec="seconds"),
        "base_url": args.url,
        "poll_ms": args.poll_ms,
        "final_effort": final_st.get("effort"),
        "final_state": final_st.get("state"),
        "summary": build_summary(results),
        "results": results,
    }

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    args.md_out.write_text(render_markdown(report), encoding="utf-8")
    print(f"[Saved] JSON -> {args.json_out}")
    print(f"[Saved] MD   -> {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
