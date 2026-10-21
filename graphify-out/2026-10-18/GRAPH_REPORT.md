# Graph Report - gideon  (2026-10-18)

## Corpus Check
- 45 files · ~60,533 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 25 file(s) not represented in the graph (top: .jsonl 7, (none) 6, .log 3)

## Summary
- 575 nodes · 1055 edges · 37 communities (17 shown, 20 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 109 edges (avg confidence: 0.89)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- gideon.py
- SignalBus
- try_reflex
- Engine Interface & TTS Tests
- GIDEON Antigravity Autonomous Voice Companion
- WakeWordListener
- Antigravity Engine Harness
- Pipelined TTS Player
- Wake Word & Hot Mic
- TTSPlayer
- Visualizer Faces & Docs
- Visualizer HTTP Server
- parse_command
- Fake Daemon Test Harness
- typing
- Post-Deploy Validation
- Rain Face Renderer
- Visualizer Update Script
- Visualizer Core JS
- Board Face Builders
- Neural Face Renderer
- Radial Face Renderer
- Visualizer Run Script
- Gideon Launch Script
- Board Face Renderer
- Board Control Handler
- Board Frame Loop
- Board Control Message Handler
- Board Control Message Handler
- time
- TTSPlayer
- benchmark_voice_pipeline.py
- main
- random
- Handler
- VoiceListener
- AudioStreamRecorder

## God Nodes (most connected - your core abstractions)
1. `parse_command()` - 36 edges
2. `try_reflex()` - 36 edges
3. `SignalBus` - 25 edges
4. `main()` - 24 edges
5. `OpenCodeEngine` - 23 edges
6. `TTSPlayer` - 19 edges
7. `TestLiveTTSSanitizer` - 15 edges
8. `_run_live_watchdog()` - 15 edges
9. `_LiveWatchdogMixin` - 15 edges
10. `AntigravityEngine` - 13 edges

## Surprising Connections (you probably didn't know these)
- `ai-visualizer Voice Signal Bus (.voice_state / .voice_waveform / .voice_loading_pid)` --shares_data_with--> `SignalBus`  [INFERRED]
  ai-visualizer/README.md → gideon.py
- `U5 — Graph-driven change-impact gate in CI` --references--> `SignalBus`  [INFERRED]
  docs/research_audit_vram_vectors.md → gideon.py
- `1.3 Structural findings (highest-leverage, found before any new feature)` --references--> `TTSPlayer`  [INFERRED]
  docs/research_audit_vram_vectors.md → gideon.py
- `U5 — Graph-driven change-impact gate in CI` --references--> `TTSPlayer`  [INFERRED]
  docs/research_audit_vram_vectors.md → gideon.py
- `Failure modes → safeguards` --references--> `clean_text_for_tts()`  [INFERRED]
  docs/research_audit_vram_vectors.md → gideon.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **ai-visualizer Faces on the Shared Voice Signal Bus** — ai_visualizer_faces_board_index_circuit_board, ai_visualizer_faces_radial_index_radial_face, ai_visualizer_faces_rain_index_face_in_code, ai_visualizer_faces_neural_index_neural_core, ai_visualizer_signal_bus [INFERRED 0.85]
- **GIDEON Concurrent Voice Subsystems** — gideon_agy_headless_engine, gideon_zero_gap_pipeline, gideon_faster_whisper_cuda_stt, gideon_visualizer_server, gideon_signalbus [INFERRED 0.85]
- **JARVIS Bug Hardening Lessons Preserved by GIDEON** — gideon_jarvis_audit, docs_audit_and_hardening_log_jarvis_bug_matrix, gideon_talk_buffer_zone, gideon_cuda_oom_protection, gideon_corrupt_wav_immunity, gideon_tts_sanitization [INFERRED 0.85]

## Communities (37 total, 20 thin omitted)

### Community 2 - "try_reflex"
Cohesion: 0.06
Nodes (16): 4. Vector A — Voice-Activated Graphify Lookup, Concrete implementation steps, Decisive evidence (measured, this session), Failure modes → safeguards, Mechanism, _await_if_needed(), dispatch(), _extract_graph_nodes() (+8 more)

### Community 3 - "Engine Interface & TTS Tests"
Cohesion: 0.08
Nodes (9): HardenedWatchdogSimulator, _ImmediateSleepAsyncio, _LiveWatchdogMixin, make_live_bus(), MockSignalBus, MockTTSPlayer, _run_live_watchdog(), TestLiveAntigravityWatchdog (+1 more)

### Community 4 - "GIDEON Antigravity Autonomous Voice Companion"
Cohesion: 0.19
Nodes (7): format_row(), main(), classify_migration(), main(), format_row(), main(), run_eval()

### Community 5 - "WakeWordListener"
Cohesion: 0.06
Nodes (43): ai-visualizer Setup Wizard, SIL Open Font License 1.1, VT323 Typeface, backtalk Voice Line, The Circuit Board Face, Neural Core Constellation Face, The Radial Starburst Face, Face in the Code (Matrix Rain) Face (+35 more)

### Community 6 - "Antigravity Engine Harness"
Cohesion: 0.09
Nodes (4): _auth_headers(), OpenCodeEngine, TestLiveOpenCodeEventHandling, TestLiveOpenCodeSpeakSentence

### Community 7 - "Pipelined TTS Player"
Cohesion: 0.06
Nodes (3): BaseEngine, clean_text_for_tts(), TestLiveTTSSanitizer

### Community 8 - "Wake Word & Hot Mic"
Cohesion: 0.09
Nodes (6): play_sound(), AudioListener, U2 — Preload-on-wake for Whisper (frees ~1056 MiB when idle), WakeWordListener, _loop(), on_wake()

### Community 12 - "parse_command"
Cohesion: 0.06
Nodes (25): 1.1 VRAM ledger — the real constraint, 1.2 Latency floor — where the real tax is, 1.3 Structural findings (highest-leverage, found before any new feature), 1. Measured Baseline (not estimates), 2. Feasibility Scores (1–10), 3. Vector B — Hyprland Window Management, 6. Rejected Vector X — Any CUDA Embedding / Reranker / Local Vector Store, 7. High-Leverage Architectural Upgrades (ordered) (+17 more)

### Community 14 - "typing"
Cohesion: 0.16
Nodes (5): 5. Vector C — Proactive Earcons + Barehands Mood Airlock, Concrete implementation steps, Failure modes → safeguards, AntigravityEngine, on_barge_in()

### Community 15 - "Post-Deploy Validation"
Cohesion: 0.25
Nodes (7): run_visualizer_server(), do_GET(), do_POST(), _dispatch(), _process_voice(), _send(), _static()

### Community 16 - "Rain Face Renderer"
Cohesion: 0.47
Nodes (6): main(), on_connect(), scenario_loop(), send(), stream_gideon_sentence(), stream_rms()

### Community 17 - "Visualizer Update Script"
Cohesion: 0.83
Nodes (3): report_fail(), report_pass(), validate_system_post_deploy.sh script

### Community 18 - "Visualizer Core JS"
Cohesion: 0.67
Nodes (3): Rain Emergent Face Mask, Rain Face Frame Loop, Rain Digital Column Simulator

### Community 29 - "time"
Cohesion: 0.19
Nodes (5): mock_bus(), read_bus(), main(), make_gold_record(), render_query()

### Community 31 - "benchmark_voice_pipeline.py"
Cohesion: 0.21
Nodes (6): benchmark_sanitizer(), benchmark_stt(), benchmark_ttft(), clean_text_for_tts_patched(), main(), verify_zero_audio_bleed()

### Community 32 - "main"
Cohesion: 0.20
Nodes (7): main(), on_wake_speech(), _process_voice(), switch_engine(), _make_engine(), _shutdown_engine(), StdinReader

### Community 33 - "random"
Cohesion: 0.36
Nodes (4): clean_sentence(), format_row(), generate_clean_pool(), main()

## Knowledge Gaps
- **18 isolated node(s):** `GIDEON PRIME Root Visualizer Page`, `thinking.wav Thinking Sound`, `SIL Open Font License 1.1`, `Rain Emergent Face Mask`, `Rain Digital Column Simulator` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 197 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **20 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SignalBus` connect `SignalBus` to `gideon.py`, `main`, `Engine Interface & TTS Tests`, `AudioStreamRecorder`, `WakeWordListener`, `Wake Word & Hot Mic`, `parse_command`, `typing`, `Post-Deploy Validation`?**
  _High betweenness centrality (0.203) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `parse_command()` (e.g. with `1.2 Latency floor — where the real tax is` and `8. Handoff Summary`) actually correct?**
  _`parse_command()` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `GIDEON PRIME Root Visualizer Page`, `thinking.wav Thinking Sound`, `SIL Open Font License 1.1` to the rest of the system?**
  _18 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `gideon.py` be split into smaller, more focused modules?**
  _Cohesion score 0.14705882352941177 - nodes in this community are weakly interconnected._
- **Why does `try_reflex()` connect `try_reflex` to `gideon.py`, `main`, `parse_command`, `Post-Deploy Validation`, `TTSPlayer`?**
  _High betweenness centrality (0.115) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `try_reflex()` (e.g. with `1.3 Structural findings (highest-leverage, found before any new feature)` and `Concrete implementation steps`) actually correct?**
  _`try_reflex()` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Should `try_reflex` be split into smaller, more focused modules?**
  _Cohesion score 0.06359189378057302 - nodes in this community are weakly interconnected._