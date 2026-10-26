from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from gideon import SignalBus


class TestUiTranscriptLifecycle(unittest.TestCase):
    def test_clear_transcript_resets_http_state_and_broadcasts_clear_event(self):
        bus = SignalBus.__new__(SignalBus)
        bus.current_user = "a spoken request"
        bus.current_reply = "an answer"
        bus.broadcast_transcript = MagicMock()

        bus.clear_transcript()

        self.assertEqual(bus.current_user, "")
        self.assertEqual(bus.current_reply, "")
        bus.broadcast_transcript.assert_called_once_with("", "")

    def test_new_user_transcript_replaces_stale_reply(self):
        bus = SignalBus.__new__(SignalBus)
        bus.current_reply = "stale answer"
        bus.broadcast_transcript = MagicMock()

        bus.set_user_transcript("new request")

        self.assertEqual(bus.current_user, "new request")
        self.assertEqual(bus.current_reply, "")
        bus.broadcast_transcript.assert_called_once_with("user", "new request")

    def test_browser_hud_has_no_stale_link_marker_and_sanitizes_reply_display(self):
        for path in (
            Path("ai-visualizer/index.html"),
            Path("ai-visualizer/faces/board/index.html"),
        ):
            text = path.read_text()
            self.assertNotIn("WS LINKED", text)
            self.assertIn("cleanDisplayText", text)


if __name__ == "__main__":
    unittest.main()
