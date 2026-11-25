# Runtime and signal bus

This document describes the message paths implemented in `gideon.py` and `ai-visualizer/server.py`. Gideon's main daemon and the standalone visualizer server share visualizer conventions, but they are separate processes with different bus readers.

## Main daemon

`gideon.py` starts the voice pipeline, an embedded HTTP server, and the Quickshell Unix socket server:

```text
sounddevice input
      |
      v
Parakeet STT ---> reflex_router.py ---> local action
      |                    |
      |                    +-------------> handled
      v                                  (no engine request)
codex / agy <---------------------------- unmatched prompt
      |
streamed reply -> Fish Audio -> pw-play
      |
      +--> SignalBus state / waveform / transcript
             |                         |
             v                         v
     HTTP on 127.0.0.1:8790     /tmp/gideon-ui.sock
```

The embedded HTTP server serves the HUD at `http://127.0.0.1:8790/` and binds only to loopback. The visualizer pages poll its endpoints; the desktop overlay receives newline-delimited JSON over the Unix socket.

## Engine persona and per-turn context

The default backend is `CodexEngine` (`codex_engine.py`): one persistent
`codex app-server --listen stdio://` subprocess speaking JSON-RPC 2.0 over
stdio, under an isolated `CODEX_HOME`
(`~/.local/share/gideon/codex_home`) with hooks/apps disabled and only the
`second-brain` and `agent-browser` MCPs plus live `web_search` enabled.
`CODEX_VOICE_PREAMBLE` is passed once as `developerInstructions` in
`thread/start`; turns run via `turn/start` with a per-turn `effort` override
(protocol-verified), `approvalPolicy: never`, and `sandbox: danger-full-access`.
Interrupts use `turn/interrupt` against the observed turn id.

The Antigravity backend (`agy`) is `ACPAntigravityEngine` (`acp_antigravity_engine.py`), which launches `agy acp` speaking Agent Client Protocol (newline-delimited JSON-RPC 2.0 over stdio). Its persona is delivered once during `session/new` via `_meta.antigravity.systemPrompt.append`. Turns stream live via `session/prompt` and `session/update` (`agent_message_chunk`, `tool_call`), model selection runs via `session/set_config_option` (`configId: "model"`), and tool permissions are answered natively via inbound `session/request_permission`. Live barge-in uses `session/cancel` (notification) and awaits terminal settlement. The legacy NDJSON engine (`AntigravityEngine`) is retained as `agy-headless` for headless rollback and extreme effort levels (`xhigh`, `max`).

Before engine dispatch, explicit selected-text requests are prepared by
`selected_context.py`. The current selection is wrapped as reference material
alongside the request and a short spoken-prose reminder. This context is read
for each qualifying request, including the first turn; ordinary turns do not
read the clipboard. The persona itself is static, not regenerated per turn.

## Advanced engine configuration

Codex (`codex`) is the default backend; Antigravity (`agy`) is the fallback
and is selected automatically if Codex fails to start. Choose explicitly with
`--engine codex|agy` or `GIDEON_ENGINE`. The OpenCode sidecar was removed.

At runtime the Tier-1 `engine_switch` voice intent calls `bus.switch_engine`
("switch to Antigravity", "use Codex"), which shuts the old engine down,
starts the new one, and rebinds the wake listener. Codex effort changes are
in-thread (no respawn); agy effort changes resume the conversation with the
new `--effort` flag.

## Embedded HTTP interface

> **Deprecated 2026-11-20:** the browser HUD/PTT web portal is no longer used
> and the server does not start by default. Set `GIDEON_ENABLE_HUD=1` to
> re-enable it. Voice input runs through the wake word / hot mic; the
> Quickshell orb socket is unaffected. The routes below document the opt-in
> mode only.

### Read endpoints

- `GET /state` returns a JSON object with `state`, `level`, `samples`, `bands`, `user`, `reply`, `tool`, `effort`, `wake`, `alert`, `loading`, and `rate_limits`. `samples` contains the daemon's current 64 waveform values; `bands` is its first 32 values. `state` is the daemon's current state string and is not restricted to the standalone server's four-state set.
- `GET /config` returns the embedded HUD's name, badge, selected face, thinking-sound setting, and face list.
- Other GET paths serve files under `ai-visualizer/` (the root URL maps to `/faces/board/index.html`).

### Control endpoints

The browser uses POST requests to the following local routes:

| Route | Request | Effect |
| --- | --- | --- |
| `/api/talk/start` | Empty body | Opens the microphone recorder and returns `{"status":"recording"}`. |
| `/api/talk/stop` | Empty body | Stops capture, returns `{"status":"processing"}`, then transcribes and routes any captured audio asynchronously. |
| `/api/prompt` | `{"text":"..."}`; optional `effort` | Routes a text prompt and returns `{"status":"queued"}`. Empty text returns HTTP 400. |
| `/api/effort` | `{"effort":"..."}` | Requests an engine effort update when supported; returns `{"status":"ok"}`. |
| `/api/wake` | `{"enabled":true}` | Enables or disables the wake listener; disabling it stops an active wake capture and prevents hot-mic re-arming; returns `{"status":"ok","enabled":<bool>}`. |
| `/api/switch-to-orb` | Empty body | Rebroadcasts current state to the overlay and returns `{"status":"switched_to_orb"}`. |

These routes are implemented by the embedded server; they are not available from the standalone server. They are unauthenticated and loopback-bound in the current implementation.

### Explicit selected-text context

The daemon's terminal, browser, and wake-word dispatch paths share an opt-in
selected-text preparer. Ordinary prompts are passed through unchanged. Requests
such as `what does this mean?`, `read this and summarize`, `look at my clipboard`, or
`with selected text: ...` read the Wayland primary selection first and fall back
to the normal clipboard through `wl-paste`; the text is capped at 6,000
characters and wrapped as untrusted reference material before being sent to the
active engine. No clipboard contents are read for ordinary prompts. An explicit
`and copy the answer` suffix writes the completed response with `wl-copy`.

## Quickshell socket protocol

The daemon unlinks any existing `/tmp/gideon-ui.sock` before binding a Unix stream socket. Each event is one compact JSON object followed by a newline. On connection, the server first sends the current state. It then broadcasts:

| `event` | Fields | Meaning |
| --- | --- | --- |
| `state` | `val`, `detail`, `engine` | Current daemon state and human-readable detail; `engine` is the active engine id (codex\|agy). |
| `rms` | `val` | Current normalized audio-level value; the daemon publishes it during microphone input and speech playback. |
| `transcript` | `speaker`, `text` | Transcript text from `user` or `gideon`. |
| `graph_query_result` | `query`, `nodes`, `status` | Background graph query result; `nodes` contains up to three labels and `status` is `ok` or `empty`. |
| `ping` | `ts` | Unix timestamp heartbeat, sent every 2.5 seconds while clients are connected. |

The socket is a server-to-client event stream. The daemon reads incoming bytes only to detect client disconnects; it does not parse commands sent by clients.

## File signal bus

`gideon.py` writes runtime files under `signals/`. It creates the directory when it starts.

| File | Format | Writer behavior |
| --- | --- | --- |
| `.voice_state` | UTF-8 state string | Replaced through `.voice_state.tmp` when state changes. |
| `.voice_waveform` | JSON object: `{"ts": <unix time>, "samples": [<float>, ...]}` | Replaced through `.voice_waveform.tmp` as waveform updates arrive. The daemon publishes 64 values. |

The waveform file's `samples` values are floats. Their scale depends on whether they came from microphone input or the speech playback animation; consumers should normalize the current frame rather than assume a fixed amplitude unit. The timestamp lets readers discard stale frames.

## Standalone visualizer interface

`ai-visualizer/server.py` is an independent Python standard-library HTTP server. It binds to `127.0.0.1` and defaults to port `8790`; override it with `--port PORT` or the optional `port` field in `ai-visualizer.json`.

The standalone server reads files from `bus_dir` in `ai-visualizer.json` (defaults to the visualizer directory itself):

| File | Interpretation |
| --- | --- |
| `.voice_state` | `idle`, `listening`, `thinking`, or `speaking`; unknown or missing values become `idle`. |
| `.voice_waveform` | JSON `{"ts": ..., "samples": [...]}`; a frame older than 0.6 seconds is treated as stale. Fresh waveform data marks the returned state as `speaking`. |
| `.voice_alert` | A non-empty file sets `alert: true`. |
| `.voice_loading_pid` | File existence sets `loading: true`. |
| `.voice_rate_limits` | Optional JSON object returned as `rate_limits`. |

`GET /state` returns `state`, `level`, `samples`, `alert`, `loading`, and `rate_limits`. `GET /config` returns the configured display name, badge, face, thinking-sound flag, and discovered faces. The browser polls `/state` about eight times a second. `--mock STATE` supplies a synthetic state for visual preview without a producer.

The standalone file reader expects the four-state vocabulary above. Gideon's embedded server instead exposes its richer in-memory state directly, so an external consumer should handle additional values such as `conversing` or `toolcall`.

## Boundaries and known integration gap

Starting the standalone server and the Gideon daemon on the default port conflicts. To view the standalone server while the daemon runs, use another port from the visualizer directory, for example `cd ai-visualizer && python3 server.py --port 8791 --no-open`, and set its `bus_dir` to `../signals` if it should read daemon waveform/state files. The daemon itself does not use the standalone server's file reader: its embedded HUD reads state directly from `SignalBus` memory.
