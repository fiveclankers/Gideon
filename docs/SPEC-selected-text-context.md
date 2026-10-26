# Spec: Explicit Selected-Text Context

## Objective

Let a user explicitly ask GIDEON to use the current Wayland selection or
clipboard as context for one prompt, without silently attaching desktop text to
ordinary requests. The first slice is channel-neutral: terminal text, browser
prompts, and wake-word transcripts all use the same opt-in phrases.

Proposed trigger:

```text
with selected text: summarize the argument
what does this mean?
read this and explain, then copy the answer
```

The selected text is read locally, bounded, delimited, and sent to the active
engine as prompt context. It is not spoken back, logged, persisted, or attached
to prompts that do not use the explicit trigger.

## Tech Stack and Boundary

- Python 3.10+ daemon and existing `agy` / OpenCode engine interface.
- Wayland selection and clipboard read through fixed `wl-paste` invocations;
  primary selection is tried first, then the normal clipboard.
- Explicit response copy uses fixed `wl-copy --trim-newline`.
- No new cloud service, database, vector store, or model dependency.
- Existing loopback HUD routes and voice/terminal dispatch paths remain the
  public integration points.

## Proposed Interface Contract

- Input triggers: case-insensitive `with selected text:` / `using selected
  text:` prefixes, or a narrow allowlist such as `what does this mean?`,
  `explain this`, `summarize this`, and `read this and explain`.
- Selection source: Wayland primary selection first, normal clipboard fallback.
  Neither source is read for an ordinary prompt.
- Selection cap: 6,000 characters; excess text is truncated deterministically.
- Engine payload shape: a clearly delimited context block followed by the user
  request; exact delimiter text is an implementation detail covered by tests.
- Optional output suffix: `and copy the answer` / `then copy the answer` copies
  the completed response to the Wayland clipboard after the turn finishes.
- Failure behavior: no selection, unavailable `wl-paste`, or an empty request
  produces a local error and does not call the engine.
- Default behavior: prompts without the explicit trigger are byte-for-byte
  unchanged.

## Project Structure

- `selected_context.py` — trigger parsing, selection capture, bounds, and prompt
  composition.
- `gideon.py` — call the helper at each existing engine-dispatch boundary so
  browser, terminal, and wake-word prompts behave consistently.
- `tests/test_selected_context.py` — pure parsing/composition tests plus a
  subprocess-boundary fake for unavailable/empty selection.
- `docs/ARCHITECTURE.md` — document the opt-in behavior and failure response.

## Code Style

Keep selection handling explicit and side-effect bounded:

```python
prepared = prepare_prompt(user_text, selection_reader=read_wayland_selection)
await engine.send_prompt(prepared)
```

The helper must not log the selected text or construct shell commands from it;
only the executable and fixed arguments are passed to `subprocess.run`.

## Testing Strategy

- Unit tests for trigger recognition, case/whitespace handling, no-trigger
  passthrough, empty request rejection, truncation, and delimiter placement.
- Boundary tests for missing `wl-paste`, non-zero exit, empty output, and command
  timeout; selected text must never reach logs or an engine call in those cases.
- Integration-level tests for each existing dispatch boundary using a fake
  selection reader and fake engine, verifying ordinary prompts are unchanged
  and explicit prompts receive context exactly once.
- Run the focused tests, the existing routing/integration tests, and
  `python3 -m py_compile` on changed Python files.

## Boundaries

- Always: explicit opt-in, local-only capture, fixed subprocess arguments,
  length cap, no persistence, and no automatic context injection.
- Ask first: a HUD toggle/button, a different size cap, or automatically
  including selection in every prompt.
- Never: send selection to a new service, store it in notes/graph state, log it,
  or include it in an ordinary prompt without the trigger.

## Success Criteria

1. `with selected text: ...` works through terminal, browser, and wake-word
   dispatch paths.
2. Ordinary prompts produce exactly the existing engine input.
3. Missing/empty/unavailable selections fail locally without an engine request.
4. Selected text is capped, never logged, never persisted, and never spoken as a
   standalone reply.
5. No hotmic timing, wake threshold, or microphone gain preference changes are
   required.

## Open Questions

- Approve the explicit-prefix UX and the proposed 6,000-character cap.
- Decide whether the natural trigger allowlist should grow beyond the current
  small set of explanation/summarization phrases.
