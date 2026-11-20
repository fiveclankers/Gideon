# GIDEON Engineering Audit, Hardening & Lessons Log
**Date:** 2026-10-18  
**Scope:** Extricating 3D Quickshell Orb overlay from JARVIS, Boreal Pine aesthetic overhaul, Faster-Whisper upgrade to `medium.en` on CUDA, Vocabulary Conditioning Prompt integration, and complete audit of all 46 JARVIS critical bug lessons.

---

## 1. Executive Summary

GIDEON was successfully decoupled from JARVIS's legacy runtime:
1. **Visual Shell Extrication:** Extricated the 3D GPU plasma shader orb from JARVIS into `/home/cat/gideon/ui/` with a **Boreal Pine** palette (`#0e3d2f`, `#10b981`, `#047857`, `#d97706`).
2. **Top-Right Corner Standby Docking:** When idle, the orb scales to `0.44`, fades to `0.30` opacity, and translates into the top-right corner (`slideX: 68`, `slideY: -10`). On activity (listening, thinking, toolcall, speaking), it glides smoothly to full size and opacity.
3. **Strict Tool-Call Binding:** Bound strictly to NDJSON `tool_call` events, morphing into an amber canopy state with real-time tool names and targets without background engine polling.
4. **Whisper Transcription Overhaul:** Upgraded from `small.en` to **Faster-Whisper `medium.en` on CUDA int8** with VAD silence filtering (`min_silence_duration_ms=300`) and vocabulary `initial_prompt` conditioning.
5. **Acoustic & Concurrency Hardening:** Integrated lessons from JARVIS Bugs 1, 3, 16, 17, 23, 24, 25, 44, and 46.

---

## 2. Whisper Decoder Conditioning Prompt Explained

### 2.1 The Problem: Phonetic Drift in Standard Whisper
In default configurations, Faster-Whisper's language model decoder relies heavily on standard conversational frequency distributions. Rare, technical, or domain-specific terms suffer severe phonetic drift:
- *"Euphonium"* -> *"Eucalyptus"* or *"You phone me"*
- *"Antigravity"* -> *"Anti gravity"* or *"Ante gravity"*
- *"Hyprland"* -> *"Hyper land"* or *"High perland"*

### 2.2 The Mechanism: Transformer Cross-Attention Priming
Whisper uses an encoder-decoder Transformer architecture:
1. The **Audio Encoder** maps 80-channel Mel spectrograms into latent acoustic representations.
2. The **Text Decoder** is an autoregressive Transformer that attends to the encoder representations (via cross-attention) and its own previous token outputs (via masked self-attention).
3. The `initial_prompt` parameter injects a sequence of prefix tokens directly into the decoder's prompt context prior to the start-of-transcript token.

```
[Audio Input] ──► [Mel Filterbank] ──► [Transformer Audio Encoder]
                                                      │
                                           (Cross-Attention Keys/Values)
                                                      ▼
[initial_prompt Tokens] ───────────────► [Transformer Text Decoder] ──► [High-Accuracy Transcription]
("Gideon, Antigravity, Euphonium...")     (Biased Prior Distribution)
```

### 2.3 Key Operational Rules
- **Biasing, Not Echoing:** Conditioning tokens only alter the decoder's prior probability distribution; Whisper will never emit words from `initial_prompt` unless the acoustic features in the audio match.
- **VAD Gating:** Coupled with `vad_filter=True` and `min_silence_duration_ms=300`, room fan hum and background silence do not generate hallucinated prefix tokens.

---

## 3. Retrospective Bug Audit & Hardening Matrix

| JARVIS Bug | Root Cause | GIDEON Safeguard & Implementation |
| :--- | :--- | :--- |
| **Bug 1 & 16: Noise & Typing Transients + Mic Calibration** | Desk vibrations & keyboard click transients tripping hot-mic VAD; or excessive speech floor (`0.024` RMS vs ambient `0.0022`) forcing screaming. | Lowered speech onset to `max(0.008, ambient * 2.2)`, silence cutoff to `max(0.004, ambient * 1.4)`, boosted `mic_gain` to `1.8x`, relaxed `wake_threshold` to `0.20`. 2nd-order Butterworth bandpass (200Hz–3800Hz) + **2 consecutive chunk requirement** (~160ms sustained voice energy) rejects keyboard clicks. |
| **Bug 3: Syllable Leaking** | Trailing "-vis" lingering into recording buffer, causing natural pre-speech pause to trigger trailing cutoff. | Enforced a strict **0.35s blanking window** after wake detection + minimum 0.40s speech duration before silence cutoff. |
| **Bug 17 & 23: CUDA OOM & cuBLAS Regression** | `faster-whisper` throwing CUDA OOM on multi-turn conversations on the 4GB GTX 1650; missing cuBLAS after removing Torch. | `_preload_cuda_libs()` pre-loads `libcublas.so.12` via `ctypes.CDLL` with `RTLD_GLOBAL`; `VoiceListener` wrapped in a defensive handler that purges `torch.cuda.empty_cache()` and falls back to CPU `small.en` int8 without dropping the turn. |
| **Bug 24: Corrupt WAV Header Freeze** | Fish Audio returning `0x7FFFFF00` frames, wedging sleep loops in "speaking" state for hours. | Audio streaming uses raw PipeWire byte piping (`pw-play -`) via `proc.communicate(wav_bytes)`. Process lifecycle is bounded by actual audio playback time, completely immune to bogus RIFF headers. |
| **Bug 25: Quickshell Socket Deadlock** | `QLocalSocket` silently stalling across daemon pre-warming restarts. | Two-phase `Loader` tear-down and rebuild cycle in `shell.qml` behind `socketAlive` and `socketResetArmed` with 1.5s knocking timer. Tested & surviving daemon bounces cleanly. |
| **Bug 44: Text-to-Speech Error & Tag Spam** | TTS vocalizing raw HTML, CSS classes, markdown headings, or em-dash stutters. | Hardened `clean_text_for_tts()`: strips markdown headings (`#`), blockquotes (`>`), HTML tags (`<...>`), links, and converts em-dashes (`—`) to natural pauses. |
| **Bug 46 & Hot-Mic Freeze: Acoustic Echo & Talk Buffer Zone** | Speaker echo triggering turns; or missing `hot_mic_checker` causing perpetual open-mic freeze while waiting for wake word instead of timing out or cutting off speech. | Dynamic mid-stream arming (`hot_mic_checker`), speech cutoff on trailing silence, automatic `idle` reversion on timeout, and fail-safe `threading.Timer`. Audio dropped during active TTS (`is_speaking == True`). |
| **Conversational Thought Drift & Hesitation Calibration** | Users speaking deep philosophical thoughts or rambling paused briefly (0.6s) or spoke >12s, triggering premature cutoff via `max_duration_s=12.0s`. | Extended `max_duration_s` from 12.0s to 35.0s, widened hesitation silence window to 0.85s (natural breath without tripping cutoff), while preserving crisp response and automatic 7.0s silence idle reversion. |

---

## 4. Subsystem Verification & Runtime Status

1. **Wayland Layer-Shell Overlay:**
   - Window: `WlrLayershell.layer: Overlay`, `WlrLayershell.namespace: "gideon-orb"`.
   - Input: `mask: Region {}` leaves the overlay fully click-through (the orb hitbox was removed by user decision); `WlrLayershell.keyboardFocus: WlrKeyboardFocus.None` guarantees zero spacebar/keyboard capture.
2. **Systemd Supervision:**
   - `gideon-ui.service` is enabled for `graphical-session.target`.
   - `gideon.service` is available in `~/.config/systemd/user/gideon.service`.
   - Omarchy `autostart.lua` launches `start_gideon.sh`, ensuring both UI and daemon start on login.
   - All JARVIS services (`jarvis.service`, `jarvis-ui.service`, `jarvis-browser.service`) are stopped and disabled.
3. **IPC Socket:**
   - `/tmp/gideon-ui.sock` actively connects `quickshell` and `gideon.py` with bidirectional event synchronization.

---

# GIDEON Engineering Audit — 2026-10-25 Session
**Date:** 2026-10-25
**Scope:** Rebuilding `~/jarvis/.venv` after storage purge, removing the dead Hermes engine half-implementation, visualizer security hardening, engine-parity bug fixes, and GitHub-readiness cleanup. Commit `b229d37`, pushed to `github.com/fiveclankers/Gideon` (main).

## 1. Executive Summary

1. **Venv rebuilt from scratch** on uv CPython 3.11.15 (the interpreter the daemon already ran): 149 packages, torch 2.14.1+cu130 with CUDA verified. No manifest existed, so the dependency set was reconstructed from the union of imports across `~/gideon` and `~/jarvis`. Playwright chromium browsers and openWakeWord models (openwakeword 0.6.0 ships without them; `openwakeword.utils.download_models()`) restored. Gotchas recorded: this ZCode session's AppImage `LD_LIBRARY_PATH` breaks the interpreter's path init — test venvs with `env -i` or via systemd, never from inside the agent shell.
2. **Hermes engine removed.** `hermes_engine.py` was unselectable (`--engine` accepts agy|opencode only), unimportable (`from ui.bus import SignalBus` — module never existed), and protocol-violating (sync `interrupt()`/`close()` under an async `BaseEngine` → `TypeError` on first `await`). Archived with its exclusive config (`brain_models.yaml`, `wake_routes.yaml`, both personas, changelog) at `~/.local/share/gideon-attic/`.
3. **Security: Host-header validation on the visualizer** (`127.0.0.1:8790`). The POST endpoints drive reflexes that execute local commands, and the wildcard CORS policy answered cross-origin preflights — any website could have driven GIDEON. GET/POST/OPTIONS now 403 unless Host is loopback. Verified: legit Host 200, forged Host 403 (GET and POST).
4. **Engine parity fixes:** terminal `engine <name>` switch now transfers `wake_listener` and updates `bus.engine_name`; `OpenCodeEngine.interrupt()` now silences TTS, cancels its watchdog and resets turn state (was: API ping only); `_finish_turn` re-arms the hot-mic window like agy.
5. **Smaller fixes:** browser launch falls back chrome→brave→firefox; dispatcher model paths repo-relative (were absolute `/home/cat/...`); `parse_number` treats "hundred" as a multiplier ("one hundred"=100, was 101 — note: `reflex_router._parse_number` intentionally left untouched per owner instruction); `MockTTSPlayer` in tests gained `interrupt()`.
6. **Untouched by design:** all tuned regexes, `HOT_MIC_WINDOW_S`/`MAX_UTTERANCE_S`, wake threshold 0.20, mic_gain, sentence-splitting, risk thresholds.

## 2. Repo Hygiene (commit b229d37, 51 files, −8188 lines)

- `graphify-out/` untracked (35 generated files) and gitignored; kept on disk so the `graphify query` Tier-0 reflex still works. `codemap.md` outputs ignored.
- Removed `gideon.py.pre_patch_bak`, `shell.qml.bak`, stray logs, `__pycache__`.
- Secrets swept (working tree + full git history): clean. `FISH_API_KEY` stays in `~/.config/gideon/gideon.env`.

## 3. Verification

`pytest tests/` under the rebuilt venv: **141 passed, 230 subtests**. Daemon restarted and live-verified: BGE classifier pre-warmed, Whisper turbo on CUDA, `openWakeWord loaded models: ['hey_jarvis_v0.1']`, agy engine connected, HTTP Host gate active.

---

# GIDEON Engineering Audit — 2026-11-20 Session
**Date:** 2026-11-20
**Scope:** Full architecture audit of the live Codex-engine daemon (uncommitted codex migration included): correctness bugs, security completion, and measured latency work. Artistic/tuned choices (Tier-0 regex scope, hot-mic windows, silence budgets, wake thresholds, sentence-splitter, persona) deliberately untouched.

## 1. Real bugs found and fixed (with evidence)

1. **Phantom hourly microphone capture under systemd (Critical).** `StdinReader.prompt_line()` returned `""` after a 3600s sleep when stdin was not a TTY; `main()` interprets `""` as "record a 5s terminal mic chunk". Under `gideon.service` (stdin=/dev/null) the daemon opened the mic and dispatched room audio as a user prompt every hour. Confirmed twice in the journal: Nov 19 mic OPEN 16:11:36 = ready 15:11:36 + 3600s (159,690 bytes ≈ 5.0s), and live-captured Nov 20 10:30:08 = ready 09:30:08 + 3600s (161,412 bytes). Fix: `prompt_line()` returns a `None` sentinel; the main loop idles on `None` without touching the recorder.
2. **TTS sanitizer decapitated leading decimals.** The list-marker regex `^\s*(?:\d{1,2}[.)]|[-*•])\s*` turned "9.81 meters per second squared." into "81 meters…", "3.14 is roughly pi." into "14 is roughly pi.", "12.5 percent growth." into "5 percent growth." (demonstrated pre-fix). Numeric markers now require trailing whitespace; bullets no longer eat a following digit ("-40 degrees"). All existing list/heading spec tests still pass; new regressions added.
3. **Acoustic barge-in did not stop the turn.** `on_barge_in` only called `tts.interrupt()`; the engine kept streaming deltas and queueing fresh sentences, so Gideon resumed speaking over the user within ~1s. Now `WakeWordListener._handle_barge_in()` interrupts `bus.engine` (both engines' `interrupt()` cancels the turn and clears speech), falling back to TTS-only when no engine is bound.
4. **2026-10-25 Host-gate hardening was incomplete (security).** A cross-origin `fetch('http://127.0.0.1:8790/api/prompt')` still carries `Host: 127.0.0.1:8790`, so the Host check never blocked it, and `Access-Control-Allow-Origin: *` let any page read `/state` transcripts (user + reply text) and drive prompt injection into a full-access shell engine (browser local-network rules were the only, uneven, defense). Added an Origin allowlist: foreign Origins (incl. `null`) get 403 on GET/POST/OPTIONS; loopback origins get an echoed CORS header (cross-port HUD/visualizer use keeps working); no-Origin requests (same-origin HUD, curl) unchanged. Verified against the real embedded server on an ephemeral port (8 new tests).
5. **Dispatcher init race.** `get_gideon_dispatcher()` check-then-set is now guarded by a `threading.Lock` (pre-warm moved off-loop, so a first prompt could race it).

## 2. Measured latency work (before → after, on this 4-core box)

| Path | Before | After | How |
| --- | --- | --- | --- |
| Boot critical path | ~47s (journal: 09:29:21→09:30:08; 34s BGE gap) | **8.1s** sequential, less in-daemon (parallel) | `transformers` AutoTokenizer (10.6s warm / up to 34s cold import chain incl. torch) replaced by `tokenizers.Tokenizer` (0.1s) with **exact encoding parity** (no_padding + truncation 512, verified on 11 samples incl. unicode/2000-char truncation); BGE pre-warm and STT init moved to `asyncio.to_thread`, pre-warm overlaps Codex handshake. torch/transformers no longer imported at all on the Parakeet boot path. |
| Fish TTS per-sentence connect | 389ms warm / 1613ms cold handshake **per sentence** | **0ms** sentences 2..N; first sentence's handshake hidden behind LLM time | Verified Fish live API supports sequential sessions on one socket. Finished sessions hand their OPEN socket to a warm pool (TTL 30s); `prewarm_ws()` fires at `send_prompt`; stale sockets retry once fresh before any audio; interrupts close warm sockets. Live probe: 3 sentences, 1 connection, first-audio 689/945/835ms. |
| Waveform DSP per 160ms chunk | 9.43ms on the event loop | **0.65ms** (14.5×) | Hann/rfftfreq/geomspace/64-band masks cached (`lru_cache`), band means vectorized to one matmul; empty-band fallback preserved. Numerical parity test vs reference implementation (rtol 1e-4). |
| Parakeet STT (3.7s utterance) | 675–1002ms (3 intra-op threads) | **576–878ms** (2 threads) | Thread sweep 2/3/4: extra threads contend with wake loop on 4 cores. |
| PortAudio PTT callback | scipy import + double `np.max` per chunk | hoisted module ref, single reduction | Real-time callback hygiene. |
| Wake RMS bars | 32 `np.sin` calls per chunk | precomputed shape vector | Same values. |

Voice-turn floor left as tuned (artistic): ~960ms trailing-silence budget (12×80ms chunks), wake patience/refractory, hot-mic windows. pw-play spawn measured at 6.3ms — not worth a persistent-process refactor.

## 3. Verified NOT bugs (checked, left alone)

- Codex `turn/start` `effort` param: confirmed in `codex app-server generate-json-schema` ("Override the reasoning effort for this turn and subsequent turns") — in-thread `set_effort` is protocol-correct.
- No-regex-gate philosophy, dismissal "bye bye" precedence, hallucination filters, selected-text opt-in clipboard contract, agy queued-stop interrupt semantics, watchdog 45s check-ins: deliberate, untouched.
- `_copy_reply_after_turn` fast-turn copy: a proposed `started` guard broke the documented finished-before-watcher contract (test caught it); upstream `set_user_transcript` already clears stale replies. Reverted.

## 4. Verification

`pytest tests/` (venv `/home/cat/jarvis/.venv`): **234 passed, 287 subtests** (213 pre-existing + 21 new regressions in `tests/test_audit_regressions.py`). Live probes: Fish WS reuse (1 connect / 3 sentences), boot simulation (8.1s, no torch/transformers), phantom-mic journal capture on the still-running old daemon. Changes are in the working tree alongside the owner's uncommitted Codex migration; `gideon.service` NOT restarted (owner permission required).

## 5. Addendum — HUD deprecation & efficiency pass (2026-11-20, owner directive)

- **Web HUD/PTT portal deprecated and disabled by default.** The :8790 embedded HTTP server (browser HUD, hold-to-talk, /api/* routes) no longer starts; `GIDEON_ENABLE_HUD=1` opts back in. Wake word + hot mic and the Quickshell orb socket are unaffected. Honest accounting: the server was a thread inside the daemon, so disabling it frees negligible RAM by itself — the real savings below come from the ONNX arena change.
- **Default Codex reasoning effort: remains xhigh.** A brief xhigh→low change was made on a misreading of "more efficient ways of thinking" and reverted immediately on owner correction: Luna is genuinely unusable below xhigh. Guard comment added at `DEFAULT_CODEX_EFFORT`; factory test asserts xhigh. Engine effort is NOT a latency tuning knob for this system.
- **Parakeet ONNX CPU memory arena disabled** (`enable_cpu_mem_arena=False`): measured load RSS 1228→1048MB, peak 1278→1075MB (~200MB saved) at equal transcription latency (493-559ms vs 461-617ms).
- Suite still green: 234 passed, 287 subtests.

## 6. Addendum — dead waveform computation gated (2026-11-20, owner directive)

- With the HUD deprecated, `bus.current_samples` (64-band waveform) had exactly one reader: the dead `/state` HTTP endpoint. The orb renders scalar `rms` events only. Producers now skip the spectral math when `bus.waveform_enabled` is false (set from `HUD_ENABLED` in `main()`): TTS playback passes `with_bands=False` to `_extract_audio_frames` (0.573→0.123 ms/chunk; RMS math bit-identical, regression-tested), and the PTT recorder FFT + wake-listener bar construction are skipped entirely. **All `broadcast_rms` telemetry is untouched** — the orb's listening ripple and speaking-ring amplitude ride on rms, not bars. Opt-in HUD mode (`GIDEON_ENABLE_HUD=1`) restores full waveform behavior.
- Owner artistic directives recorded (do not "fix"): the synthetic speaking rings are deliberate (real-audio reactivity was too spiky + fan noise on the mic); subtitles/heard-text were tried in older versions and rejected as ugly; the thinking-badge + orbit-spinner loading treatment is disliked and slated for a redesign discussion, not a unilateral repaint.
- Suite: 238 passed, 287 subtests (4 new gate regressions).

## 7. Addendum — Option A orb redesign (2026-11-20, owner-approved)

- Status badge removed in ALL states; orbit/scan spinner ring removed. The shader presets are the sole state indicator: thinking = fast emerald swirl, conversing = violet, toolcall = amber, speaking = blue + acoustic rings, listening = emerald ripple. Listening ripple and speaking acoustic rings kept (audio telemetry, not loading furniture; speaking rings remain synthetic per owner directive).
- Gerund vocabulary retired from display — owner loved the words, hated the badge typography. Full list preserved in git history (d401e21) for a future better-designed revival.
- Wake entrance de-crashified: 500ms OutCubic unfurl for slide+scale, no OutBack overshoot bounce. Exit/dock animation unchanged (owner likes the fade-out).
- Verified with a disposable test orb against fake_daemon.py on a scratch socket: thinking/speaking screenshots show no badge, no spinner; real gideon-ui.service restarted cleanly ("Configuration Loaded", no QML errors); daemon untouched.
- Lesson: the temporary test orb rendered over the owner's active chat window and startled him ("WRONG PLACE ENTIRELY"). Announce visible test instances BEFORE launching them, or keep them off the working area.
