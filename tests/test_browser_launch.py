from __future__ import annotations

import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from gideon_handler import GideonDispatcher
from gideon_shared import extract_params
from handlers_study_passthrough import handle_browser_use_passthrough
from handlers_system import handle_launch_browser


class TestBrowserLaunch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Keep launch mock failures from dumping the real process environment.
        env = patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    async def test_url_launch_submits_exactly_one_new_tab_with_or_without_window(self):
        url = "https://example.com/Path?q=a%20b&next=two#section"
        for browser in ("google-chrome-stable", "brave", "firefox"):
            for existing in (False, True):
                with (
                    self.subTest(browser=browser, existing=existing),
                    patch("handlers_system.shutil.which", side_effect=lambda name: name if name == browser else None),
                    patch("handlers_system._find_active_window", return_value=existing),
                    patch("handlers_system.subprocess.Popen") as spawn,
                ):
                    result = await handle_launch_browser({"url": url, "tab_id": "phantom-tab"})

                    self.assertTrue(result.ok)
                    spawn.assert_called_once()
                    self.assertEqual(spawn.call_args.args[0], [browser, "--new-tab", url])
                    self.assertTrue(spawn.call_args.kwargs["start_new_session"])
                    self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
                    self.assertEqual(result.data, {"url": url})

    async def test_dispatcher_extracted_url_reaches_browser_unchanged(self):
        text = "open https://example.com/Case?x=1&y=2#fragment"
        params = extract_params("launch_browser", text)
        self.assertEqual(params, {"url": "https://example.com/Case?x=1&y=2#fragment"})
        dispatcher = GideonDispatcher.__new__(GideonDispatcher)
        dispatcher.schema = {"launch_browser": {"risk_tier": "safe", "slots": {}}}
        handler = AsyncMock(wraps=handle_launch_browser)
        with (
            patch.object(dispatcher, "predict", return_value=("launch_browser", 0.99, [])),
            patch.dict("gideon_handler.INTENT_DISPATCH_MAP", {"launch_browser": handler}),
            patch("handlers_system.shutil.which", side_effect=lambda name: name if name == "google-chrome-stable" else None),
            patch("handlers_system.subprocess.Popen") as spawn,
        ):
            result = await dispatcher.dispatch(text)
        self.assertTrue(result.ok)
        handler.assert_awaited_once_with(params, raw_text=text)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][-1], params["url"])

    async def test_uwsm_wraps_one_launch_without_extra_blank_tab(self):
        with (
            patch("handlers_system.shutil.which", return_value="installed"),
            patch("handlers_system.subprocess.Popen") as spawn,
        ):
            result = await handle_launch_browser({"url": "https://example.com"})
        self.assertTrue(result.ok)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0], ["uwsm", "app", "--", "google-chrome-stable", "--new-tab", "https://example.com"])

    async def test_no_url_without_existing_window_spawns_once(self):
        with (
            patch("handlers_system.shutil.which", side_effect=lambda name: name if name == "firefox" else None),
            patch("handlers_system._find_active_window", return_value=False),
            patch("handlers_system.subprocess.Popen") as spawn,
        ):
            result = await handle_launch_browser({})
        self.assertTrue(result.ok)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0], ["firefox"])

    async def test_raw_url_is_not_lost_when_caller_supplies_no_slots(self):
        with (
            patch("handlers_system.shutil.which", side_effect=lambda name: name if name == "firefox" else None),
            patch("handlers_system.subprocess.Popen") as spawn,
        ):
            result = await handle_launch_browser({}, "open https://example.com/requested")
        self.assertTrue(result.ok)
        self.assertEqual(spawn.call_args.args[0], ["firefox", "--new-tab", "https://example.com/requested"])

    async def test_url_punctuation_is_preserved_as_one_literal_argument(self):
        url = "https://example.com/it's?q=a;b&literal=$(ignored)"
        params = extract_params("launch_browser", f"open {url}")
        self.assertEqual(params, {"url": url})
        with (
            patch("handlers_system.shutil.which", side_effect=lambda name: name if name == "firefox" else None),
            patch("handlers_system.subprocess.Popen") as spawn,
        ):
            result = await handle_launch_browser(params)
        self.assertTrue(result.ok)
        self.assertEqual(spawn.call_args.args[0], ["firefox", "--new-tab", url])
        self.assertFalse(spawn.call_args.kwargs.get("shell", False))

    async def test_no_url_reuses_existing_browser_without_spawning(self):
        for browser, patterns in (
            ("google-chrome-stable", ["chrome", "chromium", "google-chrome"]),
            ("brave", ["brave", "brave-browser"]),
            ("firefox", ["firefox"]),
        ):
            with (
                self.subTest(browser=browser),
                patch("handlers_system.shutil.which", side_effect=lambda name: name if name == browser else None),
                patch("handlers_system._find_active_window", return_value=True) as find,
                patch("handlers_system.subprocess.Popen") as spawn,
            ):
                result = await handle_launch_browser({})
                self.assertTrue(result.ok)
                find.assert_called_once_with(patterns)
                spawn.assert_not_called()

    async def test_invalid_urls_do_not_spawn_or_turn_into_browser_flags(self):
        for url in ("--incognito", "javascript:alert(1)", "file:///etc/passwd", "https://", "https://example.com https://other.test", 123):
            with (
                self.subTest(url=url),
                patch("handlers_system.shutil.which", return_value="google-chrome-stable"),
                patch("handlers_system.subprocess.Popen") as spawn,
            ):
                result = await handle_launch_browser({"url": url})
                self.assertFalse(result.ok)
                spawn.assert_not_called()

    async def test_launch_failure_does_not_retry_or_claim_a_tab_exists(self):
        with (
            patch("handlers_system.shutil.which", return_value="google-chrome-stable"),
            patch("handlers_system.subprocess.Popen", side_effect=OSError("launch failed")) as spawn,
        ):
            result = await handle_launch_browser({"url": "https://example.com"})
        self.assertFalse(result.ok)
        self.assertIn("launch failed", result.error)
        spawn.assert_called_once()
        self.assertNotIn("tab_id", result.data)

    async def test_missing_browser_reports_failure_without_launch(self):
        with (
            patch("handlers_system.shutil.which", return_value=None),
            patch("handlers_system.subprocess.Popen") as spawn,
        ):
            result = await handle_launch_browser({"url": "https://example.com"})
        self.assertFalse(result.ok)
        spawn.assert_not_called()

    async def test_browser_passthrough_preserves_query_without_inventing_tab_state(self):
        query = "open https://example.com in a new tab"
        result = await handle_browser_use_passthrough({"tab_id": "phantom-tab"}, query)
        self.assertEqual(result.data, {"route": "browser_use", "query": query})
