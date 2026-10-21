# Graph Report - gideon  (2026-10-21)

## Corpus Check
- 67 files · ~201,591 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 36 file(s) not represented in the graph (top: .jsonl 10, (none) 6, .onnx 4)

## Summary
- 786 nodes · 1673 edges · 58 communities (29 shown, 29 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 191 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- try_reflex
- parse_command
- _run_graph_query
- Live Engine Watchdog Tests
- OpenCode Sidecar Engine
- ai-visualizer
- WakeWordListener
- Engine Base & TTS Sanitizer Tests
- sys
- Hardened Watchdog Simulation Tests
- Gideon Env & Visualizer Server
- TestLiveTTSSanitizer
- Antigravity Engine & Barge-in
- SignalBus & UI Broadcast
- Pipelined TTS Playback
- benchmark_voice_pipeline.py
- Voice Loop & Engine Switching
- TestLiveVoiceListenerTranscribe
- Integrated Visualizer HTTP Server
- GideonDispatcher
- Gideon v8 Dataset Builders
- UI Fake Daemon Simulator
- TestSTTPromptTokenizer
- WakeWordListener
- Atomic Intent Handlers: System, Omarchy & Study
- Post-Deploy Validation Script
- Matrix Rain Face Renderer
- Visualizer Self-Update Script
- AGENTS.md
- Visualizer Core JavaScript
- Circuit Board Backdrop
- Neural Core Constellation Face
- Radial Starburst Face
- Visualizer Launch Script
- GIDEON Startup Script
- Board Face Frame Loop
- Board Face Control Handler
- GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors
- BGE Classifier Training & ONNX Calibration
- lyra_enabled
- gideon.py
- reflex_router.py
- BaseEngine
- TestByeByeDismissal
- overnight_generator.py
- TestGraphQueryExecution
- pathlib
- TestGraphQueryParsing
- OpenCode MCP Configuration
- Handler
- TestGideonReflexIntegration
- 4. Vector A — Voice-Activated Graphify Lookup
- TestAudioOutputSwitcher
- TestWindowControlParsing
- opencode_engine.py
- 3. Vector B — Hyprland Window Management
- TestDismissReflex
- TestNewIntentLatency

## God Nodes (most connected - your core abstractions)
1. `HandlerResult` - 81 edges
2. `try_reflex()` - 52 edges
3. `parse_command()` - 47 edges
4. `run_cmd_async()` - 44 edges
5. `SignalBus` - 25 edges
6. `main()` - 25 edges
7. `OpenCodeEngine` - 23 edges
8. `TTSPlayer` - 19 edges
9. `TestLiveTTSSanitizer` - 15 edges
10. `_run_live_watchdog()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `1.2 Latency floor — where the real tax is` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `8. Handoff Summary` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `Failure modes → safeguards` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `Mechanism` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `U4 — Declarative reflex registry replacing the if/elif chain` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **ai-visualizer Faces on the Shared Voice Signal Bus** — ai_visualizer_faces_board_index_circuit_board, ai_visualizer_faces_radial_index_radial_face, ai_visualizer_faces_rain_index_face_in_code, ai_visualizer_faces_neural_index_neural_core, ai_visualizer_signal_bus [INFERRED 0.85]
- **GIDEON Concurrent Voice Subsystems** — gideon_agy_headless_engine, gideon_zero_gap_pipeline, gideon_faster_whisper_cuda_stt, gideon_visualizer_server, gideon_signalbus [INFERRED 0.85]
- **JARVIS Bug Hardening Lessons Preserved by GIDEON** — gideon_jarvis_audit, docs_audit_and_hardening_log_jarvis_bug_matrix, gideon_talk_buffer_zone, gideon_cuda_oom_protection, gideon_corrupt_wav_immunity, gideon_tts_sanitization [INFERRED 0.85]

## Communities (58 total, 29 thin omitted)

### Community 0 - "try_reflex"
Cohesion: 0.15
Nodes (4): try_reflex(), TestDismissExecution, TestReflexExecution, TestWindowAndAudioExecution

### Community 2 - "_run_graph_query"
Cohesion: 0.18
Nodes (7): _await_if_needed(), dispatch(), _extract_graph_nodes(), get_gideon_dispatcher(), _run_graph_query(), _revert_tier0_idle(), _revert_tier1_idle()

### Community 3 - "Live Engine Watchdog Tests"
Cohesion: 0.14
Nodes (5): _ImmediateSleepAsyncio, _LiveWatchdogMixin, _run_live_watchdog(), TestLiveAntigravityWatchdog, TestLiveOpenCodeWatchdog

### Community 4 - "OpenCode Sidecar Engine"
Cohesion: 0.10
Nodes (4): OpenCodeEngine, make_live_bus(), TestLiveOpenCodeEventHandling, TestLiveOpenCodeSpeakSentence

### Community 5 - "ai-visualizer"
Cohesion: 0.06
Nodes (43): ai-visualizer Setup Wizard, SIL Open Font License 1.1, VT323 Typeface, backtalk Voice Line, The Circuit Board Face, Neural Core Constellation Face, The Radial Starburst Face, Face in the Code (Matrix Rain) Face (+35 more)

### Community 6 - "WakeWordListener"
Cohesion: 0.17
Nodes (3): AudioListener, U2 — Preload-on-wake for Whisper (frees ~1056 MiB when idle), on_wake()

### Community 9 - "Hardened Watchdog Simulation Tests"
Cohesion: 0.08
Nodes (4): HardenedWatchdogSimulator, MockSignalBus, MockTTSPlayer, TestHardenedWatchdog

### Community 10 - "Gideon Env & Visualizer Server"
Cohesion: 0.17
Nodes (5): list_faces(), load_config(), mock_bus(), read_bus(), run_eval()

### Community 12 - "Antigravity Engine & Barge-in"
Cohesion: 0.15
Nodes (6): 5. Vector C — Proactive Earcons + Barehands Mood Airlock, Concrete implementation steps, Failure modes → safeguards, Mechanism (as it exists today), AntigravityEngine, on_barge_in()

### Community 15 - "benchmark_voice_pipeline.py"
Cohesion: 0.19
Nodes (6): benchmark_sanitizer(), benchmark_stt(), benchmark_ttft(), clean_text_for_tts_patched(), main(), verify_zero_audio_bleed()

### Community 16 - "Voice Loop & Engine Switching"
Cohesion: 0.11
Nodes (9): AudioStreamRecorder, main(), on_wake_speech(), _process_voice(), switch_engine(), _make_engine(), _shutdown_engine(), StdinReader (+1 more)

### Community 18 - "Integrated Visualizer HTTP Server"
Cohesion: 0.23
Nodes (7): run_visualizer_server(), do_GET(), do_POST(), _dispatch(), _process_voice(), _send(), _static()

### Community 19 - "GideonDispatcher"
Cohesion: 0.13
Nodes (5): GideonDispatcher, is_negated(), extract_params(), evaluate_risk_gate(), run_verification()

### Community 20 - "Gideon v8 Dataset Builders"
Cohesion: 0.27
Nodes (5): parse_number(), clean_sentence(), format_row(), generate_clean_pool(), main()

### Community 21 - "UI Fake Daemon Simulator"
Cohesion: 0.47
Nodes (6): main(), on_connect(), scenario_loop(), send(), stream_gideon_sentence(), stream_rms()

### Community 23 - "WakeWordListener"
Cohesion: 0.21
Nodes (5): 1.3 Structural findings (highest-leverage, found before any new feature), U3 — Collapse the four dispatch sites into `reflex_router.dispatch()`, WakeWordListener, _timeout_disarm(), _loop()

### Community 24 - "Atomic Intent Handlers: System, Omarchy & Study"
Cohesion: 0.08
Nodes (73): HandlerResult, run_cmd_async(), handle_omarchy_keybindings(), handle_omarchy_screenshot(), handle_omarchy_screenshot_full(), handle_omarchy_theme_dark(), handle_omarchy_theme_light(), handle_omarchy_theme_list() (+65 more)

### Community 25 - "Post-Deploy Validation Script"
Cohesion: 0.83
Nodes (3): report_fail(), report_pass(), validate_system_post_deploy.sh script

### Community 26 - "Matrix Rain Face Renderer"
Cohesion: 0.67
Nodes (3): Rain Emergent Face Mask, Rain Face Frame Loop, Rain Digital Column Simulator

### Community 28 - "AGENTS.md"
Cohesion: 0.29
Nodes (6): Clipboard Directives, Contact Mappings, graphify, Instagram Automation Directives, Music Playback Directives, Web Component & Modern SPA Interaction Directives

### Community 37 - "GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors"
Cohesion: 0.20
Nodes (9): 1.1 VRAM ledger — the real constraint, 1.2 Latency floor — where the real tax is, 1. Measured Baseline (not estimates), 2. Feasibility Scores (1–10), 7. High-Leverage Architectural Upgrades (ordered), 8. Handoff Summary, GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors, U4 — Declarative reflex registry replacing the if/elif chain (+1 more)

### Community 38 - "BGE Classifier Training & ONNX Calibration"
Cohesion: 0.11
Nodes (6): main(), evaluate(), load_jsonl(), main(), TextIntentDataset, train_epoch()

### Community 39 - "lyra_enabled"
Cohesion: 0.33
Nodes (4): 6. Rejected Vector X — Any CUDA Embedding / Reranker / Local Vector Store, U1 — Single-writer VRAM arbiter (`vram_arbiter.py`) — *do first*, get_lyra_classifier(), lyra_enabled()

### Community 41 - "reflex_router.py"
Cohesion: 0.12
Nodes (10): _load_gideon_env(), _clean_music_value(), ensure_hyprland_env(), _exec_raw(), hypr_socket_dispatch(), _is_dismissal(), _looks_conversational(), _parse_number() (+2 more)

### Community 43 - "TestByeByeDismissal"
Cohesion: 0.20
Nodes (4): TestByeByeDismissal, run(), run(), run()

### Community 44 - "overnight_generator.py"
Cohesion: 0.14
Nodes (10): format_row(), main(), generate_samples_for_intent(), main(), mutate_utterance(), main(), make_gold_record(), render_query() (+2 more)

### Community 48 - "OpenCode MCP Configuration"
Cohesion: 0.29
Nodes (6): command, enabled, type, mcp, graphify, $schema

### Community 51 - "4. Vector A — Voice-Activated Graphify Lookup"
Cohesion: 0.40
Nodes (5): 4. Vector A — Voice-Activated Graphify Lookup, Concrete implementation steps, Decisive evidence (measured, this session), Failure modes → safeguards, Mechanism

### Community 55 - "3. Vector B — Hyprland Window Management"
Cohesion: 0.50
Nodes (4): 3. Vector B — Hyprland Window Management, Concrete implementation steps, Failure modes → safeguards, Mechanism

## Knowledge Gaps
- **18 isolated node(s):** `Rain Emergent Face Mask`, `Rain Digital Column Simulator`, `Board Backdrop Baker`, `Board Trace Builder`, `Neural Core Frame Loop` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 256 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **29 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SignalBus` connect `SignalBus & UI Broadcast` to `OpenCode Sidecar Engine`, `GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors`, `ai-visualizer`, `gideon.py`, `Antigravity Engine & Barge-in`, `Pipelined TTS Playback`, `Voice Loop & Engine Switching`, `Integrated Visualizer HTTP Server`, `WakeWordListener`?**
  _High betweenness centrality (0.150) - this node is a cross-community bridge._
- **Are the 15 inferred relationships involving `HandlerResult` (e.g. with `GideonDispatcher` and `handle_launch_brave()`) actually correct?**
  _`HandlerResult` has 15 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Rain Emergent Face Mask`, `Rain Digital Column Simulator`, `Board Backdrop Baker` to the rest of the system?**
  _18 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Live Engine Watchdog Tests` be split into smaller, more focused modules?**
  _Cohesion score 0.14333333333333334 - nodes in this community are weakly interconnected._
- **Why does `try_reflex()` connect `try_reflex` to `parse_command`, `_run_graph_query`, `GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors`, `gideon.py`, `reflex_router.py`, `TestByeByeDismissal`, `TestGraphQueryExecution`, `Pipelined TTS Playback`, `Voice Loop & Engine Switching`, `Integrated Visualizer HTTP Server`, `TestGideonReflexIntegration`, `WakeWordListener`, `3. Vector B — Hyprland Window Management`, `Atomic Intent Handlers: System, Omarchy & Study`?**
  _High betweenness centrality (0.124) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `try_reflex()` (e.g. with `1.3 Structural findings (highest-leverage, found before any new feature)` and `Concrete implementation steps`) actually correct?**
  _`try_reflex()` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Should `OpenCode Sidecar Engine` be split into smaller, more focused modules?**
  _Cohesion score 0.0962566844919786 - nodes in this community are weakly interconnected._