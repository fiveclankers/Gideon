# GIDEON: System-1 (Lyra) & System-2 (Antigravity) Architecture Blueprint

This document specifies the engineering design, process integration, and long-term capability roadmap for pairing **Lyra (Laya)** as a sub-zero reflex gate with **Google Antigravity (`agy`)** inside the Gideon autonomous companion daemon.

---

## 1. The Core Problem: The Monolithic Agent Fallacy

In the initial Gideon implementation, every spoken utterance and terminal prompt was routed directly into `agy.stdin`:

```
User Input ──> Whisper STT ──> Antigravity (agy) ──> Fish Audio TTS
                                [3.0s - 4.5s TTFT]
```

While Antigravity possesses general-purpose reasoning and autonomous tool capabilities, using it as a monolithic, single-tier brain introduces three fatal bottlenecks:
1. **The Latency Tax on Reflexes**: Asking to *"mute audio"*, *"pause music"*, or *"switch to workspace 3"* forces a 3.0s–4.5s reasoning round-trip for a 5ms OS operation.
2. **Session Context Bloat**: Trivial desktop commands clutter Antigravity's active session history, increasing prompt token overhead and degrading long-term pair-programming performance.
3. **Unfiltered Execution Risk**: Running headless `agy` with `--dangerously-skip-permissions` leaves no local semantic safety valve between Whisper disfluencies and arbitrary shell execution.

---

## 2. The Solution: The "Sub-Zero" Process Pipeline

Gideon decouples cognitive processing into a **bifurcated biological architecture**:
- **System-1 (Spinal Reflex)**: Local Lyra (`laya_intent_v8`) running on CUDA in fp16 (~15–25ms).
- **System-2 (Deep Deliberation)**: Headless Antigravity (`agy` NDJSON stream) handling complex coding, debugging, and multi-step plans (~3–5s).

```mermaid
flowchart TD
    Voice["User Voice / Keyboard Input"] --> STT["Faster-Whisper on CUDA (small.en)"]
    STT --> Transcribed["Transcribed Prompt String"]
    
    Transcribed --> LayaGate["Tier-0: Laya Sub-Zero Interceptor\n(~20ms / fp16 on GTX 1650)"]
    
    LayaGate -->|High Conf (>0.90) System Action / Reflex| LocalReflex["Local Fast-Path Executor (<30ms)\n(wpctl, hyprctl, omarchy, app launch)"]
    LocalReflex --> Earcon["Instant Audio Earcon / Chime\n(ack.wav or 1-word TTS)"]
    
    LayaGate -->|Flash Note / Retention| FlashNote["Obsidian Markdown Appender\n(/home/cat/notes/daily_ledger.md)"]
    FlashNote --> Earcon
    
    LayaGate -->|Hard Goal / Coding / Deep Reasoning| AGY["System-2: Antigravity Core (agy)\n(Headless bidirectional NDJSON stream)"]
    AGY --> TTSWorker["Fish Audio Streaming TTS Pipeline\n(pw-play zero-gap playback)"]
```

### 2.1 Mechanical Implementation Inside `gideon.py`

In `gideon.py`, prompt dispatching occurs in two places:
1. Web visualizer hold-to-talk: `/api/talk/stop` in `VisualizerHandler`.
2. Terminal console input: `stdin_reader.prompt_line()`.

Instead of directly calling `await engine.send_prompt(text)`, the prompt is intercepted by a lightweight asynchronous dispatcher:

```python
class SubZeroRouter:
    """Tier-0 Spinal Reflex Router powered by local Lyra checkpoint."""
    def __init__(self, checkpoint_path: Path):
        import laya
        self.agent = laya.load(str(checkpoint_path))
        self.agent.model.half()
        torch.cuda.empty_cache()

    def route_prompt(self, text: str) -> dict:
        return self.agent.predict(text, INTENT_QUESTION)


async def dispatch_user_prompt(text: str, engine, bus, tts, router: SubZeroRouter):
    """Sub-zero triage: executes fast local reflexes in <50ms or delegates to Antigravity."""
    # 1. Evaluate intent in 20ms on local GPU
    decision = await asyncio.to_thread(router.route_prompt, text)
    intent = decision.get("answers", {}).get("intent", {}).get("choice")
    conf = float(decision.get("answers", {}).get("intent", {}).get("confidence", 0.0))

    # Tier-0: High-Confidence System Reflex (Bypass Antigravity entirely)
    if conf > 0.90 and intent in ("system_action", "omarchy", "app_launch"):
        logger.info(f"[Tier-0 Reflex] {intent} ({conf:.2f}) -> {text}")
        handled = await execute_local_reflex(intent, text)
        if handled:
            tts.play_earcon("ack.wav")
            bus.set_state("idle")
            return

    # Tier-0: Instant Flash Note to Obsidian (Bypass Antigravity)
    if conf > 0.90 and intent == "study_retention" and is_flash_note(text):
        await append_to_obsidian_ledger(text)
        tts.play_earcon("ack.wav")
        bus.set_state("idle")
        return

    # Tier-1: Deep Cognitive Delegation (Hand off to Antigravity)
    logger.info(f"[Tier-1 Delegation] Handing off to Antigravity ({intent}, conf={conf:.2f})")
    await engine.send_prompt(text)
```

---

## 3. Strategic Training Staging: v8 vs. v9/v10

### 3.1 Why Train v8 on the 18,987-Row Corpus Right Now
* **Immediate Baseline Hardening**: The 18,987-row augmented dataset built overnight cures the 4 primary failure modes discovered during stress testing:
  1. *Target noun hijacking* (`"resume spotify"` $\rightarrow$ `app_launch`).
  2. *Spatial overshadowing* (`"on my screen"` $\rightarrow$ `omarchy`).
  3. *Temporal drift* (`"today"` $\rightarrow$ `weather`).
  4. *Code debugging domain collisions* (`"fix pipewire bug"` $\rightarrow$ `system_action`).
* **Zero Engineering Overhead**: The dataset strictly matches your existing Kaggle notebook `laya_intent_finetune.ipynb` byte-for-byte. You can upload and train immediately.
* **Separation of Concerns**: Training `v8` first gives you an authoritative, rock-solid **Intent Champion**. You establish an empirical benchmark before adding new decision heads.

### 3.2 Roadmap: v9 and v10 Multi-Head Frontiers

In Laya's architecture (`RLAgent`), decisions are not limited to 13 intents. In `v9` and `v10`, you can train specialized multi-head questions:

| Version | Focus Area | Question Schema | Responsibility |
| :--- | :--- | :--- | :--- |
| **v8** *(Current)* | **Intent & Reflex Champion** | 13-class `choice` (18,987 rows) | Separates fast OS actions from Antigravity goals. |
| **v9** | **Skill & Memory Gating** | Multi-question: `skill` (`choice`) + `needs_memory` (`noul`) | Pre-hippocampal gate: loads skills into context without Gemini thinking. |
| **v10** | **Advanced Document & Safety** | `doc_relevance` (`noul`) + `safety_severity` (`score` 1–5) | 1,000-page PDF sieving, semantic circuit breakers for bash commands. |

---

## 4. The 5 Advanced Frontier Workflows

### 4.1 The "Thousand-Page Needle Sieve"
* **Mechanics**: A 500-page PDF or technical manual is chunked into 500-token sections.
* **Evaluation**: Laya runs a batched forward pass across all chunks on local GPU (0 API tokens).
* **Question**: `{"relevance": {"type": "noul", "instructions": "Does this text contain latency benchmarks or kernel optimization flags?"}}`
* **Result**: 98% of sections are discarded locally. Only the top 2% of pages are passed to Antigravity for synthesis, avoiding context window degradation.

### 4.2 The Zero-Maintenance Obsidian Vault
* **Mechanics**: Every markdown file, web clipping, or note dropped into Obsidian passes through a Laya hook.
* **Multi-Question Triage**:
  1. `domain`: `[machine_learning, omarchy_linux, organic_chemistry, personal_journal]`
  2. `actionability`: `[permanent_note, reference_snippet, urgent_todo, fleeting_thought]`
* **Result**: Notes are auto-tagged and routed to the proper Map of Content (MOC) without manual YAML frontmatter entry.

### 4.3 Voice-to-Vault "Flash Ledger"
* **Mechanics**: Speaking *"note this down: ... "* to Gideon triggers an instant file append in local Python in $<200\text{ms}$, returning a confirmation chime without waking Antigravity.

### 4.4 Semantic Circuit Breaker for `--dangerously-skip-permissions`
* **Mechanics**: Intercepts Antigravity `tool_call: run_command` payloads before execution.
* **Evaluation**: Laya scores command risk (1 = safe query, 5 = destructive deletion). Commands $\ge 4$ trigger an audible voice confirmation dialog.

### 4.5 Multi-Query Contradiction & Deduplication Sieve
* **Mechanics**: Evaluates search query snippets against a rolling accumulator. Redundant paragraphs are dropped, passing only high-entropy, novel insights to Gemini.


---

## 5. Unified 15-Intent Master Schema Specification (v8 Execution)

To eliminate ambiguity between desktop reflexes, note capture, browser recipes, and deep agent goals, the schema is formalized into **15 mutually exclusive core actions**:

```python
INTENT_LABELS_15 = [
    "system_action",   # Local OS reflexes (volume, playback, workspaces, windows)
    "app_launch",      # Launch installed applications (kitty, chrome, obsidian)
    "omarchy",         # Hyprland themes, wallpapers, shortcuts, screenshots
    "flash_note",      # Hands-free capture of thoughts/formulas to Obsidian markdown
    "browser_recipe",  # Deterministic browser actions (Instagram DMs, form autofill)
    "study_retention", # Review due cards, revision queues, spaced repetition status
    "hard_goal",       # Multi-step coding, refactoring, building scripts, daemons (Antigravity)
    "research_task",   # Multi-query paper comparisons, architectural reviews (Antigravity)
    "web_search",      # Fast web searches and online queries
    "url_open",        # Direct web domain navigation (github.com, arxiv.org)
    "desktop_vision",  # Clicking or tapping on-screen visual targets
    "engine_switch",   # Dynamic cognitive engine switching
    "weather",         # Weather and temperature queries
    "skill_route",     # Explicit requests to activate an agent skill (diagnose-crash, anti-slop)
    "conversation",    # Pure chat, math explanations, definitions, theory, who are you
]
```

### Dataset Migration & Balancing Strategy
1. **Migration (Step 1)**: All 18,987 augmented rows from overnight are upgraded to the 15-intent question schema, maintaining baseline probabilities while splitting note-taking into `flash_note` and deterministic web scripts into `browser_recipe`.
2. **Targeted Synthesis (Step 2)**: Synthesizing ~3,200 new balanced instances:
   - `flash_note`: ~1,200 rows (formulas, exam notes, code memos, journal snippets).
   - `browser_recipe`: ~1,200 rows (Instagram DMs, form autocomplete, tab summaries).
   - `skill_route`: ~800 rows (systemd core dump diagnosis, anti-slop linters).
3. **Master Corpus**: Produces `data/train_gideon_v8_master.jsonl` (~22,200 total rows) with tightly controlled class distribution (~1,000–3,000 rows per intent) to prevent prior frequency distortion.
