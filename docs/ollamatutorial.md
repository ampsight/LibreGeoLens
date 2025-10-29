# 🧠 Tutorial: Adding an Ollama Backend to LibreGeoLens (QGIS)

> **Goal:** Run a multimodal LLM locally using **Ollama**, then connect it to **LibreGeoLens** through the OpenAI-compatible API for offline or private inference.

---

## 📋 Overview

In this tutorial, you will:
1. Set up Ollama on your local machine (Windows, macOS, or Linux)  
2. Pull and run a lightweight multimodal model  
3. Configure LibreGeoLens to connect to your local Ollama instance  
4. Test the workflow end-to-end inside QGIS  

---

## 1️⃣ Install and Run Ollama Locally

### Installation

#### macOS / Linux
```bash
curl -fsSL https://ollama.com/install.sh | sh
````

#### Windows

Download and install from:
👉 [https://ollama.com/download](https://ollama.com/download)

### Start the Ollama Service

```bash
ollama serve
```

By default, Ollama listens on `http://localhost:11434`.

---

## 2️⃣ Pull and Test a Multimodal Model

Ollama can run both text-only and vision-language models.
Choose a lightweight multimodal one for fast inference.

### Recommended Models

| Model             | Description                                    | Approx. VRAM / RAM |
| ----------------- | ---------------------------------------------- | ------------------ |
| `moondream`       | Very lightweight VLM designed for edge devices | < 8 GB             |
| `llava:7b`        | LLaVA 7B, solid performance on single GPU      | ~16 GB             |
| `llava-phi3`      | Smaller variant using Phi-3 backend            | ~8 GB              |
| `llava-llama3:8b` | LLaVA built on Llama-3                         | ~18 GB             |

### Pull the Model

```bash
ollama pull moondream
# or another model, e.g.
# ollama pull llava:7b
```

### Test Locally

```bash
ollama run moondream
> /set image /path/to/your/image.png
> Describe this image.
```

✅ You should see a short description returned.
If that works, the local API is ready for LibreGeoLens.

---

## 3️⃣ Configure LibreGeoLens (Manage MLLM Services)

Open **QGIS → LibreGeoLens → Manage MLLM Services → Add Service** and fill in:

| Field                  | Value                           |
| ---------------------- | ------------------------------- |
| **Display Name**       | `Ollama (Local)`                |
| **Provider Name**      | `openai`                        |
| **Provider API Key**   | *(blank or any dummy value)*    |
| **API Base**           | `http://127.0.0.1:11434/v1`     |
| **Supports Streaming** | ✅                               |
| **Models**             | e.g., `moondream` or `llava:7b` |

> ⚠️ The `/v1` at the end of **API Base** is important — it makes Ollama behave as an OpenAI-compatible endpoint for the plugin.

---

## 4️⃣ Test the Integration

### A. Direct API Test

Run this from a terminal (while Ollama is running):

```bash
curl http://127.0.0.1:11434/v1/chat/completions \
 -H "Content-Type: application/json" \
 -d '{
  "model":"moondream",
  "messages":[{"role":"user","content":[
    {"type":"text","text":"Describe the contents of this image."},
    {"type":"image_url","image_url":{"url":"file:///absolute/path/to/image.png"}}
  ]}],
  "max_tokens":128
 }'
```

If you get a textual description, the API is functioning.

---

### B. LibreGeoLens Test

1. In QGIS, open LibreGeoLens and choose **Ollama (Local)**.
2. Pick your model (e.g., `moondream`).
3. Draw or select a map chip image (prefer ≤ 1024 px).
4. Click **Send to MLLM** and confirm that you receive a caption or analysis.

---

## 🧰 Troubleshooting

| Symptom              | Likely Cause                                              | Fix                                       |
| -------------------- | --------------------------------------------------------- | ----------------------------------------- |
| `connection refused` | Ollama not running                                        | Start with `ollama serve`                 |
| `404 Not Found`      | Missing `/v1` in API Base                                 | Use `http://127.0.0.1:11434/v1`           |
| Model not found      | Typo in model name                                        | Run `ollama list` to verify               |
| Output slow / laggy  | CPU mode or large images                                  | Resize image ≤ 1024 px, try smaller model |
| Permission denied    | File path inaccessible                                    | Use full absolute path to image           |
| Want faster startup  | Pre-pull model with `ollama pull <model>` before workshop |                                           |

---

## 🧱 Optional: Advanced Setup for Shared Machines

If you need a shared demo environment:

* Run Ollama on a small **EC2 g5 instance** instead of locally
* Use `--host 0.0.0.0` when starting the service:

  ```bash
  OLLAMA_HOST=0.0.0.0 ollama serve
  ```
* Open port **11434/tcp** to your workshop IPs
* Connect using `http://<EC2_PUBLIC_IP>:11434/v1` in LibreGeoLens

---

## 🔒 Security & Performance Tips

* Keep images small for quick responses (≤ 1 MP recommended)
* Close Ollama when done (`Ctrl+C`) to free memory
* Restrict port `11434` to localhost unless intentionally sharing
* For persistent workshop setups, use `tmux` or `systemd`

---

## 📝 Fill-In Summary

| Item                      | Your Entry                   |
| ------------------------- | ---------------------------- |
| **Host Machine / EC2 IP** | `__________________________` |
| **Model ID**              | `__________________________` |
| **API Base**              | `http://127.0.0.1:11434/v1`  |
| **Chip / Image Path**     | `__________________________` |
| **Result Notes**          | `__________________________` |

---

**✅ Done!**
You’ve successfully connected **Ollama** to **LibreGeoLens**.
You can now run multimodal LLMs like *moondream* or *LLaVA* entirely on your own hardware — no external API required.

```
---
```
