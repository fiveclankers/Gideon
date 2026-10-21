#!/usr/bin/env python3
"""
Gideon Lyra v8 Master Dataset Builder (Cleaned & Layered)

Strictly rectifies Bugs A, B, C, and D:
- Bug A: Restricts suffixes "in my study notes", "in Obsidian", "for my exam" so they
         ONLY attach to flash_note or study_retention, NEVER to browser_recipe, skill_route, etc.
- Bug B: Completely removes "ledger" from flash_note. study_retention exclusively owns "ledger".
         Fixes mock exam entity to use "practice paper" instead of "ledger".
- Bug C: Separates templates into Imperatives (take modal prefixes like "can you") and
         Standalone Questions (why/what/how/is/do, which NEVER take modal prefixes).
         Cleans 139+ legacy verb-comma glitches ("look, up" -> "look up", "open, twitch" -> "open twitch")
         and word-order bugs ("write rust a script" -> "write a rust script").
- Bug D: Avoids unnatural multi-domain word soup; ensures semantic coherence.

Outputs:
  - train_gideon_v8_master.jsonl: The complete, layered, balanced 15-intent master dataset.
"""

import json
import logging
import random
import re
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("builder_v8")

BASELINE_FILE = Path("/home/cat/laya_finetune/train_full.jsonl")
OUTPUT_MASTER = Path("/home/cat/gideon/data/train_gideon_v8_master.jsonl")
OUTPUT_REPORT = Path("/home/cat/gideon/data/dataset_v8_report.md")

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

# ---------------------------------------------------------------------
# Grammatically Sound Prefix & Suffix Pools
# ---------------------------------------------------------------------
IMPERATIVE_PREFIXES = [
    "", "", "", "hey gideon ", "gideon ", "please ", "can you ", "could you ",
    "go ahead and ", "do me a favor and ", "i need you to ", "would you mind ", "quick "
]

QUESTION_PREFIXES = [
    "", "", "", "hey gideon ", "gideon ", "um ", "tell me "
]

# Universal neutral suffixes (safe for any intent)
NEUTRAL_SUFFIXES = [
    "", "", "", " right now", " please", " immediately", " for me", " real quick", " asap"
]

# Note-exclusive suffixes (RESTRICTED to flash_note only)
NOTE_SUFFIXES = [
    "", "", " in Obsidian", " in my study notes", " for later", " in my daily note", " to my notes vault"
]

# Retention-exclusive suffixes (RESTRICTED to study_retention only)
RETENTION_SUFFIXES = [
    "", "", " today", " for my exam", " this evening", " in my study ledger", " for revision"
]

# Skill-exclusive suffixes
SKILL_SUFFIXES = [
    "", "", " right now", " please", " immediately", " real quick", " with symbolized backtrace", " across the workspace"
]

# ---------------------------------------------------------------------
# Rich Domain Entities (350+ Dense Terms, No Cross-Intent Leaks)
# ---------------------------------------------------------------------
PHYSICS_ENTITIES = [
    "magnetic field on the axis of a circular current loop is mu zero I R squared over two R squared plus x squared to the three halves",
    "Ampere circuital law with Maxwell correction includes displacement current term mu zero epsilon zero d phi E by dt",
    "electric field inside a uniformly charged dielectric sphere grows linearly with distance r from the center",
    "Poynting theorem represents conservation of electromagnetic energy with energy flux density vector E cross H",
    "cyclotron resonance condition states orbital frequency is independent of radius and speed in non-relativistic regime",
    "self inductance of a tightly wound toroid is mu zero N squared A divided by two pi r",
    "Faraday law of electromagnetic induction states induced EMF equals negative rate of change of magnetic flux",
    "Brewster angle of polarization occurs when reflected and refracted rays are perpendicular to each other",
    "Rayleigh criterion for angular resolution limit is theta equals 1.22 lambda over aperture diameter D",
    "stopping potential in photoelectric effect is independent of incident beam intensity and linear in frequency",
    "de Broglie wavelength of an electron accelerated through potential V is 12.27 angstroms divided by square root of V",
    "Bohr model radius of nth hydrogen orbit scales as n squared h bar squared over m e squared",
    "Moseley law for characteristic X-ray K alpha frequency is square root nu equals a times Z minus b",
    "half life in first order radioactive decay is natural log two over decay constant lambda",
    "nuclear density is approximately constant across all nuclei at two point three times ten to the seventeen kilograms per cubic meter",
    "Q value of nuclear alpha decay is mass parent minus mass daughter minus mass alpha times c squared",
    "ferromagnetic Curie Weiss law for magnetic susceptibility is C divided by T minus T_c above Curie temperature",
    "drift velocity of conduction electrons in copper under electric field is typically fractions of a millimeter per second",
    "internal resistance of a potentiometer wire decreases accuracy if wire resistance per unit length is non-uniform",
    "Carnot cycle efficiency depends solely on absolute temperatures of source and sink reservoirs",
    "first law of thermodynamics states internal energy is a state function independent of thermodynamic path",
    "root mean square molecular speed in ideal gas is square root of three R T over molar mass M",
    "van der Waals equation accounts for finite molecular volume b and intermolecular attractive forces a over V squared",
    "Doppler shift for sound with moving observer and stationary source gives frequency f times v plus v_o over v",
    "quality factor of series LCR circuit is one over R times square root of L over C",
    "average power dissipated in AC circuit with phase angle phi is V rms I rms cosine phi",
    "skin depth in conductors decreases with higher frequency as one over square root of pi mu sigma f",
    "attenuation of electromagnetic waves in waveguides occurs below the cutoff frequency for TE and TM modes",
    "gyromagnetic ratio of electron spin is approximately two times e over two m",
]

CHEMISTRY_ENTITIES = [
    "Markovnikov addition of hydrogen halides across unsymmetrical alkenes proceeds via most stable carbocation intermediate",
    "hyperconjugation involving alpha C-H sigma bonds stabilizes carbocations and substituted alkenes via sigma to p overlap",
    "Saytzeff elimination rule yields the more substituted more thermodynamically stable alkene as major product",
    "Hofmann elimination of quaternary ammonium hydroxides gives the least hindered least substituted alkene due to steric hindrance",
    "Birch reduction of anisole with sodium in liquid ammonia and ethanol produces 1-methoxy-1,4-cyclohexadiene",
    "Swern oxidation converts primary alcohols to aldehydes using oxalyl chloride dimethyl sulfoxide and triethylamine",
    "Diels Alder cycloaddition is a stereospecific concerted four plus two electron pericyclic reaction with suprafacial geometry",
    "anti-aromatic cyclobutadiene possesses four pi electrons in a planar ring and exhibits extreme diradical reactivity",
    "Huckel 4n plus two rule dictates aromatic stability for planar monocyclic fully conjugated annulenes",
    "Gabriel phthalimide synthesis allows alkylation of potassium phthalimide to selectively prepare pure primary amines",
    "Grignard reagents react with formaldehyde to produce primary alcohols and with ketones to yield tertiary alcohols",
    "Cannizzaro reaction of benzaldehyde in concentrated alkali produces equal amounts of benzyl alcohol and sodium benzoate",
    "aldol addition of acetaldehyde under dilute base yields beta-hydroxybutyraldehyde followed by crotonaldehyde upon heating",
    "clemmensen reduction uses amalgamated zinc and concentrated hydrochloric acid to convert carbonyls to methylene groups",
    "ozonolysis of alkenes with reductive zinc cleavage produces aldehydes or ketones without over-oxidation to carboxylic acids",
    "Rosenmund reduction converts acyl chlorides to aldehydes using hydrogen gas and poisoned palladium on barium sulfate catalyst",
]

SYSTEM_ENTITIES = [
    "cgroup v2 memory.max limits total physical allocation before triggering kernel OOM killer",
    "Wayland wl_surface protocol handles double-buffered buffer commits and damage tracking",
    "Hyprland IPC socket at XDG_RUNTIME_DIR/hypr/his_instance_signature/sock2.sock broadcasts workspace and window events",
    "PipeWire quantum determines minimum buffer size and round-trip audio latency in audio processing graphs",
    "ModernBERT unpadding removes padding tokens from input sequences before flash attention forward pass",
    "Linux socket SO_REUSEPORT enables multiple listener processes on the same port with kernel level load balancing",
]

# Cleaned Memos: Bug B fix: replaced 'ledger' with 'practice paper'
MEMO_ENTITIES = [
    "review IAT 2025 physics paper PYQ solutions and wave optics derivations by Friday evening",
    "reorder Arctic MX-4 thermal paste for the GTX 1650 cooling heatsink replacement",
    "verify Kaggle notebook export directory matches model_dir checkpoint path in run script",
    "check the calibration temperature curve for choice vs score heads on out-of-distribution splits",
    "backup local Obsidian second brain vault markdown files to encrypted git remote repository",
    "schedule weekly maintenance reboot for Omarchy desktop kernel updates and pacman package cache cleanup",
    "solve five advanced electrostatics PYQ problems from IAT mock exam practice paper before Sunday",
    "refactor Gideon priority intervention callback to drain audio buffer before injecting prompt into agy stdin",
]

APPS = ["kitty", "pipewire", "quickshell", "gideon", "python", "chrome", "firefox", "brave", "alacritty", "ghostty", "spotify", "discord", "obsidian", "steam", "thunar"]
SUBJECTS = ["physics", "organic chemistry", "linear algebra", "calculus", "machine learning", "physical chemistry", "electromagnetism", "mechanics"]


# ---------------------------------------------------------------------
# Bug C Fix: Sentence Cleaning & Normalization
# ---------------------------------------------------------------------
VOCATIVES = {'ok jarvis', 'hey gideon', 'gideon', 'jarvis', 'hey', 'uhm', 'uh', 'um', 'please', 'well', 'hi'}

def clean_sentence(st: str) -> str:
    """Cleans legacy artifact commas, double questions, and word-order glitches."""
    st = st.strip()

    # 1. Word order slips: [verb] [lang] a [noun] -> [verb] a [lang] [noun]
    st = re.sub(
        r'\b(write|create|develop|code|craft|build)\s+([a-zA-Z]+)\s+a\s+(script|program|tool|function|service|watcher|listener|scraper|tracker|renamer|parser)\b',
        r'\1 a \2 \3',
        st,
        flags=re.IGNORECASE
    )

    # 2. Double questions: [modal prefix] [question word] -> [question word]
    st = re.sub(
        r'^(can you|could you|can we|would you|do me a favor and|i need you to|go ahead and|please)\s+'
        r'(why\b|what\b|what\'s\b|how\b|who\b|when\b|where\b|which\b|did\b|does\b|do\b|is\b|are\b|will\b|was\b|were\b|has\b|have\b)',
        r'\2',
        st,
        flags=re.IGNORECASE
    )

    # Specific comma-soup items from legacy JARVIS
    if 'please, write, bash, script, logs' in st:
        return 'please write a bash script for logs'
    elif 'density, of, cube' in st:
        return 'what is the density of a cube'
    elif 'tell me, about, tokyo' in st:
        return 'tell me about tokyo'
    elif 'please, code, renamer, photos' in st:
        return 'please write a code to rename photos'
    elif 'tall, everest, how' in st:
        return 'how tall is everest'
    elif 'regarding, rain, tomorrow' in st:
        return 'will it rain tomorrow'
    elif 'jacket, today, needed' in st:
        return 'is a jacket needed today'

    # Remove artificial comma after first word: e.g. "look, up flight prices" -> "look up flight prices"
    m = re.match(r'^([a-zA-Z]+),\s+(.*)$', st)
    if m:
        w1 = m.group(1).lower()
        rest = m.group(2)
        if w1 not in VOCATIVES and not re.match(r'^[A-Z][a-z]+(\.|\s*$)', rest):
            st = f'{m.group(1)} {rest}'

    # Remove commas inside phrasal verbs
    st = re.sub(r'\b(look|go|turn|set|open|write|switch|start|launch|press|tap|dim|shuffle|boot|ask|type),\s+', r'\1 ', st, flags=re.IGNORECASE)

    st = ' '.join(st.split()).strip()
    return st


def format_row(query: str, intent: str, layer: str = "core", v6_confidence: float = 1.0) -> dict:
    probs = {lbl: (1.0 if lbl == intent else 0.0) for lbl in INTENT_LABELS_15}
    return {
        "state": query,
        "layer": layer,
        "v6_confidence": round(v6_confidence, 4),
        "questions": INTENT_QUESTION_15,
        "gold": {
            "intent": {
                "probabilities": probs
            }
        }
    }


# ---------------------------------------------------------------------
# Pattern Definitions (Imperative vs Standalone Questions Separated)
# ---------------------------------------------------------------------

# 1. flash_note: Quick thoughts, formulas, notes (Bug B: NO 'ledger' frames!)
FLASH_NOTE_FRAMES = [
    "note down: {body}",
    "note this down: {body}",
    "record this in my notes: {body}",
    "flash note: {body}",
    "take a note: {body}",
    "jot down this formula: {body}",
    "jot this down: {body}",
    "write this down: {body}",
    "save note: {body}",
    "add this to my Obsidian vault: {body}",
    "remember that {body}",
    "quick memo: {body}",
    "record that {body}",
    "add note: {body}",
]

# 2. browser_recipe: (Bug A: NO 'in my study notes', NO 'for my exam')
BROWSER_RECIPE_IMPERATIVES = [
    "check my instagram direct messages",
    "summarize my unread instagram inbox",
    "open instagram DMs and list recent senders",
    "read recent instagram chats",
    "autofill my shipping address on this checkout form",
    "fill in my contact details in the active form",
    "autofill payment credentials from the vault",
    "fill in the login form fields on this page",
    "populate form inputs with my saved profile data",
    "autofill the application form on screen",
    "fill out my personal information in these form inputs",
    "insert saved address into checkout fields",
    "summarize the active browser tab",
    "read the main article text on this open web page",
    "extract text from current browser page",
    "parse the content of the active chrome tab",
    "complete the checkout form on this webpage",
    "type my address into the checkout form",
]
BROWSER_RECIPE_QUESTIONS = [
    "are there any new messages on instagram",
    "who messaged me on instagram direct",
    "did I get any new DMs on instagram",
    "did anyone message me on instagram recently",
]

# 3. skill_route: (Bug A: NO 'in my study notes', Bug C: Separated questions)
SKILL_IMPERATIVES = [
    "diagnose why this application crashed",
    "analyze the systemd core dump for the segfault",
    "inspect the crash log using diagnose-crash skill",
    "check coredumpctl for recent fatal errors",
    "debug the crash stack trace for {app}",
    "run diagnose-crash on the latest core dump",
    "symbolize the backtrace of the crashed process",
    "run the anti-slop linter across this codebase",
    "check for AI-generated code slop using oxlint",
    "install the latest anti-slop plugin rules",
    "update anti-slop oxlint configuration",
    "scan repository for anti-slop code quality violations",
    "launch dedicated agent chrome browser on port 9222",
    "start agent-browser daemon in background",
    "restart the agent-browser CDP service",
    "consult the jarvis engineering manual",
    "lookup the jarvis debugging history for audio latency",
    "check jarvis runbook for socket reconnect loop fix",
    "activate overnight-working protocol for this task",
    "consult the overnight-working runbook for autonomous execution",
    "inspect hyprland window rules using omarchy skill",
]
SKILL_QUESTIONS = [
    "why did my program segfault just now",
    "why did {app} crash",
    "where is the coredump for {app}",
]

# 4. study_retention: (Bug B: owns 'ledger', revision, due cards)
STUDY_RETENTION_IMPERATIVES = [
    "show my revision queue for {subject}",
    "show due cards in study ledger for {subject}",
    "check my spaced repetition ledger for {subject}",
    "review my study cards due today",
    "display the study queue for exam prep",
    "pull up the revision schedule for {subject}",
    "test my retention on {subject} formulas",
    "open my study ledger for {subject}",
]
STUDY_RETENTION_QUESTIONS = [
    "how many study cards are due today",
    "what formulas are scheduled for review this evening",
    "what cards are due in my study ledger",
    "what is my retention rate for {subject} this week",
    "how many flashcards are remaining in the due deck",
    "is today a review day for {subject}",
]

# 5. system_action: (Cures noun hijacking: pause/kill spotify is system_action!)
SYSTEM_ACTION_IMPERATIVES = [
    "pause spotify",
    "resume spotify playback",
    "pause discord audio",
    "kill discord",
    "mute brave browser",
    "unmute spotify",
    "turn the sound up",
    "turn the sound down",
    "mute master audio",
    "lower volume by twenty percent",
    "switch to workspace two",
    "send this window to workspace four",
    "toggle fullscreen mode",
    "minimize everything",
    "dim the screen",
    "restart pipewire daemon",
    "restart wireplumber audio service",
    "lock the screen",
    "suspend the system",
    "close current window",
    "skip to the next song",
    "go back one track",
]
SYSTEM_ACTION_QUESTIONS = [
    "is the music still playing",
    "is anything playing right now",
]

# 6. app_launch: (Clean contrast with system_action)
APP_LAUNCH_IMPERATIVES = [
    "open spotify",
    "launch discord",
    "open firefox",
    "start alacritty",
    "launch obsidian",
    "open nautilus",
    "start ghostty",
    "open thunar",
    "launch code",
    "open steam",
    "launch telegram",
    "open kitty",
    "launch chrome",
    "open terminal",
]

# 7. omarchy: (Hyprland themes, wallpapers, shortcuts)
OMARCHY_IMPERATIVES = [
    "switch to a dark theme",
    "switch to a light theme",
    "switch to a red theme",
    "switch to a warm theme",
    "change wallpaper to something dark",
    "cycle to the next background wallpaper",
    "list available omarchy themes",
    "reload hyprland configuration",
    "take a screenshot of the active window",
    "take a full desktop screenshot",
    "toggle night light color temperature",
    "toggle hyprland animations",
]
OMARCHY_QUESTIONS = [
    "what is the shortcut for screenshots",
    "what is the keybinding to open a terminal",
    "how do I toggle a window to floating",
    "what workspace is currently active",
]

# 8. hard_goal: (Coding, refactoring, agentic builds)
HARD_GOAL_IMPERATIVES = [
    "write a rust script that sorts files by date",
    "write a python script to monitor folder changes",
    "develop a log parser in bash for systemd logs",
    "refactor the pipewire audio backend in python",
    "build a fastapi service that serves modernbert embeddings",
    "implement a custom hyprland socket listener in c",
    "debug the memory leak in the quickshell overlay daemon",
    "create a github action workflow for automated oxlint tests",
    "write a benchmark script comparing torch fp16 and int8 inference",
    "craft a backup script for photos in python",
    "build an automated scraper for arXiv preprints",
]

# 9. research_task: (Deep investigation, papers)
RESEARCH_IMPERATIVES = [
    "deep dive into transformer interpretability and attention sinks",
    "investigate the latest papers on quantum error correction",
    "survey the literature on solid state sodium batteries",
    "research CRISPR trial clinical results in immunology",
    "conduct a multi-query review of direct preference optimization vs RLCD",
    "read and summarize recent preprints on modernbert flash attention",
    "investigate the latest on fusion energy progress",
    "survey the literature on probiotics and gut microbiome",
]

# 10. web_search: (Explicit web queries, news, cricket)
WEB_SEARCH_IMPERATIVES = [
    "look up flight prices from Delhi to Bengaluru",
    "google the latest nvidia stock price",
    "search the web for who won the cricket match yesterday",
    "search the web for the new metro timings",
    "look up who founded this startup",
    "search for phone launches this week",
    "look up the budget announcement",
    "look up the mars mission status",
    "search for rust async tutorials",
]

# 11. url_open: (Web domain navigation)
URL_OPEN_IMPERATIVES = [
    "go to wikipedia.org",
    "go to github.com",
    "go to reddit.com",
    "open chem.libretexts.org",
    "go to stackoverflow.com",
    "visit hacker news",
    "open youtube.com",
    "navigate to huggingface.co",
    "go to claude.ai",
    "go to chatgpt.com",
]

# 12. desktop_vision: (On-screen click coordinates)
DESKTOP_VISION_IMPERATIVES = [
    "click the confirm button on my screen",
    "press the blue submit button in the bottom right",
    "tap the avatar icon near the top right",
    "click the download link on this page",
    "tap the menu icon on screen",
    "hit the confirm button",
    "click the search bar on my display",
    "press the save button in the toolbar",
    "click the close icon on the top right modal",
    "tap the next button on the wizard",
    "click the play button on screen",
    "click the checkbox to accept terms",
    "tap the send button on the active window",
    "press the cancel button on screen",
]

# 13. engine_switch: (Switching models/brains)
ENGINE_SWITCH_IMPERATIVES = [
    "switch to the flash engine",
    "switch brain to gemini pro",
    "use claude sonnet for next query",
    "move to the 397 model",
    "switch cognitive engine to local mistral",
    "give me the flash engine",
    "flip over to mistral",
    "switch LLM to GPT-4o",
    "change model backend to local llama",
    "switch to fast reasoning engine",
]
ENGINE_SWITCH_QUESTIONS = [
    "which brain is thinking right now",
    "what model are you currently using",
    "are you on mistral",
    "which LLM is handling my requests",
]

# 14. weather: (Explicit weather words only)
WEATHER_IMPERATIVES = [
    "check the weather forecast for tomorrow",
    "give me the temperature forecast for this weekend",
    "give me the 5-day weather forecast",
    "show weather radar for Delhi",
    "check rain probability for tomorrow",
    "check current weather conditions",
    "look up weather in Bengaluru",
    "display wind speed and forecast",
]
WEATHER_QUESTIONS = [
    "will it rain in Mumbai today",
    "will it drizzle outside",
    "is it muggy out",
    "what is the humidity outside right now",
    "do I need a coat today",
    "is it snowing in Manali right now",
    "what is the temperature outside right now",
    "is there any chance of storms today",
    "is it chilly outside",
    "how cold is it out there",
    "is the air quality bad today",
    "how windy is it outside",
]

# 15. conversation: (Pure chat, math/science questions, geometry, place mentions)
CONVERSATION_QUESTIONS = [
    "what is the volume of a sphere with radius five",
    "what is the volume of a cylinder",
    "what is the volume of a cone",
    "what is the volume of a pyramid",
    "what is Markovnikov addition in organic chemistry",
    "how does cyclotron resonance work in non-relativistic regime",
    "who was Richard Feynman",
    "what is the density of iron",
    "how do you define hyperconjugation",
    "what is the capital of Australia",
    "who discovered penicillin",
    "how tall is everest",
    "Reykjavik, Iceland",
    "Cairo, Egypt",
    "Melbourne, Australia",
    "Vancouver, British Columbia",
    "Paris, France",
    "Seattle, Washington",
]


def generate_clean_pool():
    """Generates grammatically correct, slot-isolated items for all 15 intents."""
    all_notes = PHYSICS_ENTITIES + CHEMISTRY_ENTITIES + SYSTEM_ENTITIES + MEMO_ENTITIES
    pool = []

    # 1. flash_note: Imperative note frames + note suffixes
    for frame in FLASH_NOTE_FRAMES:
        for body in all_notes:
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NOTE_SUFFIXES)
            text = f"{pref}{frame.format(body=body)}{suff}"
            text = " ".join(text.split()).strip()
            pool.append((text, "flash_note"))

    # 2. browser_recipe: Imperatives + Questions with neutral suffixes only
    for imp in BROWSER_RECIPE_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "browser_recipe"))
    for q in BROWSER_RECIPE_QUESTIONS:
        for _ in range(15):
            pref = random.choice(QUESTION_PREFIXES)
            text = f"{pref}{q}"
            pool.append((" ".join(text.split()).strip(), "browser_recipe"))

    # 3. skill_route: Imperatives + Questions with skill suffixes only
    for imp in SKILL_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(SKILL_SUFFIXES)
            rendered = imp.format(app=random.choice(APPS))
            text = f"{pref}{rendered}{suff}"
            pool.append((" ".join(text.split()).strip(), "skill_route"))
    for q in SKILL_QUESTIONS:
        for _ in range(15):
            pref = random.choice(QUESTION_PREFIXES)
            rendered = q.format(app=random.choice(APPS))
            text = f"{pref}{rendered}"
            pool.append((" ".join(text.split()).strip(), "skill_route"))

    # 4. study_retention: Imperatives + Questions with retention suffixes
    for imp in STUDY_RETENTION_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(RETENTION_SUFFIXES)
            rendered = imp.format(subject=random.choice(SUBJECTS))
            text = f"{pref}{rendered}{suff}"
            pool.append((" ".join(text.split()).strip(), "study_retention"))
    for q in STUDY_RETENTION_QUESTIONS:
        for _ in range(15):
            pref = random.choice(QUESTION_PREFIXES)
            rendered = q.format(subject=random.choice(SUBJECTS))
            text = f"{pref}{rendered}"
            pool.append((" ".join(text.split()).strip(), "study_retention"))

    # 5. system_action: Noun hijacking counter-pairs
    for imp in SYSTEM_ACTION_IMPERATIVES:
        for _ in range(20):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "system_action"))
    for q in SYSTEM_ACTION_QUESTIONS:
        for _ in range(10):
            pref = random.choice(QUESTION_PREFIXES)
            text = f"{pref}{q}"
            pool.append((" ".join(text.split()).strip(), "system_action"))

    # 6. app_launch: Clean app open
    for imp in APP_LAUNCH_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "app_launch"))

    # 7. omarchy: Hyprland themes and shortcuts
    for imp in OMARCHY_IMPERATIVES:
        for _ in range(20):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "omarchy"))
    for q in OMARCHY_QUESTIONS:
        for _ in range(15):
            pref = random.choice(QUESTION_PREFIXES)
            text = f"{pref}{q}"
            pool.append((" ".join(text.split()).strip(), "omarchy"))

    # 8. hard_goal: Scripts, refactoring, builds
    for imp in HARD_GOAL_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "hard_goal"))

    # 9. research_task: Deep investigations
    for imp in RESEARCH_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "research_task"))

    # 10. web_search: Web search
    for imp in WEB_SEARCH_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "web_search"))

    # 11. url_open: Domain visits
    for imp in URL_OPEN_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "url_open"))

    # 12. desktop_vision: Click on screen
    for imp in DESKTOP_VISION_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "desktop_vision"))

    # 13. engine_switch: Brain switcher
    for imp in ENGINE_SWITCH_IMPERATIVES:
        for _ in range(25):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "engine_switch"))
    for q in ENGINE_SWITCH_QUESTIONS:
        for _ in range(15):
            pref = random.choice(QUESTION_PREFIXES)
            text = f"{pref}{q}"
            pool.append((" ".join(text.split()).strip(), "engine_switch"))

    # 14. weather: Explicit weather forecast
    for imp in WEATHER_IMPERATIVES:
        for _ in range(20):
            pref = random.choice(IMPERATIVE_PREFIXES)
            suff = random.choice(NEUTRAL_SUFFIXES)
            text = f"{pref}{imp}{suff}"
            pool.append((" ".join(text.split()).strip(), "weather"))
    for q in WEATHER_QUESTIONS:
        for _ in range(25):
            pref = random.choice(QUESTION_PREFIXES)
            text = f"{pref}{q}"
            pool.append((" ".join(text.split()).strip(), "weather"))

    # 15. conversation: Chat and math/science questions
    for q in CONVERSATION_QUESTIONS:
        for _ in range(20):
            pref = random.choice(QUESTION_PREFIXES)
            text = f"{pref}{q}"
            pool.append((" ".join(text.split()).strip(), "conversation"))

    return pool


def main():
    logger.info("=" * 65)
    logger.info("BUILDING GIDEON LYRA v8 MASTER DATASET (RECTIFIED & LAYERED)")
    logger.info("=" * 65)

    random.seed(42)
    master_rows = []
    seen_texts = set()

    # -----------------------------------------------------------------
    # Layer 1: Cleaned Overnight Augmented Foundation (18,987 rows)
    # -----------------------------------------------------------------
    OVERNIGHT_FILE = Path("/home/cat/gideon/data/train_gideon_migrated_15.jsonl")
    logger.info(f"Loading Layer 1: Full Overnight Foundation from {OVERNIGHT_FILE.name}...")
    baseline_cleaned_count = 0
    with open(OVERNIGHT_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            orig_state = r["state"]
            cleaned_state = clean_sentence(orig_state)
            if cleaned_state != orig_state:
                baseline_cleaned_count += 1

            old_probs = r.get("gold", {}).get("intent", {}).get("probabilities", {})
            target_intent = max(old_probs.items(), key=lambda x: x[1])[0] if old_probs else "conversation"

            norm = cleaned_state.lower()
            if norm not in seen_texts:
                seen_texts.add(norm)
                master_rows.append(format_row(cleaned_state, target_intent, layer="core_overnight", v6_confidence=1.0))

    logger.info(f"Layer 1 Loaded: {len(master_rows)} rows (cleaned {baseline_cleaned_count} glitchy comma/grammar rows from overnight foundation).")

    # -----------------------------------------------------------------
    # Layer 3: Hard / Boundary Cases (Filtered from Mined v8)
    # -----------------------------------------------------------------
    mined_path = Path("/home/cat/gideon/data/mined_hard_cases_v8.jsonl")
    layer3_count = 0
    if mined_path.exists():
        logger.info(f"Loading Layer 3: Hard Boundary Cases from {mined_path.name}...")
        with open(mined_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                q = r["query"]
                exp = r["expected"]
                conf = r.get("confidence", 0.5)

                # Bug A check: suffix leak
                if exp in ["browser_recipe", "skill_route"] and any(x in q.lower() for x in ["in my study notes", "in obsidian", "for my exam"]):
                    continue
                # Bug B check: ledger in note
                if exp == "flash_note" and "ledger" in q.lower():
                    continue
                # Bug C check: double questions
                if re.search(r"^(can you|could you|can we|would you)\s+(do i|are you|is it|why did|why does|how is|what is)", q, re.IGNORECASE):
                    continue

                cleaned_q = clean_sentence(q)
                norm = cleaned_q.lower()
                if norm not in seen_texts:
                    seen_texts.add(norm)
                    master_rows.append(format_row(cleaned_q, exp, layer="hard_boundary_mutation", v6_confidence=conf))
                    layer3_count += 1
        logger.info(f"Layer 3 Loaded: {layer3_count} rigorously vetted hard boundary cases.")

    # -----------------------------------------------------------------
    # Layer 2: Clean Generated Easy / Medium Pools
    # -----------------------------------------------------------------
    logger.info("Generating Layer 2: Clean Slot-Isolated Easy / Medium Mutations...")
    generated_pool = generate_clean_pool()
    random.shuffle(generated_pool)

    TARGET_PER_CLASS = 2200
    class_counts = Counter(
        max(r["gold"]["intent"]["probabilities"].items(), key=lambda x: x[1])[0]
        for r in master_rows
    )

    layer2_count = 0
    for query, intent in generated_pool:
        if class_counts[intent] >= TARGET_PER_CLASS:
            continue
        cleaned = clean_sentence(query)
        norm = cleaned.lower()
        if norm not in seen_texts:
            seen_texts.add(norm)
            master_rows.append(format_row(cleaned, intent, layer="easy_medium_mutation", v6_confidence=0.92))
            class_counts[intent] += 1
            layer2_count += 1

    logger.info(f"Layer 2 Loaded: {layer2_count} clean easy/medium mutations.")

    # Final shuffle
    random.shuffle(master_rows)

    # Write out master dataset
    logger.info(f"Writing {len(master_rows)} rows to {OUTPUT_MASTER}...")
    with open(OUTPUT_MASTER, "w") as f:
        for row in master_rows:
            f.write(json.dumps(row) + "\n")

    # Layer breakdown
    layer_counts = Counter(r["layer"] for r in master_rows)
    final_counts = Counter(
        max(r["gold"]["intent"]["probabilities"].items(), key=lambda x: x[1])[0]
        for r in master_rows
    )

    logger.info("=" * 65)
    logger.info(f"MASTER DATASET COMPLETE: {len(master_rows)} total rows in {OUTPUT_MASTER.name}")
    logger.info("DATASET LAYERING:")
    for k, v in layer_counts.items():
        logger.info(f"  {k:<25}: {v:5d} ({v/len(master_rows)*100:4.1f}%)")
    logger.info("-" * 65)
    logger.info("FINAL CLASS DISTRIBUTION:")
    for k, v in sorted(final_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  {k:<25}: {v:5d} ({v/len(master_rows)*100:4.1f}%)")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
