# SAVANT — Configuration Reference

All environment variables for SAVANT. Copy `.env.example` to `.env` and fill in your values.

---

## Required Variables

| Variable | Description | Example |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio API key — powers Gemini 3.1 Flash Live | `AIzaSy...` |
| `OPENROUTER_API_KEY` | OpenRouter key — used by Hermes for multi-step task execution | `sk-or-v1-...` |
| `HERMES_WEBHOOK_SECRET` | Shared secret between backend and Hermes. Also protects `/control/*` endpoints | `<32+ char random string>` |
| `REDIS_HOST` | Redis service name in Docker Compose | `redis-container` |

Generate a strong secret:
```bash
openssl rand -hex 32
```

---

## Infrastructure Variables

| Variable | Default | Description |
|---|---|---|
| `HERMES_URL` | `http://hermes-v2:8642` | Hermes LLM gateway URL (internal Docker network) |
| `CONSOLE_EXEC_BASE` | `http://hermes-v2:8644` | Hermes exec server URL for Console Live |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | *(empty)* | Redis auth password — leave empty if Redis has no auth |
| `SAVANT_PORT` | `8000` | Backend listen port |
| `SAVANT_ENV` | `production` | Environment flag (`development` enables extra logging) |

---

## Path Variables

These match the Docker volume mounts in `docker-compose.yml`. Change only if you restructure the volumes.

| Variable | Default | Description |
|---|---|---|
| `LOG_DIR` | `/app/logs` | Directory for `savant.log`, session transcripts, and `vps_map/` |
| `DB_PATH` | `/app/memory/savant.db` | SQLite database path |
| `SKILLS_DIR` | `/app/skills` | Skill markdown files directory |

---

## Optional Variables

| Variable | Default | Description |
|---|---|---|
| `NOTION_API_KEY` | *(empty)* | Notion integration API key. When set, enables the fast-read lane for Notion pages. Leave empty to disable — nothing breaks. |

---

## Hermes-specific (`hermes-agent.env`)

Hermes uses a separate env file (`hermes-agent.env`, gitignored). It needs:

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | Same key as above |
| `API_SERVER_KEY` | Same value as `HERMES_WEBHOOK_SECRET` |
| `HERMES_WEBHOOK_SECRET` | Same value as above |
| `NOTION_TOKEN` | Same value as `NOTION_API_KEY` (if using Notion) |

Copy from your `.env`:
```bash
cp .env.example hermes-agent.env
# Fill in the same values
```

---

## Control Endpoints

All `/control/*` endpoints require:
```
Authorization: Bearer <HERMES_WEBHOOK_SECRET>
```

| Endpoint | Method | Action |
|---|---|---|
| `GET /health` | public | Backend liveness check |
| `GET /health/redis` | public | Redis connectivity check |
| `GET /health/sessions` | public | Recent sessions list |
| `POST /control/stop` | authenticated | Stop all running actions (60s TTL flag) |
| `POST /control/pause` | authenticated | Pause audio processing |
| `POST /control/resume` | authenticated | Resume audio processing |
| `GET /control/status` | authenticated | `{running, task, paused}` |
| `GET /health/prompt` | authenticated | Exact system prompt (debug) |

---

## Docker Compose Volumes

Understanding what persists:

| Mount | Host path | Container path | Persists? |
|---|---|---|---|
| SQLite DB | `../memory/` | `/app/memory/` | Yes — survives `docker compose down` |
| Logs | `../logs/` | `/app/logs/` | Yes |
| Skills | `../skills/` | `/app/skills/` | Yes |
| PWA static | `./pwa/` | `/app/pwa/` | Yes (in repo) |
| VPS map | `./hermes/vps_map/` | `/app/vps_map/` | Yes |
| Hermes data | `./hermes/` | `/opt/data/` | Yes |
| Redis data | Container memory | — | No — lost on restart |

---

## Recommended Production Checklist

- [ ] `GEMINI_API_KEY` set and valid
- [ ] `OPENROUTER_API_KEY` set and valid
- [ ] `HERMES_WEBHOOK_SECRET` is a strong random string (32+ chars)
- [ ] `hermes-agent.env` created from template
- [ ] Domain configured with TLS (required for browser microphone access)
- [ ] `../memory/` directory exists and is writable
- [ ] `../logs/` directory exists and is writable
- [ ] `../skills/` directory exists and is writable
- [ ] `docker compose up -d` returns healthy on all services
- [ ] `curl http://localhost:8000/health/redis` returns `{"status":"ok"}`
