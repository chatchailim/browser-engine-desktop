# Browser Engine Desktop — OneVault Interactive Agent Engine

> **Browser Use + Computer Use + Shell Use** in a single security-hardened, on-premise AI agent runtime.  
> [เอกสารภาษาไทยฉบับเต็ม (Thai Documentation)](./README-TH.md)

**Browser Engine Desktop** (`browser-engine-desktop`) is a high-speed interactive automation engine built on a **Slow Brain / Fast Brain** architecture:
- **Slow Brain (Planner AI):** On-premise LLM (e.g., Qwen3 / vLLM / Ollama) with automatic planner tiering and fallback.
- **Fast Brain (Local Execution):** Sub-50ms local execution via Playwright/CDP, Windows UI Automation (UIA), Vision Grounding, and Sandboxed PowerShell.
- **OneVault Fact Integrity:** Tamper-evident audit trail (`FactWebTaskExecuted`, `FactWebActionApproved`, `FactDesktopWindowRead`, `FactApiCallExecuted`, `FactDataErased`) with SHA-256 content hashing and Ed25519 chained command manifests.

---

## Core Capabilities

### 1. 5-Level Interaction Router
1. **Level 1 — Direct API (`Strategy.DIRECT_API`):** Pre-approved API recipes only (`api_recipes.json`), host allowlist, anti-redirect guard, idempotency keys, and Risk 0/2/3 approval gates.
2. **Level 2 — DOM Locator:** Fast indexed DOM elements (`dom_index.js`) with `<15ms` execution.
3. **Level 3 — Accessibility Tree:** Semantic UI role/name/bounds extraction for complex web apps.
4. **Level 4 — Vision Grounding:** On-premise VLM coordinate grounding with confidence thresholds and private-LAN enforcement.
5. **Level 5 — Computer Use (Windows UIA & Desktop Vision):** Governed Windows desktop inspection and human-approved desktop automation (`desktop_engine.py`, `windows_uia.py`, `desktop_vision_reader.py`).

### 2. Sandboxed Shell Use & Audit Chain
- Executes PowerShell commands inside a policy-guarded sandbox with rollback support.
- Every command is recorded in an **Ed25519-signed sequential audit chain** verifiable via `/api/audit/list` and `/api/audit/manifest`.

### 3. Security, Privacy & Governance by Design
- **Localhost-Only API (`127.0.0.1:8205`):** Token-authenticated (`X-Desktop-Token`) with role-based access control (`admin`, `operator`, `viewer`) and token expiration/revocation.
- **Inverted Risk Gate (Risk 0–3):** Checkout/payment/destructive contexts default to approval-required (`POST /api/approve/<task>/<step>`).
- **Anti-Bot & Human Takeover Guard:** Automatically pauses on Cloudflare Turnstile, reCAPTCHA, hCaptcha, or login walls for safe human takeover.
- **PDPA & 90-Day Retention:** Right-to-erasure (`/api/pdpa/erase`) and automated trace/screenshot retention policies.
- **Task Slot Pool & Queue:** Concurrent browser task slots (`TaskSlotPool`) with exclusive machine-lock semantics for desktop tasks.

---

## Quick Start (Windows)

### Prerequisites
- Windows 10/11 with Python 3.10+
- An OpenAI-compatible local LLM endpoint (e.g., vLLM or Ollama)

### Installation
```powershell
git clone https://github.com/chatchailim/browser-engine-desktop.git
cd browser-engine-desktop

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

### Configuration
Copy `config.example.json` to `config.json` (or `config.local.json`) and point `ai_endpoints` / `planner_url` to your local LLM / DGX gateway:
```powershell
Copy-Item config.example.json config.json
```

### Run the Server
```powershell
.\run.ps1
```
Then open the Interactive Lab UI at `http://127.0.0.1:8205/` (or `http://127.0.0.1:8205/lab.html`).

---

## Architecture Overview

```text
User / App / Lab UI
        │  (X-Desktop-Token, 127.0.0.1:8205)
        ▼
  Interactive Server (server.py)
   ├── TaskSlotPool & TaskQueue
   ├── Planner (Slow Brain: Local vLLM / Ollama)
   ├── 5-Level Interaction Router
   │     ├── L1: Direct API (Recipes + Risk Gate)
   │     ├── L2: Playwright DOM (dom_index.js)
   │     ├── L3: Accessibility Tree
   │     ├── L4: Vision Grounding (VLM)
   │     └── L5: Windows UIA / Desktop Use
   ├── Sandboxed Shell Runner + Ed25519 Audit Chain
   └── OneVault Fact Spool (SHA-256 + RFC 8785)
```

---

## License
Released under the [MIT License](./LICENSE).
