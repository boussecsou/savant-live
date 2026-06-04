<div align="center">

![SAVANT Banner](docs/assets/banner.png)

# SAVANT

### Voice AI Agent for VPS Management

**Built for the [IApreneur × Hostinger Hackathon 2026](https://hostinger.com)**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Gemini Live](https://img.shields.io/badge/Gemini_3.1_Flash_Live-4285F4?style=flat&logo=google&logoColor=white)](https://ai.google.dev)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat&logo=docker&logoColor=white)](https://docker.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

*Manage your Linux VPS through natural voice conversation. No SSH. No terminal. Just talk.*

</div>

---

## What is SAVANT?

SAVANT is a **real-time voice DevOps agent** that sits between you and your Linux server. You speak; SAVANT understands your intent, executes the appropriate commands, verifies results, and narrates what happened—all through natural conversation.

**The problem it solves:** DevOps operations require constant context-switching between terminals, dashboards, and documentation. SAVANT collapses that into a single voice conversation. You stay focused on what matters.

**What makes it different:**
- **Gemini 3.1 Flash Live** — true bidirectional audio streaming, not speech-to-text + chat + TTS
- **Two-tier execution** — fast inline commands (~1-3s) or complex multi-step Hermes agent tasks (~10-30s)
- **Persistent memory** — SAVANT remembers your infrastructure, preferences, and past sessions across conversations
- **Safety-first design** — no destructive operation executes without explicit voice confirmation

---

## Features

| Feature | Description |
|---|---|
| **Real-time voice** | Full-duplex audio via Gemini 3.1 Flash Live — speak naturally, interrupt anytime |
| **VPS command execution** | Run any shell command on your VPS in ~1-3 seconds inline |
| **Complex task agent** | Multi-step Hermes agent for Notion, vision scripts, and long-running operations |
| **Persistent memory** | Remembers your server stack, preferences, and key facts across sessions |
| **Proactive monitoring** | Polls CPU/RAM/DISK/containers every 20s — alerts you before things break |
| **Skill library** | Procedural knowledge stored as markdown files, loaded on demand |
| **Workflow triggers** | Register webhook URLs once, trigger them by voice with custom payloads |
| **Console Live** | Isolated Gemini-narrated shell accessible from the PWA data panel |
| **Notion integration** | Fast-read lane for Notion pages (~1-3s direct REST, no Hermes overhead) |
| **Confirmation gate** | Destructive operations require explicit "oui" — no accidental `rm -rf` |
| **Backup before write** | Every write/critical action is backed up first, undo available via voice |
| **Session summaries** | End-of-session AI summary saved to SQLite, briefed at next login |

---

## Architecture

```
Owner (voice)
     │ PCM 16kHz
     ▼
PWA  (Vanilla JS · Web Audio API · Glass-morphism UI)
     │ WebSocket — binary PCM + JSON control frames
     ▼
Backend  (FastAPI · asyncio · Python 3.11)
     │
     ├── Gemini 3.1 Flash Live  ←── system prompt + memory + metrics
     │        │ tool calls
     │        ├── run_command ──────────► Hermes Exec Server :8644  (~1-3s)
     │        └── execute_action ────────► Hermes v2 LLM Gateway :8642  (~10-30s)
     │                                          │
     │                                          └─► VPS Host (nsenter / /host)
     │
     ├── Redis    — session flags (paused, stop, current task)
     ├── SQLite   — memory, sessions, incidents, action log, workflows
     └── Files    — METRICS.txt, QUICK_VISION.md (token-free fast lanes)
```

See [`docs/architecture.md`](docs/architecture.md) for the full breakdown.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11 · FastAPI · WebSockets · asyncio |
| Voice LLM | Google Gemini 3.1 Flash Live (`google-genai==2.4.0`) |
| Summarizer | `gemini-2.5-flash-lite` via REST |
| VPS Execution | Hermes v2 — OpenAI-compatible gateway + raw exec server |
| Ephemeral state | Redis |
| Persistent memory | SQLite WAL |
| Frontend | Vanilla JS PWA · Web Audio API · Inline markdown renderer |
| Infrastructure | Docker Compose · Caddy (TLS) |

---

## Prerequisites

- **Docker** and **Docker Compose** (v2+)
- **Google Gemini API key** — [Get one here](https://aistudio.google.com/apikey)
- **OpenRouter API key** — used by Hermes for multi-step task execution
- A **Linux VPS** — tested on Ubuntu 22.04 / Debian 12
- (Optional) **Notion API key** — enables voice-querying your Notion workspace

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/your-username/savant.git
cd savant

# 2. Configure environment
cp .env.example .env
# Edit .env and fill in your API keys

# 3. Launch
docker compose build
docker compose up -d

# 4. Verify
curl http://localhost:8000/health
curl http://localhost:8000/health/redis
```

Then open `https://your-domain` in a modern browser (Chrome or Edge recommended for Web Audio API support).

See [`docs/getting-started.md`](docs/getting-started.md) for a detailed walkthrough including first-run onboarding.

---

## Configuration

Copy `.env.example` to `.env` and fill in the required values:

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google AI Studio API key |
| `OPENROUTER_API_KEY` | Yes | OpenRouter key for Hermes agent |
| `HERMES_WEBHOOK_SECRET` | Yes | Shared secret between backend and Hermes |
| `REDIS_HOST` | Yes | Redis container name (default: `redis-container`) |
| `NOTION_API_KEY` | No | Enables Notion fast-read lane |

See [`docs/configuration.md`](docs/configuration.md) for the complete reference.

---

## How It Works

1. **Connect** — Open the PWA and enter your access code to unlock the voice interface
2. **Speak** — Talk naturally: *"What's the CPU usage?"* or *"Restart the nginx container"*
3. **Confirm** — For destructive operations, SAVANT asks first and waits for *"oui"*
4. **See results** — The data panel shows structured output (tables, reports, JSON) while SAVANT narrates
5. **Ask anything** — *"What did you change yesterday?"*, *"Show me unresolved incidents"*, *"Trigger my deploy workflow"*

---

## Project Structure

```
app/
├── main.py                 WebSocket core — session, Gemini, tools, reconciler
├── backend/
│   ├── prompts.py          System prompt blocks
│   ├── skills.py           Markdown skill registry
│   ├── workflows.py        Webhook workflow CRUD + triggers
│   ├── notion.py           Notion REST fast-read lane
│   ├── console.py          Console Live mode
│   ├── hermes/client.py    SSE streaming client to Hermes
│   └── memory/database.py  SQLite — all persistent state
├── pwa/                    Vanilla JS PWA (index.html)
├── scripts/                Reset and maintenance scripts
└── docs/                   Full documentation
```

---

## Documentation

| Document | Description |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Full system architecture — components, data flows, design patterns |
| [`docs/getting-started.md`](docs/getting-started.md) | Step-by-step installation and first-run guide |
| [`docs/features.md`](docs/features.md) | Every feature explained with voice command examples |
| [`docs/configuration.md`](docs/configuration.md) | All environment variables and configuration options |
| [`docs/security.md`](docs/security.md) | Safety mechanisms — confirmation gate, backup, verification |

---

## Hackathon

This project was built for the **IApreneur × Hostinger Hackathon 2026**, a competition focused on AI-powered applications hosted on Hostinger VPS infrastructure.

**Category:** AI Agent / Voice Interface  
**Stack:** Google Gemini Live · FastAPI · Docker · Hostinger VPS  
**Built by:** boussecsouali@gmail.com

---

## License

MIT — see [LICENSE](LICENSE) for details.
