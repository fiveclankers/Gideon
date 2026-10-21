#!/usr/bin/env python3
"""
Gideon Evaluation Harness for Lyra (laya_intent_v6).
Adheres strictly to the 4 ground rules:
1. Read-only on /home/cat/jarvis/models/laya_intent_v6/
2. Frozen question: INTENT_QUESTION from brain.laya_router used byte-identical
3. fp16 dance: .half() + torch.cuda.empty_cache()
4. Measurement-only (no local training).
5. Output matches data/laya_shadow.jsonl format: {query, expected, predicted, confidence, probs}
"""
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure jarvis root is in path for frozen question and laya dependencies
JARVIS_DIR = Path("/home/cat/jarvis")
GIDEON_DIR = Path("/home/cat/gideon")
sys.path.insert(0, str(JARVIS_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("eval_gideon_laya")

# Frozen question from brain.laya_router
from brain.laya_router import INTENT_QUESTION

MODEL_DIR = JARVIS_DIR / "models" / "laya_intent_v6"
BATTERY_FILE = GIDEON_DIR / "tests" / "gideon_battery.json"
OUTPUT_SHADOW_FILE = GIDEON_DIR / "data" / "gideon_laya_eval.jsonl"
OUTPUT_MISS_FILE = GIDEON_DIR / "data" / "gideon_laya_misses.jsonl"


def run_eval():
    if not MODEL_DIR.exists():
        logger.error(f"Deployed model directory not found: {MODEL_DIR}")
        sys.exit(1)

    if not BATTERY_FILE.exists():
        logger.error(f"Battery file not found: {BATTERY_FILE}")
        sys.exit(1)

    import torch
    import laya

    logger.info("Checking initial CUDA state...")
    if torch.cuda.is_available():
        free_bytes, total_bytes = torch.cuda.mem_get_info()
        logger.info(f"CUDA memory: Free={free_bytes / (1024**2):.1f}MB, Total={total_bytes / (1024**2):.1f}MB")

    t_load_0 = time.perf_counter()
    logger.info(f"Loading checkpoint read-only from {MODEL_DIR}...")
    agent = laya.load(str(MODEL_DIR))

    # fp16 dance
    logger.info("Applying fp16 conversion (.half()) and releasing allocator blocks...")
    agent.model.half()
    torch.cuda.empty_cache()
    load_time_s = time.perf_counter() - t_load_0
    logger.info(f"Model loaded in {load_time_s:.2f}s")

    if torch.cuda.is_available():
        free_bytes, total_bytes = torch.cuda.mem_get_info()
        logger.info(f"Post-load CUDA memory: Free={free_bytes / (1024**2):.1f}MB, Total={total_bytes / (1024**2):.1f}MB")

    with open(BATTERY_FILE, "r") as f:
        cases = json.load(f)

    logger.info(f"Running evaluation against {len(cases)} cases...")

    results = []
    misses = []
    latencies = []
    cat_stats = defaultdict(lambda: {"total": 0, "correct": 0})

    total_correct = 0

    for idx, case in enumerate(cases, 1):
        q = case["q"]
        exp = case["expected"]
        cat = case.get("category", "default")

        t0 = time.perf_counter()
        pred_out = agent.predict(q, INTENT_QUESTION)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(latency_ms)

        ans = pred_out.get("answers", {}).get("intent", {})
        predicted = ans.get("choice")
        conf = float(ans.get("confidence", 0.0) or 0.0)
        probs = ans.get("probabilities", {})

        is_correct = (predicted == exp)
        if is_correct:
            total_correct += 1
            cat_stats[cat]["correct"] += 1
        cat_stats[cat]["total"] += 1

        record = {
            "query": q,
            "expected": exp,
            "predicted": predicted,
            "confidence": round(conf, 4),
            "probs": {k: round(v, 4) for k, v in probs.items()},
            "latency_ms": round(latency_ms, 1),
            "category": cat,
            "correct": is_correct,
        }
        results.append(record)

        tag = "  [PASS] " if is_correct else "  [FAIL] "
        logger.info(f"{tag}[{idx:02d}/{len(cases):02d}] {latency_ms:4.0f}ms conf={conf:.2f} "
                    f"exp='{exp}' pred='{predicted}' :: \"{q}\"")

        if not is_correct:
            misses.append(record)

    # Save outputs following data/laya_shadow.jsonl conventions
    OUTPUT_SHADOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_SHADOW_FILE, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    with open(OUTPUT_MISS_FILE, "w") as f:
        for m in misses:
            # Matches exact shadow fine-tune training row format
            f.write(json.dumps({
                "query": m["query"],
                "expected": m["expected"],
                "predicted": m["predicted"],
                "confidence": m["confidence"],
                "probs": m["probs"],
            }) + "\n")

    # Summary report
    p50_lat = sorted(latencies)[len(latencies) // 2]
    p95_lat = sorted(latencies)[int(len(latencies) * 0.95)]
    acc = (total_correct / len(cases)) * 100.0

    print("\n" + "=" * 65)
    print(f"GIDEON LAYA INTENT EVALUATION SUMMARY ({MODEL_DIR.name})")
    print("=" * 65)
    print(f"Total Cases:     {len(cases)}")
    print(f"Accuracy:        {total_correct}/{len(cases)} ({acc:.1f}%)")
    print(f"Latency P50:     {p50_lat:.1f} ms")
    print(f"Latency P95:     {p95_lat:.1f} ms")
    print("-" * 65)
    print("CATEGORY BREAKDOWN:")
    for cat, st in sorted(cat_stats.items()):
        cat_acc = (st["correct"] / st["total"]) * 100.0
        print(f"  {cat:<25}: {st['correct']:2d}/{st['total']:2d} ({cat_acc:5.1f}%)")
    print("-" * 65)
    print(f"Full results written to: {OUTPUT_SHADOW_FILE}")
    print(f"Adversarial misses to:   {OUTPUT_MISS_FILE} ({len(misses)} rows for Kaggle fine-tuning)")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    run_eval()
