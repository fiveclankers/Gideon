"""Train, mine hard negatives, retrain, export to INT8 ONNX, and calibrate
BAAI/bge-small-en-v1.5 sequence classifier for GIDEON v2.
"""
import json
import os
import time
from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torch.optim import AdamW
from transformers import AutoTokenizer, AutoModelForSequenceClassification, get_linear_schedule_with_warmup
import onnx
import onnxruntime as ort
from onnxruntime.quantization import quantize_dynamic, QuantType
from scipy.optimize import minimize

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device} (GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'None'})")

SCHEMA_PATH = Path("/home/cat/gideon/intents_schema.json")
intents_schema = json.loads(SCHEMA_PATH.read_text())
all_intents = sorted(list(intents_schema.keys()))
id2label = {i: name for i, name in enumerate(all_intents)}
label2id = {name: i for i, name in enumerate(all_intents)}
num_labels = len(all_intents)

print(f"Total atomic classes: {num_labels}")

class TextIntentDataset(Dataset):
    def __init__(self, rows, tokenizer, max_len=64):
        self.rows = rows
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        row = self.rows[idx]
        text = row["text"]
        label = label2id[row["label"]]
        enc = self.tokenizer(
            text,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_tensors="pt"
        )
        return {
            "input_ids": enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "label": torch.tensor(label, dtype=torch.long)
        }

def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows

def train_epoch(model, loader, optimizer, scheduler):
    model.train()
    total_loss = 0.0
    for batch in loader:
        optimizer.zero_grad()
        input_ids = batch["input_ids"].to(device)
        attention_mask = batch["attention_mask"].to(device)
        labels = batch["label"].to(device)

        outputs = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
        loss = outputs.loss
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        scheduler.step()
        total_loss += loss.item()
    return total_loss / len(loader)

def evaluate(model, loader):
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for batch in loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["label"].to(device)

            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            preds = torch.argmax(outputs.logits, dim=-1)
            correct += (preds == labels).sum().item()
            total += labels.size(0)
    return correct / total if total > 0 else 0.0

def main():
    model_name = "BAAI/bge-small-en-v1.5"
    print(f"Loading tokenizer & pretrained backbone: {model_name}")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name,
        num_labels=num_labels,
        id2label=id2label,
        label2id=label2id
    ).to(device)

    train_rows = load_jsonl("/home/cat/gideon/data/train_atomic.jsonl")
    val_rows = load_jsonl("/home/cat/gideon/data/val_atomic.jsonl")
    calib_rows = load_jsonl("/home/cat/gideon/data/calib_atomic.jsonl")

    print(f"Dataset sizes: Train={len(train_rows)}, Val={len(val_rows)}, Calib={len(calib_rows)}")

    train_ds = TextIntentDataset(train_rows, tokenizer)
    val_ds = TextIntentDataset(val_rows, tokenizer)

    train_loader = DataLoader(train_ds, batch_size=32, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=64, shuffle=False)

    # Round 1: Train v0
    epochs = 3
    total_steps = len(train_loader) * epochs
    optimizer = AdamW(model.parameters(), lr=3e-5, weight_decay=0.01)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=int(0.1 * total_steps), num_training_steps=total_steps)

    print("\n--- Starting Training Round 1 (v0) on GPU ---")
    for epoch in range(1, epochs + 1):
        loss = train_epoch(model, train_loader, optimizer, scheduler)
        val_acc = evaluate(model, val_loader)
        print(f"Epoch {epoch}/{epochs} | Train Loss: {loss:.4f} | Val Acc: {val_acc*100:.2f}%")

    # Hard Negative Mining on tricky candidates
    print("\n--- Mining Hard Negatives ---")
    tricky_candidates = [
        # Crucial noun-verb collision cases:
        {"text": "resume spotify", "label": "media_play"},
        {"text": "pause spotify", "label": "media_pause"},
        {"text": "play spotify", "label": "media_play"},
        {"text": "open spotify", "label": "launch_spotify"},
        {"text": "start spotify", "label": "launch_spotify"},
        {"text": "launch spotify app", "label": "launch_spotify"},
        {"text": "automate workspace switching using a bash daemon", "label": "hard_goal"},
        {"text": "write a python script to monitor system volume", "label": "hard_goal"},
        {"text": "how does rm -rf delete directories recursively", "label": "conversation"},
        {"text": "when should we stop training the neural net", "label": "conversation"},
        {"text": "click on the blue submit button on screen", "label": "browser_use_passthrough"},
        {"text": "switch audio to headphones", "label": "audio_output_headphones"},
        {"text": "switch audio to speakers", "label": "audio_output_speakers"},
        {"text": "set dark theme in omarchy", "label": "omarchy_theme_dark"},
        {"text": "set light theme in omarchy", "label": "omarchy_theme_light"},
        {"text": "next wallpaper", "label": "omarchy_wallpaper_next"},
        {"text": "change wallpaper", "label": "omarchy_wallpaper_next"},
        {"text": "show revision queue", "label": "study_queue_view"},
        {"text": "run study doctor", "label": "study_doctor"},
        {"text": "show study status", "label": "study_status_view"},
        {"text": "show traps for phy_01", "label": "study_traps_view"},
        {"text": "start study sprint for phy_01", "label": "study_sprint_start"},
        {"text": "close active window", "label": "window_close"},
        {"text": "kill window", "label": "window_close"},
        {"text": "toggle fullscreen", "label": "window_fullscreen_toggle"},
        {"text": "move focus left", "label": "window_focus_left"},
        {"text": "move focus right", "label": "window_focus_right"},
        {"text": "mute microphone", "label": "audio_mic_mute"},
        {"text": "unmute microphone", "label": "audio_mic_unmute"},
        {"text": "mute volume", "label": "audio_mute"},
        {"text": "unmute volume", "label": "audio_unmute"},
        {"text": "set volume to 40%", "label": "audio_volume_set"},
        {"text": "turn up volume", "label": "audio_volume_up"},
        {"text": "turn down volume", "label": "audio_volume_down"},
        # Conversational "play" / theatre / sport / instrument hard negatives.
        # The atomic dataset contains almost no conversational uses of "play",
        # so the model over-fires media_play on these.
        {"text": "That play seems nice.", "label": "conversation"},
        {"text": "The play was good.", "label": "conversation"},
        {"text": "I liked that play.", "label": "conversation"},
        {"text": "What did you think of the play?", "label": "conversation"},
        {"text": "We saw a play last night.", "label": "conversation"},
        {"text": "I really enjoyed that play.", "label": "conversation"},
        {"text": "That sounds like a nice play.", "label": "conversation"},
        {"text": "The play starts at eight.", "label": "conversation"},
        {"text": "The play received good reviews.", "label": "conversation"},
        {"text": "I saw a great play at the theatre.", "label": "conversation"},
        {"text": "We watched a play on Broadway.", "label": "conversation"},
        {"text": "That movie is about a play.", "label": "conversation"},
        {"text": "I play tennis every weekend.", "label": "conversation"},
        {"text": "He plays guitar.", "label": "conversation"},
        {"text": "She played the piano.", "label": "conversation"},
        {"text": "I'm playing a video game.", "label": "conversation"},
        {"text": "My kids play outside.", "label": "conversation"},
        {"text": "I love playing chess.", "label": "conversation"},
        {"text": "We went to the theatre.", "label": "conversation"},
        {"text": "I play football on Sundays.", "label": "conversation"},
        {"text": "He is playing football with his friends.", "label": "conversation"},
        {"text": "She plays the violin in an orchestra.", "label": "conversation"},
        {"text": "I played chess with my grandfather.", "label": "conversation"},
        {"text": "They are playing video games upstairs.", "label": "conversation"},
        {"text": "My daughter loves to play the piano.", "label": "conversation"},
        {"text": "I was thinking about what you said earlier. That play seems nice, but I'm not sure.", "label": "conversation"},
        {"text": "We went to the theatre last night. The play was excellent.", "label": "conversation"},
        {"text": "I saw a great play at the theatre. Have you seen it?", "label": "conversation"},
        {"text": "The play starts at eight, so we should leave soon.", "label": "conversation"},
        {"text": "I play tennis every weekend. It keeps me fit.", "label": "conversation"},
        # Keep the actual media commands anchored while adding those negatives.
        {"text": "play some music", "label": "media_play"},
        {"text": "can you play some music", "label": "media_play"},
        {"text": "put on some music", "label": "media_play"},
        {"text": "I want you to keep playing music", "label": "media_play"},
        {"text": "resume the video", "label": "media_play"},
        {"text": "pause the video", "label": "media_pause"},
        {"text": "next song", "label": "media_next"},
        {"text": "stop the music", "label": "media_stop"},
    ]

    mined_hard_samples = []
    model.eval()
    with torch.no_grad():
        for cand in tricky_candidates:
            enc = tokenizer(cand["text"], return_tensors="pt", max_length=64, truncation=True).to(device)
            out = model(**enc)
            probs = torch.softmax(out.logits, dim=-1).squeeze(0)
            pred_id = torch.argmax(probs).item()
            pred_label = id2label[pred_id]
            conf = probs[pred_id].item()
            if pred_label != cand["label"] or conf < 0.90:
                print(f"  [Mined Hard Case] '{cand['text']}': True={cand['label']}, Pred={pred_label} ({conf:.3f})")
                for _ in range(5):
                    mined_hard_samples.append(cand)

    if mined_hard_samples:
        print(f"Adding {len(mined_hard_samples)} weighted mined hard negatives to training set.")
        train_rows.extend(mined_hard_samples)
        train_ds = TextIntentDataset(train_rows, tokenizer)
        train_loader = DataLoader(train_ds, batch_size=32, shuffle=True)

        print("\n--- Retraining Round 2 with Hard Negatives ---")
        epochs_r2 = 2
        total_steps_r2 = len(train_loader) * epochs_r2
        optimizer_r2 = AdamW(model.parameters(), lr=1.5e-5, weight_decay=0.01)
        scheduler_r2 = get_linear_schedule_with_warmup(optimizer_r2, num_warmup_steps=int(0.1 * total_steps_r2), num_training_steps=total_steps_r2)

        for epoch in range(1, epochs_r2 + 1):
            loss = train_epoch(model, train_loader, optimizer_r2, scheduler_r2)
            val_acc = evaluate(model, val_loader)
            print(f"Round 2 Epoch {epoch}/{epochs_r2} | Train Loss: {loss:.4f} | Val Acc: {val_acc*100:.2f}%")

    final_val_acc = evaluate(model, val_loader)
    print(f"\nFinal Validation Accuracy: {final_val_acc*100:.2f}%")
    assert final_val_acc >= 0.95, f"Validation accuracy {final_val_acc} is below required 95%!"

    # Save PyTorch checkpoint
    save_dir = Path("/home/cat/gideon/models/bge_classifier")
    save_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(save_dir)
    tokenizer.save_pretrained(save_dir)
    print(f"Saved PyTorch model and tokenizer to {save_dir}")

    # Export to ONNX
    print("\n--- Exporting to ONNX ---")
    model.eval()
    model.to("cpu")
    dummy_input = tokenizer("set volume to 50%", return_tensors="pt", max_length=64, padding="max_length")
    onnx_path = Path("/home/cat/gideon/models/model.onnx")

    torch.onnx.export(
        model,
        (dummy_input["input_ids"], dummy_input["attention_mask"]),
        str(onnx_path),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch_size", 1: "seq_len"},
            "attention_mask": {0: "batch_size", 1: "seq_len"},
            "logits": {0: "batch_size"}
        },
        opset_version=14
    )
    print(f"Exported FP32 ONNX model to {onnx_path} ({onnx_path.stat().st_size / (1024*1024):.2f} MB)")

    # Quantize to INT8 ONNX
    print("\n--- Quantizing to INT8 ONNX (Dynamic) ---")
    int8_path = Path("/home/cat/gideon/models/model_int8.onnx")
    quantize_dynamic(
        model_input=str(onnx_path),
        model_output=str(int8_path),
        weight_type=QuantType.QInt8
    )
    print(f"Exported INT8 ONNX model to {int8_path} ({int8_path.stat().st_size / (1024*1024):.2f} MB)")

    # Temperature Scaling on Calibration Set
    print("\n--- Fitting Temperature Scaling on Calibration Set ---")
    session = ort.InferenceSession(str(int8_path), providers=["CPUExecutionProvider"])
    
    calib_logits = []
    calib_labels = []
    for row in calib_rows:
        enc = tokenizer(row["text"], return_tensors="np", max_length=64, padding="max_length")
        ort_inputs = {
            "input_ids": enc["input_ids"].astype(np.int64),
            "attention_mask": enc["attention_mask"].astype(np.int64)
        }
        logits = session.run(["logits"], ort_inputs)[0]
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
        "val_accuracy": float(final_val_acc)
    }
    Path("/home/cat/gideon/models/temperature.json").write_text(json.dumps(temp_info, indent=2))

    # CPU Latency Benchmark (including tokenization)
    print("\n--- Benchmarking CPU Latency (Tokenization + INT8 ONNX Inference) ---")
    test_queries = [
        "set volume to seventy percent",
        "switch to workspace four",
        "resume spotify",
        "what are my hyprland keybindings",
        "show revision queue for today",
        "take a screenshot of the region",
        "what is the difference between a tensor and a matrix"
    ]
    latencies = []
    for q in test_queries * 3:
        enc = tokenizer(q, return_tensors="np", max_length=64, padding="max_length")
        _ = session.run(["logits"], {"input_ids": enc["input_ids"].astype(np.int64), "attention_mask": enc["attention_mask"].astype(np.int64)})

    for q in test_queries * 20:
        t0 = time.perf_counter()
        enc = tokenizer(q, return_tensors="np", max_length=64, padding="max_length")
        _ = session.run(["logits"], {"input_ids": enc["input_ids"].astype(np.int64), "attention_mask": enc["attention_mask"].astype(np.int64)})
        latencies.append((time.perf_counter() - t0) * 1000.0)

    p50 = float(np.percentile(latencies, 50))
    p95 = float(np.percentile(latencies, 95))
    p99 = float(np.percentile(latencies, 99))
    print(f"CPU Latency (incl. tokenization): p50={p50:.2f} ms | p95={p95:.2f} ms | p99={p99:.2f} ms")

    benchmark_info = {
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "int8_model_size_mb": int8_path.stat().st_size / (1024*1024)
    }
    Path("/home/cat/gideon/models/benchmark.json").write_text(json.dumps(benchmark_info, indent=2))

    print("\nPhase 1 Model Training, Mining, Export & Benchmarking Complete!")

if __name__ == "__main__":
    main()
