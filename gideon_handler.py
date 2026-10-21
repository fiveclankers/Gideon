"""GIDEON v2 Atomic Intent Dispatcher.

Architecture:
- BAAI/bge-small-en-v1.5 INT8 ONNX sequence classification across 65 atomic intents.
- Dynamic sequence length evaluation (~2.5ms inference on CPU).
- Negation detection guard preventing inverted commands from executing affirmatively.
- Temperature-scaled calibrated confidence scores.
- Risk tier gating (safe / confirm / dangerous).
- Parameter extraction layer.
- O(1) INTENT_DISPATCH_MAP connecting every atomic intent to a verified local command or callable.
- Downstream pass-through routing for conversation, hard_goal, web_search, browser_use_passthrough.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

from gideon_shared import HandlerResult, extract_params
from handlers_system import (
    handle_audio_volume_set,
    handle_audio_volume_up,
    handle_audio_volume_down,
    handle_audio_mute,
    handle_audio_unmute,
    handle_audio_mute_toggle,
    handle_audio_mic_mute,
    handle_audio_mic_unmute,
    handle_audio_mic_mute_toggle,
    handle_audio_output_headphones,
    handle_audio_output_speakers,
    handle_media_play,
    handle_media_pause,
    handle_media_play_pause,
    handle_media_next,
    handle_media_previous,
    handle_media_stop,
    handle_brightness_up,
    handle_brightness_down,
    handle_brightness_set,
    handle_window_close,
    handle_window_fullscreen_toggle,
    handle_window_focus_left,
    handle_window_focus_right,
    handle_window_focus_up,
    handle_window_focus_down,
    handle_window_floating_toggle,
    handle_workspace_switch,
    handle_workspace_previous,
    handle_workspace_next,
    handle_workspace_prev,
    handle_launch_browser,
    handle_launch_brave,
    handle_launch_spotify,
    handle_launch_terminal,
    handle_launch_obsidian,
    handle_launch_code,
    handle_launch_file_manager,
    handle_system_status,
    handle_system_lock,
    handle_system_logout,
    handle_system_reboot,
    handle_system_shutdown,
)
from handlers_omarchy import (
    handle_omarchy_theme_list,
    handle_omarchy_theme_set,
    handle_omarchy_theme_next,
    handle_omarchy_theme_dark,
    handle_omarchy_theme_light,
    handle_omarchy_wallpaper_next,
    handle_omarchy_wallpaper_set,
    handle_omarchy_keybindings,
    handle_omarchy_screenshot,
    handle_omarchy_screenshot_full,
    handle_omarchy_update,
)
from handlers_study_passthrough import (
    evaluate_risk_gate,
    handle_study_queue_view,
    handle_study_status_view,
    handle_study_doctor,
    handle_study_traps_view,
    handle_study_sprint_start,
    handle_flash_note_add,
    handle_engine_switch,
    handle_conversation,
    handle_hard_goal,
    handle_web_search,
    handle_browser_use_passthrough,
)

logger = logging.getLogger("gideon.dispatcher")

# Universal O(1) Intent Dispatch Map matching intents_schema.json 1:1
INTENT_DISPATCH_MAP: Dict[str, Callable[[Dict[str, Any], str], Coroutine[Any, Any, HandlerResult]]] = {
    # System Audio
    "audio_volume_set": handle_audio_volume_set,
    "audio_volume_up": handle_audio_volume_up,
    "audio_volume_down": handle_audio_volume_down,
    "audio_mute": handle_audio_mute,
    "audio_unmute": handle_audio_unmute,
    "audio_mute_toggle": handle_audio_mute_toggle,
    "audio_mic_mute": handle_audio_mic_mute,
    "audio_mic_unmute": handle_audio_mic_unmute,
    "audio_mic_mute_toggle": handle_audio_mic_mute_toggle,
    "audio_output_headphones": handle_audio_output_headphones,
    "audio_output_speakers": handle_audio_output_speakers,

    # Media
    "media_play": handle_media_play,
    "media_pause": handle_media_pause,
    "media_play_pause": handle_media_play_pause,
    "media_next": handle_media_next,
    "media_previous": handle_media_previous,
    "media_stop": handle_media_stop,

    # Brightness
    "brightness_up": handle_brightness_up,
    "brightness_down": handle_brightness_down,
    "brightness_set": handle_brightness_set,

    # Window Management
    "window_close": handle_window_close,
    "window_fullscreen_toggle": handle_window_fullscreen_toggle,
    "window_focus_left": handle_window_focus_left,
    "window_focus_right": handle_window_focus_right,
    "window_focus_up": handle_window_focus_up,
    "window_focus_down": handle_window_focus_down,
    "window_floating_toggle": handle_window_floating_toggle,

    # Workspace Management
    "workspace_switch": handle_workspace_switch,
    "workspace_previous": handle_workspace_previous,
    "workspace_next": handle_workspace_next,
    "workspace_prev": handle_workspace_prev,

    # App Launch
    "launch_browser": handle_launch_browser,
    "launch_brave": handle_launch_brave,
    "launch_spotify": handle_launch_spotify,
    "launch_terminal": handle_launch_terminal,
    "launch_obsidian": handle_launch_obsidian,
    "launch_code": handle_launch_code,
    "launch_file_manager": handle_launch_file_manager,

    # Omarchy & Desktop
    "omarchy_theme_list": handle_omarchy_theme_list,
    "omarchy_theme_set": handle_omarchy_theme_set,
    "omarchy_theme_next": handle_omarchy_theme_next,
    "omarchy_theme_dark": handle_omarchy_theme_dark,
    "omarchy_theme_light": handle_omarchy_theme_light,
    "omarchy_wallpaper_next": handle_omarchy_wallpaper_next,
    "omarchy_wallpaper_set": handle_omarchy_wallpaper_set,
    "omarchy_keybindings": handle_omarchy_keybindings,
    "omarchy_screenshot": handle_omarchy_screenshot,
    "omarchy_screenshot_full": handle_omarchy_screenshot_full,
    "omarchy_update": handle_omarchy_update,

    # System & Power
    "system_status": handle_system_status,
    "system_lock": handle_system_lock,
    "system_logout": handle_system_logout,
    "system_reboot": handle_system_reboot,
    "system_shutdown": handle_system_shutdown,

    # Study System
    "study_queue_view": handle_study_queue_view,
    "study_status_view": handle_study_status_view,
    "study_doctor": handle_study_doctor,
    "study_traps_view": handle_study_traps_view,
    "study_sprint_start": handle_study_sprint_start,
    "flash_note_add": handle_flash_note_add,

    # Engine Switch
    "engine_switch": handle_engine_switch,

    # Pass-Through
    "conversation": handle_conversation,
    "hard_goal": handle_hard_goal,
    "web_search": handle_web_search,
    "browser_use_passthrough": handle_browser_use_passthrough,
}

_NEGATION_RE = re.compile(
    r"\b(don't|dont|do not|never|cancel|undo|avoid|refrain|abort)\b",
    re.I,
)


def is_negated(text: str, intent_id: str) -> bool:
    """Detect negative phrasing that should prevent affirmative local execution."""
    lower = text.lower().strip()
    if _NEGATION_RE.search(lower):
        return True
    if re.search(r"\b(stop|halt)\b", lower):
        if intent_id == "media_stop" and re.search(r"\b(stop|halt)\s+(?:the\s+)?(?:music|song|playback|media|track|player|audio)\b", lower):
            return False
        if intent_id != "media_stop":
            return True
    return False


class GideonDispatcher:
    """Atomic intent classifier and deterministic execution engine."""

    def __init__(
        self,
        model_path: str = "/home/cat/gideon/models/model_int8.onnx",
        temperature_path: str = "/home/cat/gideon/models/temperature.json",
        schema_path: str = "/home/cat/gideon/intents_schema.json",
    ):
        self.schema = json.loads(Path(schema_path).read_text())
        
        # Load temperature and label metadata
        temp_data = json.loads(Path(temperature_path).read_text())
        self.temperature = float(temp_data.get("temperature", 1.0))
        self.id2label = {int(k): v for k, v in temp_data.get("id2label", {}).items()}
        self.label2id = {v: int(k) for k, v in self.id2label.items()}

        # Load Tokenizer & ONNX Session
        model_name = "BAAI/bge-small-en-v1.5"
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])

    def predict(self, text: str) -> Tuple[str, float, np.ndarray]:
        """Classify utterance into an atomic intent with calibrated confidence.
        
        Uses dynamic sequence length (~2.5ms on CPU) rather than fixed 64 padding.
        """
        enc = self.tokenizer(text, return_tensors="np", truncation=True)
        inputs = {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64)
        }
        logits = self.session.run(["logits"], inputs)[0][0]

        # Apply temperature scaling
        scaled_logits = logits / self.temperature
        exp_logits = np.exp(scaled_logits - np.max(scaled_logits))
        probs = exp_logits / np.sum(exp_logits)

        pred_id = int(np.argmax(probs))
        pred_label = self.id2label[pred_id]
        confidence = float(probs[pred_id])
        return pred_label, confidence, probs

    async def dispatch(self, utterance: str, dry_run: bool = False, confirmed: bool = False) -> HandlerResult:
        """End-to-end dispatch pipeline:
        1. Predict intent & calibrated confidence
        2. Negation guard check (preventing affirmative execution on negated commands)
        3. Evaluate risk gate
        4. Extract slot parameters
        5. Execute local handler or route pass-through
        """
        intent_id, confidence, probs = self.predict(utterance)
        intent_meta = self.schema.get(intent_id, {})
        risk_tier = intent_meta.get("risk_tier", "safe")

        # Negation Guard
        if is_negated(utterance, intent_id):
            logger.info("Utterance '%s' contains negation for intent '%s'. Escalating to LLM.", utterance, intent_id)
            return HandlerResult(
                ok=False,
                message="Negated command detected; escalating to LLM to prevent unintended affirmative action.",
                data={
                    "escalated": True,
                    "negated": True,
                    "predicted_intent": intent_id,
                    "confidence": confidence,
                    "utterance": utterance,
                }
            )

        # Risk Gating
        allowed, needs_confirm, reason = evaluate_risk_gate(intent_id, confidence, risk_tier)
        if not allowed:
            # Below threshold: escalate to downstream LLM
            logger.info("Escalating '%s' to LLM: %s", utterance, reason)
            return HandlerResult(
                ok=False,
                message=f"Escalated to LLM: {reason}",
                data={
                    "escalated": True,
                    "predicted_intent": intent_id,
                    "confidence": confidence,
                    "utterance": utterance,
                }
            )

        # Parameter extraction
        params = extract_params(intent_id, utterance)
        if confirmed:
            params["confirmed"] = True

        # Special fallback: if omarchy_theme_set was predicted but no specific theme name was mentioned
        # (e.g. "change my theme", "switch the theme"), seamlessly route to omarchy_theme_next
        if intent_id == "omarchy_theme_set" and "theme" not in params:
            intent_id = "omarchy_theme_next"
            intent_meta = self.schema.get(intent_id, {})

        # Check required slots
        req_slots = intent_meta.get("slots", {})
        for slot_name in req_slots:
            if slot_name not in params:
                logger.info("Required slot '%s' missing for '%s'. Escalating to LLM.", slot_name, intent_id)
                return HandlerResult(
                    ok=False,
                    message=f"Missing slot '{slot_name}' for intent '{intent_id}'. Escalating to LLM.",
                    data={"escalated": True, "intent": intent_id, "params": params}
                )

        if needs_confirm and not dry_run and not confirmed:
            return HandlerResult(
                ok=True,
                message=f"Confirmation required for {intent_id} ({risk_tier}): {intent_meta.get('command_or_callable')}",
                needs_confirmation=True,
                data={
                    "intent": intent_id,
                    "confidence": confidence,
                    "risk_tier": risk_tier,
                    "params": params,
                }
            )

        if dry_run:
            handler_fn = INTENT_DISPATCH_MAP.get(intent_id)
            return HandlerResult(
                ok=True,
                message=f"[DRY RUN] Would execute {intent_id} via {handler_fn.__name__ if handler_fn else 'None'}",
                needs_confirmation=needs_confirm,
                data={
                    "intent": intent_id,
                    "confidence": confidence,
                    "risk_tier": risk_tier,
                    "params": params,
                    "dry_run": True,
                }
            )

        handler = INTENT_DISPATCH_MAP.get(intent_id)
        if not handler:
            return HandlerResult(ok=False, error=f"No handler mapped for intent '{intent_id}'")

        res = await handler(params, raw_text=utterance)
        if res.data is None:
            res.data = {}
        res.data["intent"] = intent_id
        res.data["confidence"] = confidence
        res.data["risk_tier"] = risk_tier
        res.data["params"] = params
        return res
