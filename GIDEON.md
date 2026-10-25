---
name: gideon
description: >-
  Complete engineering manual, architectural blueprint, protocol specification, and operational runbook for GIDEON — the Antigravity-powered autonomous voice companion, 3D WebGL Circuit Board visualizer, and cognitive executive for Omarchy / Hyprland Linux.
---

# GIDEON: Antigravity Autonomous Companion Engineering Guide

This document is the authoritative engineering manual, operational runbook, and architectural map for **GIDEON** (Antigravity Autonomous Voice Companion). It details how the original agent-loop concept behind Gideon was adapted to run on **Google Antigravity (`agy`)**, how the Antigravity CLI was reversed to extract headless bidirectional NDJSON streaming, the diagnosis and elimination of audio streaming latency, the evaluation of internal CLI dictation vs. local CUDA STT, and the operating manual.

---

## 1. System Architecture & End-to-End Pipeline

Gideon is a single-process unified daemon (`gideon.py`) that coordinates four concurrent subsystems:
1. **Headless Antigravity Engine (`agy`)**: Running in bidirectional NDJSON mode with low reasoning effort.
2. **Asynchronous Audio Pipeline**: Decoupled sentence pre-fetching on Fish Audio Cloud TTS (`s2.1-pro-free`) with zero-gap PipeWire playback (`pw-play -`).
3. **Local CUDA STT**: Faster-Whisper (`medium.en`) on CUDA int8 executing with specialized vocabulary conditioning prompt (`initial_prompt`) and real-time waveform extraction.
4. **Integrated Visualizer Server**: Built-in HTTP server on port 8790 driving the 3D procedural WebGL Circuit Board (`ai-visualizer/faces/board/`) with in-browser Spacebar hold-to-talk.

```mermaid
flowchart TD
    subgraph UI ["User Input & Visual Interface"]
        KB["In-Browser Spacebar / HUD Button"] -->|Hold-to-Talk| MicRec["AudioStreamRecorder\n(16kHz Mono PCM via sounddevice)"]
        Term["Terminal Keyboard"] -->|ENTER or Direct Text| StdinR["Async StdinReader"]
        Vis["3D Circuit Board Visualizer\n(WebGL Canvas on :8790/faces/board/)"]
    end

    subgraph STT ["Speech-to-Text Subsystem"]
        MicRec -->|PCM Audio Buffer| FW["Faster-Whisper on CUDA\n(medium.en, int8, beam_size=1, initial_prompt)"]
        FW -->|Transcribed Prompt| Dispatcher["Prompt Dispatcher"]
        StdinR -->|User Prompt| Dispatcher
    end

    subgraph Core ["Cognitive Core (Antigravity Harness)"]
        Dispatcher -->|Priority Intervention\n(Cut off current audio)| TTSInt["TTS Interrupt"]
        Dispatcher -->|NDJSON Event: user\nvia stdin| AGY["Antigravity CLI Subprocess\n(agy --input-format stream-json --output-format stream-json --effort low)"]
        
        AGY -->|NDJSON stdout| NDJSON["NDJSON Parser Loop"]
        NDJSON -->|step_type: agent_response\ntext_delta| SentSplit["Sentence Boundary Splitter\n([.!?] regex)"]
        NDJSON -->|step_type: tool_call| WDContext["Update Active Tool"]
        
        Watchdog["45s Watchdog Timer"] -->|Calm check-in if tool running| SynthQ
    end

    subgraph AudioPipe ["Zero-Gap Pipelined TTS Pipeline"]
        SentSplit -->|Sentence Chunk N| SynthQ["Background Synthesis Task\n(asyncio.create_task)"]
        SynthQ -->|HTTP POST\n~1.5s latency| Fish["Fish Audio Cloud API\n(s2.1-pro-free / low latency)"]
        SynthQ -->|Enqueue (Sentence, Task)| PlayQ["Async Playback Queue\n(asyncio.Queue)"]
        
        PlayQ -->|Sequential Audio Worker| PlayWorker["_playback_loop"]
        Fish -->|WAV Bytes| PlayWorker
        PlayWorker -->|Pipe Audio Buffer| PW["PipeWire (pw-play -)"]
        PlayWorker -->|64-Band Waveform| SigBus["SignalBus (.voice_waveform & .voice_state)"]
    end

    SigBus -->|GET /state HTTP Poll (120ms)| Vis
```

---

## 2. Reverse Engineering the Antigravity CLI Harness

### 2.1 Headless Bidirectional NDJSON Execution

Antigravity (`agy`) typically runs as an interactive Bubbletea terminal UI. To embed it seamlessly inside an autonomous voice loop without a pseudo-terminal (PTY) wrapper or terminal scraping, Gideon launches `agy` in headless NDJSON stream mode:

```bash
agy --input-format stream-json \
    --output-format stream-json \
    --dangerously-skip-permissions \
    --effort low \
    -p ""
```

#### Flags Explained:
- `--input-format stream-json`: Tells the Go CLI runtime to accept JSON event payloads on `stdin` terminated by newlines.
- `--output-format stream-json`: Instructs the agentic harness to emit structured events on `stdout` as JSON Lines.
- `--dangerously-skip-permissions`: Suppresses interactive CLI confirmation dialogs for file read/write, bash execution, and MCP tool invocations, allowing autonomous background tool execution.
- `--effort low`: Reduces the internal reasoning token budget. This drops the Time-to-First-Token (TTFT) from **4.40s down to 3.01s** (saving ~1.4 seconds before Gideon starts speaking).
- `-p ""`: Suppresses the interactive TUI prompt and immediately connects `stdin`/`stdout` pipes to the NDJSON protocol handler.

### 2.2 Wire Protocol Specification

#### Inbound (stdin -> agy):
To send a prompt or instruction into an active session:
```json
{"event": "user", "message": {"content": "Check system uptime and summarize."}}
```

#### Outbound (agy -> stdout):
1. **Session Initialized (`init`)**:
   ```json
   {"event": "init", "session_id": "...", "model": "gemini-..."}
   ```
2. **Text Generation Delta (`step_update` / `agent_response`)**:
   Emitted as tokens are generated in real time:
   ```json
   {
     "event": "step_update",
     "step_update": {
       "step_type": "agent_response",
       "text_delta": "Checking the system status now."
     }
   }
   ```
3. **Tool Call Invocation (`step_update` / `tool_call`)**:
   Emitted when the agent decides to invoke an environment tool:
   ```json
   {
     "event": "step_update",
     "step_update": {
       "step_type": "tool_call",
       "tool_name": "run_command",
       "tool_args": {"CommandLine": "uptime"}
     }
   }
   ```
4. **Turn Finalized (`result`)**:
   Emitted when all reasoning, tool execution, and responses for the turn have concluded:
   ```json
   {"event": "result", "status": "success"}
   ```

### 2.3 Priority Intervention vs. SIGINT

Traditional voice agents terminate in-flight generation by sending `SIGINT` (Ctrl+C). In Antigravity, sending `SIGINT` disrupts the CLI subprocess, breaks active session states, and can leave locks on resources.

**Gideon's Priority Intervention Approach:**
1. **Audio Preemption:** Gideon instantly terminates the local audio playback process (`pw-play`) and drains the queued audio sentences via `tts.interrupt()`.
2. **Stream Injection:** Gideon immediately pushes the new user instruction as an in-flight `{"event": "user", ...}` payload into `agy.stdin`.
3. **Continuous Context:** The Antigravity cognitive core ingests the new instruction into its active context without process restarting or loss of conversation memory.

---

## 3. Audio Latency Diagnosis & The Zero-Gap Pipeline

### 3.1 The Root Cause of "Broken & Slow" Streaming

During early testing, voice responses exhibited 2–3 seconds of dead air between sentences. Profiling the stack revealed the breakdown:

| Operation | Typical Latency | Notes |
| :--- | :--- | :--- |
| **Antigravity TTFT (standard)** | `4.40s` | High reasoning token budget before first sentence |
| **Antigravity TTFT (`--effort low`)** | `3.01s` | **1.39s faster** response onset |
| **Fish Audio HTTP Roundtrip** | `1.47s - 2.23s` | Cloud neural TTS synthesis per sentence |
| **Sentence Playback Duration** | `3.0s - 5.5s` | Normal spoken duration of an 8–15 word sentence |

#### The Serial Blocking Flaw:
The original naive loop in `gideon.py` was structured synchronously:
```python
# FLAWED SYNCHRONOUS CODE:
sentence = extract_sentence(sentence_buffer)
wav = await tts.synthesize(sentence)   # Waited 1.8s
await tts.play_wav(wav)                # Waited 4.0s (FROZE STDOUT READING!)
```
While Sentence 1 played for 4 seconds, the reader loop was frozen. Although Antigravity had already emitted Sentence 2, it sat unread in the pipe buffer. Only *after* Sentence 1 finished did the reader wake up, read Sentence 2, send the HTTP request, and wait another 1.8s for Fish Audio. The user heard:
`Sentence 1 ... [2.0s DEAD AIR] ... Sentence 2 ... [2.0s DEAD AIR] ... Sentence 3`

### 3.2 The Zero-Gap Concurrent Pipeline Solution

To completely eliminate dead air, Gideon implements an asynchronous producer-consumer queue with speculative pre-fetching:

```python
class TTSPlayer:
    def __init__(self, bus: SignalBus):
        self._queue = asyncio.Queue()
        self._current_synth_tasks = set()
        self._current_proc = None

    def queue_sentence(self, text: str):
        # 1. Start HTTP fetch IMMEDIATELY in the background
        synth_task = asyncio.create_task(self.synthesize(text))
        self._current_synth_tasks.add(synth_task)
        synth_task.add_done_callback(lambda t: self._current_synth_tasks.discard(t))
        # 2. Put future into playback queue
        self._queue.put_nowait((text, synth_task))

    async def _playback_loop(self):
        while True:
            text, synth_task = await self._queue.get()
            try:
                # If synthesized during previous sentence, wav is ready in 0ms!
                wav = await synth_task
                if wav:
                    await self.play_wav(wav)
            finally:
                self._queue.task_done()
```

#### Pipeline Timing Diagram:
```
t = 0.0s:  agy outputs Sentence 1 -> synthesize(Sentence 1) dispatches
t = 1.6s:  Sentence 1 WAV arrives  -> pw-play begins playing (duration: 4.0s, ends t=5.6s)
t = 2.0s:  agy outputs Sentence 2 -> synthesize(Sentence 2) dispatches concurrently
t = 3.6s:  Sentence 2 WAV arrives  -> sits ready in memory (Sentence 1 still playing!)
t = 5.6s:  Sentence 1 finishes     -> pw-play launches Sentence 2 INSTANTLY (0ms GAP!)
```

---

## 4. Voice Input Engineering: Antigravity F5 Dictation vs. Local CUDA STT

### 4.1 Internal Antigravity CLI Dictation (F5) Analysis

Disassembly and inspection of the `agy` Go binary revealed:
- `agy` contains a built-in voice dictation subsystem (`mic-serve`, `BidiGenerateContentRealtimeInput`, `audio: sending audio for transcription failed`).
- Pressing `F5` triggers an internal audio capture loop that streams PCM chunks directly to Google's Cloud Gemini BiDi WebSockets endpoint.

### 4.2 Engineering Verdict: Why Local CUDA `medium.en` with Conditioning Prompt is Superior

| Evaluation Dimension | Native `agy` F5 Dictation | Local CUDA Faster-Whisper (`medium.en` int8) | Winner |
| :--- | :--- | :--- | :--- |
| **Execution Mode** | Bound to Bubbletea interactive TUI | Headless daemon compatible (any HTTP/terminal/wake event) | **CUDA Whisper** |
| **Accuracy & Domain Vocabulary** | Prone to phonetic drift on uncommon terms | Primed via `initial_prompt` (zero drift on Euphonium, Antigravity, Hyprland) | **CUDA Whisper** |
| **Latency** | Network roundtrip to Google Bidi API | **<500ms** on local GTX 1650 with greedy decoding (`beam_size=1`) | **CUDA Whisper** |
| **Waveform Visualization** | Opaque (audio sent directly to cloud) | Full access to raw PCM for 64-band visualizer FFT | **CUDA Whisper** |
| **VRAM Footprint** | N/A (Cloud) | **~1.5 GB VRAM** with `compute_type="int8"` (half the footprint of float16) | **CUDA Whisper** |
| **Offline Reliability** | Fails if network or Google auth drops | 100% offline local CUDA tensor execution | **CUDA Whisper** |

**Whisper Conditioning Prompt Mechanism:**
In Faster-Whisper, `initial_prompt` acts as an autoregressive prefix token sequence injected into the text decoder cross-attention layers. Rather than predicting words from a generic conversational distribution (which misidentifies technical terms like *euphonium* as *eucalyptus*), the decoder's prior probabilities are biased toward our exact domain vocabulary while strictly requiring matching acoustic evidence from the speech encoder. Coupled with `vad_filter=True` (`min_silence_duration_ms=300`), room fan rumble and quiet pauses never hallucinate text.

---

## 5. Visualizer & UI Architecture

### 5.1 Circuit Board Face

Gideon ships a procedural WebGL Circuit Board face (`ai-visualizer/faces/board/`), served directly by `gideon.py` on `http://127.0.0.1:8790/`:

1. **Integrated Server:** No external Python server or Node.js process is required. `gideon.py` embeds `ThreadingHTTPServer` to serve static assets and handle JSON API routes.
2. **Signal Bus Protocol:**
   - `/state`: Returns `{"state": "idle|listening|thinking|speaking", "level": float, "samples": [float*64]}`.
   - Dynamic 64-band waveforms are synthesized during audio playback to drive the circuit pulse animations.
3. **In-Browser Push-to-Talk:**
   - In `core.js`, a global `keydown`/`keyup` listener intercepts the **Spacebar** (while not in text fields) and triggers `POST /api/talk/start` and `POST /api/talk/stop`.
   - In `faces/board/index.html`, an interactive glowing HUD button (`#gideon-talk-btn`) provides real-time state feedback:
     - Default: `🎙️ HOLD SPACE TO TALK` (Cyan glow)
     - Held: `🔴 LISTENING... (RELEASE TO SEND)` (Red neon glow)
     - Releasing: `⚡ PROCESSING SPEECH...` (Amber glow)

---

## 6. Operational Runbook

### 6.1 File Locations
- Core Daemon: `/home/cat/gideon/gideon.py`
- Shell Launcher: `/home/cat/gideon/start_gideon.sh`
- Desktop Application: `~/.local/share/applications/gideon.desktop`
- Visualizer Assets: `/home/cat/gideon/ai-visualizer/`
- Circuit Board Face: `http://127.0.0.1:8790/faces/board/`
- Python Environment: `/home/cat/jarvis/.venv/bin/python`

### 6.2 Launching Gideon
Launch from terminal:
```bash
~/gideon/start_gideon.sh
```
Or launch via Omarchy Application Launcher: Search for **Gideon**.

### 6.3 Interacting with Gideon
- **In-Browser:** Open `http://127.0.0.1:8790/faces/board/` and hold **Spacebar** or click-and-hold the HUD button to speak.
- **In Terminal:** Press **ENTER** to record a 5-second microphone snippet, or type your query directly.
- **Priority Intervention:** Type or speak while Gideon is working or speaking to steer the agent immediately without killing the process.
- **Watchdog:** If a tool call runs longer than 45 seconds, Gideon automatically speaks a calm status check-in (`"Still running <tool>, 45 seconds in."`).
- **Engine Switching:** Type `engine opencode` or `engine agy` to dynamically switch execution brains at runtime.

---

## 7. Barehands Spatial Interface & Gesture Subsystem

Gideon integrates the **Barehands** webcam-driven 3D glass stage (`/home/cat/barehands/`), allowing tactile spatial manipulation of notes, images, and 3D GLB/GLTF models alongside real-time voice interaction.

### 7.1 Architecture & IPC Bridge

```
[ Gideon Daemon (:8790) ]
   ├── SignalBus ──> /home/cat/barehands/state/ (state, mood.json, wave.json)
   └── HTTP Server ──> /api/talk/start & /api/talk/stop (Faster-Whisper on CUDA)
         ▲
         │ (HTTP Proxy / Direct Fetch)
[ Barehands Server (:8794) & Chrome Stage ]
   ├── Google MediaPipe Tasks-Vision (Hand Tracking on Webcam / Iriun)
   ├── Three.js WebGL Stage (Glass cards, models, holograms, props airlock)
   └── Interactive Core Ring (Visual presence + Pinch-to-Talk)
```

1. **State & Audio Waveform Mirroring**: `SignalBus` in `gideon.py` writes live states (`idle`, `listening`, `thinking`, `speaking`) and 64-band FFT audio samples into `/home/cat/barehands/state/`. The floating Barehands ring pulses, glows, and animates its audio equalizer in real-time sync with Fish Audio playback.
2. **Unified Proxy**: `barehands/server.py` proxies `/api/talk/start` and `/api/talk/stop` requests directly to Gideon on port `8790`.
3. **Headless Daemon Support**: `StdinReader` in `gideon.py` checks `sys.stdin.isatty()` to support background/detached execution without terminating on `EOF`.

### 7.2 Natural Hand Gesture Calibration

The original Barehands rules were over-fitted to one specific hand geometry. Gideon applies the **Natural Hand Adaptation** in `stage.html`:

| Subsystem | Original Metric | Natural Adaptation | Rationale |
| :--- | :--- | :--- | :--- |
| **Pinch Trigger** | Rigid OK sign (`backMean > 1.30`, delta `> 0.18`) | `backMean > 1.05`, delta `> 0.08` + Direct Touch (`ratio < 0.28`) | Accepts naturally relaxed, curved fingers without demanding hyperextended back fingers. |
| **Pinch Gap Ceiling** | `0.32` frontal / `0.38` profile | `0.36` frontal / `0.42` profile | Avoids dropped pinches due to camera perspective distortion. |
| **Probation Gate** | Drops on 4 bad frames, sets `probKill` | 8 bad frames tolerance, no premature `probKill` | Prevents webcam frame jitter from canceling note taps. |
| **Tap Discrimination** | `tMs < 300ms`, `tTrav < 26px` | `tMs < 450ms`, `tTrav < 45px` | Normal human pinch-and-release reliably unfurls notes and blooms orbs. |
| **3D Rotate Latch** | `mdrift > 45px` resets 1.0s timer | `mdrift > 85px` tolerance, `750ms` hold | Tolerates sensor noise; latches into 3D rotation smoothly. |
| **Claw & Force-Pull** | 11 inequalities incl. straight pinky (`c20 > 0.1`) + 900ms flash | Multi-tier hysteresis (`isAiming ? 0.95 : 0.72`), cone widening (`proj * 0.55`), 350ms lock grace period | Prevents mid-pull tracking jitter from resetting the strain ramp. |
| **Force-Pull Strain** | Must hold shaking for 2000ms | `800ms` strain with live radial progress ring (`--p` & `--fp-p`) | Immediate visual feedback around target and cursor; crisp snappy grab. |
| **Backdrop Contrast** | Low-alpha tint washed out against light clothing | Deep alpha (`rgba(8, 28, 26, 0.84)`), 16–18px blur, saturated backdrop, multi-layer halo text-shadow | Crystal clear typography regardless of room lighting or clothing color. |
| **HUD Collision** | Pinned NOTES/PROPS badges overlap reading area | Active pane detector (`hasActivePane`), pushes orbs to `R + 240px` and activates `.hud-dimmed` | Zero text occlusion; orbs fade to 12% opacity and slide out of read zone. |

### 7.3 Voice Control Integration

* **Deliberate Ring-Hold**: Pinching the floating Assistant Ring engages a `320ms` hold timer before opening the microphone (`🎙️ GIDEON: Listening...`). Quick taps (<320ms) bloom/collapse orbs without triggering audio recording; moving the ring avoids microphone chatter.
* **Spacebar Hold-to-Talk**: Dedicated keyboard push-to-talk listener in `stage.html`.
* **Stage Presenter Tools**: Antigravity agents can project cards and 3D assets to the user's board via `/home/cat/barehands/bin/board.sh`.

### 7.4 Launching & Operations

* **Launcher Script**: `/home/cat/barehands/launch_ui.sh`
  * Automatically boots Gideon daemon on port `8790` (if not running) and polls readiness.
  * Starts Barehands server on port `8794`.
  * Launches Chrome in dedicated App Mode with 720p tracking: `--app="http://127.0.0.1:8794/stage.html?res=1280x720"`.
* **Desktop Entry**: `~/.local/share/applications/barehands.desktop` (accessible via Omarchy / Hyprland application menu).
* **3D Props Airlock**: Textured models reside in `media/models/` (`DamagedHelmet.glb`, `BoomBox.glb`), holographic wireframes in `media/holo/`, and visual cards in `sample-notes/`.

---

## 8. Quickshell 3D Plasma Orb Visual Shell (`ui/`)

GIDEON features a dedicated, native Wayland layer-shell 3D GPU plasma orb overlay extricated from JARVIS, running in `/home/cat/gideon/ui/` with a custom **Boreal Pine & Bioluminescent Canopy** aesthetic.

### 8.1 Key Features & Architecture
1. **Visual Theme**: Deep Nordic spruce/pine shadow base (`#0e3d2f`, `#092218`, `#03130d`), vibrant emerald listening pulse (`#10b981`), pale lichen thinking reticle (`#6ee7b7`), and neon mint vocal aurora (`#34d399`).
2. **Strict Tool-Call Binding**: When an NDJSON `tool_call` occurs, the orb immediately morphs into an **Amber Canopy** palette (`#d97706`, `#15803d`, `#451a03`, accent `#fbbf24`), displaying the active tool name and target (e.g., `run_command: uptime`) without relying on engine polling or process heuristics.
3. **Click-Through Desktop Overlay**:
   - The input mask is empty (`mask: Region {}`), so the orb overlay never captures pointer input and the whole desktop stays 100% click-through.
   - Zero keyboard grabbing: `WlrLayershell.keyboardFocus: WlrKeyboardFocus.None` guarantees no spacebar or desktop hotkey hijacking.
   - The WebGL Circuit Board remains available directly at `http://localhost:8790/faces/board/`.
4. **IPC Bridge**:
   - Unix Domain Socket: `/tmp/gideon-ui.sock`.
   - Streams `state` (with dynamic tool detail), real-time scalar `rms` from microphone and PipeWire audio buffers, and live subtitle transcripts (`speaker: "user" | "gideon"`).
5. **Supervision**: Managed by `~/.config/systemd/user/gideon-ui.service` and started idempotently by `start_gideon.sh`. Standalone previewing and testing is supported via `ui/fake_daemon.py`.

---

## 9. Architectural Hardening & Acoustic Bug Immunity (The JARVIS Audit)

When extending GIDEON, the following 9 solved failure modes and operational safeguards must strictly be preserved:

### 9.1 Talk Buffer Zone: Dynamic Arming, Conversational Thought Drift & Idle Reversion (JARVIS Bug 46 & Hot-Mic Protocol)
* **Problem**: 
  1. **Perpetual Open-Mic Freeze**: When `arm_hot_mic()` was invoked at turn completion, `AudioListener.listen_for_command` was already running in wake-word detection mode (`is_hot_mic=False`). Because `hot_mic_checker` was not passed, the audio loop remained blocked waiting for the wake word, never timed out, and left the 3D Orb permanently trapped in the `"hotmic"` visual state.
  2. **Conversational Thought Drift Premature Cutoff**: When speaking long contemplative thoughts, rambling, or pausing briefly to formulate ideas, a hardcoded `max_duration_s = 12.0s` abruptly severed the user's speech mid-sentence (`Max recording duration reached`), while an aggressive `0.55s` silence cutoff truncated natural hesitation pauses.
* **Fix**:
  1. **Dynamic Mid-Stream Arming**: Passed `hot_mic_checker=self.is_hot_mic_active` and `hot_mic_timeout=self.hot_mic_timeout` directly into `AudioListener.listen_for_command`. On the very next 80ms chunk, the stream dynamically recognizes the armed follow-up window.
  2. **Extended Conversational Thought Drift Limit (35s)**: Pushed `max_duration_s` from `12.0s` to `35.0s`. Users can explore complex thoughts, explain deep philosophies, and speak continuously for up to 35 seconds without being cut off mid-thought.
  3. **Natural Breath & Hesitation Window (0.85s)**: Calibrated trailing silence timeout to `silence_timeout_s = 0.85s`. Brief pauses for breath or mid-sentence contemplation do not terminate capture, while finishing speech remains crisp, responsive, and seamless (<1s to AI response).
  4. **Adaptive Idle Reversion on Silence (7.0s)**: If no speech occurs within 7.0 seconds of silence following a turn, the hot-mic window expires, `self.disarm_hot_mic()` is invoked, and the 3D Orb smoothly glides back to idle standby corner docking.
  5. **Fail-Safe Watchdog Timer**: A dedicated `threading.Timer(timeout_s + 0.15)` guarantees that even if the audio device is delayed on a read block, the UI state unconditionally reverts to `"idle"`.
  6. **Acoustic Echo Immunity**: Microphone chunks during active TTS (`is_speaking == True`) are discarded, and the hot-mic window countdown starts only *after* speech playback concludes.

### 9.2 Acoustic Barge-in Interruption
* **Problem**: When the agent is speaking a long response, the user cannot interrupt by voice without manually typing or killing the process.
* **Fix**: `AudioListener` detects the wake word or speech energy onset during active playback and fires `on_barge_in`. This immediately calls `tts.interrupt()`, stopping `pw-play` and clearing the playback queue in <20ms.

### 9.3 Natural Conversational Sensitivity & Mechanical Transient Rejection (JARVIS Bug 16 & Mic Calibration)
* **Problem**: 
  1. **Excessive Speech Floor (Forced Shouting)**: A hardcoded minimum threshold of `0.024` RMS (11× higher than actual room ambient `0.0022`) forced the user to shout to trigger voice capture. Shouting introduced microphone clipping and plosive air blast distortion, degrading Faster-Whisper transcription accuracy.
  2. **High Wake Gate**: `wake_threshold=0.30` and low gain `mic_gain=1.2` required loud vocal projection to register *"Hey Jarvis"*.
* **Fix**:
  1. **Calibrated Thresholds**: Lowered speech onset to `speech_threshold = max(0.008, ambient_floor * 2.2)` and trailing silence to `silence_threshold = max(0.004, ambient_floor * 1.4)`. Normal, relaxed conversational speech triggers onset effortlessly.
  2. **Software Pre-amp Gain**: Increased `mic_gain` to `1.8x` (software pre-amp applied in float domain with hard clipping protection), matching the optimal input headroom for both openWakeWord and Faster-Whisper.
  3. **Relaxed Wake Word Gate**: Set `wake_threshold=0.20`, allowing casual, natural conversational volume to activate the wake word cleanly.
  4. **Transient Filtering**: Retained the 2nd-order Butterworth bandpass (200Hz–3800Hz) and the **2 consecutive chunk requirement** (~160ms of sustained voice-band energy) so mechanical keyboard clicks and desk taps are still completely rejected.

### 9.4 Trailing Syllable Leaking & Blanking Window (JARVIS Bug 3)
* **Problem**: The trailing syllable "-vis" of the wake word lingered into the command buffer, causing the natural 0.4s pause before speaking to be treated as speech end (premature cut-off).
* **Fix**: Enforces a strict **0.35s blanking window** after wake detection to flush reverberation, plus a minimum `0.40s` speech duration before trailing silence can close the audio buffer.

### 9.5 CUDA Out-of-Memory Protection & Automatic CPU Fallback (JARVIS Bug 17 & 23)
* **Problem**: Multi-turn Faster-Whisper on 4GB VRAM GPUs (GTX 1650) can trigger CUDA OOM errors. Missing cuBLAS paths after removing PyTorch dependencies caused silent STT failures.
* **Fix**: 
  1. `_preload_cuda_libs()` dynamically resolves `libcublas.so.12` with `RTLD_GLOBAL` at daemon startup.
  2. Faster-Whisper is configured with pure `compute_type="int8"`, reducing VRAM usage to ~1.5GB.
  3. `VoiceListener.transcribe_audio()` wraps inference in a defensive CUDA handler that purges `torch.cuda.empty_cache()` and falls back to CPU `small.en` int8 without dropping the conversational turn.

### 9.6 Corrupt WAV Duration Immunity (JARVIS Bug 24)
* **Problem**: Fish Audio TTS returns WAV headers with `nframes = 0x7FFFFF00` (~89478s). Sleep-based subtitle/playback loops freeze in `speaking` state for hours.
* **Fix**: GIDEON pipes raw audio bytes directly into `pw-play -` via `subprocess.Popen` and awaits `proc.communicate(wav_bytes)`. The playback lifecycle is strictly bounded by PipeWire's hardware stream completion, completely immune to bogus RIFF headers.

### 9.7 Quickshell Wayland Socket Deadlock (JARVIS Bug 25)
* **Problem**: Toggling `Socket.connected` in-place on a stalled `QLocalSocket` silently no-ops in Quickshell when the daemon is restarted.
* **Fix**: `ui/shell.qml` wraps the socket in a two-phase `Loader` cycle (`socketAlive` / `socketResetArmed`). Phase 1 destroys the socket object entirely; Phase 2 instantiates a fresh `QLocalSocket`. Proof-of-life `lastMsgMs` automatically triggers a reconnect cycle if the daemon is silent for >6s.

### 9.8 Neural TTS Error & Syntax Sanitization (JARVIS Bug 44)
* **Problem**: Passing raw markdown headers (`###`), HTML tags (`<div ...>`), or em-dashes (`—`) to neural cloud TTS causes speech stuttering, awkward literal token vocalizations, or API failures.
* **Fix**: `clean_text_for_tts()` strips markdown headers, blockquotes, HTML tags, backticks, and normalizes em-dashes into natural comma pauses before queuing for synthesis.

### 9.9 Standby Corner Docking & Click-Through Masking (JARVIS Bug 8 & 19)
* **Problem**: Setting opacity to 0 leaves users unaware if the daemon is alive, while leaving a full-screen window intercepts clicks and blurs the entire desktop.
* **Fix**: 
  - Standby Dock: When `idle`, the orb shrinks to `scale: 0.44`, fades to `opacity: 0.30`, and translates into the top-right corner (`slideX: 68`, `slideY: -10`).
  - Fully Click-Through: `mask: Region {}` leaves the overlay non-interactive; `WlrLayershell.keyboardFocus: WlrKeyboardFocus.None` guarantees zero spacebar/keyboard capture.

---

## 10. Operations & Diagnostics Runbook

### Service Control
```bash
# Check status of GIDEON UI overlay
systemctl --user status gideon-ui.service

# Check status of GIDEON cognitive daemon
systemctl --user status gideon.service

# Restart UI overlay cleanly
systemctl --user restart gideon-ui.service

# Restart complete GIDEON stack
~/gideon/start_gideon.sh --restart
```

### Inspect IPC Sockets & Logs
```bash
# Verify active Unix domain socket ESTAB pair
ss -x -a | grep gideon-ui

# Inspect Quickshell UI overlay logs
journalctl --user -u gideon-ui.service -f

# Inspect Daemon logs
journalctl --user -u gideon.service -f
# Or check active task/launcher logs in ~/gideon/
```


