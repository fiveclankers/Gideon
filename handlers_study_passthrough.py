"""Subagent 3: Personal Study Library, Pass-Through Router, and Safety Gating Module.

Implements all handlers for:
- study_queue_view (retention queue inspection)
- study_status_view (burndown & chapter rank inspection)
- study_doctor (ledger consistency verification)
- study_traps_view (Black Book traps extraction)
- study_sprint_start (launch study workspace)
- flash_note_add (Obsidian markdown capture)
- engine_switch (OpenCode / Antigravity / Lyra)
- Pass-through routing: conversation, hard_goal, web_search, browser_use_passthrough
- Safety and confidence risk gating.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from gideon_shared import HandlerResult, run_cmd_async

logger = logging.getLogger("gideon.handlers.study")

STUDY_PYTHON = "/home/cat/jarvis/.venv/bin/python"
STUDY_SCRIPT = "/home/cat/study/scripts/study_engine.py"
DAILY_LEDGER_PATH = Path("/home/cat/notes/daily_ledger.md")
OBSIDIAN_VAULT = Path("/home/cat/Documents/Obsidian Vault")

# Safety Risk Thresholds
# Intents below these confidence thresholds are escalated to LLM fallback rather than executing locally
RISK_THRESHOLDS = {
    "safe": 0.70,        # Safe commands require >=70% confidence
    "confirm": 0.85,     # Confirm commands require >=85% and confirmation flag
    "dangerous": 0.90,   # Dangerous commands require >=90% and explicit confirmation
}


def evaluate_risk_gate(intent_id: str, confidence: float, risk_tier: str) -> Tuple[bool, bool, str]:
    """Evaluate whether an intent passes confidence gating.
    
    Returns:
        (allowed_to_run, needs_confirmation, reason)
    """
    threshold = RISK_THRESHOLDS.get(risk_tier, 0.80)
    if confidence < threshold:
        return (
            False,
            False,
            f"Confidence {confidence:.3f} below risk threshold {threshold:.2f} for tier '{risk_tier}'. Escalating to LLM."
        )

    if risk_tier in ("confirm", "dangerous"):
        return (
            True,
            True,
            f"Intent '{intent_id}' requires user confirmation before execution (tier: {risk_tier})."
        )

    return True, False, "Passed risk gating"


# Study Library Handlers
async def handle_study_queue_view(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    cmd = [STUDY_PYTHON, STUDY_SCRIPT, "context", "--json"]
    code, out, err = await run_cmd_async(cmd, timeout=10.0, cwd="/home/cat/gideon")
    if code != 0:
        return HandlerResult(ok=False, error=f"study_engine context error: {err}")

    try:
        data = json.loads(out)
        phase_0 = data.get("phase_0", {})
        due_chapters = phase_0.get("due_chapters", [])
        next_up = phase_0.get("next_upcoming", {})
        due_count = phase_0.get("due_count", 0)

        msg_lines = [f"Study Retention Queue (Due Today: {due_count}):"]
        for ch in due_chapters:
            msg_lines.append(f"- {ch.get('chapter_id')}: {ch.get('chapter_name')} ({ch.get('stage')} -> {ch.get('target_rank')})")
        if next_up:
            msg_lines.append(f"Next Upcoming: {next_up.get('chapter_id')} on {next_up.get('due_date')}")

        return HandlerResult(ok=True, message="\n".join(msg_lines), data=data)
    except Exception as exc:
        return HandlerResult(ok=True, message=out, data={"raw": out})


async def handle_study_status_view(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    cmd = [STUDY_PYTHON, STUDY_SCRIPT, "context", "--json"]
    code, out, err = await run_cmd_async(cmd, timeout=10.0, cwd="/home/cat/gideon")
    if code != 0:
        return HandlerResult(ok=False, error=f"study_engine context error: {err}")

    try:
        data = json.loads(out)
        burndown = data.get("burn_down_days_remaining", "N/A")
        date = data.get("date", "")
        return HandlerResult(
            ok=True,
            message=f"Study Status ({date}): {burndown} burndown days remaining.",
            data=data
        )
    except Exception:
        return HandlerResult(ok=True, message=out)


async def handle_study_doctor(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    cmd = [STUDY_PYTHON, STUDY_SCRIPT, "doctor"]
    code, out, err = await run_cmd_async(cmd, timeout=10.0, cwd="/home/cat/gideon")
    if code == 0:
        return HandlerResult(ok=True, message="Study Doctor: all ledgers verified consistent.", data={"output": out})
    return HandlerResult(ok=False, error=f"Study Doctor flagged inconsistency:\n{err or out}")


async def handle_study_traps_view(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    chapter = params.get("chapter", "phy_01")
    # study_engine.py traps takes chapter_id as a positional argument
    cmd = [STUDY_PYTHON, STUDY_SCRIPT, "traps", str(chapter)]
    code, out, err = await run_cmd_async(cmd, timeout=10.0, cwd="/home/cat/gideon")
    if code == 0:
        return HandlerResult(ok=True, message=f"Black Book traps for {chapter}:\n{out}", data={"chapter": chapter, "traps": out})
    return HandlerResult(ok=False, error=f"Failed extracting traps: {err}")


async def handle_study_sprint_start(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    chapter = params.get("chapter", "")
    script_path = "/home/cat/study/scripts/launch_study_workspace.sh"
    cmd = ["bash", script_path]
    if chapter:
        cmd.append(str(chapter))
    # Strip ELECTRON_RUN_AS_NODE so Obsidian GUI launches cleanly without electron crashing
    clean_env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    code, out, err = await run_cmd_async(cmd, timeout=10.0, env=clean_env, cwd="/home/cat/gideon")
    if code == 0:
        return HandlerResult(ok=True, message=f"Study session launched for {chapter or 'current chapter'}")
    return HandlerResult(ok=False, error=f"Failed launching study session: {err}")


async def handle_flash_note_add(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    note_content = params.get("note", "").strip()
    if not note_content:
        return HandlerResult(ok=False, error="Note content cannot be empty")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"- **[{timestamp}]** {note_content}\n"

    try:
        DAILY_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DAILY_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(entry)

        # Also write to Obsidian Vault Flash Notes
        if OBSIDIAN_VAULT.exists():
            vault_notes = OBSIDIAN_VAULT / "Flash Notes"
            vault_notes.mkdir(parents=True, exist_ok=True)
            today_str = datetime.now().strftime("%Y-%m-%d")
            vault_file = vault_notes / f"{today_str}.md"
            with open(vault_file, "a", encoding="utf-8") as f:
                f.write(entry)

        return HandlerResult(ok=True, message=f"Note captured: \"{note_content}\"", data={"note": note_content, "timestamp": timestamp})
    except Exception as exc:
        return HandlerResult(ok=False, error=f"Failed capturing note: {exc}")


async def handle_engine_switch(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    engine = params.get("engine", "antigravity").lower()
    return HandlerResult(ok=True, message=f"Engine switched to {engine.upper()}", data={"engine": engine})


# Pass-Through Router Handlers
async def handle_conversation(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    """Pass-through router for open chat, conceptual scientific queries, math questions."""
    return HandlerResult(
        ok=True,
        message="Routed to main conversation LLM",
        data={"route": "conversation_llm", "query": raw_text}
    )


async def handle_hard_goal(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    """Pass-through router for coding, software architecture, refactoring, and complex tasks."""
    return HandlerResult(
        ok=True,
        message="Routed to coding/reasoning LLM",
        data={"route": "coding_llm", "query": raw_text}
    )


async def handle_web_search(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    """Pass-through router for real-time live web search."""
    return HandlerResult(
        ok=True,
        message="Routed to web search handler",
        data={"route": "web_search", "query": raw_text}
    )


async def handle_browser_use_passthrough(params: Dict[str, Any], raw_text: str = "") -> HandlerResult:
    """Pass-through router for DOM navigation, web interactions, browser recipes."""
    return HandlerResult(
        ok=True,
        message="Routed to browser automation engine",
        data={"route": "browser_use", "query": raw_text}
    )
