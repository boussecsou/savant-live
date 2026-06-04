# SAVANT — Architecture Reference
v2026-06-04 | Hackathon IApreneur x Hostinger — Deadline June 4 2026

---

## What is SAVANT?

SAVANT (Speech-to-Speech Voice AI Agent on VPS) is a hands-free DevOps voice agent that lets an owner manage a Linux VPS through natural conversation. The owner speaks; SAVANT understands, reasons, executes shell commands or complex multi-step Hermes instructions, verifies results, and narrates outcomes — all through voice and a real-time data panel in the browser.

No SSH. No terminal. Just conversation.

---

## High-Level System Map

```
Owner (microphone)
     │ PCM 16kHz audio
     ▼
PWA (Vanilla JS, Web Audio API, glass-morphism UI)
     │ WebSocket :8000 — binary PCM + JSON control frames
     ▼
Backend (FastAPI + asyncio)
     │
     ├─ google-genai SDK (bidirectional streaming)
     │       ▼
     │  Gemini 3.1 Flash Live
     │  response_modalities=["AUDIO"], voice=Charon
     │  thinking_level=MEDIUM, temperature=0.25
     │       │ tool calls
     │       ├── run_command ──────────────► Hermes Exec Server :8644  (~1-3s)
     │       └── execute_action ───────────► Hermes v2 LLM Gateway :8642 (~10-30s)
     │                                            │
     │                                            └─► VPS Host
     │                                                (nsenter / /host mount)
     │
     ├─ Redis     — stop_flag, paused, current_task, last_backup
     ├─ SQLite    — memory, sessions, incidents, action_log, workflows
     └─ Files     — METRICS.txt, QUICK_VISION.md (token-free fast lanes)
```

---

## Tech Stack

| Layer | Technology | Detail |
|---|---|---|
| Backend | Python 3.11 + FastAPI | `main.py` ~3,600 lines, async throughout |
| Voice LLM | Gemini 3.1 Flash Live | `google-genai==2.4.0`, bidirectional audio streaming |
| Summarizer | `gemini-2.5-flash-lite` | REST, end-of-session summary + fact extraction + incident enrichment |
| VPS Gateway (LLM) | Hermes v2 | OpenAI-compatible gateway :8642, streaming SSE |
| VPS Gateway (exec) | Hermes v2 exec server | :8644, raw shell execution, returns `{stdout, stderr, code, cwd, user}` |
| Ephemeral state | Redis | Flags only, no durable data |
| Persistent memory | SQLite WAL | `/app/memory/savant.db`, 5 tables |
| Frontend | Vanilla JS PWA | Web Audio API, inline markdown renderer, glass-morphism |
| Web server | Caddy | TLS termination, reverse proxy to :8000 |
| Network | `n8n-automation_n8n_network` | Shared VPS infra Docker network |

---

## Folder Layout

```
app/
├── main.py                       WebSocket core — session, Gemini, tools, reconciler, metrics
├── backend/
│   ├── prompts.py                System prompt blocks + phrase lists (_PAUSE_PHRASES, etc.)
│   ├── skills.py                 Markdown skill registry (index + on-demand loader)
│   ├── workflows.py              Webhook workflow CRUD + direct httpx trigger
│   ├── notion.py                 Notion REST fast-read lane (bypasses Hermes)
│   ├── console.py                Console Live mode (/ws?mode=console)
│   ├── hermes/
│   │   └── client.py             SSE streaming HTTP client to Hermes gateway
│   ├── memory/
│   │   └── database.py           SQLite: all persistent state (5 tables, WAL mode)
│   └── tools/                    Gemini tool declarations (JSON schemas for function calling)
├── pwa/                          Static PWA (index.html, JS, CSS)
├── scripts/
│   ├── reset_memory.sh           Clear SQLite memory + sessions
│   ├── reset_hermes.sh           Clear Hermes map/sessions/memories
│   ├── reset_all.sh              Full first-run state (needs sudo)
│   └── metrics_refresher.sh     Host cron entry — writes METRICS.txt every 20s
└── hermes-entrypoint.sh          Starts Hermes LLM gateway + exec server

../memory/                        savant.db (bind-mounted, gitignored, survives rebuilds)
../logs/                          savant.log, sessions/, vps_map/
../skills/                        Skill markdown files (bind-mounted /app/skills, read-write)
hermes/vps_map/                   QUICK_VISION.md (3min cron) + METRICS.txt (20s cron)
```

---

## Session Lifecycle

### Phase 1 — WebSocket Connect & Access Gate

```
PWA opens WebSocket to /ws
  → open_session() creates row in SQLite sessions table
  → All binary audio + end_of_turn frames are DROPPED until session_verified
  → Client sends: {"type": "action_code", "value": "<sha256 of access code>"}

Backend evaluates hash:
  New hash   → save to memory('action_code'), trigger onboarding via [INTERNAL] injection
  Known hash → session_verified.set(), proceed to Smart Briefing
  Invalid    → emit {"type": "access_denied"}, close WebSocket
```

### Phase 2 — Onboarding (first-run only)

`is_first_run()` returns `True` if any of `owner_name`, `preferred_language`, `timezone` missing from SQLite.

```
Server injects [INTERNAL] → Gemini asks Q1 (name)
  Owner answers → Gemini calls save_memory('owner_name', value)
  Validation:
    - owner_name: non-empty, not purely digits
    - preferred_language: alpha characters only
    - timezone: non-empty, not purely digits
  Fail → FunctionResponse {"result": "invalid"} → server injects [INTERNAL] to re-ask
  Pass → server injects [INTERNAL] → next question

PWA tracks progress: get_onboarding_status() → {collected: {...}, missing: [...]}
Resumable: if disconnected mid-onboarding, collected keys preserved, missing asked on reconnect
```

### Phase 3 — Smart Briefing (returning sessions)

After `session_verified`, backend assembles ONE natural briefing turn and injects it:

1. Load `get_recent_sessions(3)` — last 3 session summaries from SQLite
2. Load `get_unresolved_incidents()` — unresolved alerts with cause/fix
3. Check Quick Vision file freshness:
   - Fresh (< 15 min) → embed content directly in briefing → `show_data` to PWA
   - Stale / missing → enqueue `execute_action` to refresh → fallback message
4. Check `vps_state` in memory → if `stress`, inject TONE block (Gemini goes short + urgent)
5. All assembled as ONE `[INTERNAL]` user turn → Gemini greets + announces VPS status naturally

### Phase 4 — Active Session

Concurrent async tasks per WebSocket:
- `_audio_sender()` — drains `cmd_queue` (audio chunks + EOT) → Gemini input
- `_gemini_receiver()` — reads Gemini output (audio, transcripts, tool calls)
- `_reconciler()` — serialized injector, coalesces all backend→Gemini text
- `_hermes_worker()` — FIFO executor for execute_action queue
- `_metrics_loop()` — polls METRICS.txt every 20s, evaluates thresholds
- `_auto_pause_watcher()` — detects 180s silence → auto-pause

### Phase 5 — Session End

Triggered by: Orbe button, `stop_action` frame, or `_llm_summarize_transcript` on GoAway.

```
1. _llm_summarize_transcript()
     → flash-lite generates 15-bullet technical summary
     → close_session(id, summary) in SQLite

2. _llm_extract_facts()
     → flash-lite extracts durable {key: value} facts from transcript
     → save_memory() for each (namespaces: general, infra, preferences, projects, people)

3. Transcript saved to /app/logs/sessions/session-{sid}-{ts}.log
     → auto-gzip if > 256 KB
```

---

## VPS Execution — Two Tools

### `run_command` — Fast Direct Path (~1-3s)

**Used for:** all VPS reads and writes (file reads, docker ops, package install, config edits, service checks).

```python
run_command(command: str, purpose: str)
```

Execution path:
```
Gemini emits run_command(command, purpose)
  → _is_catastrophic() check — hard block on:
       mkfs, wipefs, dd of=/dev/, rm -rf /etc, reboot, shutdown,
       fork bombs, /sbin/init kill, etc.
  → POST to hermes-v2:8644 (exec server, no LLM)
  → Returns {stdout, stderr, code, cwd, user} in ~1-3s
  → show_data to PWA panel (format: report)
  → 1-2 line summary injected to Gemini via reconciler
  → Gemini narrates outcome in voice
```

Path conventions (already in host namespace via nsenter):
- Files: `/home/`, `/etc/`, `/var/`, `/opt/` — no prefix
- System services: `nsenter -t 1 -m -u -n -i -- systemctl status nginx`
- Docker: `docker ps`, `docker logs myapp`, `docker exec ...`

### `execute_action` — Hermes LLM Async Path (~10-30s)

**Used only for:** Notion reads/writes, VPS vision scripts, undo operations.

```python
execute_action(
    description: str,    # natural language label shown in PWA job card
    instruction: str,    # full instruction for Hermes LLM
    action_type: str,    # "read" | "write" | "critical" | "undo"
    confirmed: bool      # False on first emit, True after owner affirms
)
```

Execution path:
```
Gemini emits execute_action(...)
  → Fingerprint dedup: fp = sha1(f"{action_type}|{norm(instruction)}")[:12]
       Already in job_inflight or job_recent (12s TTL) → silently dropped
  → Write/critical + confirmed=False → confirmation gate (see below)
  → Enqueue on hermes_action_queue (FIFO, prevents R/W races)
  → PWA job card created: {"status": "queued", "description": ...}
  → [INTERNAL] "Je vais lancer..." injected

_hermes_worker() (one action at a time):
  → _run_hermes_bg() launched as child task
  → Load skill if action_type in (write, critical) → prepend backup procedure
  → Append last 3 action_log entries (adaptive context)
  → POST to hermes-v2:8642/v1/chat/completions (stream: true)
  → SSE chunks → on_chunk callback → hermes_step WS tokens → PWA shimmer card
  → Heartbeat: filler speech every ~5s (max 3), cancels on speech/completion
  → 502/503/504 → [ACTION RESULT — UNCERTAIN]

Post-execution:
  → Verify: check Hermes output ends with "VERIFICATION: VERIFIED" (strict token check)
  → save_action_log(status, verified, duration_s)
  → show_data report to PWA
  → [ACTION RESULT] injected → Gemini announces outcome

Post-action chain (write/critical only):
  1. Append to HERMES_CHANGELOG.md
  2. Upsert MONITORING_REGISTRY.json (watched services)
  3. Trigger VPS_FULL_VISION.md refresh
  4. Invalidate _blueprint_cache if infra keyword detected
```

Path conventions inside Hermes:
- All paths via `/host/` prefix: `/host/home/savant/`, `/host/etc/nginx/`
- Docker socket: `/var/run/docker.sock` (mounted directly)

### Verification Proof

All write/critical instructions include `[VERIFY — MANDATORY]`. Hermes must respond:
- `VERIFICATION: VERIFIED` — success
- `VERIFICATION: NOT_VERIFIED — <reason>` — failure

Backend strictly checks the first token after `:`. Self-report without actual command output → `NOT_VERIFIED`. Verified actions get `action_log.verified=1`.

---

## Confirmation Gate

### `run_command` Destructive Operations

Gemini MUST ask the owner exactly once, then STOP and wait for `oui` in a **new turn**.

Covered:
```
docker stop / restart / kill / rm / prune
systemctl stop / restart / disable
rm / mv / cp (when overwriting)
truncate
userdel / useradd / passwd
DROP / TRUNCATE / DELETE (SQL)
iptables -D / -F
any flag: --force / -f / --remove / --delete / --purge
```

NOT required: `touch` / `mkdir` on new paths, any read-only command.

### `execute_action` Write/Critical — Two-Prong Gate

```
First call (confirmed=False):
  → inject confirmation prompt to Gemini
  → pending_confirm[fp] = user_turn_count
  → pending_action_detail[fp] = {instruction, description, action_label}
  → STOP — wait for owner

Owner responds in NEW turn:
  Affirm (oui / yes / ok / go / ouais / absolument):
    → last_affirm_turn updated

    Prong 1 — Gemini re-emits execute_action(confirmed=True):
      → backend checks: last_affirm_turn > pending_confirm[fp] → fires

    Prong 2 — Backend fallback (Gemini fails to re-emit):
      → on next turn: if affirm detected and pending_action_detail[fp] exists → fires

  Negate (non / annule / stop / cancel / pas maintenant):
    → pending_confirm[fp] cleared
    → action dropped, Gemini notified

  confirmed=True without new affirm turn → BLOCKED + re-forced (integrity check)
```

### Undo

```
execute_action(action_type="undo")
  → Redis: GET savant:last_backup → path in /home/savant/backups/hermes/ (7-day TTL)
  → Hermes restores backup
  → Standard confirmation gate applies (critical type)
```

---

## Reconciler Gate

All backend→Gemini text routes through `inject_queue` → `_reconciler()`.

```
Multiple payloads queued simultaneously (e.g., action result + metrics alert)
  ↓
_reconciler wakes on inject_queue.get()
  → Drain all remaining items (non-blocking)
  → Wait for gemini_idle.wait() (Gemini not speaking — safe to inject)
  → Coalesce all payloads into ONE user turn:
      [payload1]

      [payload2]

      [RECONCILER_GUARD] ← forbids Gemini from re-calling already running/done actions
  → send_client_content() once
  → task_done() for all drained items
```

The `[RECONCILER_GUARD]` trailer prevents the repetition bug where Gemini re-emits a tool call already in the FIFO queue.

---

## Memory System

### SQLite Schema (`/app/memory/savant.db`, WAL mode)

**`memory`** — Key-value facts
```sql
key TEXT PRIMARY KEY, value TEXT, updated_at TEXT, namespace TEXT DEFAULT 'general'
```
Namespaces: `general`, `infra`, `preferences`, `projects`, `people`

**`sessions`** — Session transcripts
```sql
id TEXT PRIMARY KEY, started_at TEXT, ended_at TEXT, summary TEXT
```

**`incidents`** — VPS alerts
```sql
id INTEGER PRIMARY KEY, date TEXT, type TEXT, action_taken TEXT,
resolved INTEGER DEFAULT 0, cause TEXT, suggested_fix TEXT
```

**`action_log`** — Hermes action history (adaptive context)
```sql
action_id TEXT, action_type TEXT, description TEXT, status TEXT,
duration_s REAL, verified INTEGER, created_at TEXT
```

**`workflows`** — Webhook registry
```sql
name TEXT PRIMARY KEY, url TEXT, description TEXT,
auth_header TEXT, auth_key TEXT, created_at TEXT
```

### Key Database Functions

| Function | Purpose |
|---|---|
| `save_memory(key, value, namespace)` | Upsert fact (snake_case keys, no embeddings) |
| `recall(query, namespace)` | Ranked keyword search across memory table |
| `load_memory()` | Format 40 facts + 3 session summaries for system prompt |
| `is_first_run()` | True if owner_name / preferred_language / timezone missing |
| `get_onboarding_status()` | `{collected: {...}, missing: [...]}` — resumable onboarding |
| `open_session()` / `close_session()` | Session row lifecycle |
| `save_session_summary()` | Insert completed session |
| `save_incident()` / `get_unresolved_incidents()` / `resolve_incidents()` | Incident lifecycle |
| `save_action_log()` / `get_recent_action_context()` | Last 3 actions → prepended to Hermes context |
| `save_workflow()` / `list_workflows()` / `run_workflow()` / `delete_workflow()` | Webhook CRUD |

### Proactive Memory Capture

- Mid-session: Gemini calls `save_memory` whenever it learns durable facts (hostname, stack, key services, preferences)
- `_REMEMBER_TRIGGERS` phrase match: safety net if owner says "retiens / souviens-toi / remember / n'oublie pas"
- End-of-session: `_llm_extract_facts()` via flash-lite extracts facts automatically from transcript

---

## Fast-Read Lanes (Token-Free)

All file-based lanes avoid LLM calls entirely:

| File | Writer | Frequency | Max Age Used | Purpose |
|---|---|---|---|---|
| `METRICS.txt` | Host cron → `vps_metrics.py` inside hermes-v2 | ~20s | 60s | CPU/RAM/DISK/containers — parsed by `_metrics_loop` |
| `QUICK_VISION.md` | Host cron → `vps_quick_vision.py` inside hermes-v2 | ~3min | 15min | VPS overview at Smart Briefing |
| `VPS_FULL_VISION.md` | Post-action chain | On-demand | Not cached | Deep inspection (not loaded at login) |
| `ALERTS.md` | `render_alerts_md()` | On incident change | N/A | Unresolved incidents with cause/fix |
| `HERMES_CHANGELOG.md` | Post-action chain | After write/critical | N/A | Audit log of all VPS changes |
| `MONITORING_REGISTRY.json` | Post-action chain | After install/enable | N/A | Services to watch (docker/systemctl/process) |

**METRICS.txt format:**
```
CPU:12%|LOAD:0.88|CORES:2|RAM:3.2/7.8GB(42%)|DISK:32/96GB(34%)|CONT:7r/0s|UPTIME:24d1h
```

### Notion Fast Lane

`action_type=="read"` + "notion" in instruction → `backend/notion.py` direct REST API (~1-3s).  
Write → always via Hermes MCP (~1-2min).  
Any error → `None` returned → fallback to Hermes automatically.

### Blueprint Cache

`_blueprint_cache` (global) stores the last Quick Vision result for 6 hours.  
Invalidated on write/critical actions containing infra-related keywords.  
Always refreshed on GoAway reconnect.

---

## Monitoring & Alerting

### Metrics Loop (active session)

```
_metrics_loop() starts after session_verified
  → Poll METRICS.txt every 20s (or fallback execute_action if stale > 60s)
  → Parse: CPU%, RAM%, DISK%, container count running/stopped
  → Evaluate thresholds:
      80% → warning: visual alert only (no voice interruption)
      90% → critical:
          1. Visual alert to PWA
          2. Terse vocal interruption via reconciler
          3. flash-lite enrichment: likely cause + suggested fix
          4. save_incident() in SQLite
          5. render_alerts_md() → update ALERTS.md
          6. Debounce: same metric key not re-alerted within 5 min
      Container state change → always critical regardless of threshold
  → send {"type": "metrics_update", "line": "..."} to PWA every cycle (silent)
```

### Background Monitor (no active session)

```
_background_monitor() runs when _active_sessions == 0
  → Poll metrics every 180s
  → Same threshold evaluation
  → On critical: enrich + persist incident (no vocal — no session)
  → Incidents announced in next Smart Briefing
```

### Security Loop

```
Every 6 hours (host cron):
  vps_security_scan.py inside hermes-v2
  Verdict: 🟢 clean / 🟡 warning / 🔴 critical
  If 🔴 → save_incident() → triggers Smart Briefing announcement on next login
```

### Adaptive Tone

`vps_state=stress` saved to memory on any critical alert.  
System prompt dynamically injects TONE block: Gemini responds in short, urgent, focused sentences until `vps_state` cleared.

---

## Skills System

Markdown procedure library at `/app/skills/` (bind-mounted, read-write by backend).

**File format:**
```markdown
---
name: backup-scope
description: Identify and back up files before a risky operation
---

[Step-by-step markdown content]
```

**How it works:**
- Only the index (name + description) is injected into the system prompt (cheap)
- `load_skill(name)` loads full body on demand before Gemini tackles a complex procedure
- `save_skill(name, description, content)` — Gemini writes new skills after solving hard problems
- `_slugify(name)` sanitizes filenames (kebab-case, no path traversal)

---

## Workflows System

Direct webhook triggers — bypass Hermes entirely.

**Registration:** `save_workflow(name, url, description, auth_header, auth_key)` → SQLite  
**Trigger:** `run_workflow(name, payload)` → `httpx.post(url, json=payload, headers={auth_header: auth_key})`  
**Response:** JSON → clipped at 4,000 chars → `show_data` to PWA panel  
**Timeout:** 30s  
**Security:** `auth_key` stored in SQLite but **never returned** in `list_workflows()` output

---

## Console Live (`/ws?mode=console`)

Isolated Gemini Live session for typed VPS interaction — independent from main voice session.

```
PWA connects to /ws?mode=console
  → handle_console_session() starts separate Gemini Live
  → Lean system prompt: no memory tools, no execute_action, exec-only

Typed command:
  → Gemini reasons → emits shell command
  → _exec_vps(cmd) → POST to hermes-v2:8644
  → Returns {stdout, stderr, code, cwd, user}
  → __SAVANT_STATE__$PWD|$USER sentinel preserves cwd + user across calls
  → Gemini narrates output, suggests next step

Destructive patterns → console_confirm gate
_CV_INTERACTIVE regex → blocks vim, top, nano, htop (no interactive TTY support)
```

---

## Key Design Patterns

### Idempotence (Dedup Fingerprint)

```python
fp = sha1(f"{action_type}|{normalized_instruction}").hexdigest()[:12]
job_inflight[fp]  # action currently queued or running → drop re-emit
job_recent[fp]    # action completed, TTL 12s → drop duplicate
```

Normalization strips extra whitespace and lowercases. Same instruction re-emitted by Gemini within 12s → silently dropped.

### Phantom Action Safety Net

If Gemini says "je regarde..." / "je lance..." (action intent phrase detected via `_ACTION_INTENT_PHRASES`) but never emits a tool call within the turn:
→ backend injects `[INTERNAL]` nudge: "You mentioned you would do X but didn't emit the call"
→ Gemini re-emits the tool call

### GoAway Recovery (Gemini Network Disconnect)

```
Receiver detects go_away message from Gemini
  → Break from session loop
  → Create new Gemini Live session (same session_handle if available)
  → session_ref[0] updated in place → in-flight Hermes actions continue uninterrupted
  → Results injected into NEW Gemini session via reconciler
  → Blueprint cache refreshed on reconnect
```

### Auto-Pause (180s silence)

```
_auto_pause_watcher() polls every 10s
  → If last_audio_time + 180s < now:
      Set savant:paused = "1" in Redis
      Emit {"type": "savant_paused", "paused": true} to PWA (amber overlay)
      SAVANT stops responding to audio
  → Owner clicks resume or speaks resume phrase → cleared
```

### Hands-Free Pause/Resume

`_PAUSE_PHRASES` ("attends", "un instant", "wait", "je reviens", "pause") → Redis flag + drop audio + amber overlay.  
`_RESUME_PHRASES` ("je suis là", "I'm back", "je suis de retour", "on continue") → clear flag, resume.

### Session Compression

Long sessions → context window compression (sliding 16k tokens) keeps Gemini Live alive without disconnecting. Transparent to owner.

### Stop & Cancel

```
Partial stop (Esc key):
  → Cancel all active_hermes_tasks (asyncio cancellation)
  → Inject [INTERNAL] ACK

Full kill (Orbe button / stop_action frame):
  → stop_flag set
  → Flush audio playback buffers
  → Enter muted state
  → WS close(1000)
  → _llm_summarize_transcript() runs
```

---

## System Prompt Architecture

System prompt assembled dynamically at session start by `_make_live_config(handle)`:

```
_SYSTEM_BASE          Core identity: silent tool use, warm tone, proactivity rules,
                      confirmation rule, memory behavior, command discipline
_CODE_BLOCK           Access code verification instructions
_ONBOARDING_*         (first-run only) Three-step onboarding flow
_HERMES_BLOCK         VPS execution philosophy: run_command vs execute_action,
                      path conventions, autonomous multi-step patterns
_NOTION_BLOCK         (if NOTION_API_KEY set) Notion search/read/write guide
_WORKFLOWS_BLOCK      Webhook workflow registry and trigger instructions
load_memory()         40 recent facts + 3 session summaries from SQLite
skills_index()        Skill names + descriptions (full body loaded on demand)
list_workflows()      Registered webhooks (names + descriptions only)
TONE block            (if vps_state=stress) Short, urgent, focused mode
```

**Gemini config:** `temperature=0.25`, `silence_duration_ms=800`  
**Voice:** `Charon`  
**thinking_level:** `MEDIUM` — LOW leaks tool syntax into audio stream, HIGH is too slow  
**Hesitation markers** (euh…, mmm…) → wait state + micro-prompt nudge  
**Tool syntax spill** → `_strip_tool_calls()` truncates before transcript log ingestion

---

## WebSocket Contract

### PWA → Backend

| Frame | Meaning |
|---|---|
| Binary | PCM audio (16-bit mono 16kHz) |
| `{"type":"end_of_turn"}` | Mic-stop signal (VAD released) |
| `{"type":"action_code","value":"<sha256>"}` | Access code authentication |
| `{"type":"stop_action"}` | Cancel running actions + full kill |
| `{"type":"text_command","text":"..."}` | Typed turn injection (Console or text input) |

### Backend → PWA

| Frame | Meaning |
|---|---|
| Binary | PCM playback audio (24kHz) |
| `{"type":"transcript","role":"assistant\|user","text":"..."}` | Live turn log |
| `{"type":"turn_complete"}` | End of speech block |
| `{"type":"session_verified"}` | Identity confirmed — unlock UI |
| `{"type":"hermes_running","action_id":"...","description":"..."}` | Action started |
| `{"type":"hermes_done","action_id":"...","success":bool,"output":"..."}` | Action completed |
| `{"type":"hermes_step","action_id":"...","text":"<token>"}` | SSE token stream (shimmer card) |
| `{"type":"show_data","title":"...","content":"...","format":"..."}` | Data panel payload |
| `{"type":"metrics_update","line":"..."}` | Silent metric state update |
| `{"type":"alert","level":"critical\|warning","message":"...","key":"..."}` | Threshold alert |
| `{"type":"job_update","jobs":[{"id":"...","description":"...","status":"queued\|running\|done"}]}` | FIFO queue state |
| `{"type":"savant_paused","paused":bool}` | Pause state change |
| `{"type":"interrupted"}` | Barge-in detected — clear audio buffers |
| `{"type":"access_denied"}` | Invalid action code |

### show_data Formats

| Format | Rendering |
|---|---|
| `text` | Plain text, ≤ 2 lines, no markdown |
| `table` | Tab-delimited → HTML table |
| `json` | Syntax-highlighted JSON block |
| `report` | Sections + headers + exit code (default after action results) |
| `list` | Bullet list |
| `markdown` | Full markdown (headers, tables, code blocks, bold) |

---

## PWA Interface

Glass-morphism WWDC25-style UI, 100% Vanilla JS, no framework.

```
┌─ Header (52px) ─────────────────────────────────────────────────┐
│  SAVANT brand  │  state pill: idle / listening / thinking /     │
│                │  speaking  │  online status indicator          │
├─ Metrics strip (64px) ──────────────────────────────────────────┤
│  CPU sparkline  RAM sparkline  DISK bar  containers count        │
│  Color: green < 80% / amber < 90% / red ≥ 90%                  │
├─ DATA panel (65vh) ─────────────────────────────────────────────┤
│  Tabs: [Data] [Transcript] [Console]                            │
│                                                                  │
│  Data tab: cards — title + content, copy button, collapse        │
│    Hermes actions → shimmer card while streaming, then result   │
│  Transcript tab: role-colored turn log (user / assistant)       │
│  Console tab: typed input → /ws?mode=console                    │
│                                                                  │
├─ Input Orb (20vh) ──────────────────────────────────────────────┤
│  VAD waveform  │  state ring indicator  │  pause / stop buttons │
└─────────────────────────────────────────────────────────────────┘
```

---

## Docker & Infrastructure

### docker-compose.yml Services

**savant-backend**
- Build: `./Dockerfile` (Python 3.11, uvicorn)
- Port: 8000 → Caddy reverse proxy
- Env: `.env` (GEMINI_API_KEY, REDIS_HOST, HERMES_URL, NOTION_API_KEY, etc.)
- Volumes:
  - `../memory:/app/memory` — persistent SQLite DB (survives rebuilds)
  - `../logs:/app/logs` — runtime logs, sessions, vps_map
  - `./pwa:/app/pwa:ro` — static PWA files
  - `./hermes/vps_map:/app/vps_map:ro` — cron-written METRICS.txt + QUICK_VISION.md
  - `../skills:/app/skills` — skill markdown files (read-write)
- Network: `n8n_network` (external)

**hermes-v2**
- `privileged: true` — full Linux capabilities
- `pid: host` — host PID namespace (nsenter for host commands)
- `entrypoint: /hermes-entrypoint.sh` — starts LLM gateway (:8642) + exec server (:8644)
- Volumes:
  - `/:/host` — full VPS filesystem at /host/
  - `./hermes:/opt/data` — persistent Hermes state, vps_map, backups
  - `/var/run/docker.sock` — Docker API access
- Port: 8643→8642 (external:internal gateway)

**Redis** — internal only, ephemeral  
**Caddy** — TLS termination, `/` → `:8000`

### Survival on Rebuild

| Data | Location | Survives rebuild? |
|---|---|---|
| Memory facts | `../memory/savant.db` | ✅ bind-mount |
| Session summaries | `../memory/savant.db` | ✅ bind-mount |
| Logs + transcripts | `../logs/` | ✅ bind-mount |
| Skills | `../skills/` | ✅ bind-mount |
| Hermes state | `./hermes/` | ✅ bind-mount |
| Redis data | Container memory | ❌ lost on restart |
| Session WebSocket state | Backend memory | ❌ lost on restart |

---

## Control Endpoints

Authentication: `Authorization: Bearer <HERMES_WEBHOOK_SECRET>`

| Endpoint | Method | Action |
|---|---|---|
| `/control/stop` | POST | Set `savant:stop_flag` in Redis (60s TTL) |
| `/control/pause` | POST | Set `savant:paused` in Redis |
| `/control/resume` | POST | Clear `savant:paused` from Redis |
| `/control/status` | GET | `{running: bool, task: str, paused: bool}` |
| `/health` | GET | FastAPI liveness |
| `/health/redis` | GET | Redis connectivity check |
| `/health/sessions` | GET | Recent sessions from SQLite |
| `/health/prompt` | GET | Exact system prompt (byte-for-byte debug) |

---

## Redis State

All keys are ephemeral (lost on Redis restart):

| Key | Value | TTL | Purpose |
|---|---|---|---|
| `savant:stop_flag` | `"1"` | 60s | Cancel all active actions |
| `savant:paused` | `"1"` | None | Pause audio processing |
| `savant:current_task` | task description | 300s | For /control/status |
| `savant:last_backup` | file path | 86400s (7 days) | Undo target |

---

## Logging & Observability

**Logger:** `"savant"` (Python logging)  
**File:** `/app/logs/savant.log`  
**Rotation:** 10 MB per file, 5 backups, gzip compression  
**Session transcripts:** `/app/logs/sessions/session-{sid}-{ts}.log` (gzip if > 256 KB)

| Level | When |
|---|---|
| INFO | Session open/close, verification, action start/end, cache hits |
| WARNING | Backup failures, reconciler timeouts, missing memory keys |
| ERROR | Gemini client not initialized, DB init failure, Hermes errors |

---

## Environment Configuration

```bash
# Required
GEMINI_API_KEY=<token>
HERMES_WEBHOOK_SECRET=<shared secret>

# Infrastructure
HERMES_URL=http://hermes-v2:8642
CONSOLE_EXEC_BASE=http://hermes-v2:8644
REDIS_HOST=<redis container name>
REDIS_PORT=6379
REDIS_PASSWORD=<optional>

# Optional features
NOTION_API_KEY=<token>           # enables Notion fast-read lane

# Paths (defaults shown)
LOG_DIR=/app/logs
DB_PATH=/app/memory/savant.db
SKILLS_DIR=/app/skills
```

---

## Core Constraints

| Rule | Reason |
|---|---|
| **English only** in code, comments, architecture strings | Team consistency |
| **No subprocess** from backend | VPS actions only through Hermes (run_command → exec server, execute_action → LLM gateway) |
| **NEVER vocalize** tool names, command text, or arguments | Gemini AUDIO mode leaks FunctionResponse content to audio stream |
| **`thinking_level="MEDIUM"`** | LOW leaks tool syntax into audio; HIGH adds too much latency |
| **`[INTERNAL]` prefix** on all backend→Gemini injections | Prevents content from being spoken |
| **`FunctionResponse`** must not carry instructions | Vocalizes in AUDIO mode — use `send_client_content` instead |
| **Hermes privileged** | Do not enforce generic Linux user mappings |
| **Auth keys never echoed** | Workflow `auth_key` stored but never returned in listings |
| **Confirmation before destructive ops** | No write/critical without explicit `oui` in a new turn |
| **Backup before write/critical** | Enables undo via Redis `last_backup` pointer |
