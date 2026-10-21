#!/usr/bin/env python3
"""
Step 2: Targeted Synthesis Script for Gideon Lyra v8.
Synthesizes:
- ~1,200 rich rows for `flash_note` (physics, chemistry, coding, personal memos)
- ~350 rich rows for `browser_recipe` (Instagram DMs, form autofill, tab summaries)
- ~1,200 rich rows for `skill_route` (diagnose-crash, anti-slop, agent-browser, jarvis manual)
- ~350 rich rows for `study_retention` (queue lookups, due cards, spaced repetition)

Merges with train_gideon_migrated_15.jsonl to produce train_gideon_v8_master.jsonl (~22,200 rows total)
with perfectly balanced class distribution (~1,100 to ~2,000 rows per intent).
"""
import itertools
import json
import logging
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("synthesize_15")

MIGRATED_FILE = Path("/home/cat/gideon/data/train_gideon_migrated_15.jsonl")
MASTER_FILE = Path("/home/cat/gideon/data/train_gideon_v8_master.jsonl")

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

PREFIXES = [
    "", "", "hey gideon ", "gideon ", "can you ", "please ", "could you ",
    "um ", "uh ", "quick ", "alright ", "hey buddy ", "do me a favor and "
]

SUFFIXES = [
    "", "", " right now", " please", " for later", " in Obsidian", " for my exam",
    " in my study notes", " immediately", " for me", " straight away", " asap"
]

# -------------------------------------------------------------
# 1. Flash Note (Hands-free capture to Obsidian markdown)
# -------------------------------------------------------------
NOTE_VERBS = [
    "note down: ", "note this down: ", "record this: ", "save to my notes: ",
    "add to my ledger: ", "jot down: ", "flash note: ", "take a note: ",
    "write down this: ", "save note: ", "quick note: ", "remember that: "
]

PHYSICS_NOTES = [
    "the magnetic field inside a long solenoid is B equals mu zero n I",
    "de Broglie wavelength is lambda equals h divided by momentum p",
    "torque on a magnetic dipole in an external field is tau equals M cross B",
    "Carnot cycle thermal efficiency is 1 minus T cold over T hot",
    "Brewster angle condition is tangent theta equals refractive index n2 over n1",
    "cyclotron frequency omega equals q B divided by m",
    "magnetic flux through any closed Gaussian surface is always zero",
    "displacement current is epsilon zero times time derivative of electric flux",
    "stopping potential depends on frequency of incident light not its intensity",
    "radioactive decay law is N of t equals N zero e to the minus lambda t",
    "binding energy per nucleon peaks around iron 56 at 8.75 MeV",
    "capacitance of parallel plate capacitor with dielectric is kappa epsilon zero A over d",
    "drift velocity of electrons is v d equals e E tau over m",
    "Hall voltage is inversely proportional to charge carrier density n",
    "Poynting vector S equals 1 over mu zero E cross B",
]

CHEMISTRY_NOTES = [
    "Markovnikov addition follows carbocation stability order tertiary over secondary",
    "PCC oxidizes primary alcohols to aldehydes without carboxylic over oxidation",
    "anti-aromatic planar rings contain 4n pi electrons and show high instability",
    "nucleophilicity increases down the halogen group in polar protic solvents",
    "SN2 reactions proceed with complete Walden inversion of stereocenters",
    "Birch reduction of benzene with sodium in liquid ammonia yields 1,4-cyclohexadiene",
    "Friedel Crafts acylation avoids carbocation rearrangement unlike alkylation",
    "Huckel rule requires 4n plus 2 pi conjugated electrons for aromaticity",
    "aldol condensation of acetone yields mesityl oxide upon dehydration",
    "Grignard reagents react violently with active hydrogens producing alkanes",
    "Gabriel phthalimide synthesis selectively prepares pure primary aliphatic amines",
    "ozonolysis of reductive alkenes using dimethyl sulfide yields aldehydes and ketones",
]

CODING_NOTES = [
    "ModernBERT unpadding mechanism eliminates up to 60 percent of padded token compute",
    "PipeWire pw-play reads raw PCM buffers over stdin using dash flag",
    "Hyprland dynamic workspaces are spawned on demand using hyprctl dispatch workspace",
    "PyTorch AMP autocast with float16 requires GradScaler to prevent underflow",
    "Linux systemd user services run under app.slice in graphical-session target",
    "SQLite FTS5 uses BM25 ranking algorithm by default for text matching",
    "CUDA memory fragmentation can be mitigated using expandable segments in alloc conf",
    "Faster-Whisper beam size 1 greedy search achieves 4x speedup with minimal WER drop",
    "Fish Audio s2.1 cloud TTS streams 24kHz mono audio with PipeWire low latency",
    "Wayland layer shell overlay surfaces require exclusive keyboard grab in Quickshell",
]

MEMO_NOTES = [
    "review IAT 2025 physics paper analysis by Friday evening",
    "reorder thermal paste for the GTX 1650 cooling heatsink",
    "verify Kaggle notebook output directory matches model_dir export path",
    "check the calibration temperature curve for choice vs score heads",
    "backup local Obsidian second brain vault to git remote",
    "schedule weekly maintenance reboot for Omarchy desktop kernel",
]

# -------------------------------------------------------------
# 2. Browser Recipe (Deterministic CDP on :9222)
# -------------------------------------------------------------
BROWSER_RECIPE_PATTERNS = [
    "{prefix}check my instagram direct messages{suffix}",
    "{prefix}summarize my unread instagram inbox{suffix}",
    "{prefix}check if there are any new messages on instagram{suffix}",
    "{prefix}open instagram DMs and list recent senders{suffix}",
    "{prefix}read recent instagram chats{suffix}",
    "{prefix}autofill my shipping address on this checkout form{suffix}",
    "{prefix}fill in my contact details in the active form{suffix}",
    "{prefix}autofill payment credentials from the vault{suffix}",
    "{prefix}fill in the login form fields on this page{suffix}",
    "{prefix}populate form inputs with my profile data{suffix}",
    "{prefix}summarize the active browser tab{suffix}",
    "{prefix}read the main article text on this open web page{suffix}",
    "{prefix}extract text from current browser page{suffix}",
    "{prefix}autofill the application form on screen{suffix}",
]

# -------------------------------------------------------------
# 3. Skill Route (Explicit specialized skill invocations)
# -------------------------------------------------------------
SKILL_PATTERNS = [
    "{prefix}diagnose why this application crashed{suffix}",
    "{prefix}analyze the systemd core dump for the segfault{suffix}",
    "{prefix}why did my program segfault just now{suffix}",
    "{prefix}inspect the crash log using diagnose-crash skill{suffix}",
    "{prefix}check coredumpctl for recent fatal errors{suffix}",
    "{prefix}debug the crash stack trace for {app}{suffix}",
    "{prefix}run the anti-slop linter across this codebase{suffix}",
    "{prefix}check for AI-generated code slop using oxlint{suffix}",
    "{prefix}install the latest anti-slop plugin rules{suffix}",
    "{prefix}update anti-slop oxlint configuration{suffix}",
    "{prefix}launch dedicated agent chrome browser on port 9222{suffix}",
    "{prefix}start agent-browser daemon in background{suffix}",
    "{prefix}consult the jarvis engineering manual{suffix}",
    "{prefix}lookup the jarvis debugging history for audio latency{suffix}",
    "{prefix}check jarvis runbook for socket reconnect loop fix{suffix}",
    "{prefix}activate overnight-working protocol for this task{suffix}",
]

# -------------------------------------------------------------
# 4. Study Retention (Review due cards, retention queue)
# -------------------------------------------------------------
RETENTION_PATTERNS = [
    "{prefix}how many study cards are due today{suffix}",
    "{prefix}show my revision queue for physics{suffix}",
    "{prefix}show due cards in study ledger for organic chemistry{suffix}",
    "{prefix}what formulas are scheduled for review this evening{suffix}",
    "{prefix}check my spaced repetition ledger for {subject}{suffix}",
    "{prefix}review my study cards due today{suffix}",
    "{prefix}what cards are due in my study ledger{suffix}",
    "{prefix}test my retention on {subject} formulas{suffix}",
    "{prefix}display the study queue for exam prep{suffix}",
    "{prefix}what is my retention rate for {subject} this week{suffix}",
]

APPS = ["kitty", "pipewire", "quickshell", "gideon", "python", "chrome", "firefox"]
SUBJECTS = ["physics", "organic chemistry", "linear algebra", "calculus", "machine learning"]


def format_row(query: str, intent: str) -> dict:
    probs = {lbl: (1.0 if lbl == intent else 0.0) for lbl in INTENT_LABELS_15}
    return {
        "state": query,
        "questions": INTENT_QUESTION_15,
        "gold": {
            "intent": {
                "probabilities": probs
            }
        }
    }


def main():
    if not MIGRATED_FILE.exists():
        logger.error(f"Migrated base file not found: {MIGRATED_FILE}")
        sys.exit(1)

    logger.info("Reading migrated dataset to seed existing set...")
    existing = set()
    initial_rows = []
    class_counts = Counter()

    with open(MIGRATED_FILE, "r") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            existing.add(row["state"].strip().lower())
            initial_rows.append(row)
            # count intent
            p = row["gold"]["intent"]["probabilities"]
            intent = max(p.items(), key=lambda x: x[1])[0]
            class_counts[intent] += 1

    logger.info(f"Loaded {len(initial_rows)} base rows across 15 classes.")
    new_rows = []

    # 1. Synthesize ~1,200 flash_note rows
    logger.info("Synthesizing ~1,200 diverse flash_note rows...")
    all_notes = PHYSICS_NOTES + CHEMISTRY_NOTES + CODING_NOTES + MEMO_NOTES
    flash_count = 0
    while flash_count < 1250:
        pref = random.choice(PREFIXES)
        verb = random.choice(NOTE_VERBS)
        note = random.choice(all_notes)
        suff = random.choice(SUFFIXES)
        q = f"{pref}{verb}{note}{suff}".strip()
        q = " ".join(q.split())
        norm = q.lower()
        if norm not in existing:
            existing.add(norm)
            new_rows.append(format_row(q, "flash_note"))
            class_counts["flash_note"] += 1
            flash_count += 1

    # 2. Synthesize ~350 browser_recipe rows (bringing total to ~1,250)
    logger.info("Synthesizing ~350 browser_recipe rows...")
    br_count = 0
    while br_count < 350:
        pat = random.choice(BROWSER_RECIPE_PATTERNS)
        q = pat.format(prefix=random.choice(PREFIXES), suffix=random.choice(SUFFIXES)).strip()
        q = " ".join(q.split())
        norm = q.lower()
        if norm not in existing:
            existing.add(norm)
            new_rows.append(format_row(q, "browser_recipe"))
            class_counts["browser_recipe"] += 1
            br_count += 1

    # 3. Synthesize ~1,200 skill_route rows
    logger.info("Synthesizing ~1,200 skill_route rows...")
    sr_count = 0
    while sr_count < 1200:
        pat = random.choice(SKILL_PATTERNS)
        q = pat.format(
            prefix=random.choice(PREFIXES),
            suffix=random.choice(SUFFIXES),
            app=random.choice(APPS)
        ).strip()
        q = " ".join(q.split())
        norm = q.lower()
        if norm not in existing:
            existing.add(norm)
            new_rows.append(format_row(q, "skill_route"))
            class_counts["skill_route"] += 1
            sr_count += 1

    # 4. Synthesize ~350 study_retention rows (bringing total to ~1,250)
    logger.info("Synthesizing ~350 study_retention rows...")
    ret_count = 0
    while ret_count < 350:
        pat = random.choice(RETENTION_PATTERNS)
        q = pat.format(
            prefix=random.choice(PREFIXES),
            suffix=random.choice(SUFFIXES),
            subject=random.choice(SUBJECTS)
        ).strip()
        q = " ".join(q.split())
        norm = q.lower()
        if norm not in existing:
            existing.add(norm)
            new_rows.append(format_row(q, "study_retention"))
            class_counts["study_retention"] += 1
            ret_count += 1

    # Write out unified master corpus
    logger.info(f"Writing total of {len(initial_rows) + len(new_rows)} rows to {MASTER_FILE}...")
    with open(MASTER_FILE, "w") as f_out:
        for r in initial_rows:
            f_out.write(json.dumps(r) + "\n")
        for r in new_rows:
            f_out.write(json.dumps(r) + "\n")

    logger.info("=" * 65)
    logger.info(f"MASTER DATASET COMPLETE: {len(initial_rows) + len(new_rows)} TOTAL ROWS")
    logger.info("=" * 65)
    logger.info("FINAL CLASS DISTRIBUTION (15 INTENTS):")
    for k, v in sorted(class_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {k:<20}: {v:5d}")
    logger.info("=" * 65)
    logger.info(f"Saved to: {MASTER_FILE}")


if __name__ == "__main__":
    main()
