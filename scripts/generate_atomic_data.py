"""Generate synthetic and template-based training, validation, and calibration datasets
for GIDEON v2 BGE-small atomic intent classifier across all 65 intents.
"""
import json
import random
import re
from pathlib import Path
from collections import defaultdict

random.seed(42)

# Load schema
schema = json.loads(Path("/home/cat/gideon/intents_schema.json").read_text())
all_intents = sorted(list(schema.keys()))

FILLERS_PREFIX = [
    "", "", "", "", # Weight empty prefix higher
    "hey gideon ", "gideon ", "yo gideon ", "ok gideon ",
    "please ", "can you ", "could you ", "quick ", "real quick ",
    "uh ", "hey ", "just ", "i want you to ", "gideon please "
]

FILLERS_SUFFIX = [
    "", "", "", "",
    " please", " real quick", " now", " thanks", " for me"
]

# Base seed templates per intent (each template will generate multiple variations)
INTENT_TEMPLATES = {
    # Audio Volume
    "audio_volume_set": [
        "set volume to {level}%",
        "set volume to {level}",
        "volume {level} percent",
        "volume to {level}%",
        "turn volume to {level}",
        "adjust audio volume to {level}%",
        "change volume to {level}",
        "set sound to {level} percent",
        "put the volume at {level}%",
        "sound level {level}"
    ],
    "audio_volume_up": [
        "volume up",
        "turn volume up",
        "turn it up",
        "increase volume",
        "increase volume by {step}%",
        "raise the volume",
        "make it louder",
        "boost volume",
        "crank up the sound",
        "volume up {step} percent",
        "bump volume up",
        "raise sound"
    ],
    "audio_volume_down": [
        "volume down",
        "turn volume down",
        "turn it down",
        "decrease volume",
        "lower the volume",
        "decrease volume by {step}%",
        "lower audio level",
        "make it quieter",
        "softer please",
        "drop volume by {step} percent",
        "bring volume down",
        "reduce the volume"
    ],
    "audio_mute": [
        "mute audio",
        "mute sound",
        "mute volume",
        "silence audio",
        "mute output",
        "mute speakers",
        "silence the speakers",
        "turn off sound",
        "shut off audio"
    ],
    "audio_unmute": [
        "unmute audio",
        "unmute sound",
        "unmute volume",
        "restore audio",
        "turn sound back on",
        "unmute output",
        "turn the sound back on",
        "enable audio output"
    ],
    "audio_mute_toggle": [
        "toggle mute",
        "toggle sound mute",
        "toggle audio mute",
        "switch audio mute",
        "toggle speakers mute"
    ],
    "audio_mic_mute": [
        "mute microphone",
        "mute mic",
        "mute my mic",
        "turn off microphone",
        "disable mic input",
        "silence the mic",
        "mute voice input"
    ],
    "audio_mic_unmute": [
        "unmute microphone",
        "unmute mic",
        "unmute my mic",
        "turn on microphone",
        "enable mic input",
        "mic unmute",
        "turn mic back on"
    ],
    "audio_mic_mute_toggle": [
        "toggle mic",
        "toggle microphone",
        "toggle mic mute",
        "toggle microphone mute",
        "switch mic mute state"
    ],
    "audio_output_headphones": [
        "switch audio to headphones",
        "switch sound to headset",
        "set output to headphones",
        "route audio to headphones",
        "use headphones",
        "headphones output"
    ],
    "audio_output_speakers": [
        "switch audio to speakers",
        "switch sound to speakers",
        "set output to speakers",
        "route audio to speakers",
        "use speakers",
        "speaker output"
    ],

    # Media Playback (Crucial: hard negatives vs launch_spotify)
    "media_play": [
        "play music",
        "resume music",
        "resume playback",
        "play media",
        "resume spotify",
        "unpause spotify",
        "continue music",
        "resume audio",
        "start playback",
        "continue playing the song",
        "keep playing music",
        "hit play"
    ],
    "media_pause": [
        "pause music",
        "pause media",
        "pause playback",
        "pause spotify",
        "pause the track",
        "pause song",
        "freeze playback",
        "hold the music",
        "pause audio"
    ],
    "media_play_pause": [
        "play pause toggle",
        "toggle media playback",
        "play pause media",
        "toggle music playback",
        "toggle play pause"
    ],
    "media_next": [
        "next track",
        "skip track",
        "next song",
        "skip this song",
        "skip to next track",
        "next track please",
        "play next track",
        "forward track"
    ],
    "media_previous": [
        "previous track",
        "previous song",
        "go back a song",
        "restart track",
        "play previous track",
        "prior song",
        "back one track"
    ],
    "media_stop": [
        "stop media",
        "stop music",
        "stop playback",
        "stop playing audio",
        "halt playback",
        "kill music playback"
    ],

    # Brightness
    "brightness_up": [
        "brightness up",
        "increase brightness",
        "turn brightness up",
        "raise brightness",
        "screen brighter",
        "brighten screen",
        "increase brightness by {step}%",
        "raise display brightness"
    ],
    "brightness_down": [
        "brightness down",
        "decrease brightness",
        "lower brightness",
        "turn brightness down",
        "dim the screen",
        "dim display",
        "lower brightness by {step}%",
        "reduce monitor brightness"
    ],
    "brightness_set": [
        "set brightness to {level}%",
        "brightness {level} percent",
        "set display brightness to {level}",
        "screen brightness {level}%",
        "adjust brightness to {level}%"
    ],

    # Window Management
    "window_close": [
        "close window",
        "close active window",
        "close this window",
        "kill active window",
        "close current app window",
        "shut this window",
        "terminate active window"
    ],
    "window_fullscreen_toggle": [
        "toggle fullscreen",
        "make window fullscreen",
        "exit fullscreen",
        "fullscreen active window",
        "toggle full screen",
        "maximize window toggle"
    ],
    "window_focus_left": [
        "focus window left",
        "focus left window",
        "move focus left",
        "switch to left window",
        "focus left pane"
    ],
    "window_focus_right": [
        "focus window right",
        "focus right window",
        "move focus right",
        "switch to right window",
        "focus right pane"
    ],
    "window_focus_up": [
        "focus window up",
        "focus upper window",
        "move focus up",
        "switch to top window"
    ],
    "window_focus_down": [
        "focus window down",
        "focus bottom window",
        "move focus down",
        "switch to bottom window"
    ],
    "window_floating_toggle": [
        "toggle floating window",
        "toggle floating state",
        "float active window",
        "make window floating",
        "toggle window float"
    ],

    # Workspace Management
    "workspace_switch": [
        "switch to workspace {workspace}",
        "go to workspace {workspace}",
        "take me to workspace {workspace}",
        "workspace {workspace}",
        "jump to workspace {workspace}",
        "move to workspace {workspace}"
    ],
    "workspace_previous": [
        "switch to previous workspace",
        "go back to previous workspace",
        "last workspace",
        "prior workspace",
        "jump back to last workspace",
        "return to previous workspace"
    ],
    "workspace_next": [
        "switch to next workspace",
        "go to next workspace",
        "next workspace",
        "advance to next workspace"
    ],
    "workspace_prev": [
        "switch to preceding workspace",
        "go to prior workspace",
        "workspace minus one",
        "previous workspace relative"
    ],

    # App Launchers
    "launch_browser": [
        "launch chrome",
        "open google chrome",
        "open chrome browser",
        "start chrome",
        "open web browser",
        "launch browser",
        "start google chrome"
    ],
    "launch_brave": [
        "launch brave",
        "open brave browser",
        "start brave",
        "launch brave browser",
        "open brave"
    ],
    "launch_spotify": [
        "launch spotify",
        "open spotify",
        "start spotify app",
        "open spotify client",
        "launch the spotify app"
    ],
    "launch_terminal": [
        "launch terminal",
        "open kitty",
        "open terminal",
        "start kitty terminal",
        "launch kitty terminal",
        "open command line terminal"
    ],
    "launch_obsidian": [
        "launch obsidian",
        "open obsidian",
        "open notes app",
        "start obsidian vault",
        "open obsidian notes"
    ],
    "launch_code": [
        "launch vs code",
        "open visual studio code",
        "open code editor",
        "start vscode",
        "open code",
        "launch visual studio code"
    ],
    "launch_file_manager": [
        "launch file manager",
        "open thunar",
        "open files app",
        "open file explorer",
        "browse files",
        "open files folder"
    ],

    # Omarchy & Desktop
    "omarchy_theme_list": [
        "list omarchy themes",
        "show available themes",
        "what themes are installed",
        "list themes",
        "display theme list",
        "show all omarchy themes"
    ],
    "omarchy_theme_set": [
        "set theme to {theme}",
        "apply {theme} theme",
        "switch theme to {theme}",
        "omarchy theme set {theme}",
        "change theme to {theme}",
        "use {theme} theme"
    ],
    "omarchy_theme_next": [
        "next theme",
        "cycle theme",
        "switch to next theme",
        "cycle to next omarchy theme",
        "try next theme"
    ],
    "omarchy_theme_dark": [
        "set dark theme in omarchy",
        "enable dark mode",
        "switch to dark theme",
        "turn on dark mode",
        "switch to dark style",
        "apply dark theme"
    ],
    "omarchy_theme_light": [
        "set light theme in omarchy",
        "enable light mode",
        "switch to light theme",
        "turn on light mode",
        "apply light theme",
        "switch to bright theme"
    ],
    "omarchy_wallpaper_next": [
        "next wallpaper",
        "change wallpaper",
        "next background",
        "cycle wallpaper",
        "switch background image",
        "next desktop wallpaper"
    ],
    "omarchy_wallpaper_set": [
        "set wallpaper to {path}",
        "change wallpaper to {path}",
        "apply background image {path}"
    ],
    "omarchy_keybindings": [
        "show omarchy keybindings",
        "what are my hyprland keybindings",
        "open keybinding menu",
        "show keyboard shortcuts",
        "hyprland shortcut lookup",
        "view desktop keybindings"
    ],
    "omarchy_screenshot": [
        "take screenshot",
        "capture screen region",
        "screenshot area",
        "take a snip",
        "capture screenshot to clipboard",
        "snip region on screen"
    ],
    "omarchy_screenshot_full": [
        "capture full screen",
        "fullscreen screenshot",
        "take full screenshot and save",
        "save full screen capture",
        "screenshot entire monitor"
    ],
    "omarchy_update": [
        "update omarchy",
        "run omarchy update",
        "upgrade system packages",
        "update system and omarchy",
        "run system update"
    ],

    # System & Power
    "system_status": [
        "check system status",
        "show system stats",
        "system performance stats",
        "display cpu and memory usage",
        "how is system health",
        "check battery and cpu status"
    ],
    "system_lock": [
        "lock screen",
        "lock computer",
        "lock session",
        "lock the workstation",
        "lock my desktop"
    ],
    "system_logout": [
        "log out of session",
        "logout of desktop",
        "log me out",
        "exit desktop session"
    ],
    "system_reboot": [
        "reboot system",
        "restart computer",
        "reboot computer",
        "restart machine",
        "system reboot now"
    ],
    "system_shutdown": [
        "shut down computer",
        "power off machine",
        "turn off computer",
        "system shutdown",
        "power down workstation"
    ],

    # Study System
    "study_queue_view": [
        "show revision queue",
        "view study queue",
        "what reviews are due today",
        "check retention queue",
        "show today due study cards",
        "open revision queue",
        "what study sessions are due"
    ],
    "study_status_view": [
        "show study progress",
        "view study status",
        "what is my study burndown",
        "show chapter ranks and mastery",
        "view study telemetry",
        "how is my exam prep burndown",
        "check study progress overview"
    ],
    "study_doctor": [
        "run study doctor",
        "verify study ledgers",
        "check study consistency",
        "run doctor on study system",
        "validate ledger integrity"
    ],
    "study_traps_view": [
        "show black book traps for {chapter}",
        "view traps for {chapter}",
        "extract traps for chapter {chapter}",
        "what misconceptions exist in {chapter}",
        "show study traps for {chapter}"
    ],
    "study_sprint_start": [
        "start study sprint for {chapter}",
        "launch study workspace for {chapter}",
        "begin recall session for {chapter}",
        "start drill session on {chapter}",
        "open study workspace for {chapter}"
    ],
    "flash_note_add": [
        "note down: {note}",
        "take a note: {note}",
        "quick memo: {note}",
        "flash note: {note}",
        "jot this down: {note}",
        "add note: {note}",
        "save a note: {note}"
    ],

    # Engine Switch
    "engine_switch": [
        "switch to opencode",
        "use opencode engine",
        "engine opencode",
        "switch to antigravity",
        "switch to agy",
        "use antigravity engine",
        "engine agy",
        "switch engine to lyra",
        "change model engine"
    ],

    # Pass-Through Buckets
    "conversation": [
        "what is the difference between a tensor and a matrix",
        "who are you",
        "how does gravity work according to general relativity",
        "explain electromagnetic induction intuitively",
        "what is the definition of momentum",
        "how does rm -rf delete directories recursively",
        "when should we stop training the neural net",
        "can you explain why the sky is blue",
        "what are the core principles of quantum mechanics",
        "tell me a joke about computer science",
        "how does a transformer neural network attention mechanism work",
        "what is the derivative of sin(x) squared"
    ],
    "hard_goal": [
        "write a python script to monitor system volume",
        "automate workspace switching using a bash daemon",
        "implement a quicksort algorithm in c++",
        "refactor the websocket server to use asyncio streams",
        "build an end to end test pipeline in pytest",
        "write a rust program to parse pcap network packets",
        "optimize the database query indices for postgres",
        "delete all temporary files in the cache directory using python",
        "create a dockerfile and docker-compose for the service",
        "implement backpropagation from scratch with numpy",
        "debug this memory leak in the c extension"
    ],
    "web_search": [
        "search the web for latest space news",
        "search google for today's breaking news",
        "look up current weather in tokyo",
        "find latest stock price of nvidia",
        "search online for iat 2026 syllabus updates",
        "search duckduckgo for python 3.12 release notes",
        "look up recent nest examination notifications",
        "search web for best hyprland plugins 2026",
        "web search recent developments in fusion energy"
    ],
    "browser_use_passthrough": [
        "click on the blue submit button on screen",
        "autofill my shipping address in the browser form",
        "read the text in the active browser tab",
        "click the login button on the webpage",
        "scroll down to the comments section on this page",
        "navigate to the settings tab in the web app",
        "type my username into the web input field",
        "select the third option in the dropdown on the website"
    ]
}

# Slot values pools
SLOT_LEVELS = [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 75, 80, 85, 90, 95, 100]
SLOT_STEPS = [5, 10, 15, 20, 25]
SLOT_WORKSPACES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
SLOT_THEMES = [
    "Tokyo Night", "Gruvbox", "Catppuccin", "Catppuccin Latte", "Flexoki Light",
    "Everforest", "Nord", "Kanagawa", "Rose Pine", "Miasma", "Matte Black",
    "Lumon", "Lupine", "Solitude", "White", "Ash", "Aamis", "Aether", "Retro 82"
]
SLOT_CHAPTERS = [
    "phy_01", "phy_02", "phy_14", "che_01", "che_02", "mat_01", "mat_02",
    "Electrostatics", "Semiconductors", "Thermodynamics", "Chemical Kinetics"
]
SLOT_NOTES = [
    "remember to review Gauss's Law before tomorrow's sprint",
    "the work function depends on metal surface properties",
    "matrix multiplication is associative but not commutative",
    "check PipeWire audio configuration for latency",
    "IAT registration deadline is next Monday"
]
SLOT_PATHS = [
    "~/Pictures/wallpapers/forest.jpg",
    "/usr/share/backgrounds/nature.png",
    "~/Downloads/cyberpunk.png"
]

def mutate_utterance(text: str) -> str:
    """Apply realistic natural spoken/typed mutations without breaking meaning."""
    text = text.strip()
    # 20% chance: lowercase
    if random.random() < 0.3:
        text = text.lower()
    # 15% chance: add filler prefix
    if random.random() < 0.4:
        pfx = random.choice(FILLERS_PREFIX)
        text = pfx + text
    # 15% chance: add filler suffix
    if random.random() < 0.3:
        sfx = random.choice(FILLERS_SUFFIX)
        text = text + sfx
    # Clean whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text

def generate_samples_for_intent(intent_id: str, target_count: int = 250) -> list[str]:
    templates = INTENT_TEMPLATES.get(intent_id, [])
    if not templates:
        print(f"Warning: no templates for {intent_id}")
        return []
    
    samples = set()
    attempts = 0
    max_attempts = target_count * 20
    
    while len(samples) < target_count and attempts < max_attempts:
        attempts += 1
        tpl = random.choice(templates)
        
        # Format slots
        filled = tpl
        if "{level}" in filled:
            lvl = random.choice(SLOT_LEVELS)
            filled = filled.replace("{level}", str(lvl))
        if "{step}" in filled:
            step = random.choice(SLOT_STEPS)
            filled = filled.replace("{step}", str(step))
        if "{workspace}" in filled:
            ws = random.choice(SLOT_WORKSPACES)
            filled = filled.replace("{workspace}", str(ws))
        if "{theme}" in filled:
            th = random.choice(SLOT_THEMES)
            filled = filled.replace("{theme}", th)
        if "{chapter}" in filled:
            ch = random.choice(SLOT_CHAPTERS)
            filled = filled.replace("{chapter}", ch)
        if "{note}" in filled:
            nt = random.choice(SLOT_NOTES)
            filled = filled.replace("{note}", nt)
        if "{path}" in filled:
            pt = random.choice(SLOT_PATHS)
            filled = filled.replace("{path}", pt)
            
        mutated = mutate_utterance(filled)
        if mutated and len(mutated) >= 3:
            samples.add(mutated)
            
    return list(samples)

def main():
    print("Generating atomic dataset across 65 intents...")
    data_by_intent = {}
    
    for intent_id in all_intents:
        # Generate 220-280 samples per intent
        target = 260 if intent_id in ("conversation", "hard_goal", "system_status", "media_play", "launch_spotify") else 220
        samples = generate_samples_for_intent(intent_id, target_count=target)
        data_by_intent[intent_id] = samples
        print(f"  {intent_id}: {len(samples)} samples")
        
    # Split into train (70%), val (15%), calib (15%)
    train_rows = []
    val_rows = []
    calib_rows = []
    
    all_seen_queries = set()
    
    for intent_id, samples in data_by_intent.items():
        # Shuffle deterministically
        random.shuffle(samples)
        n = len(samples)
        n_train = int(n * 0.70)
        n_val = int(n * 0.15)
        
        train_s = samples[:n_train]
        val_s = samples[n_train:n_train + n_val]
        calib_s = samples[n_train + n_val:]
        
        for q in train_s:
            if q not in all_seen_queries:
                all_seen_queries.add(q)
                train_rows.append({"text": q, "label": intent_id})
        for q in val_s:
            if q not in all_seen_queries:
                all_seen_queries.add(q)
                val_rows.append({"text": q, "label": intent_id})
        for q in calib_s:
            if q not in all_seen_queries:
                all_seen_queries.add(q)
                calib_rows.append({"text": q, "label": intent_id})
                
    random.shuffle(train_rows)
    random.shuffle(val_rows)
    random.shuffle(calib_rows)
    
    out_dir = Path("/home/cat/gideon/data")
    out_dir.mkdir(exist_ok=True, parents=True)
    
    def write_jsonl(path, rows):
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"Wrote {len(rows)} rows to {path}")
        
    write_jsonl(out_dir / "train_atomic.jsonl", train_rows)
    write_jsonl(out_dir / "val_atomic.jsonl", val_rows)
    write_jsonl(out_dir / "calib_atomic.jsonl", calib_rows)
    
    print("\nDataset generation complete!")

if __name__ == "__main__":
    main()
