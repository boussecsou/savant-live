# SAVANT — Gemini CLI Context

SAVANT (Self-Aware Voice Agent for Network Tasks) is a personalized Speech-to-Speech Voice AI Agent hosted on a VPS. It enables DevOps tasks via natural language voice commands, diagnosticates VPS health, and remembers technical context across sessions.

## Project Overview

- **Core Mission:** Provide a voice-driven DevOps interface to manage VPS infrastructure (Docker, services, files) and remember decisions/context via a persistent memory layer.
- **Primary Technologies:**
    - **Backend:** FastAPI (Python) with WebSockets for real-time audio/data streaming.
    - **AI Engine:** Gemini 3.1 Flash Live (`google-genai`) for speech-to-speech interaction.
    - **VPS Executor:** Hermes v2 (privileged container) for autonomous shell execution on the host.
    - **State Management:** Redis (ephemeral session state) and SQLite (long-term memory).
    - **Frontend:** Vanilla JS PWA with Web Audio API (PCM 16-bit 16kHz mic, 24kHz playback).
    - **Infrastructure:** Docker Compose, Caddy (Reverse Proxy + TLS).

## Architecture Reference

- **Audio Flow:** PWA (WS) ↔ Backend ↔ Gemini 3.1 Flash Live.
- **Execution Flow:** Gemini (Tool Call) → Backend → Hermes v2 → VPS Host.
- **Memory Layer:** 
    - `savant.db` (SQLite): Long-term facts, sessions, and incident logs.
    - Redis: Real-time flags (`STOP_FLAG`, `PENDING_CONFIRMATION`).
- **Security:** Access code gate (SHA256), PIN confirmation for `critical` actions.

## Building and Running

### Local Development
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Docker (Production)
```bash
docker compose build && docker compose up -d
docker compose logs -f savant-backend
```

### Maintenance Scripts
- `./scripts/reset_memory.sh`: Clear SQLite memory and sessions.
- `./scripts/reset_hermes.sh`: Clear Hermes map and sessions.
- `./scripts/reset_all.sh`: Full first-run state reset.

## Development Conventions

- **Language:** English only for code, comments, and documentation.
- **VPS Actions:** **NEVER** use `subprocess` in the backend. All VPS interactions must go through `call_hermes`.
- **Gemini Interaction:**
    - Vocalize natural sentences; execute tools silently (`thinking_level="MEDIUM"`).
    - Use `[INTERNAL]` prefix for mid-session instructions to Gemini (never `FunctionResponse`).
    - Tool syntax in audio is a failure; use `_strip_tool_calls()` to truncate logs.
- **Security:**
    - Never log or commit `.env`, `*.db`, or API keys.
    - `write` and `critical` actions require vocal/PIN affirmation.
- **Code Style:** Surgical edits preferred. Maintain the single-file nature of `main.py` unless refactoring is explicitly requested.

## Key Files
- `main.py`: Core backend logic and Gemini Live bridge.
- `backend/hermes/client.py`: Hermes execution gateway.
- `backend/memory/database.py`: SQLite persistence layer.
- `ARCHITECTURE.md`: Technical deep-dive into state machines and gates.
- `CLAUDE.md`: Dev-specific commands and core rules.
- `MEMORY.md`: Project vision and hackathon use cases.
