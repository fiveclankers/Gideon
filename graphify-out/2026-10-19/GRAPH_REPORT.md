# Graph Report - gideon  (2026-10-19)

## Corpus Check
- 62 files · ~136,377 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 32 file(s) not represented in the graph (top: .jsonl 10, (none) 6, .log 3)

## Summary
- 738 nodes · 1583 edges · 51 communities (26 shown, 25 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 184 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- try_reflex
- parse_command
- ai-visualizer
- _LiveWatchdogMixin
- OpenCodeEngine
- TestLiveTTSSanitizer
- WakeWordListener
- clean_text_for_tts_patched
- sys
- TestHardenedWatchdog
- gideon.py
- time
- AntigravityEngine
- SignalBus
- TTSPlayer
- benchmark_voice_pipeline.py
- main
- TestLiveVoiceListenerTranscribe
- run_visualizer_server
- TestSTTPromptTokenizer
- re
- fake_daemon.py
- Handler
- VoiceListener
- gideon_handler.py
- validate_system_post_deploy.sh
- Rain Face Frame Loop
- update.sh
- AGENTS.md
- core.js
- Board Backdrop Baker
- Neural Core Frame Loop
- Radial Frame Loop
- run.sh
- start_gideon.sh
- Board Frame Loop
- Board Control Message Handler
- gideon_shared.py
- pathlib
- GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors
- handlers_study_passthrough.py
- reflex_router.py
- MockTTSPlayer
- _run_graph_query
- TestLiveOpenCodeSpeakSentence
- 7. High-Leverage Architectural Upgrades (ordered)
- test_watchdog_and_stt_prompt.py
- test_gideon_reflex_integration.py
- graphify
- json
- _ImmediateSleepAsyncio

## God Nodes (most connected - your core abstractions)
1. `HandlerResult` - 75 edges
2. `run_cmd_async()` - 48 edges
3. `parse_command()` - 37 edges
4. `try_reflex()` - 37 edges
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

## Communities (51 total, 25 thin omitted)

### Community 0 - "try_reflex"
Cohesion: 0.15
Nodes (4): try_reflex(), TestGraphQueryExecution, TestReflexExecution, TestWindowAndAudioExecution

### Community 1 - "parse_command"
Cohesion: 0.10
Nodes (6): parse_command(), TestAudioOutputSwitcher, TestGraphQueryParsing, TestNewIntentLatency, TestReflexParsing, TestWindowControlParsing

### Community 2 - "ai-visualizer"
Cohesion: 0.06
Nodes (43): ai-visualizer Setup Wizard, SIL Open Font License 1.1, VT323 Typeface, backtalk Voice Line, The Circuit Board Face, Neural Core Constellation Face, The Radial Starburst Face, Face in the Code (Matrix Rain) Face (+35 more)

### Community 6 - "WakeWordListener"
Cohesion: 0.09
Nodes (8): play_sound(), AudioListener, U2 — Preload-on-wake for Whisper (frees ~1056 MiB when idle), U3 — Collapse the four dispatch sites into `reflex_router.dispatch()`, WakeWordListener, _timeout_disarm(), _loop(), on_wake()

### Community 7 - "clean_text_for_tts_patched"
Cohesion: 0.06
Nodes (5): Failure modes → safeguards, BaseEngine, clean_text_for_tts(), clean_text_for_tts_patched(), TestTTSSanitizer

### Community 8 - "sys"
Cohesion: 0.17
Nodes (7): format_row(), main(), classify_migration(), main(), format_row(), main(), run_eval()

### Community 11 - "time"
Cohesion: 0.19
Nodes (5): mock_bus(), read_bus(), main(), make_gold_record(), render_query()

### Community 12 - "AntigravityEngine"
Cohesion: 0.14
Nodes (7): 5. Vector C — Proactive Earcons + Barehands Mood Airlock, Concrete implementation steps, Failure modes → safeguards, Mechanism (as it exists today), Recommended trigger allowlist (closed set), AntigravityEngine, on_barge_in()

### Community 15 - "benchmark_voice_pipeline.py"
Cohesion: 0.18
Nodes (6): benchmark_sanitizer(), benchmark_stt(), benchmark_ttft(), clean_text_for_tts_patched(), main(), verify_zero_audio_bleed()

### Community 16 - "main"
Cohesion: 0.16
Nodes (8): AudioStreamRecorder, main(), on_wake_speech(), _process_voice(), switch_engine(), _make_engine(), _shutdown_engine(), StdinReader

### Community 18 - "run_visualizer_server"
Cohesion: 0.25
Nodes (7): run_visualizer_server(), do_GET(), do_POST(), _dispatch(), _process_voice(), _send(), _static()

### Community 20 - "re"
Cohesion: 0.22
Nodes (7): clean_sentence(), format_row(), generate_clean_pool(), main(), generate_samples_for_intent(), main(), mutate_utterance()

### Community 21 - "fake_daemon.py"
Cohesion: 0.47
Nodes (6): main(), on_connect(), scenario_loop(), send(), stream_gideon_sentence(), stream_rms()

### Community 24 - "gideon_handler.py"
Cohesion: 0.12
Nodes (61): HandlerResult, run_cmd_async(), handle_omarchy_keybindings(), handle_omarchy_screenshot(), handle_omarchy_screenshot_full(), handle_omarchy_theme_dark(), handle_omarchy_theme_light(), handle_omarchy_theme_list() (+53 more)

### Community 25 - "validate_system_post_deploy.sh"
Cohesion: 0.83
Nodes (3): report_fail(), report_pass(), validate_system_post_deploy.sh script

### Community 26 - "Rain Face Frame Loop"
Cohesion: 0.67
Nodes (3): Rain Emergent Face Mask, Rain Face Frame Loop, Rain Digital Column Simulator

### Community 37 - "gideon_shared.py"
Cohesion: 0.09
Nodes (7): GideonDispatcher, is_negated(), extract_params(), parse_number(), run_cmd(), evaluate_risk_gate(), run_verification()

### Community 38 - "pathlib"
Cohesion: 0.11
Nodes (6): main(), evaluate(), load_jsonl(), main(), TextIntentDataset, train_epoch()

### Community 39 - "GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors"
Cohesion: 0.13
Nodes (14): 1.1 VRAM ledger — the real constraint, 1.2 Latency floor — where the real tax is, 1. Measured Baseline (not estimates), 2. Feasibility Scores (1–10), 3. Vector B — Hyprland Window Management, 4. Vector A — Voice-Activated Graphify Lookup, 8. Handoff Summary, Concrete implementation steps (+6 more)

### Community 40 - "handlers_study_passthrough.py"
Cohesion: 0.20
Nodes (8): handle_browser_use_passthrough(), handle_conversation(), handle_engine_switch(), handle_flash_note_add(), handle_hard_goal(), handle_study_queue_view(), handle_study_status_view(), handle_web_search()

### Community 41 - "reflex_router.py"
Cohesion: 0.20
Nodes (6): ensure_hyprland_env(), _exec_raw(), hypr_socket_dispatch(), _parse_number(), run_fixed(), _to_hypr_lua()

### Community 42 - "MockTTSPlayer"
Cohesion: 0.16
Nodes (3): HardenedWatchdogSimulator, MockSignalBus, MockTTSPlayer

### Community 43 - "_run_graph_query"
Cohesion: 0.19
Nodes (6): 1.3 Structural findings (highest-leverage, found before any new feature), _await_if_needed(), dispatch(), _extract_graph_nodes(), get_gideon_dispatcher(), _run_graph_query()

### Community 45 - "7. High-Leverage Architectural Upgrades (ordered)"
Cohesion: 0.22
Nodes (7): 6. Rejected Vector X — Any CUDA Embedding / Reranker / Local Vector Store, 7. High-Leverage Architectural Upgrades (ordered), U1 — Single-writer VRAM arbiter (`vram_arbiter.py`) — *do first*, U4 — Declarative reflex registry replacing the if/elif chain, U5 — Graph-driven change-impact gate in CI, get_lyra_classifier(), lyra_enabled()

### Community 46 - "test_watchdog_and_stt_prompt.py"
Cohesion: 0.25
Nodes (3): make_live_bus(), TestLiveAntigravityWatchdog, TestLiveOpenCodeWatchdog

### Community 48 - "graphify"
Cohesion: 0.29
Nodes (6): command, enabled, type, mcp, graphify, $schema

## Knowledge Gaps
- **18 isolated node(s):** `GIDEON PRIME Root Visualizer Page`, `thinking.wav Thinking Sound`, `WirePlumber Audio Ducking Policy`, `End-to-End Voice Pipeline Benchmark`, `Acoustic Barge-in Interruption` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 243 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **25 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SignalBus` connect `SignalBus` to `ai-visualizer`, `WakeWordListener`, `gideon.py`, `AntigravityEngine`, `7. High-Leverage Architectural Upgrades (ordered)`, `TTSPlayer`, `test_watchdog_and_stt_prompt.py`, `main`, `run_visualizer_server`?**
  _High betweenness centrality (0.160) - this node is a cross-community bridge._
- **Are the 8 inferred relationships involving `HandlerResult` (e.g. with `GideonDispatcher` and `handle_launch_brave()`) actually correct?**
  _`HandlerResult` has 8 INFERRED edges - model-reasoned connections that need verification._
- **What connects `GIDEON PRIME Root Visualizer Page`, `thinking.wav Thinking Sound`, `WirePlumber Audio Ducking Policy` to the rest of the system?**
  _18 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `try_reflex` be split into smaller, more focused modules?**
  _Cohesion score 0.14624505928853754 - nodes in this community are weakly interconnected._
- **Why does `try_reflex()` connect `try_reflex` to `parse_command`, `GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors`, `reflex_router.py`, `gideon.py`, `_run_graph_query`, `7. High-Leverage Architectural Upgrades (ordered)`, `TTSPlayer`, `test_gideon_reflex_integration.py`, `main`, `run_visualizer_server`?**
  _High betweenness centrality (0.085) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `parse_command()` (e.g. with `1.2 Latency floor — where the real tax is` and `8. Handoff Summary`) actually correct?**
  _`parse_command()` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Should `parse_command` be split into smaller, more focused modules?**
  _Cohesion score 0.10416666666666667 - nodes in this community are weakly interconnected._