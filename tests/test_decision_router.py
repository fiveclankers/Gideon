#!/usr/bin/env python3
"""Unit tests for the Gemini Decision 1 Tier-1 router (no real API calls)."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

import decision_router
import reflex_router
from decision_router import (
    DECISION_MODEL,
    GeminiDecisionDispatcher,
    _INTENT_INSTRUCTIONS,
    gemini_api_keys,
    load_intent_criteria,
    select_tier1_mode,
)


def _response(payload: dict, status_code: int = 200, headers: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(payload)
    resp.headers = dict(headers or {})
    resp.json.return_value = payload
    return resp


def _choice_answer(choice: str = "media_pause", confidence: float = 0.99) -> dict:
    return {
        "answers": {
            "intent": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {choice: confidence, "conversation": 1 - confidence},
            }
        },
        "usage": {"total_tokens": 115, "total_input_tokens": 115, "total_output_tokens": 0},
    }


class _FakeBGE:
    id2label = {0: "media_play", 1: "conversation"}

    def __init__(self, calls: list[str] | None = None) -> None:
        self.calls = calls if calls is not None else []

    def predict(self, text: str):
        self.calls.append(text)
        return "media_play", 0.88, [0.88, 0.12]


class TestGeminiDecisionDispatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()
        self.dispatcher = GeminiDecisionDispatcher(api_keys=["test-key"], client=self.client)

    def test_choice_prediction_loads_all_65_intents_and_caches(self):
        self.client.post.return_value = _response(_choice_answer())
        intent, confidence, probs = self.dispatcher.predict("pause the music")

        self.assertEqual(intent, "media_pause")
        self.assertEqual(confidence, 0.99)
        self.assertEqual(probs["media_pause"], 0.99)
        call = self.client.post.call_args.kwargs
        self.assertEqual(call["headers"]["x-goog-api-key"], "test-key")
        payload = call["json"]
        self.assertEqual(payload["model"], "gemini-decision-1")
        self.assertEqual(payload["input"], "pause the music")
        question = payload["questions"]["intent"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(len(question["criteria"]), 65)
        self.assertEqual(question["instructions"], _INTENT_INSTRUCTIONS)
        self.assertEqual(question["instructions"], "Pick the best intent for the voice command.")
        self.assertEqual(question["criteria"]["media_pause"], "Pause playback")
        self.assertEqual(self.dispatcher.peek_cached_intent("pause the music"), "media_pause")
        self.assertIsNone(self.dispatcher.peek_cached_intent("something else"))

    def test_peek_cached_intent_expires(self):
        self.client.post.return_value = _response(_choice_answer())
        self.dispatcher.predict("pause")
        self.dispatcher._last_prediction = ("pause", "media_pause", 0.99, 0.0)
        self.assertIsNone(self.dispatcher.peek_cached_intent("pause"))

    def test_429_rotates_keys_and_success_uses_next_key(self):
        client = MagicMock()
        client.post.side_effect = [
            _response({}, status_code=429),
            _response(_choice_answer()),
        ]
        dispatcher = GeminiDecisionDispatcher(api_keys=["key-a", "key-b"], client=client)
        intent, _confidence, _probs = dispatcher.predict("pause")
        self.assertEqual(intent, "media_pause")
        headers = [call.kwargs["headers"]["x-goog-api-key"] for call in client.post.call_args_list]
        self.assertEqual(headers, ["key-a", "key-b"])

    def test_all_keys_429_falls_back_to_bge(self):
        client = MagicMock()
        client.post.return_value = _response({}, status_code=429)
        dispatcher = GeminiDecisionDispatcher(api_keys=["key-a", "key-b"], client=client)
        fake_bge = _FakeBGE()
        with patch.object(dispatcher, "_get_bge", return_value=fake_bge):
            intent, confidence, probs = dispatcher.predict("play some music")
        self.assertEqual((intent, confidence), ("media_play", 0.88))
        self.assertEqual(probs, {"media_play": 0.88, "conversation": 0.12})
        self.assertEqual(client.post.call_count, 2)
        self.assertEqual(dispatcher.fallback_count, 1)
        self.assertEqual(fake_bge.calls, ["play some music"])
        self.assertEqual(dispatcher.peek_cached_intent("play some music"), "media_play")

    def test_timeout_and_missing_key_fall_back_to_bge(self):
        self.client.post.side_effect = httpx.TimeoutException("timed out")
        fake_bge = _FakeBGE()
        with patch.object(self.dispatcher, "_get_bge", return_value=fake_bge):
            intent, _confidence, probs = self.dispatcher.predict("play")
        self.assertEqual(intent, "media_play")
        self.assertTrue(probs)

        no_key = GeminiDecisionDispatcher(api_keys=[], client=MagicMock())
        with patch.object(no_key, "_get_bge", return_value=fake_bge):
            self.assertEqual(no_key.predict("play")[0], "media_play")
        self.assertEqual(no_key.fallback_count, 1)

    def test_browser_step_payload_is_multimodal_and_single_pass(self):
        client = MagicMock()
        answers = {
            "answers": {
                "candidate_action": {
                    "type": "choice",
                    "choice": "click_search",
                    "confidence": 0.95,
                    "probabilities": {"click_search": 0.95, "close_modal": 0.05},
                },
                "has_blocking_modal": {"type": "noul", "noul": 0.07, "confidence": 0.93},
                "goal_complete": {"type": "noul", "noul": 0.12, "confidence": 0.88},
            },
            "usage": {"total_tokens": 220},
        }
        client.post.return_value = _response(answers)
        dispatcher = GeminiDecisionDispatcher(api_keys=["test-key"], client=client)
        png = b"\x89PNG\r\n\x1a\n-fake-png"
        result = dispatcher.decide_browser_step(
            "find the order status",
            "<button id='search'>Search</button>",
            {"click_search": "Click the search button", "close_modal": "Dismiss the modal"},
            screenshot_png_bytes=png,
        )

        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(client.post.call_count, 1)
        blocks = payload["input"]
        self.assertEqual([b["type"] for b in blocks], ["text", "image"])
        self.assertIn("find the order status", blocks[0]["text"])
        self.assertIn("<button id='search'>", blocks[0]["text"])
        self.assertEqual(blocks[1]["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[1]["data"]), png)
        questions = payload["questions"]
        self.assertEqual(set(questions), {"candidate_action", "has_blocking_modal", "goal_complete"})
        self.assertEqual(questions["candidate_action"]["type"], "choice")
        self.assertEqual(set(questions["candidate_action"]["criteria"]), {"click_search", "close_modal"})
        self.assertEqual(questions["has_blocking_modal"]["type"], "noul")
        self.assertEqual(questions["goal_complete"]["type"], "noul")
        self.assertEqual(result["candidate_action"], "click_search")
        self.assertEqual(result["has_blocking_modal"], 0.07)
        self.assertEqual(result["goal_complete"], 0.12)
        self.assertEqual(result["usage"], {"total_tokens": 220})
        with self.assertRaises(ValueError):
            dispatcher.decide_browser_step("goal", "<div/>", {})

    def test_dropin_dispatch_delegates_to_shared_bge_pipeline(self):
        from gideon_handler import GideonDispatcher

        with patch.object(GideonDispatcher, "dispatch", new_callable=AsyncMock) as mock_dispatch, patch.object(
            GideonDispatcher, "dispatch_confirmed", new_callable=AsyncMock
        ) as mock_confirmed:
            outcome = asyncio.run(self.dispatcher.dispatch("pause"))
            asyncio.run(self.dispatcher.dispatch_confirmed("media_pause", {"x": 1}, "pause"))
        self.assertEqual(outcome, mock_dispatch.return_value)
        mock_dispatch.assert_awaited_once_with("pause", dry_run=False, confirmed=False)
        mock_confirmed.assert_awaited_once_with(
            "media_pause", params={"x": 1}, utterance="pause", risk_tier=None
        )


class TestKeyPoolCooldown(unittest.TestCase):
    """429 handling, cooldown rotation and pool status (no real network)."""

    def test_429_puts_key_in_62s_cooldown_and_next_turn_skips_it(self):
        client = MagicMock()
        client.post.side_effect = [
            _response({}, status_code=429),
            _response(_choice_answer()),
            _response(_choice_answer()),
        ]
        dispatcher = GeminiDecisionDispatcher(api_keys=["key-a", "key-b"], client=client)
        with patch.object(decision_router.time, "monotonic", return_value=1000.0):
            self.assertEqual(dispatcher.predict("pause")[0], "media_pause")
            self.assertEqual(dispatcher._rate_limit_strikes["key-a"], 1)
            self.assertAlmostEqual(dispatcher._cooldown_until["key-a"], 1062.0)
            # Second turn must never touch the cooling key again.
            self.assertEqual(dispatcher.predict("pause again")[0], "media_pause")
        keys = [call.kwargs["headers"]["x-goog-api-key"] for call in client.post.call_args_list]
        self.assertEqual(keys, ["key-a", "key-b", "key-b"])

    def test_429_honours_and_clamps_retry_after_header(self):
        for header, expected in (("17", 17.0), ("1", 5.0), ("99999", 3600.0), ("garbage", 62.0)):
            client = MagicMock()
            client.post.return_value = _response({}, status_code=429, headers={"Retry-After": header})
            dispatcher = GeminiDecisionDispatcher(api_keys=["key-a"], client=client)
            with patch.object(dispatcher, "_get_bge", return_value=_FakeBGE()), patch.object(
                decision_router.time, "monotonic", return_value=500.0
            ):
                dispatcher.predict("pause")
            self.assertAlmostEqual(
                dispatcher._cooldown_until["key-a"], 500.0 + expected, msg=header
            )

    def test_consecutive_429s_escalate_and_200_resets_streak(self):
        client = MagicMock()
        client.post.return_value = _response({}, status_code=429)
        dispatcher = GeminiDecisionDispatcher(api_keys=["key-a"], client=client)
        now = 1000.0
        with patch.object(dispatcher, "_get_bge", return_value=_FakeBGE()):
            for strikes, expected in ((1, 62.0), (2, 180.0), (3, 600.0)):
                with patch.object(decision_router.time, "monotonic", return_value=now):
                    self.assertEqual(dispatcher.predict("pause")[0], "media_play")
                self.assertEqual(dispatcher._rate_limit_strikes["key-a"], strikes)
                self.assertAlmostEqual(dispatcher._cooldown_until["key-a"], now + expected)
                now += expected + 1.0  # let the cooldown lapse before the next turn

            client.post.return_value = _response(_choice_answer())
            with patch.object(decision_router.time, "monotonic", return_value=now):
                self.assertEqual(dispatcher.predict("pause")[0], "media_pause")
        self.assertEqual(dispatcher._rate_limit_strikes.get("key-a", 0), 0)
        self.assertNotIn("key-a", dispatcher._cooldown_until)

    def test_all_keys_cooling_down_skips_http_and_falls_back_immediately(self):
        client = MagicMock()
        client.post.return_value = _response({}, status_code=429)
        dispatcher = GeminiDecisionDispatcher(api_keys=["key-a", "key-b"], client=client)
        with patch.object(dispatcher, "_get_bge", return_value=_FakeBGE()), patch.object(
            decision_router.time, "monotonic", return_value=1000.0
        ):
            self.assertEqual(dispatcher.predict("pause")[0], "media_play")
            self.assertEqual(client.post.call_count, 2)
            self.assertEqual(dispatcher.key_pool_status()["available_keys"], 0)
            self.assertEqual(dispatcher.key_pool_status()["cooling_down_keys"], 2)
            # Second turn: no HTTP at all, straight to BGE-small.
            self.assertEqual(dispatcher.predict("play something")[0], "media_play")
        self.assertEqual(client.post.call_count, 2)
        self.assertEqual(dispatcher.fallback_count, 2)

    def test_key_pool_status_never_leaks_key_strings(self):
        dispatcher = GeminiDecisionDispatcher(
            api_keys=["secret-key-alpha", "secret-key-beta"], client=MagicMock()
        )
        status = dispatcher.key_pool_status()
        self.assertEqual(
            set(status),
            {"total_keys", "available_keys", "cooling_down_keys", "next_recovery_s"},
        )
        self.assertEqual(status["total_keys"], 2)
        self.assertEqual(status["available_keys"], 2)
        self.assertEqual(status["cooling_down_keys"], 0)
        self.assertEqual(status["next_recovery_s"], 0.0)
        blob = json.dumps(status) + repr(dispatcher)
        self.assertNotIn("secret-key", blob)
        self.assertNotIn("alpha", blob)
        self.assertNotIn("beta", blob)


class TestHotReloadKeysFile(unittest.TestCase):
    """The keys file is re-read on every request when no keys were passed in."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "gemini_keys.txt"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict:
        return {"GIDEON_GEMINI_KEYS_FILE": str(self.path)}

    def test_create_edit_delete_reloads_live_and_preserves_cooldowns(self):
        client = MagicMock()
        client.post.return_value = _response(_choice_answer())
        with patch.dict(os.environ, self._env(), clear=True):
            dispatcher = GeminiDecisionDispatcher(client=client)  # no explicit keys
            self.assertEqual(dispatcher.api_keys, ())
            self.assertEqual(dispatcher.key_pool_status()["total_keys"], 0)

            # Created: comments, blank lines, inline comments and duplicates ignored.
            self.path.write_text(
                "# gideon keys\n\n  key-a  \nkey-b   # secondary\nkey-a\n", encoding="utf-8"
            )
            self.assertEqual(dispatcher.key_pool_status()["total_keys"], 2)
            self.assertEqual(dispatcher.api_keys, ("key-a", "key-b"))
            dispatcher.predict("pause")
            self.assertEqual(client.post.call_args.kwargs["headers"]["x-goog-api-key"], "key-a")

            # Pin cooldown state on a key that survives the next edit.
            dispatcher._cooldown_until["key-a"] = time.monotonic() + 300.0
            dispatcher._rate_limit_strikes["key-a"] = 2

            self.path.write_text("key-a\nkey-b\nkey-c\n", encoding="utf-8")
            self.assertEqual(dispatcher.key_pool_status()["total_keys"], 3)
            self.assertIn("key-a", dispatcher._cooldown_until)
            self.assertEqual(dispatcher._rate_limit_strikes["key-a"], 2)

            # Removed keys are pruned from the cooldown/strike bookkeeping.
            self.path.write_text("key-b\nkey-c\n", encoding="utf-8")
            self.assertEqual(dispatcher.key_pool_status()["total_keys"], 2)
            self.assertNotIn("key-a", dispatcher._cooldown_until)
            self.assertNotIn("key-a", dispatcher._rate_limit_strikes)
            self.assertEqual(dispatcher.api_keys, ("key-b", "key-c"))

            # Deleted: pool empties without raising.
            self.path.unlink()
            self.assertEqual(dispatcher.key_pool_status()["total_keys"], 0)
            self.assertEqual(dispatcher.api_keys, ())

    def test_explicit_keys_ignore_the_keys_file(self):
        self.path.write_text("file-key\n", encoding="utf-8")
        with patch.dict(os.environ, self._env(), clear=True):
            dispatcher = GeminiDecisionDispatcher(api_keys=["explicit-key"], client=MagicMock())
            self.assertEqual(dispatcher.api_keys, ("explicit-key",))
            self.assertIsNone(dispatcher._keys_file)


class TestCompactCriteria(unittest.TestCase):
    """Compact descriptions must cover every intent and still be distinguishable."""

    def test_compact_criteria_cover_all_65_intents_and_stay_distinct(self):
        raw = load_intent_criteria(compact=False)
        compact = load_intent_criteria(compact=True)
        self.assertEqual(len(raw), 65)
        self.assertEqual(len(compact), 65)
        self.assertEqual(set(raw), set(compact))
        for intent_id, text in compact.items():
            self.assertTrue(text.strip(), intent_id)
            self.assertLessEqual(len(text), 55, intent_id)
            self.assertNotEqual(text, raw[intent_id], intent_id)
        self.assertEqual(len(set(compact.values())), 65, "compact descriptions must be distinct")

    def test_compact_criteria_cut_serialized_payload_substantially(self):
        def payload(criteria: dict) -> str:
            return json.dumps(
                {
                    "model": DECISION_MODEL,
                    "input": "pause the music",
                    "questions": {
                        "intent": {
                            "type": "choice",
                            "instructions": _INTENT_INSTRUCTIONS,
                            "criteria": criteria,
                        }
                    },
                },
                ensure_ascii=False,
            )

        raw_size = len(payload(load_intent_criteria(compact=False)))
        compact_size = len(payload(load_intent_criteria(compact=True)))
        self.assertLessEqual(compact_size, 2600)
        self.assertGreaterEqual(1 - compact_size / raw_size, 0.40)

    def test_compact_flag_and_env_toggle(self):
        with patch.dict(os.environ, {"GIDEON_DECISION_COMPACT_CRITERIA": "0"}, clear=True):
            dispatcher = GeminiDecisionDispatcher(api_keys=["k"], client=MagicMock())
            self.assertEqual(dispatcher.criteria["media_pause"], "Pause currently playing media")
        with patch.dict(os.environ, {"GIDEON_DECISION_COMPACT_CRITERIA": "1"}, clear=True):
            dispatcher = GeminiDecisionDispatcher(api_keys=["k"], client=MagicMock())
            self.assertEqual(dispatcher.criteria["media_pause"], "Pause playback")
        with patch.dict(os.environ, {"GIDEON_DECISION_COMPACT_CRITERIA": "off"}, clear=True):
            dispatcher = GeminiDecisionDispatcher(api_keys=["k"], client=MagicMock())
            self.assertEqual(len(dispatcher.criteria["media_pause"]), len("Pause currently playing media"))


class TestTier1ModeSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_instance = reflex_router._gideon_dispatcher_instance
        reflex_router._gideon_dispatcher_instance = None

    def tearDown(self) -> None:
        reflex_router._gideon_dispatcher_instance = self.saved_instance

    def test_auto_mode_follows_api_key_presence(self):
        with patch.dict(os.environ, {"GIDEON_GEMINI_KEYS_FILE": ""}, clear=True):
            self.assertEqual(select_tier1_mode(), "bge")
            self.assertEqual(gemini_api_keys(), ())
        with patch.dict(
            os.environ, {"GIDEON_GEMINI_KEYS_FILE": "", "GEMINI_API_KEY": "***"}, clear=True
        ):
            self.assertEqual(select_tier1_mode(), "decision")
        with patch.dict(
            os.environ,
            {
                "GIDEON_GEMINI_KEYS_FILE": "",
                "GEMINI_API_KEYS": "key-one, key-two",
                "GIDEON_TIER1_ROUTER": "AUTO",
            },
            clear=True,
        ):
            self.assertEqual(select_tier1_mode(), "decision")
            self.assertEqual(gemini_api_keys(), ("key-one", "key-two"))

    def test_auto_mode_treats_non_empty_keys_file_as_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gemini_keys.txt"
            with patch.dict(
                os.environ, {"GIDEON_GEMINI_KEYS_FILE": str(path)}, clear=True
            ):
                # File does not exist yet -> still BGE.
                self.assertEqual(select_tier1_mode(), "bge")
                path.write_text("# only comments\n\n", encoding="utf-8")
                self.assertEqual(select_tier1_mode(), "bge")
                path.write_text("file-only-key\n", encoding="utf-8")
                self.assertEqual(select_tier1_mode(), "decision")
                self.assertEqual(gemini_api_keys(), ("file-only-key",))
                # Key file entries win ordering but are deduplicated against env.
                with patch.dict(os.environ, {"GEMINI_API_KEY": "file-only-key"}, clear=False):
                    self.assertEqual(gemini_api_keys(), ("file-only-key",))

    def test_explicit_modes_and_unknown_value(self):
        with patch.dict(
            os.environ,
            {"GIDEON_GEMINI_KEYS_FILE": "", "GIDEON_TIER1_ROUTER": "bge", "GEMINI_API_KEY": "***"},
            clear=True,
        ):
            self.assertEqual(select_tier1_mode(), "bge")
        with patch.dict(os.environ, {"GIDEON_TIER1_ROUTER": "decision"}, clear=True):
            self.assertEqual(select_tier1_mode(), "decision")  # keyless decision mode still allowed
        with patch.dict(
            os.environ, {"GIDEON_GEMINI_KEYS_FILE": "", "GIDEON_TIER1_ROUTER": "nonsense"}, clear=True
        ):
            self.assertEqual(select_tier1_mode(), "bge")

    def test_factory_uses_selected_router_only(self):
        with patch.dict(
            os.environ, {"GIDEON_TIER1_ROUTER": "decision", "GEMINI_API_KEY": "***"}, clear=True
        ), patch("decision_router.GeminiDecisionDispatcher") as decision_cls, patch(
            "gideon_handler.GideonDispatcher"
        ) as bge_cls:
            dispatcher = reflex_router.get_gideon_dispatcher()
            self.assertIs(dispatcher, decision_cls.return_value)
            decision_cls.assert_called_once_with()
            bge_cls.assert_not_called()

        reflex_router._gideon_dispatcher_instance = None
        with patch.dict(os.environ, {"GIDEON_TIER1_ROUTER": "bge"}, clear=True), patch(
            "decision_router.GeminiDecisionDispatcher"
        ) as decision_cls, patch("gideon_handler.GideonDispatcher") as bge_cls:
            dispatcher = reflex_router.get_gideon_dispatcher()
            self.assertIs(dispatcher, bge_cls.return_value)
            bge_cls.assert_called_once_with()
            decision_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
