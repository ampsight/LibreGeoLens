# 🛰️ Tutorial: Adding a vLLM Backend to LibreGeoLens (QGIS)

> **Goal:** Run a multimodal LLM on an EC2 GPU using **vLLM**, then connect it to **LibreGeoLens** for inference through the OpenAI-compatible API.

---

## 📋 Overview
In this tutorial, we’ll:
1. Set up an EC2 instance capable of running vLLM  
2. Pick and deploy a multimodal model (e.g., LLaVA, Idefics, etc.)  
3. Connect LibreGeoLens to your vLLM endpoint  
4. Test the integration end-to-end inside QGIS  

---

## 1️⃣ Set Up the EC2 Instance

### Instance Configuration
| Setting | Recommended Value |
|----------|------------------|
| **Instance Type** | `g5.xlarge` (A10G GPU, 24 GB VRAM) |
| **Storage** | 150 GiB gp3 |
| **AMI** | Ubuntu 22.04 LTS (or Deep Learning AMI GPU PyTorch) |
| **Inbound Rules** | TCP 22 (SSH), TCP 8000 (vLLM API) — both from your IP/32 |

> 💡 Ensure the subnet routes to an Internet Gateway (for a public IP) and that the instance actually has a **Public IPv4 address**.

### SSH In and Install vLLM
```bash
ssh -i <path-to-key.pem> ubuntu@<EC2_PUBLIC_IP>

sudo apt update && sudo apt install -y python3-pip git
pip install --upgrade pip "vllm>=0.5.0"
# If using a base Ubuntu AMI, install NVIDIA drivers per AWS docs (skip if using a DLAMI)
````

---

## 2️⃣ Pick and Deploy a Model

> **Placeholder:** `***add a placeholder idk yet***`
> Example non-China-origin multimodal models:
>
> * `llava-hf/llava-v1.6-mistral-7b-hf`
> * `llava-hf/llava-v1.6-llama3-8b-hf`

> Optional: use an AWQ checkpoint if available and add `--quantization awq` for more headroom.

### Serve the Model with vLLM

```bash
vllm serve <MODEL_ID> \
  --host 0.0.0.0 --port 8000 \
  --dtype bfloat16 \
  --gpu-memory-utilization 0.92 \
  --max-model-len 4096
```

### Sanity Check

```bash
curl http://127.0.0.1:8000/v1/models
```

> ✅ Expected output: a JSON listing that includes `<MODEL_ID>`

> **API Base (for QGIS):** `http://<EC2_PUBLIC_IP>:8000/v1`

---

## 3️⃣ Configure LibreGeoLens (Manage MLLM Services)

Open **QGIS → LibreGeoLens → Manage MLLM Services → Add Service** and fill in the fields below.

| Field                  | Value                                                |
| ---------------------- | ---------------------------------------------------- |
| **Display Name**       | `vLLM (EC2)`                                         |
| **Provider Name**      | `openai`                                             |
| **Provider API Key**   | *(blank or dummy string)*                            |
| **API Base**           | `http://<EC2_PUBLIC_IP>:8000/v1`                     |
| **Supports Streaming** | ✅                                                   |
| **Models**             | `<MODEL_ID>` (exactly what you used in `vllm serve`) |

> ⚠️ Ensure your EC2 Security Group has inbound **TCP 8000** from your IP and that vLLM is bound to **0.0.0.0**.

---

## 4️⃣ LibreGeoLens Test

1. In QGIS, open LibreGeoLens and select your **vLLM (EC2)** service.
2. Choose the same model (`<MODEL_ID>`) you served.
3. Select a map chip (exported image).

   * Smaller images = faster inference.
4. Click **Send to MLLM** — you should receive a sensible output.


---

## 🔒 Operational & Security Recommendations

* Restrict EC2 **Security Group** to necessary IPs only
* Tag your AWS resources (`Program`, `Environment`, `Owner`) for cost tracking
* Redirect logs to CloudWatch or a file for monitoring
