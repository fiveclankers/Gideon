# Pending publication: GIDEON voice and desktop updates

Recorded 2026-10-27. Repository: https://github.com/fiveclankers/Gideon.
Local branch: `main`; base commit: `ae596c7`.

## State and ownership

The changes below are in the working tree, not committed or pushed by this
assistant. The user will publish from their authenticated host terminal.
Container GitHub authentication is separate from the host's login.
Remote freshness has not been checked with a fetch.

The earlier assistant pass edited `README.md` and parts of
`docs/ARCHITECTURE.md` for Antigravity positioning and persona documentation.
The runtime, UI, tests, and selection specification were already pending work.
This handoff pass adds this file and `docs/NOTES-subtitle-redesign.md`.

## Pending change inventory

| Files | Changes present |
| --- | --- |
| `README.md` | Antigravity-first main page; persona and opt-in selected-text usage; optional backend details moved out of the main page. |
| `docs/ARCHITECTURE.md` | First-turn persona transport, advanced engine configuration, selection flow, wake API behavior. |
| `gideon.py` | First-turn voice persona; selection preparation on input paths; explicit response-copy workflow; transcript publish/clear lifecycle; wake toggle and sanitizer updates. |
| `selected_context.py` | Explicit selection request parsing, primary-selection/clipboard reads, bounded reference-text wrapping, response-copy helper. New file: include it when publishing the daemon changes. |
| `docs/SPEC-selected-text-context.md` | Selected-text feature specification. |
| `audio/wake.py` | Enabled-state checks during capture so disabling wake can end recording. |
| `opencode_engine.py` | Accumulate and reset reply text for the response-copy flow. |
| `ai-visualizer/index.html`, `ai-visualizer/faces/board/index.html` | Remove link-status branding, sanitize displayed replies, clear stale transcript state. |
| `ui/shell.qml` | Existing pending transcript card, socket handling, and expiry timer. The proposed full subtitle redesign is not implemented. |
| `tests/test_voice_preamble.py`, `tests/test_selected_context.py`, `tests/test_audio_wake.py`, `tests/test_ui_transcript.py` | New regression tests for these behaviors. |
| `tests/test_tts_sanitizer.py` | Additional heading, list-marker, and pointer cleanup checks. |

## Checks run in this handoff pass

Working directory for all commands: repository root.

- **Targeted tests: passed**, 44 tests:
  `python3 -m unittest tests.test_voice_preamble tests.test_selected_context tests.test_audio_wake tests.test_ui_transcript tests.test_tts_sanitizer`
- **Full discovered test suite: passed**, 161 tests:
  `python3 -m unittest discover -s tests`
- **Whitespace/document checks: passed:** `git diff --check`, plus a Python
  check of trailing whitespace, final newlines, and balanced code fences in
  both new Markdown files.
- **Lint/typecheck/build:** not run; this pass changes Markdown only.
- **Live smoke tests:** not run. Actual Antigravity, Wayland clipboard, speech
  playback, and Quickshell rendering remain host checks.
- Tests logged missing `FISH_API_KEY` and unavailable PyTorch in the container;
  passing unit tests do not establish live speech or model execution.

## Open implementation notes

- The selected-text wrapper is passed to `send_prompt()` and then published as
  the user transcript. Preserve the original utterance separately from the
  engine payload in a follow-up so wrapper instructions are not displayed.
- `selected_context.py::copy_to_wayland_clipboard()` currently supplies both
  `input=` and `stdin=subprocess.PIPE` to `subprocess.run()`. Python rejects
  that combination with `ValueError`; mocked subprocess tests do not cover it.
  A direct helper call reproduced `stdin and input arguments may not both be
  used.` before any clipboard process launched.
  Remove the explicit `stdin` argument and verify this helper before relying on
  response-copy behavior.
- Subtitle motion, timing, and orb redesign choices are proposals in
  `NOTES-subtitle-redesign.md`, not shipped features.
- The instructed root `codemap.md` was absent during this pass; relevant files
  and diffs were inspected directly.

## Publish from the host

Start in your existing checkout. These files are already written there; this
document is a change inventory, not a patch to apply.

```bash
cd /home/cat/gideon
git status --short --branch
git diff --check
git diff --stat
git fetch origin
git log --oneline --left-right HEAD...origin/main
```

Inspect the pending files above, including the new files (ordinary `git diff`
does not show untracked content). If the remote has advanced, reconcile that
history before pushing. Use the host's project Python environment for tests:

```bash
python3 -m unittest discover -s tests
```

For publishing the entire pending implementation described above:

```bash
git add gideon.py selected_context.py audio/wake.py opencode_engine.py \
  ai-visualizer/index.html ai-visualizer/faces/board/index.html ui/shell.qml \
  tests/test_voice_preamble.py tests/test_selected_context.py \
  tests/test_audio_wake.py tests/test_ui_transcript.py tests/test_tts_sanitizer.py \
  docs/SPEC-selected-text-context.md
git diff --cached --check
git diff --cached --stat
git commit -m "feat: improve voice context and desktop interaction"

git add README.md docs/ARCHITECTURE.md docs/CHANGES-publish.md \
  docs/NOTES-subtitle-redesign.md
git diff --cached --check
git commit -m "docs: present Antigravity workflow and record subtitle redesign"
git push origin main
```

The commit examples assume the current branch remains `main` and the listed
pending changes are the intended publication set. After any code follow-up,
run `graphify update .` per repository instructions and include its resulting
tracked updates. Existing hooks may also refresh graph outputs.

Next concrete step: inspect `git status --short --branch` on the host and review
this inventory against the checkout before committing.
