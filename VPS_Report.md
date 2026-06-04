# SAVANT — VPS Context for Claude Code
**Last updated: 20 May 2026 — srv1101276.hstgr.cloud**
> Read this FIRST before touching any file. This is ground truth.

---

## 1. Server Identity

| Field | Value |
|---|---|
| Hostname | `srv1101276.hstgr.cloud` |
| OS | Ubuntu 24.04.4 LTS — kernel `6.8.0-111-generic` |
| Virtualization | QEMU (Hostinger VPS) |
| IPv4 public | `72.61.101.44` |
| IPv6 public | `2a02:4780:28:dd3d::1` |
| Domain | `ali-n8n.com` (Cloudflare DNS, proxy OFF — DNS only) |
| SAVANT subdomain | `savant.ali-n8n.com` → A record → `72.61.101.44` |

⚠️ **System restart required** (kernel update pending).
⚠️ **10 apt updates available** — apply with `apt upgrade` before heavy dev work.

---

## 2. Hardware Resources (current state — 20 May 2026)

| Resource | Total | Used | Available | Status |
|---|---|---|---|---|
| CPU | 2 vCPU | load 0.04 | — | OK — idle |
| RAM | ~8 GB | **52% (~4.2 GB)** | ~3.8 GB | Watch — up from 26% before OpenClaw |
| Swap | 2 GB | 0% | — | OK |
| Disk | 100 GB | 24% (~24 GB) | ~76 GB | OK |

**RAM went from 26% → 52%** between May 18 and May 20. Likely caused by OpenClaw container being added. Monitor this.

---

## 3. Linux Users

| User | Role | Home | Sudoer |
|---|---|---|---|
| `root` | System admin | `/root` | yes |
| `adlin` | Dev user (you SSH as this) | `/home/adlin` | yes |
| `savant` | Runtime service user | `/home/savant` | NO |
| `ubuntu` | Legacy user (Hostinger default) | `/home/ubuntu` | yes |
| `alice_b` | Unknown — not SAVANT-related | `/home/alice_b` | unknown |
| `bob_b` | Unknown — not SAVANT-related | `/home/bob_b` | unknown |

**Rule**: code lives under `adlin`, runtime data lives under `savant`. Never run SAVANT backend as root.

---

## 4. Filesystem Layout

### `/root/` — n8n-automation stack (DO NOT TOUCH)
```
/root/
├── n8n-automation/          ← main n8n stack (docker-compose.yml, .env, Caddyfile)
│   ├── docker-compose.yml   ← EXISTING stack — add SAVANT here, don't replace
│   ├── docker-compose.yml.bak
│   ├── .env                 ← secrets for n8n/Evolution/Redis (DO NOT OVERWRITE)
│   ├── Caddyfile            ← routing config (savant.ali-n8n.com bloc already added)
│   ├── caddy_config/        ← Caddy persistent config
│   ├── caddy_data/          ← Let's Encrypt certs
│   ├── n8n_data/            ← n8n workflows data
│   ├── evolution_instances/ ← Evolution API WhatsApp sessions
│   ├── evolution_db/        ← Evolution API DB
│   └── redis_data/          ← Redis persistence (owner: savant:root — unusual, note it)
├── backups/                 ← n8n tar.gz backups (automated, ~254MB each)
│   └── n8n_backup_*.tar.gz  ← daily backups present (May 17–20)
└── logs/
    └── n8n-update.log
```

### `/home/savant/` — SAVANT project root
```
/home/savant/
├── app/           ← Python backend code (owner: adlin:adlin) ← YOUR WORK HERE
│   ├── main.py    ← FastAPI + WebSocket entrypoint
│   ├── pwa/       ← PWA static files (served by Caddy)
│   ├── docker-compose.yml  ← SAVANT containers (savant-backend + openclaw)
│   ├── .env       ← SAVANT secrets (GEMINI_API_KEY, OPENROUTER_API_KEY, tokens...)
│   ├── .env.example
│   ├── requirements.txt
│   ├── CLAUDE.md  ← Claude Code project instructions (READ THIS TOO)
│   └── MEMORY.md  ← Project memory/decisions log (READ THIS TOO)
├── memory/        ← SQLite memory.db (owner: savant:savant) — runtime only
├── logs/          ← SAVANT runtime logs (owner: savant:savant)
├── backups/       ← SQLite backups (owner: savant:savant)
└── openclaw/      ← OpenClaw data volume (owner: ubuntu — CHECK THIS)
```

⚠️ `/home/savant/openclaw/` owner is `ubuntu` — should be `node` (UID 1000) for the container. Verify before relying on volume persistence.

---

## 5. Docker Stack — Full Picture

### Network
All containers share one network: **`n8n-automation_n8n_network`** (bridge, `172.18.0.0/16`).

SAVANT containers MUST join this network via `external: true` in docker-compose.yml.

```yaml
networks:
  n8n-automation_n8n_network:
    external: true
```

### Existing containers (n8n stack — DO NOT MODIFY)

| Container name | Image | Internal port | Role |
|---|---|---|---|
| `caddy-proxy` | custom Caddy | 80, 443, 2019 | Reverse proxy + TLS |
| `n8n-container` | `n8nio/n8n:stable` | 5678 | Workflow orchestration |
| `redis-container` | Redis official | 6379 | Session state |
| `evolution-api` | `evoapicloud/evolution-api:v2.3.7` | 8080 | WhatsApp API (unrelated to SAVANT) |
| `postgres` | PostgreSQL | 5432 | Evolution API DB (unrelated to SAVANT) |

### SAVANT containers (in `/home/savant/app/docker-compose.yml`)

| Container name | Image | Internal port | Role |
|---|---|---|---|
| `savant-backend` | custom Python | 8000 | FastAPI + WebSocket bridge |
| `openclaw` | `ghcr.io/openclaw/openclaw:latest` | 18789 | Agent framework (Claude via OpenRouter) |

Both must be on `n8n-automation_n8n_network`. Neither should be exposed publicly — Caddy handles all ingress.

---

## 6. Internal Hostname Routing (Docker network)

Inside the Docker network, containers call each other by name:

```
Redis:   redis-container:6379    (NO password — confirmed)
n8n:     n8n-container:5678
OpenClaw: openclaw:18789
SAVANT backend: savant-backend:8000
```

**Caddy routes from outside:**
```
savant.ali-n8n.com/ws      → savant-backend:8000  (WebSocket)
savant.ali-n8n.com/health* → savant-backend:8000  (HTTP healthcheck)
savant.ali-n8n.com/*       → /home/savant/app/pwa/ (static files)
```

**Critical**: Cloudflare proxy is OFF (DNS only) on `savant.ali-n8n.com`. WebSockets need this — Cloudflare free tier kills long-lived WS connections.

---

## 7. SAVANT Architecture — Data Flow

```
[Browser PWA — savant.ali-n8n.com]
  getUserMedia() → PCM 16kHz mono 16-bit
       ↕ WSS WebSocket (audio bytes + JSON commands)

[Caddy]  ← TLS termination, wss:// → ws:// internally

[savant-backend:8000 — FastAPI async]
  websocket_endpoint /ws
  ├── pwa_to_gemini():  receive_bytes() → send PCM to Gemini Live
  ├── gemini_to_pwa():  receive Gemini audio chunks → send_bytes() to PWA
  ├── tool_call handler: intercept Gemini tool_calls → call OpenClaw
  ├── Redis client: STOP_FLAG, CURRENT_TASK, PENDING_CONFIRMATION
  └── SQLite: memory.db read at session start, write at session end

[Gemini 3.1 Flash Live — google-genai SDK]
  Speech-to-Speech native
  Generates: audio response + tool_calls
  Input format:  PCM 16kHz, mono, 16-bit little-endian, chunks 20-40ms
  Output format: PCM 24kHz

[OpenClaw:18789 — Claude via OpenRouter]
  Receives: tool_call payload from savant-backend
  Reasons: what to check, in what order
  Executes: bash commands on VPS, HTTP calls, file reads
  Returns: structured result → savant-backend → Gemini → spoken to user

[Redis:6379]
  STOP_FLAG            ← user said "stop" → all operations check this
  CURRENT_TASK         ← what is running right now
  PENDING_CONFIRMATION ← waiting for user voice confirmation (critical actions)

[n8n:5678]  ← workflows still available but OpenClaw replaces most of it
  WF1: Runbook Incident (Docker, logs, CPU/RAM)  ← OpenClaw can do this directly now
  WF4: Notion task dispatcher                    ← still useful via OpenClaw tool call
```

---

## 8. Current Implementation State (20 May 2026)

### Done ✅
- DNS: `savant.ali-n8n.com` → VPS (Cloudflare, proxy OFF)
- Caddy: bloc added for savant.ali-n8n.com (WS + healthcheck + static PWA)
- FastAPI backend: `/health`, `/health/redis`, `/ws` stub
- WebSocket bug fixed: using `websocket.receive()` dispatch (was `receive_text()` → crash on binary frames)
- Half-duplex audio: `micMuted` flag in AudioWorklet (prevents feedback loop)
- Redis connection: no password, `redis-container:6379`
- `deploy-openclaw.sh`: script exists, OpenClaw container is in `/home/savant/openclaw/`
- OpenClaw volume: `/home/savant/openclaw` mounted to `/home/node/.openclaw`

### NOT done yet ❌
- Gemini Live integration NOT wired: `websocket_endpoint` in `main.py` is still a stub
- OpenClaw healthcheck NOT validated: `curl http://openclaw:18789/healthz` — unknown state
- Backend → OpenClaw connection NOT coded: no HTTP call from `main.py` to OpenClaw yet
- Tool declarations NOT defined in Gemini: `analyze_vps`, `create_notion_task`, `memory_query`
- Session management NOT implemented: SQLite load at session start, summary at session close
- Action Code system NOT implemented: voice confirmation for critical actions

---

## 9. Environment Variables (`.env` in `/home/savant/app/`)

| Variable | Description | Status |
|---|---|---|
| `GEMINI_API_KEY` | Gemini Live API | ✅ present |
| `OPENROUTER_API_KEY` | OpenRouter → Claude via OpenClaw | ✅ present |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw API auth token | ✅ present |
| `OPENCLAW_HOOKS_TOKEN` | OpenClaw webhook token (MUST differ from GATEWAY_TOKEN) | ✅ present (generated by deploy script) |
| `REDIS_HOST` | `redis-container` | in CLAUDE.md |
| `REDIS_PORT` | `6379` | in CLAUDE.md |
| `N8N_BASE_URL` | `http://n8n-container:5678` | in CLAUDE.md |
| `N8N_WEBHOOK_SECRET` | Shared secret for n8n | in CLAUDE.md |
| `SAVANT_PORT` | `8000` | default |

---

## 10. Critical Rules — Never Violate

1. **Never expose ports publicly** — all internal traffic goes via Caddy. Use `expose:` not `ports:` for SAVANT containers.
2. **Never write `openclaw.json` manually** — use `node /app/dist/index.js config set --batch-json` command inside container.
3. **OpenClaw volume path is `/home/node/.openclaw`** — NOT `/root/.openclaw`. User inside container is `node` (UID 1000).
4. **Redis has NO password** — confirmed. Connect directly: `redis://redis-container:6379`.
5. **Caddy reload = zero downtime** — `docker exec caddy-proxy caddy reload --config /etc/caddy/Caddyfile`. Never restart the container.
6. **`OPENCLAW_HOOKS_TOKEN` ≠ `OPENCLAW_GATEWAY_TOKEN`** — OpenClaw refuses to start if they're identical.
7. **Never modify `/root/n8n-automation/docker-compose.yml`** — n8n stack is separate. SAVANT has its own docker-compose in `/home/savant/app/`.

---

## 11. Useful Commands

```bash
# State of all containers
docker ps -a

# SAVANT backend logs
docker logs savant-backend --tail 50 -f

# OpenClaw logs
docker logs openclaw --tail 50 -f

# Validate OpenClaw is up
curl http://localhost:18789/healthz   # from VPS host
# or from inside network:
docker exec savant-backend curl http://openclaw:18789/healthz

# Redis check
docker exec redis-container redis-cli ping

# Reload Caddy (zero downtime)
docker exec caddy-proxy caddy reload --config /etc/caddy/Caddyfile

# Restart only SAVANT backend
cd /home/savant/app && docker compose restart savant-backend

# Check ports in use
ss -tulpn | grep LISTEN

# Check disk
df -h /

# Check memory
free -h
```

---

## 12. Immediate Next Steps (priority order)

1. **Validate OpenClaw**: `curl http://localhost:18789/healthz` → must return `{"ok":true}`
2. **Fix openclaw/ owner**: `chown -R 1000:1000 /home/savant/openclaw` if not already node-owned
3. **Wire Gemini Live in `main.py`**: replace stub with real `google-genai` async session
4. **Connect backend → OpenClaw**: POST to `http://openclaw:18789/hooks/agent` on tool_call
5. **Declare Gemini tools**: `analyze_vps`, `web_search`, `create_notion_task`, `memory_query`

---

*Generated 20 May 2026 — based on live VPS terminal output + session history*