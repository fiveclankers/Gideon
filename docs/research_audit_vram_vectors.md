# GIDEON — Analytical Systems Audit: VRAM Envelope & Upgrade Vectors

**Auditor role:** Analytical Systems Auditor & Feasibility Filter
**Date:** 2026-10-18
**Target:** `/home/cat/gideon` (daemon PID 927, `/home/cat/jarvis/.venv/bin/python -u gideon.py`)
**Host:** Arch Linux 7.2.8-9-omarchy · Hyprland/Wayland · Intel i3-9100 (4c) · 7793 MiB RAM
**GPU:** NVIDIA GTX 1650 (TU117) — 4096 MiB total
**Method:** live `nvidia-smi`, `hyprctl`, timed `graphify` invocations, source read of `gideon.py`, `reflex_router.py`, `audio/*`, `barehands/server.py` + `stage.html`, `SYSTEM1_LYRA_BLUEPRINT.md`, `docs/gideon_operations_manual.md`, graphify graph (507 nodes / 914 edges / 29 communities).

---

## 1. Measured Baseline (not estimates)

### 1.1 VRAM ledger — the real constraint

| Consumer | MiB |
| :--- | ---: |
| **GIDEON daemon (Faster-Whisper medium.en int8, CUDA)** | **1056** |
| Chrome GPU process (WebGL circuit board / Barehands stage) | 147 |
| T3 Code GPU process | 92 |
| Antigravity GPU process | 31 |
| Driver / compositor / desktop residual | ~494 |
| **Total used** | **1820** |
| **Free** | **1905** |

**Conclusion:** the `< 2000 MiB` self-ceiling is currently *two-thirds consumed by one model*. Headroom for **any** new GPU-resident model is **≈ 944 MiB** — and that assumes Chrome, T3 Code, and Antigravity stay flat, which they do not.

Supporting: RAM `7793 total / 1509 available`, swap `15590 total / 6100 used`. **Host RAM is the tighter resource than VRAM.** Any vector that adds a resident Python process or a CPU model >300 MiB is riskier than one that adds GPU work.

### 1.2 Latency floor — where the real tax is

| Path | Measured | Notes |
| :--- | ---: | :--- |
| `parse_command()` (Tier-0 parse) | **4.2 µs** | 2000×18 iterations, in-process |
| `hyprctl dispatch movefocus l` | **5–6 ms** | full process spawn + Wayland IPC |
| `hyprctl dispatch focuswindow class:kitty` | **5 ms** | resolved target form |
| `hyprctl clients -j` (read + JSON parse, 4 windows) | **6.7 ms** | enables name-addressed focus |
| `graphify query "<q>" --budget N` | **248–278 ms** | CLI spawn + NetworkX BFS, **0 MiB VRAM** |
| `graphify god-nodes --json` | **207 ms** | CI-friendly |
| `graphify affected "try_reflex"` | 0.2 s, exit 0 | 16 reverse-reachability hits incl. tests |
| Fish Audio TTS round trip | ~3200 ms | per ops manual §2.1 |
| Antigravity TTFT → first sentence | ~2280–2685 ms | per ops manual §2.1 |

**Conclusion:** a `< 5 ms` deterministic fast path is *achievable for parsing only*; any path that touches a subprocess costs 5–7 ms. That is still 3 orders of magnitude below the ~5.9 s user-perceived speech onset, so **Tier-0 additions are effectively latency-free.**

### 1.3 Structural findings (highest-leverage, found before any new feature)

**(A) Tier-0 acks voluntarily pay a 3.2 s synthesis tax.** `try_reflex()` (`reflex_router.py:184–195`) fires the earcon *and then* calls `tts.speak("Switched to OpenCode.")` / `speak("Note saved.")`. A 5 ms operation is immediately followed by a 3.2 s Fish Audio round trip. The earcon already carries the information. **Deleting the `speak()` calls is the single highest-value change in this audit and it costs nothing.**

**(B) Lyra is dead code in production.** `lyra_enabled()` (`reflex_router.py:113–126`) requires `GIDEON_ENABLE_LYRA=1` **and** `free_mib > 1200` **and** `footprint ≤ free_mib − 1200`. With 1905 MiB free that is a **705 MiB** budget — but the daemon already holds 1056 MiB, Chrome is alive, and swap is 6.1 GiB in use. `SYSTEM1_LYRA_BLUEPRINT.md §2` is a *design document, not a deployed subsystem*. The production "Sub-Zero Reflex Router" is the **4.2 µs regex table** in `reflex_router.py`. Any spec that assumes Lyra is live is building on sand.

**(C) Spec/reality drift already exists.** `SYSTEM1_LYRA_BLUEPRINT.md:79,86` calls `tts.play_earcon("ack.wav")`. `TTSPlayer` has **no** `play_earcon` method, and `ack.wav` does not exist — `sounds/` contains exactly `acknowledged.wav` and `activate.wav`. Luna specs must be validated against the real symbol table before Big Pickle churns.

**(D) Four duplicated dispatch call sites.** `try_reflex(...) → if not handled: engine.send_prompt(...)` is open-coded at `gideon.py:1105`, `:1128`, `:1274`, `:1393`. Each new intent must be threaded through four places (plus the `arm_hot_mic` follow-up behaviour). `reflex_router.dispatch()` already exists and is used nowhere.

**(E) The graph is currently used for retrieval, not for control.** `graphify affected "try_reflex"` returns exactly the 4 dispatch sites, the `dispatch()` wrapper, and the 5 tests that cover them — in 0.2 s, 0 API tokens, 0 MiB VRAM. This is the highest-value, lowest-risk use of the 507-node graph and it should exist before any voice-query feature.

---

## 2. Feasibility Scores (1–10)

Scoring axes: `VRAM cost`, `latency`, `determinism`, `blast radius`, `spec clarity`.

| # | Vector | Score | Verdict |
| :- | :--- | :---: | :--- |
| **0** | Tier-0 latency cleanup + unified dispatch funnel + VRAM arbiter | **10/10** | Do first. Prerequisite for everything else. |
| **B** | Hyprland window management (name-addressed) | **9/10** | Highest-leverage new capability. |
| **A** | Voice-activated Graphify lookup | **7.5/10** | Ship as *lexical graph lookup + vocab gate*, **not** semantic retrieval. |
| **C** | Proactive earcons + Barehands mood airlock | **8/10** (earcon-only) / **4/10** (general proactive surface) | Split the vector; ship the narrow half. |
| **X** | Any CUDA embedding / reranker / local vector store | **1/10** | Rejected. Breaches the envelope. See §6. |

---

## 3. Vector B — Hyprland Window Management

**Score 9/10** — deterministic, zero VRAM, 5–12 ms, fully testable with `unittest.mock`.

### Mechanism
Extend `reflex_router.parse_command()` with a window-control block that **resolves names to addresses as a read step** before dispatching a write:

```
"focus the browser window"
  → hyprctl clients -j            (6.7 ms, read)
  → filter class/title, require UNIQUE match
  → hyprctl dispatch focuswindow address:0x...   (5 ms, write)
```

Measured today: `focuswindow class:kitty` already works and is unique-safe. Directly verified against the live 4-window session (`agent-chrome`, `kitty`, `com.t3tools.T3Code`, `antigravity`).

### Failure modes → safeguards

| # | Failure mode | Evidence | Safeguard |
| :- | :--- | :--- | :--- |
| B1 | **Relative dispatch is not name-addressed.** `movefocus l/r/u/d` moves from the *focused* window, can jump monitors, and can land on layer-shell surfaces. Voice means *name*. | `hyprctl dispatch movefocus l` = 5 ms but target is unknowable | Never dispatch `movefocus` for a named target. Only `focuswindow address:`. |
| B2 | **Ambiguity.** Two kitty windows → arbitrary focus. | live session has `class:kitty` ×1, but `antigravity` and `agent-chrome` coexist | Mirror Barehands' containment discipline (`server.py:225–231`): **require exactly 1 match, else 400/fall-through.** 0 or >1 → delegate to engine. |
| B3 | **Destructive dispatch allowlist creep.** `killactive`, `exit`, `exec`, `togglespecialworkspace` on a `--dangerously-skip-permissions` daemon. | daemon runs `agy --dangerously-skip-permissions` | Hard allowlist tuple; no free-form string may reach `argv`. Mandatory invariant test. |
| B4 | **`_UNSAFE_CONTEXT` over-triggering.** `\b(how|why|code|fix|script)\b` anywhere in the utterance kills the reflex. | `parse_command` returned `None` for "how do I move this window" | Correct and intended. Preserve. Add an explicit test asserting window imperatives still route (e.g. "move the terminal to workspace 2"). |
| B5 | **Named workspaces are invisible to `_WORKSPACE_RE`.** Live session has `workspace id -98 name "special:agent"`. | `hyprctl workspaces -j` → `[(1,'1'),(-98,'special:agent'),(2,'2')]` | Keep the 1–10 int guard for `workspace N`; add a **separate** named-workspace path with its own allowlist read from `hyprctl workspaces -j`. Never interpolate a raw spoken string. |
| B6 | **Submap capture.** If a submap is active, dispatches are intercepted. | `hyprctl submap` currently `default` | Read `hyprctl submap`; if not `default`, refuse and delegate. |
| B7 | **Hyprland JSON drift across versions.** Fields `focusHistoryID`, `stableId`, `pinFullscreened` are unstable. | kernel/Hyprland 0.5x churn | Parse only `address`, `class`, `title`, `workspace.id`. Wrap in try/except → fall through to engine, never crash the loop. |
| B8 | **Fullscreen semantics.** `fullscreen 0/1/2` differ (`1` = maximize, `2` = true fullscreen, `0` = toggle). "Toggle fullscreen" via `0` is state-dependent. | Hyprland docs | Prefer `fullscreen 0` (toggle) for voice, reject explicit `1/2` unless the spec asks. Read `fullscreen` from `activewindow -j` to make the ack truthful. |

### Concrete implementation steps

**GPT-6 Luna (spec):**
1. Author `docs/specs/reflex_window_control.md` with: the intent name `window_control`, the **closed verb set** `{focus, fullscreen_toggle, fullscreen_on, fullscreen_off, move_to_workspace, close?}`, and an explicit *deny list* (`killactive`, `exit`, `exec`, `resizeactive` unbounded, `movefocus` by direction).
2. Specify target resolution as a two-phase contract: `resolve(text) -> address | AmbiguousError(0|n) | NoMatch`, and *require* that `resolve` is a pure function over an injected `clients_json` fixture (testability without Hyprland).
3. Specify the address formatter as `address:0x…` string passthrough only — spec must state "no token derived from user speech is ever concatenated into `argv` except after passing a regex allowlist."
4. Specify ambiguity behaviour in one sentence: **"0 or >1 matches ⇒ `parse_command` returns `None` and the utterance is delegated to Tier-1 with the raw text unchanged."**
5. Specify the ack: earcon only. Explicitly forbid `tts.speak` in the window path.

**Big Pickle (code churn):**
1. `reflex_router.py` — add `_FOCUS_RE`, `_FULLSCREEN_RE`, `_MOVE_WS_RE`, and resolver:
   ```python
   async def _resolve_window(query: str) -> str | None:  # returns "address:0x..."
       code, out = await run_fixed(["hyprctl", "clients", "-j"], timeout_s=0.8)
       if code != 0: return None
       wins = json.loads(out)
       hits = [w for w in wins if _match(w, query)]
       return f"address:{hits[0]['address']}" if len(hits) == 1 else None
   ```
2. Extend `parse_command` to emit `{"intent": "window_control", "op": "...", "query": "..."}` — **resolver runs inside `try_reflex`, not in `parse_command`** (keeps parse at 4.2 µs and keeps parse pure/hermetic).
3. Add an `argv` validator in `run_fixed` or a `_SAFE_DISPATCH` frozenset that asserts `argv[0] == "hyprctl" ⇒ argv[1:3] == ["dispatch", <allowed verb>]`.
4. `tests/test_reflex_router.py` — add ~14 cases covering B1–B8 using a `clients -j` fixture; add one test asserting `parse_command("killactive")` is `None`.
5. Run `graphify affected "parse_command" --depth 2` and confirm the test set covers every hit.

---

## 4. Vector A — Voice-Activated Graphify Lookup

**Score 7.5/10** — but **only** as a vocabulary-gated, seed-constrained *lexical graph lookup*. Not semantic retrieval. There is no embedding model in graphify; `query` is case-folded substring + IDF over node labels and file paths.

### Mechanism
```
"Gideon, query the graph about the TTS sanitizer"
  → intent: graph_lookup, topic: "TTS sanitizer"
  → graphify query "<expanded tokens>" --budget 400    (255 ms, 0 MiB VRAM)
  → exit code gate (0 / 3 ambiguous / 4 no-match)
  → speak ≤2-sentence deterministic summary  OR  inject full text into Tier-1 context
```

### Decisive evidence (measured, this session)
```
$ graphify query "hyprctl window tiling" --budget 400
Graph: graphify-out/graph.json (507 nodes) | Start:
  ['Talk Buffer Zone (Hot-Mic Window)', 'Simulates sliding window subtitle streaming…']
```
**That is a silent, high-confidence false positive.** `hyprctl` is not a node — it is a string literal inside `reflex_router.py`. The matcher latched onto "window"/"streaming". `--budget` is a *display* cap; it does not gate relevance, and exit code was `0` (success). A voice agent that speaks this result is worse than no feature.

### Failure modes → safeguards

| # | Failure mode | Evidence | Safeguard |
| :- | :--- | :--- | :--- |
| A1 | **Vocabulary mismatch is the default, not the edge case.** No stemming, no synonyms, no cross-language. Whisper adds filler words. | see `query "hyprctl window tiling"` above; documented in `references/query.md` Step 0 | **Mandatory Step-0 vocab expansion**: build `graphify-out/.vocab.txt` from node labels, intersect the *spoken* topic against that list, and if the intersection is empty, **refuse** ("no graph vocabulary for that") rather than query. Never query raw speech. |
| A2 | **Exit code 0 ≠ relevance.** Ambiguous is 3, no-match is 4, but a misfire is 0. | measured `exit 0` on a garbage result | Require a second gate: ≥1 start node whose label shares a token with the expanded query, **and** ≥N total nodes. Otherwise refuse. |
| A3 | **Truncation illusion.** BFS depth 2 from 507 nodes routinely yields 100+ nodes; default budget 2000 hides the rest behind a `[!] TRUNCATED` marker. | `100 nodes found` / `showing 67 of 100` | Pin `--budget 400`. Parse the `TRUNCATED` marker and make the spoken summary state it ("showing N of M connectors"). |
| A4 | **TTS mangling of graph output.** Output is file paths, `L###` locations, and English docstrings. `clean_text_for_tts` reduces paths to basenames and redacts URLs — the result is unlistenable and 30 s long. | `gideon.py:416–460` | **Never speak raw subgraph.** Spoken form is ≤2 sentences generated from the top-3 node labels only. Full text goes to (a) the engine session as context, or (b) a Barehands `add_card` — both are non-audio. |
| A5 | **Daemon stall.** A hung `graphify` (Python import, lock on graph.json) inside the event loop. | CLI spawn measured 255 ms | Reuse `run_fixed`'s 1.5 s timeout; map 124/126/127 to **fall-through to Tier-1**, never to a fake success ack. |
| A6 | **Naming drift creates false confidence.** Calling this "semantic retrieval" invites someone to "improve" it with a local embedding model. | this audit brief used the phrase "semantic retrieval" | Name it `graph_lookup` in code, spec, and user-facing doc. Add a spec line: *"Graphify traversal is lexical; do not add a GPU embedding stage without re-opening the VRAM ledger in §1.1."* |
| A7 | **Graph staleness.** Hooks/incremental updates mean `graph.json` may lag the working tree. | `graphify-out/` carries `.graphify_labels.pending.json`, `graph.json.pre_rectify_bak` | On successful lookup, log the graph mtime. If `git status` is dirty for indexed sources, prefix the spoken answer with "from a possibly stale graph". |

### Concrete implementation steps

**GPT-6 Luna (spec):**
1. `docs/specs/reflex_graph_lookup.md`: define `graph_lookup` intent, the **three-gate admission contract** (vocab-intersection ≥ 1 token → start-node token match → exit code ∈ {0}), and the refusal strings for each gate.
2. Specify the **summary generator** as a pure function `summarize(subgraph_text, truncated: bool) -> str` with a hard ≤ 280-character output cap, forbidding any `L\d+` or `/` character in the spoken form.
3. Specify the **sink policy table**: `speak: short summary (≤280 ch)` · `context_inject: full text → engine.send_prompt(prefix="[graph] ")` · `board_card: full text → barehands /cmd add_card`. Default sink = `context_inject` + earcon; `speak` only on explicit "tell me about".
4. Specify rejection of the MCP-HTTP sidecar for now: **"a resident `graphify-mcp` HTTP server is rejected at this time — it trades 250 ms of CPU for ~90 MiB of scarce host RAM (1509 MiB available) and a new failure domain."** Revisit only if p95 CLI latency > 600 ms.

**Big Pickle (code churn):**
1. `scripts/build_graph_vocab.py` — emit `graphify-out/.vocab.txt` from `graph.json` node labels (reuse the tokenizer from `references/query.md` Step 0); wire into the post-`graphify update` path.
2. `reflex_router.py` — `_GRAPH_RE` → `{"intent": "graph_lookup", "topic": "..."}`; add `_expand_topic()` (vocab intersection), `_graph_query()` (via `run_fixed`, `["graphify","query",q,"--budget","400"]`), `_admit()` (three gates), `summarize()` (pure).
3. `tests/test_graph_lookup.py` — table-driven: (a) unknown topic → refusal, (b) known topic → `nodes_found > 0`, (c) simulated exit 4 → refusal, (d) `summarize` never emits `/` or `L\d+`, (e) timeout 124 → returns `False` so Tier-1 handles it.
4. Add an integration assertion: `graphify query` is never invoked for a topic whose vocab intersection is empty (mock + assert not called).

---

## 5. Vector C — Proactive Earcons + Barehands Mood Airlock

**Score 8/10 for the earcon half · 4/10 for a general proactive-notification surface.**

### Mechanism (as it exists today)
- `audio/sounds.py:play_sound()` → `pw-play --media-role Notification -P application.name=GIDEON` (fire-and-forget `Popen`).
- `SignalBus.set_state()` (`gideon.py:209–230`) mirrors `state` + `mood.json` into `/home/cat/barehands/state/`, mood ∈ `{green, amber}`.
- `stage.html:472` `MOODT: { red: [255,82,82], amber: [255,179,71], green: null }`; the stage polls `/orb` every **120 ms**.
- `barehands/server.py:125` `_ALLOWED` has **no mood action** — mood is a Gideon-written side channel, not a board command.

### Failure modes → safeguards

| # | Failure mode | Evidence | Safeguard |
| :- | :--- | :--- | :--- |
| C1 | **Acoustic collision / barge-in feedback loop.** An earcon during speech echoes into the mic → `on_barge_in` interrupts Gideon → cascading interrupts. | `AntigravityEngine._watchdog_loop` already implements an "Acoustic Collision Guard" (`gideon.py:584–586`) | Promote that guard into a **shared `_audio_busy()` predicate** and route *every* proactive sound through it. Refuse if `tts._is_speaking`, `not tts._queue.empty()`, or state ∈ `{speaking, listening, hotmic}`. |
| C2 | **Mood is last-writer-wins with a 120 ms poll.** `set_state` writes `mood.json` on every call (~45 Hz from the recorder). A notification mood is clobbered before the stage renders it. | `stage.html:474–480`; `gideon.py:220–226` | Do **not** add a `notify` mood. Keep mood strictly *derived from state* and carry notification identity in the **earcon** instead. If a transient mood is required, add `mood.json` as `{mood, ts, ttl_ms}` and have `set_state` skip the write when `state` is unchanged — which also removes ~45 Hz redundant disk I/O. |
| C3 | **Notification storms / attention vandalism.** A `--dangerously-skip-permissions` daemon that also volunteers audio is an annoyance amplifier. | daemon flag | Ship a **closed trigger allowlist** (see below) + a global min-interval cooldown (≥1500 ms) + a `dnd` reflex that disarms all proactive audio. |
| C4 | **Silent-failure earcons.** `play_sound` swallows every exception; a missing file is indistinguishable from a successful chime. | `audio/sounds.py:12–31` | Add a module-level `_WARNED: set[str]` and log once per missing filename at WARNING. Add a startup assertion that every referenced sound exists. |
| C5 | **Spec references non-existent symbols.** | `SYSTEM1_LYRA_BLUEPRINT.md:79,86` → `tts.play_earcon("ack.wav")`; neither the method nor the file exists | Luna must publish a **symbol-existence check** per spec (grep the real file). Big Pickle adds `sounds/` filename constants in one place (`audio/sounds.py:SOUND_ACK = "acknowledged.wav"`). |

### Recommended trigger allowlist (closed set)
1. **Turn complete** — *this is the real gap*: there is currently no completion chime. Add `activate.wav` on `is_turn_active: True → False`.
2. **Tool-call start/end** — differentiated earcons (`acknowledged.wav` start, soft blip end) subsuming the 45 s watchdog.
3. **Watchdog check-in** — already exists; keep, but route through `_audio_busy()`.
4. **STT GPU→CPU fallback** — currently log-only (`gideon.py:760–775`). This degrades transcription quality silently; it *must* be audible. `acknowledged.wav` + a one-line spoken notice once per session.
5. **Engine switch** — exists; earcon only, **drop the `speak()`** (§1.3-A).
6. **Tier-0 reflex ack** — exists; earcon only, **drop the `speak()`**.

**Explicitly excluded:** calendar/weather-style proactive pings, "ambient" mood animation, anything driven by an LLM deciding to interrupt. That is the 4/10 half.

### Concrete implementation steps

**GPT-6 Luna (spec):**
1. `docs/specs/audio_feedback_contract.md`: the `_audio_busy()` predicate definition, the closed trigger allowlist above, cooldown `≥1500 ms`, `dnd` semantics, and the rule **"no Tier-0 path may call `tts.speak`"**.
2. Specify the sound symbol table: `SOUND_ACK="acknowledged.wav"`, `SOUND_ACTIVATE="activate.wav"` and require that no spec names a `.wav` literal anywhere else.
3. Specify mood as **state-derived only**, with the exact mapping table, and require `set_state` to be a no-op write when `state` is unchanged.
4. Specify `dnd`: an in-memory boolean on the bus, defaulting off, persisted nowhere (survives only the daemon lifetime) — and exposed as an intent.

**Big Pickle (code churn):**
1. `audio/sounds.py` — add `SOUND_ACK` / `SOUND_ACTIVATE` constants, `_WARNED` once-per-file logging, `interactive: bool` param that routes through a caller-supplied busy predicate.
2. `gideon.py:SignalBus.set_state` — dedupe writes (`if state == self.current_state and not detail: return`), keep mood mapping, drop the redundant `mood.json` churn.
3. `reflex_router.py:try_reflex` — delete `tts.speak(...)` from `flash_note` and `engine_switch`; keep `play_sound`.
4. `gideon.py:_read_loop` / `AntigravityEngine` — fire `SOUND_ACTIVATE` on turn completion; add the STT-fallback earcon in the OOM branch.
5. Tests: `tests/test_audio_feedback.py` — assert (a) no `tts.speak` is reachable from any `intent` in the Tier-0 table, (b) cooldown suppresses back-to-back earcons, (c) `set_state` does not rewrite identical state, (d) every constant in `audio.sounds` resolves to an existing file.

---

## 6. Rejected Vector X — Any CUDA Embedding / Reranker / Local Vector Store

**Score 1/10.** Do not build.

| Proposal | Why it fails |
| :--- | :--- |
| Local embedding model on GPU (bge/e5/nomic) for "true" semantic graph search | Torch CUDA context alone ~300–500 MiB before weights; +130–500 MiB model. Current free = 1905 MiB; silent breach of the 2000 MiB self-ceiling the moment Chrome grows or a second Whisper process spawns. |
| Local reranker on GPU | Same, worse — cross-encoders are 10–100× the compute of the bi-encoder for ~400 ms of gain on a path that already costs 255 ms. |
| Resident vector DB (FAISS/Chroma) | 507 nodes do not need an index. `graph.json` is ~1 MB; a dict lookup in-process is faster than any ANN structure at this n. |
| Loading Lyra (`laya_intent_v8`) "because it exists" | `lyra_enabled()` gate demands `footprint ≤ free − 1200` ⇒ **705 MiB** today, on a box with 6.1 GiB of swap in use. The regex router is 4.2 µs and covers the intents that matter. |
| `graphify-mcp` HTTP sidecar as a permanent service | Not a VRAM issue — a **RAM** issue (1509 MiB available) and a new daemon to supervise. Revisit only on measured p95 > 600 ms. |

**Permitted GPU work, in priority order:** (1) nothing; (2) whispering budget reallocation (below); (3) a *leased* small intent model only if `vram_arbiter` grants it AND `nvidia-smi` confirms ≥ 1200 MiB free *after* Whisper.

---

## 7. High-Leverage Architectural Upgrades (ordered)

### U1 — Single-writer VRAM arbiter (`vram_arbiter.py`) — *do first*
Today `lyra_enabled()` (`reflex_router.py:113`) does an ad-hoc `nvidia-smi` spawn and an env-var footprint guess. Replace with a lease model:
`arbiter.request("whisper", mib) -> Lease | None`, `arbiter.resident()`, one cached `nvidia-smi` poll at ≤1 Hz, and a hard invariant `sum(resident) ≤ 2000`. Any future GPU feature must acquire a lease. This is what makes the ceiling *enforceable* rather than documented.

### U2 — Preload-on-wake for Whisper (frees ~1056 MiB when idle)
Whisper holds **1056 MiB permanently** to serve a **7.1 ms** inference that fires a handful of times per minute. Wake-word detection is **CPU** (openWakeWord ONNX) and the mic pre-rolls, so first speech does not need Whisper yet. Lazy-load on `on_wake` and unload after N seconds of `idle` with an empty queue.
**This is a bench-first change, not a certainty:** the cold-load time for `medium.en` int8 from the HF cache is *unmeasured* (deliberately not measured here — allocating 1056 MiB on a live box with 6.1 GiB of swap in use was judged an unacceptable experiment risk). Big Pickle must benchmark it behind `GIDEON_WHISPER_LAZY=1` with a hard revert, and the guard must refuse to unload while `current_state != idle` or `tts._queue` is non-empty.

### U3 — Collapse the four dispatch sites into `reflex_router.dispatch()`
`gideon.py:1105`, `:1128`, `:1274`, `:1393` re-implement the same sequence. `dispatch()` already exists and is unused. One funnel = one place for `dnd`, one place for the graph/`arm_hot_mic` follow-up rule, and one place for tests to hook.

### U4 — Declarative reflex registry replacing the if/elif chain
`parse_command` is ~90 lines of sequential matching and will triple with three new vectors. Move to a table: `(name, compiled_pattern, extractor, allowlist, ack_sound)` with a schema, and a **mandatory invariant test** that no free-form spoken token reaches `argv`.
Direct payoff: Luna's spec maps 1:1 to table rows, and Big Pickle's churn cannot collide across vectors.

### U5 — Graph-driven change-impact gate in CI
`graphify affected "try_reflex" --depth 2` returns 0.2 s / 0 API cost / 0 MiB. Wire it so that any diff touching a god node (`SignalBus`, `main`, `OpenCodeEngine`, `try_reflex`, `TTSPlayer`, `parse_command`) or its 1-hop dependents *requires* the regression suite to run. This is the **only** graph feature with zero failure modes, and it should ship before the voice-query feature.

---

## 8. Handoff Summary

| Deliverable | Owner | Prereq |
| :--- | :--- | :--- |
| `docs/specs/reflex_window_control.md` (§3) | GPT-6 Luna | — |
| `docs/specs/reflex_graph_lookup.md` (§4) | GPT-6 Luna | U4 recommended |
| `docs/specs/audio_feedback_contract.md` (§5) | GPT-6 Luna | — |
| `docs/specs/vram_arbiter.md` (U1) | GPT-6 Luna | — |
| Symbol-existence check in every spec (C5/§1.3-C) | GPT-6 Luna | mandatory |
| `parse_command` → registry (§U4) | Big Pickle | U4 spec |
| `_resolve_window` + window intents (§3) | Big Pickle | U4 or additive |
| `dnd` + earcon-only Tier-0 acks (§1.3-A, §5) | Big Pickle | — |
| graph vocab builder + `graph_lookup` (§4) | Big Pickle | `graphify update .` |
| `vram_arbiter.py` (U1) | Big Pickle | — |
| `dispatch()` funnel (U3) | Big Pickle | after U4 |
| Whisper lazy-load **behind env flag** (U2) | Big Pickle | bench + revert plan |
| CI impact gate via `graphify affected` (U5) | Big Pickle | — |

**Ordering:** U3/U5 (safe, no spec needed) → U4 registry → B → C → A → U1 → U2 (bench-gated, last).

**Post-change:** run `graphify update .` (AST-only, no API cost) and `unittest` suites in `tests/`.

---
*Measurements taken live on 2026-10-18 against the running daemon. VRAM and latency figures are single-sample; treat as order-of-magnitude, not SLA.*
