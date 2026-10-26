#!/usr/bin/env python3
"""
Unit and Regression Test Suite for GIDEON TTS Sanitizer and Sentence Splitting.
Tests:
1. Sentence boundary negative lookbehinds for abbreviations (e.g., i.e., Dr., v1.4, decimals).
2. Raw URL redaction to spoken-friendly "the linked website".
3. Deep Unix file path reduction to basename while preserving arithmetic slashes and and/or.
4. Backward compatibility with existing markdown, code, and persona filtering.
5. End-to-end streaming token chunk simulator.
"""
from __future__ import annotations

import re
import sys
import unittest
from typing import Optional

sys.path.insert(0, "/home/cat/gideon")

# LIVE implementations under test (the real runtime in gideon.py).
from gideon import OPTIMAL_WHISPER_PROMPT as LIVE_WHISPER_PROMPT  # noqa: E402
from gideon import _SENTENCE_END as LIVE_SENTENCE_END  # noqa: E402
from gideon import clean_text_for_tts as live_clean_text_for_tts  # noqa: E402

# Hardening spec value for the STT conditioning prompt (FIX G5).
OPTIMAL_WHISPER_PROMPT = (
    "Gideon, autonomous AI executive on Omarchy Linux with Hyprland and Wayland. "
    "Executing Antigravity, Quickshell, PipeWire, wpctl, pw-play, Pacman, Yay, "
    "systemctl, Fish Audio, Obsidian, WebGL, Barehands, Euphonium, and brass acoustics."
)

# ---------------------------------------------------------------------------
# Patched Regex and Sanitizer Implementations (Dry-run specification)
# ---------------------------------------------------------------------------

# Protected sentence boundary regex with fixed-width negative lookbehinds
_SENTENCE_END_PATCHED = re.compile(
    r'(?<!\b(?:e\.g|i\.e)\.)'
    r'(?<!\b(?:etc|mrs)\.)'
    r'(?<!\b(?:dr|mr|ms|vs)\.)'
    r'(?<!\bv\d\.\d)'
    r'(?<!\b\d\.\d)'
    r'(?<=[.!?])\s+',
    re.IGNORECASE
)

# Raw URL matcher (preserves trailing sentence punctuation)
_RAW_URL_RE = re.compile(r'https?://[^\s)]+?(?=[.,!?;:]?(?:\s|$))')

# Deep Unix path matcher (requires leading / or ~/ with at least one parent folder)
_DEEP_PATH_RE = re.compile(
    r'(?:~/|/)(?:[a-zA-Z0-9_.-]+/)+([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*)/?(?=[.,!?;:]?(?:\s|$))'
)


def clean_text_for_tts_patched(raw: str) -> Optional[str]:
    """Sanitizes text for neural TTS playback, stripping markdown, code, raw URLs, and paths."""
    if not raw:
        return None
    # Strip markdown headers, including malformed streamed forms such as ###1.
    text = re.sub(r'(?m)^\s*#{1,6}\s*(?:\d+[.:)\-]?\s*)?', '', raw)
    # Strip markdown blockquotes and arrow-style pointer lines.
    text = re.sub(r'(?m)^\s*[>→⇒]\s*', '', text)
    # Strip HTML tags (<div ...>, </span>, etc.)
    text = re.sub(r'<[^>]+>', '', text)
    # Strip markdown links [label](url) -> label
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Redact raw URLs to spoken-friendly "the linked website"
    text = _RAW_URL_RE.sub('the linked website', text)
    # Redact deep Unix file paths to their basename
    text = _DEEP_PATH_RE.sub(r'\1', text)
    # Strip code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Strip inline code ticks `foo` -> foo
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Strip bold / italics
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    # Strip list markers at each streamed line.
    text = re.sub(r'(?m)^\s*(\d+[\.\)]|[-*•])\s*', '', text)
    # Replace em-dashes and en-dashes with natural comma pauses
    text = text.replace('—', ', ').replace('–', ', ')
    # Collapse extra whitespace
    text = ' '.join(text.split()).strip()

    # Drop single numbers, empty or trivial text
    if not text or len(text) < 3 or re.match(r'^\d+[\.\)]?$', text):
        return None
    # Drop purely punctuation or single characters
    if not any(c.isalnum() for c in text):
        return None
    # Drop intermediate planning phrases if they leak through
    lower = text.lower()
    if lower.startswith(("let me check", "let me inspect", "let me search", "checking for", "running command")):
        return None

    return text


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestTTSSanitizer(unittest.TestCase):

    def test_sentence_split_abbreviations_no_fracture(self):
        """Verifies that abbreviations and version numbers do not prematurely split sentences."""
        sample = (
            "The daemon runs on v1.4 and e.g. executes Dr. Smith's routine. "
            "It costs $3.14 per hour. "
            "Done."
        )
        splits = _SENTENCE_END_PATCHED.split(sample)
        self.assertEqual(len(splits), 3)
        self.assertEqual(splits[0], "The daemon runs on v1.4 and e.g. executes Dr. Smith's routine.")
        self.assertEqual(splits[1], "It costs $3.14 per hour.")
        self.assertEqual(splits[2], "Done.")

    def test_sentence_split_ie_and_etc(self):
        """Verifies i.e. and etc. do not fracture sentence boundaries."""
        sample = "Use the fast path, i.e. local reflexes, etc. to reduce latency. System is stable."
        splits = _SENTENCE_END_PATCHED.split(sample)
        self.assertEqual(len(splits), 2)
        self.assertEqual(splits[0], "Use the fast path, i.e. local reflexes, etc. to reduce latency.")
        self.assertEqual(splits[1], "System is stable.")

    def test_raw_url_redaction(self):
        """Verifies raw URLs are converted to 'the linked website' with sentence period intact."""
        raw = "Documentation is at https://github.com/google/antigravity."
        cleaned = clean_text_for_tts_patched(raw)
        self.assertEqual(cleaned, "Documentation is at the linked website.")

        raw2 = "Check http://127.0.0.1:8790/faces/board/ for visual telemetry."
        cleaned2 = clean_text_for_tts_patched(raw2)
        self.assertEqual(cleaned2, "Check the linked website for visual telemetry.")

    def test_markdown_link_vs_raw_url(self):
        """Verifies markdown links use the anchor text while raw URLs are redacted."""
        raw = "See [Antigravity Manual](https://docs.google.com) or visit https://gemini.google.com."
        cleaned = clean_text_for_tts_patched(raw)
        self.assertEqual(cleaned, "See Antigravity Manual or visit the linked website.")

    def test_deep_unix_path_to_basename(self):
        """Verifies deep Unix paths reduce to basename while preserving trailing punctuation."""
        raw = "Config saved to /home/cat/gideon/gideon.py."
        cleaned = clean_text_for_tts_patched(raw)
        self.assertEqual(cleaned, "Config saved to gideon.py.")

        raw2 = "Review ~/.config/systemd/user/gideon.service now!"
        cleaned2 = clean_text_for_tts_patched(raw2)
        self.assertEqual(cleaned2, "Review gideon.service now!")

        raw3 = "The logs reside in /var/log/journal/system.log."
        cleaned3 = clean_text_for_tts_patched(raw3)
        self.assertEqual(cleaned3, "The logs reside in system.log.")

        raw4 = "Socket is at /tmp/gideon-ui.sock."
        cleaned4 = clean_text_for_tts_patched(raw4)
        self.assertEqual(cleaned4, "Socket is at gideon-ui.sock.")

        raw5 = "Navigate to /home/cat/gideon/."
        cleaned5 = clean_text_for_tts_patched(raw5)
        self.assertEqual(cleaned5, "Navigate to gideon.")

    def test_fractions_and_slashes_preserved(self):
        """Ensures math ratios, fractions, and and/or are not falsely recognized as paths."""
        raw = "Adjust ratio to 3/4 and/or 5/8 of total capacity."
        cleaned = clean_text_for_tts_patched(raw)
        self.assertEqual(cleaned, "Adjust ratio to 3/4 and/or 5/8 of total capacity.")

    def test_existing_markdown_and_formatting_stripping(self):
        """Regression test ensuring headers, asterisks, ticks, HTML, and em-dashes strip cleanly."""
        raw = "### Status: **All systems** are `online` & running—ready for commands."
        cleaned = clean_text_for_tts_patched(raw)
        self.assertEqual(cleaned, "Status: All systems are online & running, ready for commands.")

    def test_heading_ordinals_and_pointer_lines_do_not_leak_into_speech(self):
        self.assertEqual(
            clean_text_for_tts_patched("###1. Explain the result."),
            "Explain the result.",
        )
        self.assertEqual(
            clean_text_for_tts_patched("> Point to explain the result."),
            "Point to explain the result.",
        )
        self.assertEqual(
            clean_text_for_tts_patched("- First point.\n→ Second point."),
            "First point. Second point.",
        )

    def test_bullet_and_list_stripping(self):
        """Ensures leading bullet points and numbered list markers are stripped."""
        self.assertEqual(clean_text_for_tts_patched("- Primary core running."), "Primary core running.")
        self.assertEqual(clean_text_for_tts_patched("* Secondary worker idle."), "Secondary worker idle.")
        self.assertEqual(clean_text_for_tts_patched("1. Initializing pipewire."), "Initializing pipewire.")
        self.assertEqual(clean_text_for_tts_patched("2) Connecting to socket."), "Connecting to socket.")

    def test_intermediate_planning_phrase_rejection(self):
        """Ensures planning phrases return None so TTS does not speak internal agent thoughts."""
        self.assertIsNone(clean_text_for_tts_patched("Let me check the system uptime."))
        self.assertIsNone(clean_text_for_tts_patched("let me inspect the files."))
        self.assertIsNone(clean_text_for_tts_patched("Running command uptime."))
        self.assertIsNone(clean_text_for_tts_patched("checking for active processes."))

    def test_trivial_and_punctuation_rejection(self):
        """Ensures empty, punctuation-only, or trivial tokens return None."""
        self.assertIsNone(clean_text_for_tts_patched(""))
        self.assertIsNone(clean_text_for_tts_patched("..."))
        self.assertIsNone(clean_text_for_tts_patched("12"))
        self.assertIsNone(clean_text_for_tts_patched("   "))

    def test_streaming_token_simulation(self):
        """Simulates character-by-character token streaming from agy NDJSON step_update."""
        stream_chunks = [
            "The ", "active ", "service ", "is ", "defined ", "in ", "/etc/systemd/user/gideon.service. ",
            "For ", "more ", "details, ", "visit ", "https://github.com/google/antigravity. ",
            "Dr. ", "Smith ", "confirmed ", "version ", "v1.4 ", "is ", "running."
        ]

        sentence_buffer = ""
        emitted_sentences = []

        for chunk in stream_chunks:
            sentence_buffer += chunk
            while True:
                match = _SENTENCE_END_PATCHED.search(sentence_buffer)
                if not match:
                    break
                sentence = sentence_buffer[:match.end()].strip()
                sentence_buffer = sentence_buffer[match.end():]
                if sentence:
                    clean = clean_text_for_tts_patched(sentence)
                    if clean:
                        emitted_sentences.append(clean)

        # Flush any tail
        if sentence_buffer.strip():
            clean = clean_text_for_tts_patched(sentence_buffer.strip())
            if clean:
                emitted_sentences.append(clean)

        expected = [
            "The active service is defined in gideon.service.",
            "For more details, visit the linked website.",
            "Dr. Smith confirmed version v1.4 is running."
        ]
        self.assertEqual(emitted_sentences, expected)


class TestLiveTTSSanitizer(unittest.TestCase):
    """Exercises the LIVE gideon._SENTENCE_END and gideon.clean_text_for_tts.

    These tests import the real runtime symbols so that any drift between the
    hardening spec and gideon.py fails the suite.
    """

    def test_live_regex_matches_hardening_spec(self):
        """The live sentence regex must be the abbreviation-safe hardened pattern."""
        self.assertEqual(LIVE_SENTENCE_END.pattern, _SENTENCE_END_PATCHED.pattern)
        self.assertEqual(LIVE_SENTENCE_END.flags, _SENTENCE_END_PATCHED.flags)

    def test_live_sanitizer_removes_heading_ordinals_and_pointer_lines(self):
        self.assertEqual(live_clean_text_for_tts("###1. Explain the result."), "Explain the result.")
        self.assertEqual(live_clean_text_for_tts("- First point.\n→ Second point."), "First point. Second point.")

    def test_live_sentence_split_abbreviations_no_fracture(self):
        sample = (
            "The daemon runs on v1.4 and e.g. executes Dr. Smith's routine. "
            "It costs $3.14 per hour. "
            "Done."
        )
        splits = LIVE_SENTENCE_END.split(sample)
        self.assertEqual(len(splits), 3)
        self.assertEqual(splits[0], "The daemon runs on v1.4 and e.g. executes Dr. Smith's routine.")
        self.assertEqual(splits[1], "It costs $3.14 per hour.")
        self.assertEqual(splits[2], "Done.")

    def test_live_sentence_split_ie_and_etc(self):
        sample = "Use the fast path, i.e. local reflexes, etc. to reduce latency. System is stable."
        splits = LIVE_SENTENCE_END.split(sample)
        self.assertEqual(len(splits), 2)
        self.assertEqual(splits[0], "Use the fast path, i.e. local reflexes, etc. to reduce latency.")
        self.assertEqual(splits[1], "System is stable.")

    def test_live_raw_url_redaction(self):
        self.assertEqual(
            live_clean_text_for_tts("Documentation is at https://github.com/google/antigravity."),
            "Documentation is at the linked website.",
        )
        self.assertEqual(
            live_clean_text_for_tts("Check http://127.0.0.1:8790/faces/board/ for visual telemetry."),
            "Check the linked website for visual telemetry.",
        )

    def test_live_markdown_link_vs_raw_url(self):
        self.assertEqual(
            live_clean_text_for_tts(
                "See [Antigravity Manual](https://docs.google.com) or visit https://gemini.google.com."
            ),
            "See Antigravity Manual or visit the linked website.",
        )

    def test_live_deep_unix_path_to_basename(self):
        self.assertEqual(live_clean_text_for_tts("Config saved to /home/cat/gideon/gideon.py."), "Config saved to gideon.py.")
        self.assertEqual(live_clean_text_for_tts("Review ~/.config/systemd/user/gideon.service now!"), "Review gideon.service now!")
        self.assertEqual(live_clean_text_for_tts("The logs reside in /var/log/journal/system.log."), "The logs reside in system.log.")
        self.assertEqual(live_clean_text_for_tts("Socket is at /tmp/gideon-ui.sock."), "Socket is at gideon-ui.sock.")
        self.assertEqual(live_clean_text_for_tts("Navigate to /home/cat/gideon/."), "Navigate to gideon.")

    def test_live_fractions_and_slashes_preserved(self):
        self.assertEqual(
            live_clean_text_for_tts("Adjust ratio to 3/4 and/or 5/8 of total capacity."),
            "Adjust ratio to 3/4 and/or 5/8 of total capacity.",
        )

    def test_live_markdown_and_formatting_stripping(self):
        self.assertEqual(
            live_clean_text_for_tts("### Status: **All systems** are `online` & running—ready for commands."),
            "Status: All systems are online & running, ready for commands.",
        )

    def test_live_bullet_and_list_stripping(self):
        self.assertEqual(live_clean_text_for_tts("- Primary core running."), "Primary core running.")
        self.assertEqual(live_clean_text_for_tts("* Secondary worker idle."), "Secondary worker idle.")
        self.assertEqual(live_clean_text_for_tts("1. Initializing pipewire."), "Initializing pipewire.")
        self.assertEqual(live_clean_text_for_tts("2) Connecting to socket."), "Connecting to socket.")

    def test_live_intermediate_planning_phrase_rejection(self):
        self.assertIsNone(live_clean_text_for_tts("Let me check the system uptime."))
        self.assertIsNone(live_clean_text_for_tts("let me inspect the files."))
        self.assertIsNone(live_clean_text_for_tts("Running command uptime."))
        self.assertIsNone(live_clean_text_for_tts("checking for active processes."))

    def test_live_trivial_and_punctuation_rejection(self):
        self.assertIsNone(live_clean_text_for_tts(""))
        self.assertIsNone(live_clean_text_for_tts("..."))
        self.assertIsNone(live_clean_text_for_tts("12"))
        self.assertIsNone(live_clean_text_for_tts("   "))

    def test_live_streaming_token_simulation(self):
        """Simulates token streaming through the LIVE sentence regex + sanitizer."""
        stream_chunks = [
            "The ", "active ", "service ", "is ", "defined ", "in ", "/etc/systemd/user/gideon.service. ",
            "For ", "more ", "details, ", "visit ", "https://github.com/google/antigravity. ",
            "Dr. ", "Smith ", "confirmed ", "version ", "v1.4 ", "is ", "running."
        ]

        sentence_buffer = ""
        emitted_sentences = []

        for chunk in stream_chunks:
            sentence_buffer += chunk
            while True:
                match = LIVE_SENTENCE_END.search(sentence_buffer)
                if not match:
                    break
                sentence = sentence_buffer[:match.end()].strip()
                sentence_buffer = sentence_buffer[match.end():]
                if sentence:
                    clean = live_clean_text_for_tts(sentence)
                    if clean:
                        emitted_sentences.append(clean)

        if sentence_buffer.strip():
            clean = live_clean_text_for_tts(sentence_buffer.strip())
            if clean:
                emitted_sentences.append(clean)

        self.assertEqual(
            emitted_sentences,
            [
                "The active service is defined in gideon.service.",
                "For more details, visit the linked website.",
                "Dr. Smith confirmed version v1.4 is running.",
            ],
        )

    def test_live_optimal_whisper_prompt_is_module_level(self):
        """FIX G5: the STT conditioning prompt is a module-level constant in gideon.py."""
        self.assertEqual(LIVE_WHISPER_PROMPT, OPTIMAL_WHISPER_PROMPT)
        self.assertGreater(len(LIVE_WHISPER_PROMPT), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
