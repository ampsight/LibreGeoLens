# 🧠 Tutorial: Adding an Ollama Backend to LibreGeoLens (QGIS)

> **Goal:** Run a multimodal LLM locally using **Ollama**, connecting it to **LibreGeoLens** through the OpenAI-compatible API for offline or private inference.

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

By default, Ollama listens on `http://localhost:11434`. If the above command either runs or throws an error stating that only one usage of each socket address is normally permitted, then you can proceed below. The error simply means that ollama began listening upon installation.

---

## 2️⃣ Pull and Test a Multimodal Model

Ollama can run both text-only and vision-language models.
Choose a lightweight multimodal one for fast inference.

### Recommended Models

| Model             | Description                                    | Approx. VRAM / RAM |
| ----------------- | ---------------------------------------------- | ------------------ |
| `moondream:latest`       | Very lightweight VLM designed for edge devices | < 8 GB             |
| `llava:7b`        | LLaVA 7B, solid performance on single GPU      | ~16 GB             |
| `llava-phi3`      | Smaller variant using Phi-3 backend            | ~8 GB              |
| `llava-llama3:8b` | LLaVA built on Llama-3                         | ~18 GB             |

### Pull the Model

```bash
ollama pull moondream
# or another model, e.g.
# ollama pull llava:7b
```

---

## 3️⃣ Configure LibreGeoLens (Manage MLLM Services)

Open **QGIS → LibreGeoLens → Manage MLLM Services → Add Service** and fill in:

| Field                  | Value                           |
| ---------------------- | ------------------------------- |
| **Display Name**       | `Ollama (Local)`                |
| **Provider Name**      | `openai`                        |
| **Provider API Key**   | *(blank or any dummy value)*    |
| **API Base**           | `http://127.0.0.1:11434/v1`     |
| **Supports Streaming** | ✅                             |
| **Models**             | `moondream:latest`              |

> ⚠️ The `/v1` at the end of **API Base** is important — it makes Ollama behave as an OpenAI-compatible endpoint for the plugin.

---

## 4️⃣ Test the Integration

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

## 🔒 Security & Performance Tips

* Keep images small for quick responses (≤ 1 MP recommended)
* Close Ollama when done (`Ctrl+C`) to free memory
* Restrict port `11434` to localhost unless intentionally sharing
* For persistent workshop setups, use `tmux` or `systemd`

---

**✅ Done!**
You’ve successfully connected **Ollama** to **LibreGeoLens**.
You can now run multimodal LLMs like *moondream* or *LLaVA* entirely on your own hardware — no external API required.
