# GIDEON subtitle and animation design notes

Recorded 2026-10-27. Status: design options, not an implemented redesign.

## User direction

Completely replace the subtitle presentation. The current pill/card, lighting,
and decorative telemetry feel generic. Aim for distinctive typography, useful
motion, and a quieter desktop presence. Antigravity remains the conversational
engine; configured reminders and memory belong to its tools and instructions.

## Current implementation and reference

GIDEON uses Quickshell with Qt Quick/QML and a Wayland overlay layer. Elfie uses
GTK layer-shell windows containing transparent WebKit views. Layer-shell places
the window; QML or WebKit renders its contents. WebKit is not required for rich
animation.

In `ui/shell.qml`, the pending subtitles use a 244-pixel card, 12-pixel text,
three-line elision, the last 320 characters, and a fixed 6.5-second timeout.
The card includes a speaker label, accent stripe, and colored border. Binding
`visible` directly to content presence cuts off its exit opacity animation.

In `gideon.py::TTSPlayer.play_wav()`, sentence text is broadcast immediately
before launching playback. The speaking waveform and RMS are generated from
sine functions rather than the audio samples. No word timing is sent today.

Earlier source inspection of Elfie found borderless Canvas subtitles,
three-word chunks, staggered character arrivals with overshoot, and tumbling
exits. Its subtitle timing estimates 2.9 words/second, rather than using word
timestamps. Its emblem combines rotating geometry, particles, and coordinated
state transitions. This comparison is source-based, not a live visual review.

## Subtitle directions

| Direction | Appearance and motion | Tradeoff |
| --- | --- | --- |
| Editorial kinetic type | Three-to-five-word phrases; words rise a few pixels, settle, then leave together. Strong typography, monochrome outline. | Recommended balance of character and legibility. |
| Cinema captions | One or two complete lines; short fades or vertical wipes at natural phrase boundaries. | Quietest option; less playful, easiest for long answers. |
| Karaoke focus | Phrase stays stationary; current word brightens or gains an underline as spoken. | Best with real word timestamps; estimated timing can visibly drift. |
| Mechanical typesetting | Words slide into aligned positions; baseline stays fixed; occasional split-flap or stamped emphasis. | Distinctive instrument-panel feel; flipping every letter becomes tiring. |
| Elastic / Elfie-inspired | Staggered word or character entrances; small overshoot; brief gravity/tumble departure. | Most lively; constrain movement so reading remains easy. |
| Ink / stencil reveal | Text appears through a directional mask, brush-like edge, or fine stipple dissolve. | Strong identity without glow; custom masks/shaders require more work. |
| Rolling transcript | Current phrase stays prominent while the previous phrase moves upward and fades. | More reading context, but needs additional screen space. |
| Orb-origin captions | Words travel a short distance outward from the companion, then lock onto a reading baseline. | Connects voice to the character; reserve long arcs for transitions. |

Avoid recentering the line every time another word arrives. Lay out the complete
phrase first and reveal words in their final slots. Animate word delegates for
normal speech; per-character delegates complicate ligatures and joined scripts.

## Recommended first prototype

- Bottom-center, independently positioned from the corner companion; alternatively
  a wide text area beside it. Do not squeeze larger text into the existing card.
- Start around 26–32 QML pixels, adjusted on the actual display. Use one verified
  installed font or a bundled licensed font, with an explicit fallback strategy.
- Off-white semibold text with a narrow dark outline; no background card,
  colored border, accent stripe, speaker heading, or bloom.
- Punctuation-aware phrases of roughly three to five words, wrapping to two
  lines if necessary. Show every spoken phrase rather than truncating the tail.
- Proposed entrance: 6–10 pixels upward over 160–220 ms, with a small settle;
  20–35 ms word stagger. These are tuning values, not measured optimums.
- Hold the text still during reading. Proposed exit: 120–180 ms fade and a
  short directional shift. Retain the outgoing item until its animation ends.
- Minimal mode: same phrasing and position, with fades only.

## Speech pacing comes first

1. Keep the displayed utterance separate from the engine's context wrapper.
2. Emit playback lifecycle metadata: utterance ID, sentence ID, duration,
   start, completion, and cancellation. Reset queued captions on interruption.
3. Read duration from the actual WAV. Allocate phrase durations using word
   lengths and punctuation pauses as an explicitly approximate fallback.
4. For precise karaoke highlighting, use provider timestamps if available or
   forced alignment. WAV duration alone cannot locate each spoken word.
5. Schedule against playback progress when available. A process-start event is
   only an approximation of when audio becomes audible because of buffering.
6. Preserve the full transcript elsewhere for rereading; the animated overlay
   is a presentation of speech, not the storage model.

Host acceptance checks: a short answer, a long answer, a long word, punctuation,
rapid successive sentences, interruption during an exit, playback failure,
monitor scaling, bright/dark backgrounds, and click-through behavior. Confirm
no stale words return after cancellation and no words are silently omitted.

## Animation options for the companion itself

- **Drawn emblem:** thin arcs and vector strokes assemble when listening,
  rearrange while working, then retract. Animate path parameters or stroke
  presentation; arbitrary SVGs do not automatically morph into each other.
- **Mechanical iris:** segments open on wake, hold for listening, and close
  after speech. A compact silhouette can replace the glowing orb entirely.
- **Ribbon / waveform:** a single line or ribbon reacts to measured speech
  amplitude. Precompute a short-window envelope from WAV samples and smooth it
  with fast attack/slower release; do not shake the subtitle letters with RMS.
- **Geometric companion:** a rotating wireframe or orbiting points change
  arrangement by state. A 2D projection can give depth without a full 3D scene.
- **Sprite character:** a small illustrated face with listening, working,
  speaking, and resting loops. This requires artwork but gives a strong identity.
- **Tool-specific glyphs:** a magnifier for search, document for reading, or
  terminal symbol for execution, driven by actual available tool events.
- **Restrained particles:** short bursts on completion or dismissal, rather
  than constant sparks behind text. Qt Quick particles supply emitters,
  gravity, and attraction; text-shaped disintegration needs custom rendering.
- **Shader transitions:** localized dither, grain, ink dissolution, or ripple.
  Qt 6 ShaderEffect uses compiled shader assets; budget for that build step.

Prefer a clear motion vocabulary: open to listen, deliberate movement while
working, audio-responsive motion while speaking, and a single settle on finish.
Stop decorative loops when hidden or idle. Avoid full-screen offscreen effects
when a small local animated item is sufficient.

## QML implementation choices and references

Basic transforms, clipping, springs/easing, and coordinated sequences cover the
first prototype. Use `Text.Outline` for a simple contrast outline. A thicker
custom stroke or masked dissolve may need an additional rendering layer.
Treat optional modules and newer font features as dependent on the host's Qt
version and installed QML modules; they were not runtime-tested here.

- [Quickshell introduction](https://quickshell.org/docs/v0.1.0/guide/introduction/)
- [Qt Quick animation and transitions](https://doc.qt.io/qt-6/qtquick-statesanimations-animations.html): property animations, springs, paths, parallel/sequential choreography.
- [Qt Quick Text](https://doc.qt.io/qt-6/qml-qtquick-text.html): outlines and typography; variable font axes require Qt 6.7+ and a suitable font. Animating axes can change metrics and layout.
- [Qt Quick Shapes](https://doc.qt.io/qt-6/qtquick-shapes-qmlmodule.html): vector paths, lines, arcs, and SVG path descriptions; changing geometry can require re-triangulation.
- [Qt Quick particles](https://doc.qt.io/qt-6/qtquick-effects-particles.html): emitters and particle motion.
- [Qt Quick ShaderEffect](https://doc.qt.io/qt-6/qml-qtquick-shadereffect.html): custom shader-based effects.
- [Elfie reference](https://github.com/izumiishikawa/elfie-assistant): `daemon/overlay/subtitle.html`, `daemon/elfie_overlay.py`, `daemon/overlay/great_sage.html`.

Next design decision: choose a subtitle direction and placement. Recommendation:
editorial kinetic type at bottom-center, with a drawn emblem or mechanical iris
as a separate companion identity.
