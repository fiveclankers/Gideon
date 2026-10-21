# Graph Report - gideon  (2026-10-21)

## Corpus Check
- 61 files · ~138,939 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 25 file(s) not represented in the graph (top: .jsonl 10, (none) 7, .onnx 2)

## Summary
- 812 nodes · 1708 edges · 58 communities (28 shown, 30 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 192 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `d239945d`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- try_reflex
- parse_command
- _run_graph_query
- Live Engine Watchdog Tests
- OpenCode Sidecar Engine
- GIDEON Antigravity Autonomous Voice Companion
- WakeWordListener
- Engine Base & TTS Sanitizer Tests
- ai-visualizer
- Hardened Watchdog Simulation Tests
- reflex_router.py
- TestLiveTTSSanitizer
- Antigravity Engine & Barge-in
- SignalBus & UI Broadcast
- Pipelined TTS Playback
- gideon.py
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
- overnight_generator.py
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
- TestGraphQueryParsing
- time
- run_fixed
- BaseEngine
- TestByeByeDismissal
- overnight_generator.py
- TestGraphQueryExecution
- pathlib
- extract_params
- OpenCode MCP Configuration
- Handler
- TestGideonReflexIntegration
- opencode_engine.py
- TestAudioOutputSwitcher
- TestWindowControlParsing
- VoiceListener
- is_generic_music_request
- TestDismissReflex
- TestNewIntentLatency

## God Nodes (most connected - your core abstractions)
1. `HandlerResult` - 82 edges
2. `try_reflex()` - 53 edges
3. `parse_command()` - 49 edges
4. `run_cmd_async()` - 45 edges
5. `SignalBus` - 26 edges
6. `main()` - 25 edges
7. `OpenCodeEngine` - 23 edges
8. `TTSPlayer` - 19 edges
9. `TestLiveTTSSanitizer` - 15 edges
10. `_run_live_watchdog()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `Boundaries and known integration gap` --references--> `SignalBus`  [INFERRED]
  docs/ARCHITECTURE.md → gideon.py
- `1.2 Latency floor — where the real tax is` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `8. Handoff Summary` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `Failure modes → safeguards` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py
- `Mechanism` --references--> `parse_command()`  [INFERRED]
  docs/research_audit_vram_vectors.md → reflex_router.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **ai-visualizer Faces on the Shared Voice Signal Bus** — ai_visualizer_faces_board_index_circuit_board, ai_visualizer_faces_radial_index_radial_face, ai_visualizer_faces_rain_index_face_in_code, ai_visualizer_faces_neural_index_neural_core, ai_visualizer_signal_bus [INFERRED 0.85]
- **GIDEON Concurrent Voice Subsystems** — gideon_agy_headless_engine, gideon_zero_gap_pipeline, gideon_faster_whisper_cuda_stt, gideon_visualizer_server, gideon_signalbus [INFERRED 0.85]
- **JARVIS Bug Hardening Lessons Preserved by GIDEON** — gideon_jarvis_audit, docs_audit_and_hardening_log_jarvis_bug_matrix, gideon_talk_buffer_zone, gideon_cuda_oom_protection, gideon_corrupt_wav_immunity, gideon_tts_sanitization [INFERRED 0.85]

## Communities (58 total, 30 thin omitted)

### Community 0 - "try_reflex"
Cohesion: 0.13
Nodes (5): try_reflex(), run(), TestDismissExecution, TestReflexExecution, TestWindowAndAudioExecution

### Community 2 - "_run_graph_query"
Cohesion: 0.18
Nodes (7): 1.3 Structural findings (highest-leverage, found before any new feature), _await_if_needed(), dispatch(), get_gideon_dispatcher(), _run_graph_query(), _revert_tier0_idle(), _revert_tier1_idle()

### Community 3 - "Live Engine Watchdog Tests"
Cohesion: 0.14
Nodes (5): _ImmediateSleepAsyncio, _LiveWatchdogMixin, _run_live_watchdog(), TestLiveAntigravityWatchdog, TestLiveOpenCodeWatchdog

### Community 4 - "OpenCode Sidecar Engine"
Cohesion: 0.10
Nodes (4): OpenCodeEngine, make_live_bus(), TestLiveOpenCodeEventHandling, TestLiveOpenCodeSpeakSentence

### Community 5 - "GIDEON Antigravity Autonomous Voice Companion"
Cohesion: 0.08
Nodes (30): Overnight Gideon Dataset Generator Progress Report, Hard-Case Mining, Schema Migration to 15-Intent Master (v8), GIDEON Engineering Audit, Hardening and Lessons Log, GIDEON Operations, Architecture and Performance Manual, 22-Test Regression Suite, WirePlumber Audio Ducking Policy, End-to-End Voice Pipeline Benchmark (+22 more)

### Community 8 - "ai-visualizer"
Cohesion: 0.07
Nodes (34): SIL Open Font License 1.1, VT323 Typeface, backtalk Voice Line, The Circuit Board Face, Neural Core Constellation Face, The Radial Starburst Face, Face in the Code (Matrix Rain) Face, GIDEON PRIME Root Visualizer Page (+26 more)

### Community 9 - "Hardened Watchdog Simulation Tests"
Cohesion: 0.08
Nodes (4): HardenedWatchdogSimulator, MockSignalBus, MockTTSPlayer, TestHardenedWatchdog

### Community 10 - "reflex_router.py"
Cohesion: 0.11
Nodes (9): 6. Rejected Vector X — Any CUDA Embedding / Reranker / Local Vector Store, U1 — Single-writer VRAM arbiter (`vram_arbiter.py`) — *do first*, _clean_music_value(), _extract_graph_nodes(), get_lyra_classifier(), _is_dismissal(), _looks_conversational(), lyra_enabled() (+1 more)

### Community 12 - "Antigravity Engine & Barge-in"
Cohesion: 0.14
Nodes (7): 5. Vector C — Proactive Earcons + Barehands Mood Airlock, Concrete implementation steps, Failure modes → safeguards, Mechanism (as it exists today), Recommended trigger allowlist (closed set), AntigravityEngine, on_barge_in()

### Community 15 - "gideon.py"
Cohesion: 0.10
Nodes (8): clean_text_for_tts(), _preload_cuda_libs(), benchmark_sanitizer(), benchmark_stt(), benchmark_ttft(), clean_text_for_tts_patched(), main(), verify_zero_audio_bleed()

### Community 16 - "Voice Loop & Engine Switching"
Cohesion: 0.15
Nodes (8): AudioStreamRecorder, main(), on_wake_speech(), _process_voice(), switch_engine(), _make_engine(), _shutdown_engine(), StdinReader

### Community 18 - "Integrated Visualizer HTTP Server"
Cohesion: 0.23
Nodes (7): run_visualizer_server(), do_GET(), do_POST(), _dispatch(), _process_voice(), _send(), _static()

### Community 19 - "GideonDispatcher"
Cohesion: 0.18
Nodes (3): GideonDispatcher, is_negated(), evaluate_risk_gate()

### Community 20 - "Gideon v8 Dataset Builders"
Cohesion: 0.22
Nodes (7): clean_sentence(), format_row(), generate_clean_pool(), main(), generate_samples_for_intent(), main(), mutate_utterance()

### Community 21 - "UI Fake Daemon Simulator"
Cohesion: 0.47
Nodes (6): main(), on_connect(), scenario_loop(), send(), stream_gideon_sentence(), stream_rms()

### Community 23 - "WakeWordListener"
Cohesion: 0.21
Nodes (6): U2 — Preload-on-wake for Whisper (frees ~1056 MiB when idle), U3 — Collapse the four dispatch sites into `reflex_router.dispatch()`, WakeWordListener, _timeout_disarm(), _loop(), on_wake()

### Community 24 - "Atomic Intent Handlers: System, Omarchy & Study"
Cohesion: 0.08
Nodes (74): HandlerResult, run_cmd_async(), handle_omarchy_keybindings(), handle_omarchy_screenshot(), handle_omarchy_screenshot_full(), handle_omarchy_theme_dark(), handle_omarchy_theme_light(), handle_omarchy_theme_list() (+66 more)

### Community 25 - "Post-Deploy Validation Script"
Cohesion: 0.83
Nodes (3): report_fail(), report_pass(), validate_system_post_deploy.sh script

### Community 26 - "Matrix Rain Face Renderer"
Cohesion: 0.67
Nodes (3): Rain Emergent Face Mask, Rain Face Frame Loop, Rain Digital Column Simulator

### Community 27 - "overnight_generator.py"
Cohesion: 0.36
Nodes (3): main(), make_gold_record(), render_query()

### Community 28 - "AGENTS.md"
Cohesion: 0.29
Nodes (6): Clipboard Directives, Contact Mappings, graphify, Instagram Automation Directives, Music Playback Directives, Web Component & Modern SPA Interaction Directives

### Community 37 - "GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors"
Cohesion: 0.14
Nodes (13): 1.1 VRAM ledger — the real constraint, 1.2 Latency floor — where the real tax is, 1. Measured Baseline (not estimates), 2. Feasibility Scores (1–10), 3. Vector B — Hyprland Window Management, 7. High-Leverage Architectural Upgrades (ordered), 8. Handoff Summary, Concrete implementation steps (+5 more)

### Community 38 - "BGE Classifier Training & ONNX Calibration"
Cohesion: 0.11
Nodes (6): main(), evaluate(), load_jsonl(), main(), TextIntentDataset, train_epoch()

### Community 40 - "time"
Cohesion: 0.21
Nodes (3): load_config(), mock_bus(), read_bus()

### Community 41 - "run_fixed"
Cohesion: 0.14
Nodes (11): 4. Vector A — Voice-Activated Graphify Lookup, Concrete implementation steps, Decisive evidence (measured, this session), Failure modes → safeguards, Mechanism, _load_gideon_env(), ensure_hyprland_env(), _exec_raw() (+3 more)

### Community 43 - "TestByeByeDismissal"
Cohesion: 0.25
Nodes (3): TestByeByeDismissal, run(), run()

### Community 44 - "overnight_generator.py"
Cohesion: 0.17
Nodes (8): format_row(), main(), classify_migration(), main(), format_row(), main(), run_eval(), run_verification()

### Community 48 - "OpenCode MCP Configuration"
Cohesion: 0.29
Nodes (6): command, enabled, type, mcp, graphify, $schema

## Knowledge Gaps
- **18 isolated node(s):** `Rain Emergent Face Mask`, `Rain Digital Column Simulator`, `WirePlumber Audio Ducking Policy`, `End-to-End Voice Pipeline Benchmark`, `Thousand-Page Needle Sieve` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 274 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **30 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SignalBus` connect `SignalBus & UI Broadcast` to `OpenCode Sidecar Engine`, `GIDEON Antigravity Autonomous Voice Companion`, `GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors`, `ai-visualizer`, `Antigravity Engine & Barge-in`, `Pipelined TTS Playback`, `gideon.py`, `Voice Loop & Engine Switching`, `Integrated Visualizer HTTP Server`?**
  _High betweenness centrality (0.190) - this node is a cross-community bridge._
- **Are the 15 inferred relationships involving `HandlerResult` (e.g. with `GideonDispatcher` and `handle_launch_brave()`) actually correct?**
  _`HandlerResult` has 15 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Rain Emergent Face Mask`, `Rain Digital Column Simulator`, `WirePlumber Audio Ducking Policy` to the rest of the system?**
  _18 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `try_reflex` be split into smaller, more focused modules?**
  _Cohesion score 0.13043478260869565 - nodes in this community are weakly interconnected._
- **Why does `try_reflex()` connect `try_reflex` to `parse_command`, `_run_graph_query`, `GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors`, `run_fixed`, `reflex_router.py`, `TestByeByeDismissal`, `TestGraphQueryExecution`, `Pipelined TTS Playback`, `gideon.py`, `Voice Loop & Engine Switching`, `Integrated Visualizer HTTP Server`, `TestGideonReflexIntegration`, `Atomic Intent Handlers: System, Omarchy & Study`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `try_reflex()` (e.g. with `1.3 Structural findings (highest-leverage, found before any new feature)` and `Concrete implementation steps`) actually correct?**
  _`try_reflex()` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Should `Live Engine Watchdog Tests` be split into smaller, more focused modules?**
  _Cohesion score 0.14333333333333334 - nodes in this community are weakly interconnected._