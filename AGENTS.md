## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Query the graph on judgement, not as a mandatory first step:
- Reach for `graphify query "<question>"` when you do not yet know which file or symbol to look at, when a grep would return hundreds of hits, or when you need cross-file impact ("what calls this", "what breaks if I change it"). Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts; these return a scoped subgraph, usually smaller than GRAPH_REPORT.md or raw grep output.
- Go straight to grep/glob/read when the target path or symbol is already known or inferable, when the answer is a specific function body or a literal value (the graph indexes symbols and call edges, not literals), or when the repo is small and well-named enough that one targeted grep beats a subgraph.
- Carry that judgement into subagent prompts: tell explorers to query the graph only when they don't know where to look, not unconditionally.

Maintenance:
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.

## Contact Mappings
- When referring to Nihal or Sinha/Sina, map directly to Sinh / sinhzeva on Instagram.

## Instagram Automation Directives
When asked to send an Instagram DM:
1. Never wander through search bars, UI dialogs, or multiple exploratory clicks.
2. If messaging a known contact (e.g. Sinh / sinhzeva), navigate directly to their thread or open `/direct/inbox/` and click them from the list.
3. Target the chat input element (`contenteditable="true"[role="textbox"]`), click it once, and dispatch the message using `browser_type` with `press_enter=True`.
4. Never attempt synthetic JavaScript `execCommand` injections or duplicate keystroke events that cause duplicate sends.
5. Do not prompt for confirmation or pause for multi-turn verification unless explicitly requested.

## Music Playback Directives
- Never use generic `playerctl play` or `playerctl` toggles when playing music, as browser tabs (Chrome/Chromium) with open YouTube/video tabs will intercept MPRIS and play unwanted video audio.
- For music playback, always route directly to the background music daemon via `/tmp/music-mpv.sock` (e.g. using `echo '{ "command": ["loadfile", "ytdl://ytsearch:<query>", "replace"] }' | socat - /tmp/music-mpv.sock`).
- Always ensure `/tmp/music-mpv.sock` is unmuted and playing properly rather than unpausing ambient browser media.

## Clipboard Directives
- Always copy directly to the Wayland clipboard (`wl-copy`), ensuring `WAYLAND_DISPLAY` (typically `wayland-1`) is set if in a subshell without inherited environment variables. Do not rely on X11 clipboard tools.

## Web Component & Modern SPA Interaction Directives
- Modern web frameworks (Polymer, React/Lexical, Lit, Angular, Shadow DOM components) often attach listeners to Pointer/Mouse events rather than basic DOM clicks.
- Never rely on simple synthetic `.click()` or raw text injection on dynamic modern apps (YouTube, Meta/Instagram, GitHub, etc.). Always dispatch the complete pointer/mouse sequence (`pointerdown`, `mousedown`, `pointerup`, `mouseup`, `click`) or use driver-level hardware typing (`browser_type` with key events) to ensure actions penetrate shadow roots and trigger framework state changes.
