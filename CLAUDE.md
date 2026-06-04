# CLAUDE.md
Guidelines for Claude Code when developing SAVANT (Speech-to-Speech Voice AI Agent on VPS).
**Hackathon IApreneur x Hostinger** — Deadline: June 4, 2026.

## Development Commands

```bash
# Local dev
pip install -r requirements.txt && uvicorn main:app --reload --port 8000

# Docker (rebuild after any backend edit)
docker compose build && docker compose up -d
docker compose logs -f savant-backend
```

```bash
# Health checks
curl http://localhost:8000/health
curl http://localhost:8000/health/redis
curl http://localhost:8000/health/sessions
curl http://localhost:8643/health   # Hermes gateway

# Resets
./scripts/reset_memory.sh           # Clear SQLite memory & sessions
./scripts/reset_hermes.sh           # Clear Hermes map/sessions/memories
sudo bash ./scripts/reset_all.sh    # Full first-run state (sudo for root-owned backups)
```

```bash
# Integration tests (inside container)
docker exec savant-backend python3 /app/test_gemini.py
docker exec savant-backend python3 /app/test_websocket.py

# Vision & Metrics (inside Hermes container)
docker exec hermes-v2 python3 /host/home/savant/scripts/vps_quick_vision.py
docker exec hermes-v2 python3 /host/home/savant/scripts/vps_metrics.py
```

```bash
# Memory DB inspection
docker exec savant-backend python3 -c "from backend.memory.database import get_recent_sessions; import json; print(json.dumps(get_recent_sessions(5), indent=2))"
docker exec savant-backend python3 -c "from backend.memory.database import _connect; c=_connect().__enter__(); c.execute(\"DELETE FROM memory WHERE key='action_code'\"); c.commit()"
```

## Tech Stack

| Component | Detail |
|---|---|
| Backend | Python FastAPI + WebSockets — `main.py` |
| AI Core | Gemini 3.1 Flash Live (`google-genai==2.4.0`) |
| Summarizer | `gemini-2.5-flash-lite` via REST |
| VPS Executor | Hermes v2 port 8643 (internal: 8642) |
| State | Redis (ephemeral) |
| Memory | SQLite `/app/memory/savant.db` (WAL) |
| Frontend | Caddy + Vanilla JS PWA (inline markdown renderer) |
| Network | `n8n-automation_n8n_network` |

## Folder Layout

```
app/
├── pwa/                    PWA static files
├── backend/
│   ├── hermes/client.py    SSE streaming to Hermes gateway
│   ├── memory/database.py  SQLite: sessions, memory, incidents, workflows
│   ├── tools/              Gemini tool declarations
│   ├── prompts.py          System-prompt blocks + pause/resume/trigger phrases
│   ├── skills.py           Markdown skill registry (/app/skills volume)
│   ├── workflows.py        Webhook registry (httpx triggers, SQLite-stored)
│   ├── notion.py           Notion REST fast-read lane
│   └── console.py          Console Live mode (/ws?mode=console)
├── scripts/                Resets, metrics_refresher.sh, cron files
└── hermes-entrypoint.sh
../memory/                  savant.db (bind-mounted, gitignored)
../logs/                    savant.log, sessions/, vps_map/
../skills/                  Skill markdown files (bind-mounted /app/skills)
hermes/vps_map/             QUICK_VISION.md (3min) + METRICS.txt (20s) — read-only in backend
```

See `ARCHITECTURE.md` for ports, state machine, gate logic.

## Core Rules

### 1. Gemini & Audio
- **NEVER** vocalize tool names or arguments. One natural sentence, then execute silently.
- `response_modalities=["AUDIO"]`, voice `Charon`, `thinking_level="MEDIUM"` (LOW leaks tool syntax).
- Mid-session instructions → `send_client_content` with `[INTERNAL]` prefix. **Never via `FunctionResponse`** (it vocalizes in AUDIO mode).

### 2. VPS Execution — two tools, different scopes
- **`run_command`**: direct VPS ops (~1–3s inline). All reads + writes go here. No `/host/` prefix — already in host namespace.
- **`execute_action`**: Hermes FIFO async (~10–30s). Only for: Notion reads/writes, VPS vision scripts, undo.
- **Never `subprocess`** from backend. No `sh -c`/`bash -c` stacking. System services via `nsenter -t 1 -m -u -n -i --`.

### 3. Confirmation Gate
- **`run_command` destructive ops**: Gemini must ask first, wait for `oui` in a new turn, then execute. Covered: `docker stop/restart/kill/rm`, `systemctl stop/restart/disable`, `rm/mv/cp` (overwrite), `userdel/passwd`, `DROP/TRUNCATE/DELETE`, `--force/-f/--remove/--purge`. Creating new files (`touch`/`mkdir`) does NOT require confirmation.
- **`execute_action` write/critical**: backend gate — `pending_confirm[fp]` set on first call, pass only after new vocal affirmation turn. `confirmed=true` without new turn → blocked.

### 4. show_data & Display
- Formats: `text|table|json|report|list|markdown`. PWA renders full markdown for all except `text`.
- Default after any action result: `format="report"` (sections + headers + exit code). `text` only for ≤2 lines.
- Auto-format in backend: `table` if tab-delimited, `markdown` if >5 lines, `text` otherwise.

### 5. Fast-Read Lanes
- **Notion:** Read queries → direct REST (`backend/notion.py`). Write → Hermes MCP.
- **Vision:** Read `QUICK_VISION.md` / `METRICS.txt` from logs volume. Fallback to Hermes if stale/missing.

### 6. Skills & Workflows
- **Skills:** Only index (name+desc) in system prompt. Full content loaded via `load_skill(name)` on demand.
- **Workflows:** httpx trigger (GET if no payload, POST with JSON). Auth keys never echoed back.

### 7. Memory
- Proactive `save_memory` on durable facts (snake_case keys) mid-session or on `_REMEMBER_TRIGGERS` match.
- On disconnect: `gemini-2.5-flash-lite` summarizes session (max 15 bullets).

### 8. Voice Behavior
- After completing a task: ONE confirmation line, then silent. Never ask "Tu as besoin d'autre chose ?" or "Je t'écoute" unprompted.
- Voice = 1–2 sentences max. All structured detail goes to `show_data` panel.

## WebSocket Contract

### PWA → Backend
| Frame | Meaning |
|---|---|
| Binary | PCM audio (16-bit mono 16kHz) |
| `{"type":"end_of_turn"}` | Mic-stop signal |
| `{"type":"action_code","value":"<sha256>"}` | Auth gate |
| `{"type":"stop_action"}` | Cancel running actions |
| `{"type":"text_command","text":"..."}` | Typed turn injection |

### Backend → PWA
| Frame | Meaning |
|---|---|
| Binary | PCM playback (24kHz) |
| `{"type":"transcript","role":"assistant\|user","text":"..."}` | Live log |
| `{"type":"turn_complete"}` | End of speech block |
| `{"type":"session_verified"}` | Auth unlock |
| `{"type":"hermes_running\|hermes_done",...}` | Execution slot status |
| `{"type":"hermes_step","text":"<token>"}` | SSE token stream to shimmer card |
| `{"type":"show_data","title":"...","content":"...","format":"text\|table\|json\|report\|list\|markdown"}` | Data panel |
| `{"type":"metrics_update","line":"..."}` | Silent metric state update |
| `{"type":"savant_paused","paused":bool}` | Pause state |
| `{"type":"interrupted"}` | Barge-in — clear audio buffers |

## Key Constraints
- **English only:** All code, comments, architecture strings, doc updates.
- **No subprocess:** VPS actions through Hermes only.
- **Hermes privileged:** Do not enforce generic user mappings.
