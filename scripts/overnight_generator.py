#!/usr/bin/env python3
"""
Overnight Dataset Generator & Adversarial Miner for Gideon (Lyra Intent Model) - v2.
Improvements in v2:
- Resumes seamlessly from existing train_gideon_augmented.jsonl without overwriting.
- Exponentially expanded combinatorial space (conversational framing, situational suffixes, adverbs).
- Anti-spin retry ceiling: max 15 attempts per cycle; falls back to dynamic lexical mutation.
- Continues running until remaining 5-hour window concludes.
"""
import itertools
import json
import logging
import math
import os
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

JARVIS_DIR = Path("/home/cat/jarvis")
GIDEON_DIR = Path("/home/cat/gideon")
sys.path.insert(0, str(JARVIS_DIR))

LOG_FILE = GIDEON_DIR / "data" / "overnight_generator.log"
OUTPUT_AUGMENTED = GIDEON_DIR / "data" / "train_gideon_augmented.jsonl"
OUTPUT_HARD_CASES = GIDEON_DIR / "data" / "mined_hard_cases.jsonl"
BASE_TRAIN_FILE = Path("/home/cat/laya_finetune/train_full.jsonl")
MODEL_DIR = JARVIS_DIR / "models" / "laya_intent_v6"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="a"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("overnight_gen")

from brain.laya_router import INTENT_QUESTION, INTENT_LABELS

# =====================================================================
# Combinatorial Lexicons, Prefixes, and Suffixes
# =====================================================================

STT_PREFIXES = [
    "", "", "", "um ", "uh ", "hey gideon ", "gideon ", "can you ", "please ",
    "could you ", "yeah ", "wait ", "hey ", "yo gideon ", "alright ",
    "i need you to ", "would you mind ", "go ahead and ", "let's ", "can we ",
    "make sure to ", "do me a favor and ", "hey buddy ", "quickly ", "just "
]

STT_SUFFIXES = [
    "", "", "", " right now", " immediately", " for a second", " in a moment",
    " on my machine", " on my screen", " quickly", " asap", " without delay",
    " please", " if you can", " thanks", " gideon", " in the background",
    " today", " real quick", " for me", " straight away"
]

# 1. system_action
SYSTEM_TEMPLATES = [
    "{prefix}mute {target}{suffix}",
    "{prefix}unmute {target}{suffix}",
    "{prefix}turn {direction} the {audio_target}{suffix}",
    "{prefix}set {audio_target} to {level} percent{suffix}",
    "{prefix}{media_verb} the playback{suffix}",
    "{prefix}{media_verb} {app}{suffix}",
    "{prefix}{media_verb} music{suffix}",
    "{prefix}switch to workspace {ws}{suffix}",
    "{prefix}go to workspace {ws}{suffix}",
    "{prefix}move active window to workspace {ws}{suffix}",
    "{prefix}tile window to the {screen_side}{suffix}",
    "{prefix}maximize this window{suffix}",
    "{prefix}close active window{suffix}",
    "{prefix}toggle fullscreen{suffix}",
    "{prefix}stop talking{suffix}",
    "{prefix}quiet{suffix}",
    "{prefix}shut up{suffix}",
    "{prefix}pause{suffix}",
    "{prefix}restart {daemon_service} service{suffix}",
    "{prefix}adjust screen brightness {direction}{suffix}",
]

# 2. app_launch
APP_TEMPLATES = [
    "{prefix}launch {app}{suffix}",
    "{prefix}open {app}{suffix}",
    "{prefix}start {app}{suffix}",
    "{prefix}open up {app}{suffix}",
    "{prefix}run {app}{suffix}",
    "{prefix}fire up {app}{suffix}",
]

# 3. omarchy
OMARCHY_TEMPLATES = [
    "{prefix}set {theme} theme in omarchy{suffix}",
    "{prefix}switch theme to {theme}{suffix}",
    "{prefix}next wallpaper{suffix}",
    "{prefix}random wallpaper{suffix}",
    "{prefix}change background wallpaper{suffix}",
    "{prefix}show my keybindings{suffix}",
    "{prefix}what are the hyprland shortcuts for {action_name}{suffix}",
    "{prefix}reload hyprland config{suffix}",
    "{prefix}take a screenshot of the display{suffix}",
    "{prefix}omarchy theme list{suffix}",
]

# 4. hard_goal (Coding, daemons, refactoring, agentic tasks)
HARD_GOAL_TEMPLATES = [
    "{prefix}write a python script to {code_task}{suffix}",
    "{prefix}automate {sys_task} using a bash {daemon_word}{suffix}",
    "{prefix}refactor the {module_name} in gideon{suffix}",
    "{prefix}implement a {feature_name} for the circuit board{suffix}",
    "{prefix}fix the bug where {bug_desc}{suffix}",
    "{prefix}debug why {crash_component} is segfaulting{suffix}",
    "{prefix}create a git commit for the {git_scope}{suffix}",
    "{prefix}write a c function to {c_task}{suffix}",
    "{prefix}delete all temporary files in {path}{suffix}",
    "{prefix}clean up the build artifacts in {path}{suffix}",
    "{prefix}build a daemon that monitors {monitor_target}{suffix}",
    "{prefix}we need to kill this {code_issue} in {lang}{suffix}",
    "{prefix}let's open the config file and inspect {param_name}{suffix}",
    "{prefix}don't stop the loop if an error occurs{suffix}",
]

# 5. conversation (Academic, science, math, chat, idioms, definitions)
CONV_TEMPLATES = [
    "{prefix}what is the formula for the volume of a {geom_shape}{suffix}",
    "{prefix}calculate the volume of a {geom_shape} with radius {num}{suffix}",
    "{prefix}can open orbital overlapping occur in {chem_entity}{suffix}",
    "{prefix}the play of electrostatic forces in {chem_substance}{suffix}",
    "{prefix}play through this {science_concept}{suffix}",
    "{prefix}how do switch statements work in {lang}{suffix}",
    "{prefix}launch into an explanation of {sci_phenom}{suffix}",
    "{prefix}i need to kill some time while the {comp_task}{suffix}",
    "{prefix}how does rm -rf delete files recursively{suffix}",
    "{prefix}why does pipewire buffer audio packets{suffix}",
    "{prefix}explain how hyprland handles window tiling{suffix}",
    "{prefix}what causes segfaults in c programs{suffix}",
    "{prefix}who are you{suffix}",
    "{prefix}what can you do{suffix}",
    "{prefix}tell me a joke{suffix}",
    "{prefix}how are you doing today{suffix}",
    "{prefix}when should we stop training the neural net{suffix}",
    "{prefix}what is the difference between a tensor and a matrix{suffix}",
]

# 6. weather
WEATHER_TEMPLATES = [
    "{prefix}what's the weather like in {city}{suffix}",
    "{prefix}check the temperature outside{suffix}",
    "{prefix}is it going to rain {time_frame}{suffix}",
    "{prefix}weather forecast for {city} {time_frame}{suffix}",
    "{prefix}how cold is it in {city}{suffix}",
    "{prefix}what is the humidity level right now{suffix}",
]

# 7. web_search
SEARCH_TEMPLATES = [
    "{prefix}search the web for {search_topic}{suffix}",
    "{prefix}look up the docs for {tech_name}{suffix}",
    "{prefix}google {search_topic}{suffix}",
    "{prefix}search online for the latest news on {news_topic}{suffix}",
    "{prefix}find the documentation for {lib_name}{suffix}",
]

# 8. url_open
URL_TEMPLATES = [
    "{prefix}open {site_domain} in the browser{suffix}",
    "{prefix}go to {site_domain}{suffix}",
    "{prefix}navigate to {site_domain}{suffix}",
    "{prefix}open up {site_domain}{suffix}",
]

# 9. browser_task
BROWSER_TEMPLATES = [
    "{prefix}check my instagram direct messages{suffix}",
    "{prefix}fill in the form fields on this page{suffix}",
    "{prefix}autofill my address into the checkout form{suffix}",
    "{prefix}consult gemini web about this issue{suffix}",
    "{prefix}summarize my unread instagram inbox{suffix}",
]

# 10. research_task
RESEARCH_TEMPLATES = [
    "{prefix}research the latest papers on {arxiv_topic}{suffix}",
    "{prefix}investigate the trade-offs between {tech_a} and {tech_b}{suffix}",
    "{prefix}deep research on why {kernel_issue} happens in linux{suffix}",
    "{prefix}read and summarize the active browser tabs{suffix}",
]

# 11. desktop_vision
VISION_TEMPLATES = [
    "{prefix}click on the {color} {ui_element} on screen{suffix}",
    "{prefix}press the button labeled {button_label}{suffix}",
    "{prefix}tap the confirmation dialog button{suffix}",
    "{prefix}click the icon in the top right corner{suffix}",
]

# 12. study_retention
STUDY_TEMPLATES = [
    "{prefix}review my study cards due today{suffix}",
    "{prefix}show the revision queue for {subject}{suffix}",
    "{prefix}what cards are due in my study ledger{suffix}",
    "{prefix}test my retention on {subject} formulas{suffix}",
]

# 13. engine_switch
ENGINE_TEMPLATES = [
    "{prefix}switch the cognitive brain to {engine_name}{suffix}",
    "{prefix}change engine to {engine_name}{suffix}",
    "{prefix}switch to {engine_name} model{suffix}",
    "{prefix}use {engine_name} for the next prompt{suffix}",
]

# Entity pools
LEXICONS = {
    "target": ["audio", "microphone", "sound", "mic", "speakers", "headphones"],
    "audio_target": ["volume", "sound level", "audio output", "master volume"],
    "direction": ["up", "down", "higher", "lower"],
    "level": ["10", "20", "30", "50", "75", "100", "5"],
    "media_verb": ["pause", "resume", "play", "stop", "toggle"],
    "app": ["spotify", "chrome", "firefox", "kitty", "alacritty", "obsidian", "discord", "code", "vlc", "telegram", "steam"],
    "ws": ["1", "2", "3", "4", "5", "6", "7", "8", "9"],
    "screen_side": ["left", "right", "top", "bottom"],
    "daemon_service": ["pipewire", "wireplumber", "hyprland", "bluetooth", "networkmanager"],
    "theme": ["nord", "catppuccin", "dracula", "tokyo-night", "gruvbox", "solarized-dark"],
    "action_name": ["window management", "workspaces", "tiling", "screenshots"],
    "code_task": ["monitor system volume", "parse ndjson streams", "fetch live weather metrics", "extract audio waveforms", "clean temporary files"],
    "sys_task": ["workspace switching", "display brightness adjustment", "pipewire buffer flushing", "audio device toggling"],
    "daemon_word": ["daemon", "script", "cron job", "background worker"],
    "module_name": ["audio pipeline", "visualizer server", "faster-whisper stt loop", "ndjson stream reader"],
    "feature_name": ["custom webgl shader", "waveform analyzer", "socket reconnect loop", "priority intervention gate"],
    "bug_desc": ["pipewire drops audio frames", "quickshell socket disconnects", "stt buffer leaks syllables", "tokens fail to stream"],
    "crash_component": ["faster-whisper", "pipewire", "hyprland", "sounddevice", "quickshell"],
    "git_scope": ["gideon engine refactor", "laya router integration", "audio latency fix", "visualizer shader updates"],
    "c_task": ["reproduce memory corruption", "allocate ring buffers for audio", "bind to unix domain sockets"],
    "path": ["/tmp/gideon", "~/.cache/gideon", "/var/tmp", "the build directory"],
    "monitor_target": ["cpu temperature", "gpu vram usage", "pipewire stream latency", "active window focus"],
    "code_issue": ["infinite loop", "deadlock", "memory leak", "race condition"],
    "lang": ["python", "golang", "c++", "rust", "typescript"],
    "param_name": ["timeout_s", "beam_size", "buffer_duration", "sampling_rate"],
    "geom_shape": ["sphere", "cylinder", "cone", "ellipsoid", "pyramid", "prism", "cube"],
    "num": ["5", "7", "10", "12", "3.5"],
    "chem_entity": ["carbocations", "free radicals", "carbanions", "transition states"],
    "chem_substance": ["sodium chloride", "benzene rings", "water molecules", "copper sulfate"],
    "science_concept": ["organic reaction mechanism", "electromagnetic wave propagation", "photoelectric effect", "entropy increase"],
    "sci_phenom": ["quantum entanglement", "general relativity", "brownian motion", "thermodynamic equilibrium"],
    "comp_task": ["model compiles", "packages install", "kernel builds", "tests run"],
    "city": ["Tokyo", "London", "New York", "San Francisco", "Mumbai", "Paris", "Berlin"],
    "time_frame": ["tomorrow", "tonight", "this weekend", "in the next few days", "today"],
    "search_topic": ["latest linux kernel releases", "faster-whisper benchmarks", "pipewire zero latency tips", "hyprland v0.42 changes"],
    "tech_name": ["pipewire pw-play", "sounddevice stream", "faster-whisper int8", "openWakeWord vad"],
    "news_topic": ["artificial intelligence", "arch linux updates", "wayland protocols"],
    "lib_name": ["scipy signal butterworth", "ctypes preload cuda", "asyncio streamreader"],
    "site_domain": ["github.com", "youtube.com", "reddit.com", "arxiv.org", "wikipedia.org", "claude.ai", "google.com"],
    "arxiv_topic": ["low latency streaming tts", "quantized speech recognition", "rlcd policy optimization"],
    "tech_a": ["whisper small.en", "wav2vec2", "faster-whisper", "deepgram"],
    "tech_b": ["whisper medium.en", "conformer", "vosk", "kokoro"],
    "kernel_issue": ["audio underrun occurs", "vulkan context resets", "page fault occurs"],
    "color": ["blue", "green", "red", "yellow", "primary"],
    "ui_element": ["button", "link", "checkbox", "tab", "submit button"],
    "button_label": ["Submit", "Cancel", "Confirm", "Save", "Next"],
    "subject": ["physics", "organic chemistry", "linear algebra", "calculus"],
    "engine_name": ["gemini thinking", "opencode", "qwen omni", "mistral large", "antigravity"],
}

INTENT_TEMPLATES = {
    "system_action": SYSTEM_TEMPLATES,
    "app_launch": APP_TEMPLATES,
    "omarchy": OMARCHY_TEMPLATES,
    "hard_goal": HARD_GOAL_TEMPLATES,
    "conversation": CONV_TEMPLATES,
    "weather": WEATHER_TEMPLATES,
    "web_search": SEARCH_TEMPLATES,
    "url_open": URL_TEMPLATES,
    "browser_task": BROWSER_TEMPLATES,
    "research_task": RESEARCH_TEMPLATES,
    "desktop_vision": VISION_TEMPLATES,
    "study_retention": STUDY_TEMPLATES,
    "engine_switch": ENGINE_TEMPLATES,
}


def render_query(template: str, mutate: bool = False) -> str:
    """Fills slots in template using random choices from LEXICONS, prefixes, and suffixes."""
    slots = {}
    slots["prefix"] = random.choice(STT_PREFIXES)
    slots["suffix"] = random.choice(STT_SUFFIXES)
    for k, vals in LEXICONS.items():
        slots[k] = random.choice(vals)
    rendered = template.format(**slots)
    if mutate:
        # Extra variation to prevent saturation collisions
        adverbs = [" really", " just", " right now", " please", ""]
        rendered = rendered + random.choice(adverbs)
    return " ".join(rendered.split()).strip()


def make_gold_record(query: str, intent: str) -> dict:
    """Formats row identically to train_full.jsonl for Kaggle training."""
    probs = {label: 1.0 if label == intent else 0.0 for label in INTENT_LABELS}
    return {
        "state": query,
        "questions": INTENT_QUESTION,
        "gold": {
            "intent": {
                "probabilities": probs
            }
        }
    }


def main():
    logger.info("=" * 65)
    logger.info("RESUMING OVERNIGHT GIDEON DATASET GENERATOR & ADVERSARIAL MINER (v2)")
    logger.info(f"Deployed model: {MODEL_DIR}")
    logger.info("=" * 65)

    import torch
    import laya

    # Step 1: Ingest existing dataset to prevent duplicates
    existing_queries = set()
    initial_rows = 0

    if OUTPUT_AUGMENTED.exists():
        logger.info(f"Ingesting existing augmented dataset from {OUTPUT_AUGMENTED}...")
        with open(OUTPUT_AUGMENTED, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    existing_queries.add(row["state"].strip().lower())
                    initial_rows += 1
                except Exception:
                    pass
        logger.info(f"Found {initial_rows} existing rows in {OUTPUT_AUGMENTED.name} (preserved).")
    elif BASE_TRAIN_FILE.exists():
        logger.info(f"Loading baseline anchor dataset from {BASE_TRAIN_FILE}...")
        with open(BASE_TRAIN_FILE, "r") as f_in, open(OUTPUT_AUGMENTED, "w") as f_out:
            for line in f_in:
                if not line.strip():
                    continue
                row = json.loads(line)
                existing_queries.add(row["state"].strip().lower())
                f_out.write(line.strip() + "\n")
                initial_rows += 1
        logger.info(f"Successfully anchored {initial_rows} baseline examples into {OUTPUT_AUGMENTED.name}")

    # Count existing hard cases
    existing_hard = 0
    if OUTPUT_HARD_CASES.exists():
        with open(OUTPUT_HARD_CASES, "r") as f:
            for line in f:
                if line.strip():
                    existing_hard += 1

    # Step 2: Load model in fp16
    logger.info("Loading model on CUDA in fp16...")
    agent = laya.load(str(MODEL_DIR))
    agent.model.half()
    torch.cuda.empty_cache()
    logger.info("Model loaded and cached. Beginning generation loop...")

    # Step 3: Run for 2.5 remaining hours (9,000s)
    TARGET_DURATION_SEC = 2.5 * 3600.0
    start_time = time.time()
    end_time = start_time + TARGET_DURATION_SEC

    f_aug = open(OUTPUT_AUGMENTED, "a", buffering=1)
    f_hard = open(OUTPUT_HARD_CASES, "a", buffering=1)

    new_generated = 0
    new_hard = 0
    intents_cycle = itertools.cycle(INTENT_LABELS)

    try:
        while time.time() < end_time:
            target_intent = next(intents_cycle)
            templates = INTENT_TEMPLATES[target_intent]

            # Anti-spin retry ceiling
            candidate_query = None
            for attempt in range(15):
                candidate = render_query(random.choice(templates), mutate=(attempt > 8))
                norm_q = candidate.strip().lower()
                if norm_q not in existing_queries:
                    candidate_query = candidate
                    existing_queries.add(norm_q)
                    break

            if candidate_query is None:
                # Saturated: skip this turn cleanly without spinning
                continue

            # Predict on local GPU
            try:
                pred_out = agent.predict(candidate_query, INTENT_QUESTION)
                ans = pred_out.get("answers", {}).get("intent", {})
                predicted = ans.get("choice")
                conf = float(ans.get("confidence", 0.0) or 0.0)
                probs = ans.get("probabilities", {})
            except Exception as e:
                logger.warning(f"Inference error on query '{candidate_query}': {e}")
                time.sleep(1.0)
                continue

            is_correct = (predicted == target_intent)
            is_hard = (not is_correct) or (conf < 0.85)

            # Build record
            record = make_gold_record(candidate_query, target_intent)
            f_aug.write(json.dumps(record) + "\n")
            new_generated += 1

            if is_hard:
                new_hard += 1
                hard_entry = {
                    "query": candidate_query,
                    "expected": target_intent,
                    "predicted": predicted,
                    "confidence": round(conf, 4),
                    "probs": {k: round(v, 4) for k, v in probs.items()},
                    "timestamp": time.time()
                }
                f_hard.write(json.dumps(hard_entry) + "\n")

            # Heartbeat logging every 40 items (~25 seconds)
            if new_generated % 40 == 0:
                elapsed = time.time() - start_time
                remaining = max(0.0, end_time - time.time())
                free_mb = torch.cuda.mem_get_info()[0] / (1024**2) if torch.cuda.is_available() else 0.0
                total_current = initial_rows + new_generated
                total_hard_now = existing_hard + new_hard
                hard_pct = (total_hard_now / total_current) * 100.0

                logger.info(
                    f"[Heartbeat v2] Total: {total_current:5d} (+{new_generated:4d}) | "
                    f"Hard: {total_hard_now:4d} ({hard_pct:4.1f}%) | "
                    f"Elapsed: {elapsed/3600:.2f}h | Left: {remaining/3600:.2f}h | VRAM Free: {free_mb:.0f}MB"
                )

            # Pacing sleep
            time.sleep(0.35)

    except KeyboardInterrupt:
        logger.info("Generator interrupted by user.")
    except Exception as e:
        logger.error(f"Fatal error in generator loop: {e}", exc_info=True)
    finally:
        f_aug.close()
        f_hard.close()
        logger.info("=" * 65)
        logger.info(f"OVERNIGHT RUN COMPLETED. Total rows: {initial_rows + new_generated}, Hard cases: {existing_hard + new_hard}")
        logger.info("=" * 65)


if __name__ == "__main__":
    main()
