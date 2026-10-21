#!/usr/bin/env python3
"""
Step 1: Schema Migration Script for Gideon Lyra v8.
Upgrades 18,987 rows from 13-intent schema to 15-intent master schema.
Separates flash note capture into `flash_note` and deterministic web scripts into `browser_recipe`.
"""
import json
import logging
import sys
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_15")

INPUT_FILE = Path("/home/cat/gideon/data/train_gideon_augmented.jsonl")
OUTPUT_FILE = Path("/home/cat/gideon/data/train_gideon_migrated_15.jsonl")

INTENT_LABELS_15 = [
    "system_action",
    "app_launch",
    "omarchy",
    "flash_note",
    "browser_recipe",
    "study_retention",
    "hard_goal",
    "research_task",
    "web_search",
    "url_open",
    "desktop_vision",
    "engine_switch",
    "weather",
    "skill_route",
    "conversation",
]

INTENT_QUESTION_15 = {
    "intent": {
        "type": "choice",
        "instructions": (
            "What subsystem or action should handle this voice command? Bare place mentions "
            "with no explicit weather or action word are conversation. Weather ONLY on explicit "
            "weather words. Hands-free note capture to Obsidian is flash_note. Deterministic "
            "browser actions (Instagram DMs, form autofill) are browser_recipe."
        ),
        "criteria": {
            "system_action": "control computer audio volume media playback, workspaces windows, system status, pause resume media",
            "app_launch": "open or launch an installed desktop application by name",
            "omarchy": "omarchy themes wallpapers keybindings hyprland shortcuts screenshots",
            "flash_note": "hands-free capture of quick thoughts formulas quotes exam notes or memos straight to Obsidian markdown",
            "browser_recipe": "deterministic browser actions like checking instagram direct messages, autofilling checkout forms, reading active tab",
            "study_retention": "study ledger revision queue review due cards spaced repetition progress",
            "hard_goal": "write create build implement code scripts programs functions, architect refactor, background reasoning tasks",
            "research_task": "deep research investigate papers, compare architectures, multi-query literature review",
            "web_search": "explicit search the web look up latest news scores prices events",
            "url_open": "open a website or web service by name or domain",
            "desktop_vision": "click press or tap a visible on-screen target like buttons or links",
            "engine_switch": "switch change or use a different engine model brain llm",
            "weather": "explicit weather forecast temperature rain sun snow cold heat humidity",
            "skill_route": "explicitly invoke or run a specialized agent skill like diagnose-crash or install-anti-slop",
            "conversation": "pure chat, science math study questions, geometry volume of shapes, definitions, opinions, who are you, bare place mentions",
        },
    }
}

NOTE_MARKERS = [
    "note down", "note this down", "take a note", "save to notes", "record this",
    "record memo", "jot down", "save note", "flash note", "write down this",
    "remember that", "add to notes", "add note"
]

BROWSER_RECIPE_MARKERS = [
    "instagram", "direct message", "direct messages", "dm", "dms",
    "autofill", "fill form", "fill in the form", "checkout form",
    "unread inbox", "active tab", "form fields"
]


def classify_migration(state: str, old_intent: str) -> str:
    s = state.lower()

    if old_intent == "study_retention":
        if any(m in s for m in NOTE_MARKERS):
            return "flash_note"
        return "study_retention"

    if old_intent == "browser_task":
        if any(m in s for m in BROWSER_RECIPE_MARKERS):
            return "browser_recipe"
        return "browser_recipe"  # JARVIS browser_task was almost entirely instagram/forms/gemini

    return old_intent


def main():
    if not INPUT_FILE.exists():
        logger.error(f"Input file not found: {INPUT_FILE}")
        sys.exit(1)

    logger.info(f"Reading {INPUT_FILE}...")
    old_counts = Counter()
    new_counts = Counter()

    total_rows = 0
    with open(INPUT_FILE, "r") as f_in, open(OUTPUT_FILE, "w") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue

            row = json.loads(line)
            state = row["state"]

            # Find old intent
            old_probs = row.get("gold", {}).get("intent", {}).get("probabilities", {})
            old_intent = max(old_probs.items(), key=lambda x: x[1])[0] if old_probs else "conversation"
            old_counts[old_intent] += 1

            new_intent = classify_migration(state, old_intent)
            new_counts[new_intent] += 1

            # Format 15-way probabilities
            new_probs = {lbl: (1.0 if lbl == new_intent else 0.0) for lbl in INTENT_LABELS_15}

            new_row = {
                "state": state,
                "questions": INTENT_QUESTION_15,
                "gold": {
                    "intent": {
                        "probabilities": new_probs
                    }
                }
            }
            f_out.write(json.dumps(new_row) + "\n")
            total_rows += 1

    logger.info("=" * 65)
    logger.info(f"MIGRATION COMPLETE: Processed {total_rows} rows into {OUTPUT_FILE.name}")
    logger.info("=" * 65)
    logger.info("BEFORE MIGRATION (13 intents):")
    for k, v in sorted(old_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {k:<20}: {v:5d}")

    logger.info("-" * 65)
    logger.info("AFTER MIGRATION (15 intents):")
    for k, v in sorted(new_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {k:<20}: {v:5d}")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
