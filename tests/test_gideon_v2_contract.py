"""Phase 3: Integration & Contract Verification for GIDEON v2.

Tests:
1. Contract test: set(model.config.id2label.values()) == set(INTENT_DISPATCH_MAP.keys()).
2. Zero unmapped intents, zero dead handlers (100% 1:1 parity).
3. Negation guard verification: ensures inverted/cancelled commands never execute affirmatively.
4. End-to-end dry-run across 60 test utterances from the real-world evaluation set.
5. Dynamic token length latency benchmark (p50, p95, p99 on CPU).
6. Direct reflex execution rate without LLM fallback.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gideon_handler import INTENT_DISPATCH_MAP, GideonDispatcher
from gideon_shared import extract_params

EVAL_PATH = PROJECT_ROOT / "data" / "gideon_laya_eval.jsonl"
SCHEMA_PATH = PROJECT_ROOT / "intents_schema.json"
TEMP_PATH = PROJECT_ROOT / "models" / "temperature.json"

# Mapping from broad evaluation categories to acceptable atomic intent matches
CATEGORY_TO_ATOMIC = {
    "system_action": [
        "audio_volume_set", "audio_volume_up", "audio_volume_down", "audio_mute", "audio_unmute",
        "audio_mute_toggle", "audio_mic_mute", "audio_mic_unmute", "audio_mic_mute_toggle",
        "audio_output_headphones", "audio_output_speakers", "media_play", "media_pause",
        "media_play_pause", "media_next", "media_previous", "media_stop", "brightness_up",
        "brightness_down", "brightness_set", "window_close", "window_fullscreen_toggle",
        "window_focus_left", "window_focus_right", "window_focus_up", "window_focus_down",
        "window_floating_toggle", "workspace_switch", "workspace_previous", "workspace_next",
        "workspace_prev", "system_status", "system_lock", "system_logout", "system_reboot",
        "system_shutdown"
    ],
    "app_launch": [
        "launch_browser", "launch_brave", "launch_spotify", "launch_terminal",
        "launch_obsidian", "launch_code", "launch_file_manager"
    ],
    "omarchy": [
        "omarchy_theme_list", "omarchy_theme_set", "omarchy_theme_next", "omarchy_theme_dark",
        "omarchy_theme_light", "omarchy_wallpaper_next", "omarchy_wallpaper_set",
        "omarchy_keybindings", "omarchy_screenshot", "omarchy_screenshot_full", "omarchy_update"
    ],
    "study_retention": [
        "study_queue_view", "study_status_view", "study_doctor", "study_traps_view",
        "study_sprint_start", "flash_note_add"
    ],
    "flash_note": ["flash_note_add"],
    "engine_switch": ["engine_switch"],
    "conversation": ["conversation"],
    "hard_goal": ["hard_goal"],
    "web_search": ["web_search"],
    "browser_recipe": ["browser_use_passthrough"],
    "desktop_vision": ["browser_use_passthrough", "window_focus_left", "window_focus_right"],
    "url_open": ["launch_browser", "launch_brave", "web_search", "browser_use_passthrough"],
    "research_task": ["hard_goal", "web_search", "conversation"],
    "skill_route": ["hard_goal", "system_status"],
    "weather": ["web_search", "conversation"]
}


async def run_verification():
    print("================================================================================")
    print("                GIDEON v2 CONTRACT & DRY-RUN VERIFICATION                      ")
    print("================================================================================\n")

    # 1. Contract Test
    print("[1] Running Schema & Dispatch Contract Test...")
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_intents = set(schema.keys())
    dispatch_intents = set(INTENT_DISPATCH_MAP.keys())

    temp_data = json.loads(TEMP_PATH.read_text())
    model_intents = set(temp_data.get("id2label", {}).values())

    diff_schema_dispatch = schema_intents ^ dispatch_intents
    diff_model_dispatch = model_intents ^ dispatch_intents

    print(f"  Schema Intents Count:   {len(schema_intents)}")
    print(f"  Dispatch Map Count:     {len(dispatch_intents)}")
    print(f"  Model id2label Count:   {len(model_intents)}")

    assert not diff_schema_dispatch, f"Mismatch between schema and dispatch map: {diff_schema_dispatch}"
    assert not diff_model_dispatch, f"Mismatch between model labels and dispatch map: {diff_model_dispatch}"
    print("  CONTRACT TEST PASSED: Zero unmapped intents, zero dead handlers (100% 1:1 parity)!\n")

    # 2. Initialize Dispatcher
    print("[2] Initializing GideonDispatcher (Dynamic Sequence INT8 ONNX CPU Runtime)...")
    dispatcher = GideonDispatcher()
    print("  Dispatcher initialized successfully.\n")

    # 3. Negation Guard Test
    print("[3] Testing Negation Guard on Inverted Commands...")
    negation_test_cases = [
        "never mute the audio",
        "don't turn up the volume",
        "cancel the shutdown",
        "do not log out",
        "stop muting my mic",
        "don't switch workspace",
    ]
    for ntc in negation_test_cases:
        res = await dispatcher.dispatch(ntc, dry_run=True)
        assert res.data.get("negated") is True or not res.ok, f"Failed to guard negation for: {ntc}"
        print(f"  [GUARDED] '{ntc}' -> Escalated to LLM ({res.message})")
    print("  NEGATION GUARD TEST PASSED: Inverted voice commands safely escalated to LLM.\n")

    # 4. Load 60 real evaluation queries
    print("[4] Running End-to-End Dry-Run across 60 Evaluation Utterances...")
    eval_queries = []
    if EVAL_PATH.exists():
        with open(EVAL_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    eval_queries.append(json.loads(line))

    print(f"  Loaded {len(eval_queries)} real evaluation queries from {EVAL_PATH.name}.\n")

    print(f"{'STATUS':<7} | {'LATENCY':<8} | {'CONF':<6} | {'EXPECTED':<15} | {'PREDICTED':<25} | {'RISK':<9} | {'EXTRACTED PARAMS'}")
    print("-" * 115)

    passed_count = 0
    handled_directly_count = 0
    total_count = len(eval_queries)
    latencies = []

    for item in eval_queries:
        query = item.get("query", "")
        expected = item.get("expected", "")

        t0 = time.perf_counter()
        res = await dispatcher.dispatch(query, dry_run=True)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(latency_ms)

        intent_id = res.data.get("intent") or res.data.get("predicted_intent", "")
        conf = res.data.get("confidence", 0.0)

        # Check correctness against acceptable atomic mappings or direct match
        acceptable = CATEGORY_TO_ATOMIC.get(expected, [expected])
        is_pass = (intent_id == expected) or (intent_id in acceptable)

        if is_pass:
            passed_count += 1
            status_str = "[PASS]"
        else:
            status_str = "[FAIL]"

        passthrough_intents = {"conversation", "hard_goal", "web_search", "browser_use_passthrough"}
        if intent_id not in passthrough_intents and res.ok:
            handled_directly_count += 1

        risk_tier = schema.get(intent_id, {}).get("risk_tier", "safe")
        params_str = str(res.data.get("params", {})) if res.data else "{}"

        print(f"{status_str:<7} | {latency_ms:5.1f} ms | {conf:5.3f} | {expected:<15} | {intent_id:<25} | {risk_tier:<9} | {params_str}")

    print("-" * 115)
    acc = (passed_count / total_count) * 100.0 if total_count > 0 else 0.0
    direct_fraction = (handled_directly_count / total_count) * 100.0 if total_count > 0 else 0.0
    import numpy as np
    p50 = np.percentile(latencies, 50)
    p95 = np.percentile(latencies, 95)
    p99 = np.percentile(latencies, 99)

    print("\n--- RESULTS SUMMARY ---")
    print(f"Total Test Utterances:           {total_count}")
    print(f"Classification Pass Rate:       {passed_count}/{total_count} ({acc:.1f}%)")
    print(f"Direct Reflex Execution (no LLM): {handled_directly_count}/{total_count} ({direct_fraction:.1f}%) [Target ~70%]")
    print(f"End-to-End Latency:              p50 = {p50:.2f} ms | p95 = {p95:.2f} ms | p99 = {p99:.2f} ms")
    print("================================================================================\n")

if __name__ == "__main__":
    asyncio.run(run_verification())
