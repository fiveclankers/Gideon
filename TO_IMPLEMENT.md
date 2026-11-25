# Future Ideas & Deferred Features (`gideon-win`)

Items intentionally kept out of v1 to keep Windows Gideon ultra-lean, predictable, and gaming-safe, ready to pick up whenever wanted.

---

## 1. Visual Screen Annotation / Pointing Overlay (The "Circle & Arrow" Toy)
- **Concept:** Ask *"where is X?"* or *"circle the button to click"*, take a screenshot, and draw a subtle glowing animated ring / arrow on screen pointing directly at the target UI element without stealing window focus.
- **Implementation Notes:**
  - Create a click-through, transparent Win32 layered window (`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW`) using `ctypes` or a tiny C#/.NET helper so mouse clicks pass right through to the underlying app.
  - Because raw pixel coordinate guessing by LLMs is flaky on dense desktops/games, overlay an in-memory **Set-of-Mark coordinate grid** (`A1..H6`) onto the temp screenshot sent to the model, then map the returned cell/offset back to screen pixels and auto-fade after 4–5 seconds.
  - Respect `win_tools.is_fullscreen_active()` so it never pops over exclusive fullscreen games unless requested.

---

## 2. Voice-to-Keyboard Typing Control
- **Concept:** Say *"type <text>"* or *"dictate into this box: ..."* and have Gideon type directly into the currently focused window.
- **Why Deferred / Safety Guardrails Required:**
  - Firing `SendInput` (`KEYEVENTF_UNICODE`) blindly from voice STT is risky if you're focused inside a terminal, IDE, or game (accidental hotkeys, `Enter` executing commands, or anti-cheat flagging synthetic input).
  - **Recommended v2 design:**
    - Require an explicit Decision 1 intent (`keyboard_type`) + confirmation or safe mode (`clipboard + optional Ctrl+V` vs `SendInput` unicode typing).
    - Never send `Enter` automatically unless explicitly instructed (*"...and hit enter"*).
    - Pause/block when `is_fullscreen_active()` is true unless explicitly overridden.

---

## 3. Native Glowing Orb Overlay (Windows Equivalent of Quickshell Orb)
- **Concept:** Port `ui/OrbShader.frag` into a tiny native Direct2D / D3D11 or C# `WebView2`-free layered window (~10–25 MB RAM, 0 MB VRAM when hidden/idle) that pulses during `listening` / `thinking` / `speaking` and hides automatically during fullscreen games.
- **Integration:** Communicate over a lightweight Windows Named Pipe (`\\.\pipe\gideon-ui`) mirroring `SignalBus` state transitions.

---

## 4. Optional Groq Cloud STT Drop-In (~250 ms latency)
- **Status:** `stt_win.py` already supports `GROQ_API_KEY` automatically (`GIDEON_STT_BACKEND=auto`).
- **To activate:** Create a free Groq API key and add `GROQ_API_KEY=gsk_...` to `C:\Users\cat\.config\gideon\gideon.env`. Gideon will immediately switch from local on-demand `whisper-cli.exe` to Groq `whisper-large-v3-turbo` with local `whisper.cpp` kept as offline fallback.

---

## 5. Rebinding Push-to-Talk to Mouse Button 5 (`xbutton2`)
- **Status:** `gideon_win.py` already supports `GIDEON_PTT_HOTKEY=xbutton2` (Mouse 5) or `xbutton1` (Mouse 4), defaulting to `alt+space` so it does not collide with Wispr Flow.
- **To switch:** Unbind Mouse 5 in Wispr Flow (or replace it) and add `GIDEON_PTT_HOTKEY=xbutton2` to `C:\Users\cat\.config\gideon\gideon.env`.
