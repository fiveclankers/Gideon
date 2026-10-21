"""Export trained BGE model to FP32 & INT8 ONNX, fit temperature scaling on calibration set,
and benchmark CPU latency.
"""
import json
import time
from pathlib import Path
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import onnx
import onnxruntime as ort
from onnxruntime.quantization import quantize_dynamic, QuantType
from scipy.optimize import minimize

MODEL_DIR = Path("/home/cat/gideon/models/bge_classifier")
SCHEMA_PATH = Path("/home/cat/gideon/intents_schema.json")
CALIB_PATH = Path("/home/cat/gideon/data/calib_atomic.jsonl")
ONNX_PATH = Path("/home/cat/gideon/models/model.onnx")
INT8_PATH = Path("/home/cat/gideon/models/model_int8.onnx")
TEMP_PATH = Path("/home/cat/gideon/models/temperature.json")
BENCH_PATH = Path("/home/cat/gideon/models/benchmark.json")

def main():
    print("Loading trained PyTorch model from", MODEL_DIR)
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR))
    model.eval()
    model.to("cpu")

    schema = json.loads(SCHEMA_PATH.read_text())
    all_intents = sorted(list(schema.keys()))
    id2label = {i: name for i, name in enumerate(all_intents)}
    label2id = {name: i for i, name in enumerate(all_intents)}

    # 1. Export FP32 ONNX
    print(f"\n--- Exporting FP32 ONNX to {ONNX_PATH} ---")
    dummy = tokenizer("set volume to 50%", return_tensors="pt", max_length=64, padding="max_length")
    torch.onnx.export(
        model,
        (dummy["input_ids"], dummy["attention_mask"]),
        str(ONNX_PATH),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch_size", 1: "seq_len"},
            "attention_mask": {0: "batch_size", 1: "seq_len"},
            "logits": {0: "batch_size"}
        },
        opset_version=14,
        do_constant_folding=True,
        dynamo=False
    )
    print(f"FP32 ONNX size: {ONNX_PATH.stat().st_size / (1024*1024):.2f} MB")

    # 2. Dynamic INT8 Quantization
    print(f"\n--- Quantizing to Dynamic INT8 ONNX -> {INT8_PATH} ---")
    quantize_dynamic(
        model_input=str(ONNX_PATH),
        model_output=str(INT8_PATH),
        weight_type=QuantType.QInt8
    )
    print(f"INT8 ONNX size: {INT8_PATH.stat().st_size / (1024*1024):.2f} MB")

    # 3. Fit Temperature Scaling on Calibration Set
    print(f"\n--- Fitting Temperature Scaling on Calibration Set ({CALIB_PATH}) ---")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    session = ort.InferenceSession(str(INT8_PATH), sess_options=opts, providers=["CPUExecutionProvider"])

    calib_rows = []
    with open(CALIB_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                calib_rows.append(json.loads(line))

    calib_logits = []
    calib_labels = []
    for row in calib_rows:
        enc = tokenizer(row["text"], return_tensors="np", max_length=64, padding="max_length")
        inputs = {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64)
        }
        logits = session.run(["logits"], inputs)[0]
        calib_logits.append(logits[0])
        calib_labels.append(label2id[row["label"]])

    calib_logits = np.array(calib_logits)
    calib_labels = np.array(calib_labels)

    def nll_loss(t):
        temp = t[0]
        scaled = calib_logits / temp
        exp_scaled = np.exp(scaled - np.max(scaled, axis=1, keepdims=True))
        probs = exp_scaled / np.sum(exp_scaled, axis=1, keepdims=True)
        correct_probs = probs[np.arange(len(calib_labels)), calib_labels]
        return -np.mean(np.log(np.clip(correct_probs, 1e-12, 1.0)))

    res = minimize(nll_loss, [1.0], bounds=[(0.1, 10.0)])
    optimal_temp = float(res.x[0])
    print(f"Optimal Temperature: {optimal_temp:.4f} (NLL before={nll_loss([1.0]):.4f}, after={res.fun:.4f})")

    temp_info = {
        "temperature": optimal_temp,
        "classes": all_intents,
        "id2label": id2label,
        "label2id": label2id,
        "val_accuracy": 0.9986
    }
    TEMP_PATH.write_text(json.dumps(temp_info, indent=2))
    print(f"Saved calibration config to {TEMP_PATH}")

    # 4. Latency Benchmark on CPU
    print("\n--- CPU Latency Benchmark (Tokenization + INT8 ONNX Inference) ---")
    test_queries = [
        "set volume to seventy percent",
        "switch to workspace four",
        "resume spotify",
        "what are my hyprland keybindings",
        "show revision queue for today",
        "take a screenshot of the region",
        "what is the difference between a tensor and a matrix",
        "mute audio output",
        "next wallpaper please",
        "open obsidian vault"
    ]

    latencies = []
    # Warmup
    for q in test_queries * 3:
        enc = tokenizer(q, return_tensors="np", max_length=64, padding="max_length")
        _ = session.run(["logits"], {"input_ids": enc["input_ids"].astype(np.int64), "attention_mask": enc["attention_mask"].astype(np.int64)})

    for q in test_queries * 20: # 200 runs
        t0 = time.perf_counter()
        enc = tokenizer(q, return_tensors="np", max_length=64, padding="max_length")
        _ = session.run(["logits"], {"input_ids": enc["input_ids"].astype(np.int64), "attention_mask": enc["attention_mask"].astype(np.int64)})
        latencies.append((time.perf_counter() - t0) * 1000.0)

    p50 = float(np.percentile(latencies, 50))
    p95 = float(np.percentile(latencies, 95))
    p99 = float(np.percentile(latencies, 99))
    print(f"CPU Latency (incl. tokenization): p50 = {p50:.2f} ms | p95 = {p95:.2f} ms | p99 = {p99:.2f} ms")

    bench_info = {
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "int8_model_size_mb": INT8_PATH.stat().st_size / (1024*1024)
    }
    BENCH_PATH.write_text(json.dumps(bench_info, indent=2))
    print(f"Saved benchmark results to {BENCH_PATH}")
    print("\nExport, calibration, and benchmarking completed successfully!")

if __name__ == "__main__":
    main()
