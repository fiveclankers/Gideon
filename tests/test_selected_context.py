from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from selected_context import (
    MAX_SELECTION_CHARS,
    build_selected_text_prompt,
    copy_to_wayland_clipboard,
    prepare_prompt,
)


class TestSelectedContext(unittest.TestCase):
    def test_ordinary_prompt_is_unchanged_and_does_not_read_selection(self):
        reader = MagicMock(return_value="should not be read")

        prepared = prepare_prompt("Explain quantum tunneling", selection_reader=reader)

        self.assertEqual(prepared.prompt, "Explain quantum tunneling")
        self.assertFalse(prepared.used_selection)
        reader.assert_not_called()

    def test_direct_explanation_reads_selection_only_when_requested(self):
        prepared = prepare_prompt(
            "What does this mean?",
            selection_reader=lambda: "A wave function describes probability.",
        )

        self.assertTrue(prepared.used_selection)
        self.assertIn("A wave function describes probability.", prepared.prompt)
        self.assertIn("Explain the selected text in plain language.", prepared.prompt)

    def test_clipboard_read_requests_are_explicit_and_use_selection(self):
        for request in (
            "look at my clipboard",
            "read my clipboard and summarize it",
            "please check the text I selected",
            "what's in my clipboard?",
        ):
            with self.subTest(request=request):
                prepared = prepare_prompt(request, selection_reader=lambda: "Selected context")
                self.assertTrue(prepared.used_selection)
                self.assertIn("Selected context", prepared.prompt)

    def test_copy_to_clipboard_does_not_read_clipboard(self):
        reader = MagicMock(return_value="should not be read")

        prepared = prepare_prompt("copy this answer to my clipboard", selection_reader=reader)

        self.assertFalse(prepared.used_selection)
        reader.assert_not_called()

    def test_explicit_prefix_preserves_the_user_request(self):
        prepared = prepare_prompt(
            "with selected text: extract the action items",
            selection_reader=lambda: "Meeting notes",
        )

        self.assertTrue(prepared.used_selection)
        self.assertIn("User request: extract the action items", prepared.prompt)

    def test_copy_suffix_is_explicit_and_removed_from_engine_request(self):
        prepared = prepare_prompt(
            "read this and summarize, then copy the answer",
            selection_reader=lambda: "A short article.",
        )

        self.assertTrue(prepared.copy_response)
        self.assertIn("Summarize the selected text.", prepared.prompt)
        self.assertNotIn("copy the answer", prepared.prompt.lower())

    def test_missing_selection_fails_without_an_engine_prompt(self):
        prepared = prepare_prompt("explain this", selection_reader=lambda: "")

        self.assertTrue(prepared.used_selection)
        self.assertEqual(prepared.prompt, "")
        self.assertIsNotNone(prepared.error)

    def test_selection_is_capped_and_null_bytes_are_removed(self):
        selection = "x" * (MAX_SELECTION_CHARS + 100) + "\x00"

        prompt = build_selected_text_prompt(selection, "summarize it")

        body = prompt.split("--- BEGIN USER-SELECTED TEXT ---\n", 1)[1].split(
            "\n--- END USER-SELECTED TEXT ---", 1
        )[0]
        self.assertEqual(len(body), MAX_SELECTION_CHARS)
        self.assertNotIn("\x00", prompt)

    @patch("selected_context.subprocess.Popen")
    def test_wayland_reader_uses_primary_before_clipboard(self, popen):
        primary = MagicMock()
        primary.stdout.read.return_value = "primary text".encode()
        primary.poll.return_value = 0
        primary.wait.return_value = 0
        clipboard = MagicMock()
        clipboard.stdout.read.return_value = b"clipboard text"
        clipboard.poll.return_value = 0
        clipboard.wait.return_value = 0
        popen.side_effect = [primary, clipboard]

        from selected_context import read_wayland_selection

        self.assertEqual(read_wayland_selection(), "primary text")
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["wl-paste", "--primary", "--no-newline"])

    @patch("selected_context.subprocess.run")
    def test_copy_to_wayland_clipboard_invokes_wl_copy(self, mock_run):
        import subprocess
        mock_run.return_value.returncode = 0
        success = copy_to_wayland_clipboard("test response")
        self.assertTrue(success)
        mock_run.assert_called_once_with(
            ["wl-copy", "--trim-newline"],
            input="test response",
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )

    def test_copy_to_wayland_clipboard_rejects_empty_text(self):
        self.assertFalse(copy_to_wayland_clipboard(""))
        self.assertFalse(copy_to_wayland_clipboard("   "))


class TestExplicitResponseCopy(unittest.IsolatedAsyncioTestCase):
    async def test_copy_response_waits_for_the_active_turn_to_finish(self):
        from gideon import _copy_reply_after_turn

        engine = SimpleNamespace(is_turn_active=True)
        bus = SimpleNamespace(current_reply="")

        async def finish_turn():
            await asyncio.sleep(0.08)
            bus.current_reply = "A concise answer."
            engine.is_turn_active = False

        with patch("gideon.copy_to_wayland_clipboard", return_value=True) as copy:
            await asyncio.gather(_copy_reply_after_turn(engine, bus), finish_turn())

        copy.assert_called_once_with("A concise answer.")

    async def test_copy_response_handles_a_turn_that_finished_before_watcher_started(self):
        from gideon import _copy_reply_after_turn

        engine = SimpleNamespace(is_turn_active=False)
        bus = SimpleNamespace(current_reply="A fast answer.")

        with patch("gideon.copy_to_wayland_clipboard", return_value=True) as copy:
            await _copy_reply_after_turn(engine, bus)

        copy.assert_called_once_with("A fast answer.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
