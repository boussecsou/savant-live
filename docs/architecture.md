# SAVANT — Architecture

This document describes how SAVANT works end-to-end: what each component does, how data flows between them, and the design decisions that make it work reliably.

---

## System Overview

```
Owner (microphone)
     │ PCM 16kHz audio
     ▼
PWA  (Vanilla JS · Web Audio API · Glass-morphism UI)
     │ WebSocket :8000 — binary PCM + JSON control frames
     ▼
Backend  (FastAPI · asyncio · Python 3.11)
     │
     ├── google-genai SDK (bidirectional streaming)
     │        ▼
     │   Gemini 3.1 Flash Live
     │   voice: Charon · AUDIO-only mode · thinking_level=MEDIUM
     │        │ tool calls
     │        ├── run_command ──────────────► Hermes Exec Server :8644  (~1-3s)
     │        └── execute_action ───────────► Hermes v2 LLM Gateway :8642  (~10-30s)
     │                                             │
     │                                             └─► VPS Host
     │
     ├── Redis     — ephemeral flags (paused, stop_flag, current_task, last_backup)
     ├── SQLite    — memory, sessions, incidents, action_log, workflows
     └── Files     — METRICS.txt, QUICK_VISION.md (token-free fast lanes)
```

---

## Components

### Backend (`main.py`)

The core FastAPI server manages every active voice session. For each WebSocket connection, it runs several concurrent async tasks:

- **`_audio_sender`** — reads audio chunks and end-of-turn signals from a queue, sends them to Gemini
- **`_gemini_receiver`** — reads Gemini's output stream: audio playback, transcript events, and tool calls
- **`_reconciler`** — serialized injector that coalesces all backend→Gemini messages into single turns, waits for Gemini to finish speaking before injecting
- **`_hermes_worker`** — FIFO executor that processes VPS actions one at a time, preventing read/write races
- **`_metrics_loop`** — polls the METRICS.txt file every 20 seconds, evaluates thresholds, emits alerts
- **`_auto_pause_watcher`** — detects 180s of silence and auto-pauses the session

### Gemini Live

SAVANT uses Gemini 3.1 Flash Live in **AUDIO-only mode** — the model speaks directly, with no text-to-speech layer in between. This means:
- Tool names and command text must never be vocalized (they would be spoken literally)
- Backend→Gemini injections use an `[INTERNAL]` prefix to prevent content from being spoken
- `thinking_level=MEDIUM` is required — LOW causes tool syntax to leak into audio

### VPS Execution — Two Tools

SAVANT separates VPS access into two tools with different scopes:

**`run_command`** — fast direct execution (~1-3s)
- Routes to Hermes exec server at `:8644` (no LLM, pure shell execution)
- Returns `{stdout, stderr, exit_code, cwd, user}`
- Used for: everything — file reads, docker ops, package installs, service checks
- The exec server runs in a privileged container with host PID namespace, so `nsenter` gives full host access

**`execute_action`** — Hermes LLM async (~10-30s)
- Routes to Hermes v2 LLM gateway at `:8642` (OpenAI-compatible, streaming SSE)
- Used only for: Notion reads/writes, VPS vision scripts, undo operations
- Enqueued on a FIFO queue — one action at a time, no concurrent writes
- Results stream token-by-token to the PWA data panel (shimmer card effect)

### Hermes v2

A privileged Docker container that provides two services:
- **LLM Gateway (:8642)** — OpenAI-compatible API endpoint, Hermes acts as an agent with tool use to execute complex multi-step tasks
- **Exec Server (:8644)** — raw shell execution server, returns stdout/stderr/exit code

Hermes has full host access:
- `privileged: true` and `pid: host` — can use `nsenter` to enter host namespaces
- `/:/host` volume mount — full VPS filesystem at `/host/`
- `/var/run/docker.sock` — Docker API access

### Memory (`backend/memory/database.py`)

SQLite database at `/app/memory/savant.db` (WAL mode, persists across container rebuilds):

| Table | Purpose |
|---|---|
| `memory` | Key-value facts with namespaces (general, infra, preferences, projects, people) |
| `sessions` | Session transcript summaries |
| `incidents` | VPS alerts with AI-enriched cause and suggested fix |
| `action_log` | History of Hermes actions, prepended to context for adaptive behavior |
| `workflows` | Registered webhook URLs and auth credentials |

Memory has no embeddings — facts are retrieved by ranked keyword search. Simple, fast, zero dependencies.

### Fast-Read Lanes (Token-Free)

Several files are written by host cron jobs and read directly by the backend — no LLM tokens consumed:

| File | Written by | Read every | Purpose |
|---|---|---|---|
| `METRICS.txt` | `vps_metrics.py` (cron ~20s) | 20s | CPU/RAM/DISK/containers |
| `QUICK_VISION.md` | `vps_quick_vision.py` (cron ~3min) | Login | VPS overview for Smart Briefing |
| `ALERTS.md` | Backend on incident | On change | Unresolved incidents with cause/fix |
| `HERMES_CHANGELOG.md` | Backend post-action | After write/critical | Audit log |

METRICS.txt format: `CPU:12%|LOAD:0.88|CORES:2|RAM:3.2/7.8GB(42%)|DISK:32/96GB(34%)|CONT:7r/0s|UPTIME:24d1h`

---

## Session Lifecycle

### 1. Access Gate
Every new connection is locked until the owner sends their access code (SHA-256 hash). New codes trigger onboarding; known codes unlock the session immediately.

### 2. Onboarding (first run only)
Server-driven: the backend sends `[INTERNAL]` prompts to guide Gemini through collecting `owner_name`, `preferred_language`, and `timezone`. Gemini never auto-advances between questions.

### 3. Smart Briefing
After verification, the backend assembles one natural briefing: recent session summaries, unresolved incidents, and the current VPS Quick Vision — all injected as a single turn.

### 4. Active Session
Owner speaks → VAD detects end of turn → audio sent to Gemini → Gemini reasons + calls tools → backend executes + returns results → Gemini narrates outcome.

### 5. Session End
`gemini-2.5-flash-lite` generates a 15-bullet technical summary and extracts durable facts for the memory table. Transcripts are saved and gzip-compressed.

---

## Key Design Patterns

### Reconciler Gate
All backend→Gemini text goes through a reconciler that waits for Gemini to finish speaking before injecting. Multiple queued messages are coalesced into a single turn. A `[RECONCILER_GUARD]` trailer prevents Gemini from re-calling actions already in progress.

### Idempotence
Each action is fingerprinted (`sha1(action_type + normalized_instruction)[:12]`). Re-emitted actions within 12 seconds are silently dropped — no duplicate executions.

### Verification Proof
All write/critical Hermes instructions include `[VERIFY — MANDATORY]`. Hermes must end its response with `VERIFICATION: VERIFIED` or `VERIFICATION: NOT_VERIFIED — <reason>`. The backend strictly checks this token.

### GoAway Recovery
When Gemini disconnects (network GoAway), the backend reconnects and creates a new session with the same handle. In-flight Hermes actions continue uninterrupted — results are injected into the new session.

---

## Data Flow: Voice Command to VPS

```
Owner: "Restart the nginx container"
  ↓
Gemini reasons: destructive op → must ask first
  ↓
Gemini says: "I'm going to restart nginx. Ready?"
  ↓
Owner: "oui"
  ↓
Gemini emits: run_command("docker restart nginx", "restart nginx container")
  ↓
Backend: catastrophic check → passes
  ↓
Backend: POST to hermes-v2:8644 → {stdout: "nginx", exit_code: 0}
  ↓
Backend: show_data report → PWA panel
  ↓
Reconciler: inject result → Gemini narrates: "nginx restarted successfully"
```
