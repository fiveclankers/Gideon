# Gideon

Gideon is a Linux voice companion that listens locally, routes quick desktop commands directly, and sends open ended requests to a conversational engine. It can speak replies through Fish Audio and show its current state in a browser HUD or a Quickshell overlay.

Gideon is built around Omarchy and Hyprland. Some parts run on other Linux desktops, but workspace, theme, and window actions depend on those tools.

## Features

- Local speech recognition with Faster Whisper: `medium.en` on CUDA when available, with a `small.en` CPU fallback.
- A deterministic reflex router plus a BGE-small ONNX intent dispatcher for common actions such as audio volume, workspace/window control, Omarchy tasks, media commands, engine changes, and quick notes. Unmatched prompts go to the active engine.
- Antigravity (`agy`) as the default conversational engine; OpenCode can be selected with `--engine opencode` or `GIDEON_ENGINE=opencode`.
- Fish Audio speech synthesis, queued by sentence and played through PipeWire's `pw-play`.
- Microphone wake listening, browser hold-to-talk, terminal input, and an interrupt command.
- A WebGL HUD with live state and waveform data, plus newline-delimited Unix socket events for the Quickshell Orb.
- Omarchy and study handlers for machine-specific desktop and study workflows.

## Architecture

```text
 Microphone / browser / terminal
                |
                v
  sounddevice capture -> Faster Whisper (local)
                |
                v
  reflex_router.py -- known command --> desktop / media / note action
          |
          +-- no match --> BGE-small ONNX intent dispatcher
                              | local intent --> local handler
                              ` passthrough --> agy / OpenCode
                                                   |
                                  streamed text -> Fish Audio -> pw-play
                |
                +--> SignalBus -> HTTP HUD on 127.0.0.1:8790
                |             -> signals/.voice_* files
                +--> /tmp/gideon-ui.sock -> Quickshell Orb
```

The two visualizer servers are separate launch options: `gideon.py` embeds the daemon HUD server; `ai-visualizer/server.py` runs the visualizer by itself. They use the same default port, so do not start both on port 8790 at once. See [the IPC contract](docs/ARCHITECTURE.md).

## Quick Start

### Requirements

- Linux and Python 3.10 or newer. The source uses Python 3.10 union type syntax.
- Python packages used by the daemon include `numpy`, `scipy`, `sounddevice`, `faster-whisper`, and `httpx`. The BGE-small intent dispatcher also uses `onnxruntime` and `transformers`, along with the local model and calibration files under `models/`. The `openwakeword` package enables wake phrase detection; if it is unavailable, the listener falls back to an energy trigger. Install the declared set with `python3 -m pip install -r requirements.txt`; versions are intentionally unpinned.
- A working microphone and audio input stack for voice capture. Gideon uses `sounddevice` at 16 kHz mono.
- PipeWire and `pw-play` for spoken output.
- A configured `agy` command for the default engine. The OpenCode option expects a running OpenCode service and its connection details in `~/.local/state/opencode/service.json`.
- CUDA is optional. The listener attempts CUDA `medium.en` INT8, then falls back to CPU `small.en` INT8. CPU recognition is slower.
- Omarchy and Hyprland are needed for the corresponding desktop actions; other Linux desktops can still use non-desktop parts, but those actions will not work.

The code does not invoke `ffmpeg`, `mpv`, or `socat` for the daemon's normal speech path. Media reflexes call `~/.local/bin/music`; install/configure that helper if you want those commands. `start_gideon.sh` is a systemd user-service wrapper and assumes `gideon.service` and `gideon-ui.service` are already installed; it is not a general installer.

### Run the daemon

After provisioning the dependencies and engine, run from the repository root:

```bash
python3 gideon.py
```

The daemon starts its local HUD at <http://127.0.0.1:8790/>. For a preconfigured machine with the expected systemd user units, `./start_gideon.sh` starts them and follows the daemon log when run in a terminal.

## Configuration

- `GIDEON_ENGINE=agy` selects the default engine; use `opencode` to select OpenCode. The command line accepts `--engine agy` or `--engine opencode`.
- Set `FISH_API_KEY` in the environment or in `~/.config/gideon/gideon.env` for Fish Audio. Gideon loads that file without replacing variables already in the environment. There is no default key; speech synthesis will fail without a valid credential.
- `GIDEON_NOTES_FILE` changes the destination used by the reflex router's quick-note command. Its default is `~/notes/daily_ledger.md`.
- `GIDEON_HEADPHONES_SINK` and `GIDEON_SPEAKERS_SINK` can set the sink names used by audio-output switching.
- The standalone visualizer reads optional `ai-visualizer/ai-visualizer.json`; see [`ai-visualizer/ai-visualizer.json.example`](ai-visualizer/ai-visualizer.json.example). Its `port` and `bus_dir` settings apply to that server, not the embedded daemon server.

## Usage

With the daemon running, say “Hey Jarvis” to start a hands-free turn, hold Space in the browser HUD while speaking, or submit a prompt through the HUD. If you launched Gideon in an interactive terminal, you can also type prompts there; enter `interrupt` to stop the active turn, or `engine agy` / `engine opencode` to switch engines. A systemd-managed daemon does not provide that interactive terminal input.

Examples handled by the deterministic reflex router include:

```text
Mute audio
Set volume to 30 percent
Switch to workspace 2
Play music by <artist>
Note this down: revisit the routing test
```

Window and workspace requests require Hyprland. Media requests require the local `music` helper and its configured backend. Notes are written to the configured notes path. Requests the reflex router does not recognize are sent to the selected conversational engine.

The study handlers are machine-specific integrations: they expect study scripts under `/home/cat/study` and use note locations under `/home/cat/notes` or a configured Obsidian vault. Those external paths and tools are not installed by this repository.

## The visualizer

The integrated HUD is served by `gideon.py` at `http://127.0.0.1:8790/`; it also accepts browser hold-to-talk and displays daemon state. It serves the board face at `/faces/board/` and exposes local prompt and control routes. The visualizer can also run standalone with the Python standard library; see [`ai-visualizer/README.md`](ai-visualizer/README.md) for commands and its file-based bus.

## Project layout

| Path | Purpose |
| --- | --- |
| `gideon.py` | Daemon entry point, voice pipeline, engines, signal bus, and embedded HTTP HUD. |
| `reflex_router.py` | Fast deterministic command parser and action dispatcher. |
| `gideon_handler.py`, `gideon_shared.py`, `handlers_*.py` | Intent handler table, shared command/result helpers, and system, Omarchy, and study handlers. |
| `intents_schema.json` | Intent and slot definitions for the handler system. |
| `engine_base.py`, `opencode_engine.py` | Engine interface and OpenCode backend. |
| `audio/`, `sounds/` | Wake/audio helpers and local sound assets. |
| `signals/` | Runtime state and waveform files used by the visualizer. |
| `ai-visualizer/` | Browser HUD faces and standalone static server. |
| `ui/` | Quickshell Orb and shader files. |
| `scripts/` | Intent data generation, calibration, training, and benchmarking utilities. |
| `tests/` | Python `unittest` suites and system validation script. |
| `docs/` | Operations notes, verification records, and [architecture/IPC details](docs/ARCHITECTURE.md). |

## Testing

The Python tests use the standard-library `unittest` runner. From the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Some integration paths depend on the local desktop, audio stack, or configured services; the test suite does not install those dependencies.

## Troubleshooting

- **No speech output:** confirm `pw-play` exists and PipeWire is running; check `FISH_API_KEY` and the Fish Audio network request.
- **Microphone does not open:** check Linux microphone permissions and whether `sounddevice` can see the input device. Browser hold-to-talk also needs microphone permission in the browser.
- **CUDA initialization fails:** Gideon attempts CPU `small.en` automatically. CPU fallback uses more time per transcription.
- **HUD does not load:** check whether port 8790 is already occupied. The standalone and embedded servers cannot both bind it. For the standalone server, use `python3 ai-visualizer/server.py --port 8791 --no-open`.
- **Desktop actions fail:** Omarchy actions require the `omarchy` command; window and workspace actions require a working Hyprland session and environment. A shell outside that session may not have its Hyprland runtime variables.
- **`start_gideon.sh` reports a missing unit:** the script expects the project's user-level systemd units to be installed separately.

## License and credits

Gideon is released under the **GNU Affero General Public License, version 3 or later (AGPL-3.0-or-later)**. See [`LICENSE`](LICENSE). You can run it, modify it, and build on it, including commercially. If you distribute it, or run a modified version as a service other people use, your version has to ship under the same license with its source available.

The visualizer in [`ai-visualizer/`](ai-visualizer/) is adapted from ai-visualizer by **Jared Rhodenizer** and is distributed under the same AGPL-3.0-or-later terms. Attribution, the upstream copyright notice, and the list of local modifications are recorded in [`ai-visualizer/NOTICE`](ai-visualizer/NOTICE); the visualizer's own copy of the license is in [`ai-visualizer/LICENSE`](ai-visualizer/LICENSE).

Fish Audio is an external paid service and is not part of this repository. Its terms apply separately to any audio you synthesize through it.
