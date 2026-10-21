# GIDEON & Antigravity Optimization Swarm: System Verification Rubric & Adversarial Protocol

**Version:** 1.0.0  
**Authority:** Dedicated System Verifier & Critic  
**Scope:** Host safety gate, audio stream preservation, VRAM ceiling management, spoken voice discipline enforcement, and user global preferences compliance.

---

## 1. Executive Mandate

During the multi-agent optimization session, five operational subagents investigate opportunities to streamline the Linux/Omarchy setup, GIDEON companion daemon, Antigravity CLI, and AI agent token efficiency. The **System Verifier & Critic** serves as an autonomous, adversarial safety gate. No configuration file, systemd service, compositor setting, or model parameter may be altered without satisfying all 5 verification pillars.

---

## 2. The Five Verification Pillars

### Pillar 1: Arch Linux / Omarchy / Hyprland Compositor Stability
* **P1.1 (Syntax & Parsing):** Any proposed edit to `~/.config/hypr/` or `~/.config/omarchy/` must undergo dry-run syntax verification before deployment.
* **P1.2 (Layer-Shell Integrity):** GIDEON's overlay orb runs as a Quickshell layer-shell window (`WlrLayershell.layer: Overlay`, namespace `gideon-orb`). Prohibited: any window rules, opacity overrides, or fullscreen layer modifications that occlude, steal focus from, or break click-through input masking (`Region { item: orbHitbox }`, `keyboardFocus: None`).
* **P1.3 (Desktop Entry Indexing):** Any newly registered script or application must follow `~/.gemini/config/AGENTS.md`:
  - Valid `.desktop` entry in `/usr/share/applications/` or `~/.local/share/applications/`.
  - Valid, rendered icon in launchers and docks.
  - Verified searchable indexing in Omarchy launcher.
* **P1.4 (Process Protection):** Zero killing or unauthorized restarts of critical desktop daemons (`hyprland`, `hyprpaper`, `waybar`, `quickshell`, `wireplumber`, `mako`/`dunst`, `polkit-kde-authentication-agent-1`).

### Pillar 2: PipeWire Audio Subsystem & GIDEON Zero-Gap Streaming
* **P2.1 (Daemon Inviolability):** PipeWire and WirePlumber services must NEVER be restarted during live user sessions. GIDEON relies on continuous real-time streaming to `pw-play -`.
* **P2.2 (Zero-Gap Pipeline Preservation):** GIDEON's concurrent producer-consumer audio queue (`TTSPlayer` with speculative `asyncio.create_task` HTTP synthesis and sequential `pw-play -` playback) must remain intact. Naive synchronous rewrites or sleep-based RIFF header parsers (JARVIS Bug 24) are forbidden.
* **P2.3 (Acoustic Echo & Hot-Mic Gate Immunity):**
  - Input audio chunks during active playback (`is_speaking == True`) must be discarded.
  - Mid-stream hot-mic arming (`hot_mic_checker`), hesitation window (0.85s silence, 35s max duration, 7.0s idle reversion), and fail-safe watchdog timer must be preserved (JARVIS Bug 46).
  - 0.35s blanking window after wake detection must be retained (JARVIS Bug 3).
* **P2.4 (Priority Intervention):** Preemption via `tts.interrupt()` must remain immediate (<20ms) without dropping the underlying headless NDJSON `agy` session.

### Pillar 3: GTX 1650 4GB VRAM Hard Ceiling (< 3.2 GB Budget)
* **P3.1 (Hard Budget Cap):** Total VRAM consumption must remain strictly below **3.2 GB** (leaving at least 800 MB reserved for Hyprland composition, visualizer WebGL canvases, and Whisper transient allocations).
* **P3.2 (Absolute Veto on Local LLMs):** Any proposal to spawn local GPU LLMs (Ollama, llama.cpp, Llama-3, Qwen-2.5, Mistral) on CUDA:0 alongside GIDEON is an **AUTOMATIC HARD VETO**.
* **P3.3 (Auxiliary Model Constraints):** Any sub-zero classifier (e.g. Lyra Tier-0 reflex router `laya_intent_v8`) must strictly run in fp16 or int8 with a validated footprint under 400 MiB, accompanied by explicit `torch.cuda.empty_cache()` hooks.
* **P3.4 (Defensive CUDA Handlers):** Faster-Whisper STT must retain fallback to CPU `small.en` int8 with dynamic `libcublas.so.12` symbol resolution (JARVIS Bug 17 & 23).

### Pillar 4: GIDEON Spoken Voice Discipline & Context Sandboxing
* **P4.1 (Direct Output):** Never emit opening pleasantries or filler words ("Hello!", "Hi, I am Gideon...", "Certainly!").
* **P4.2 (Silent Tool Execution):** Never emit spoken planning narration ("Let me check for...", "Running command now..."). Tools execute silently, followed only by the final result.
* **P4.3 (Pure Spoken Prose):** Zero markdown headings (`#`), asterisks (`**`), bullets (`-`, `*`), numbered lists (`1.`), backticks, code fences, or raw URLs. Spoken text must be natural, fluid conversational English.
* **P4.4 (Extreme Spoken Conciseness):** Spoken turns must be 1–2 punchy, highly informative sentences.
* **P4.5 (Global Rules Decoupling):** Rules intended for Antigravity developer CLI in `~/.gemini/config/AGENTS.md` must NOT bleed into GIDEON's headless NDJSON engine or prompt context.
* **P4.6 (Sanitization Defense):** `clean_text_for_tts()` must remain active to strip accidental formatting tokens before reaching Fish Audio (JARVIS Bug 44).

### Pillar 5: User Global Planning & Privilege Preferences
* **P5.1 (Exploration Before Execution):** Subagents must thoroughly explore, dissect, and document architectural tradeoffs and failure modes before proposing any implementation.
* **P5.2 (Mandatory `pkexec` Elevation):** Privileged system commands (package installations, root modifications) must use `pkexec` (e.g. `pkexec pacman -U ...`), never raw `sudo`, to seamlessly invoke the graphical polkit dialog in Omarchy/Hyprland.
* **P5.3 (Reversibility):** All proposed changes must specify an atomic rollback procedure.

---

## 3. Proposal Lifecycle & Gatekeeping Protocol

1. **Intake:** Subagent submits proposal.
2. **Invariant Scoring:** The proposal is evaluated against the 5 pillars.
3. **Adversarial Stress Testing:** Secondary ripple effects (latency jitter, memory leaks, focus-stealing) are evaluated.
4. **Verdict Issuance:**
   - **APPROVED:** Zero violations, clean rollback, pre-flight checks passed.
   - **CONDITIONAL REVISE:** Minor defect or missing rollback plan; returned for revision.
   - **HARD VETO:** Violates VRAM ceiling, breaks PipeWire, perturbs Wayland, or pollutes voice discipline.
