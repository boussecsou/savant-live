# Getting Started with SAVANT

This guide walks you through installing SAVANT on a Linux VPS from scratch.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Linux VPS | Ubuntu 22.04 / Debian 12 recommended | 2 vCPU, 4GB RAM minimum |
| Docker Engine | 24+ | [Install guide](https://docs.docker.com/engine/install/) |
| Docker Compose | v2+ | Comes with Docker Desktop; on servers: `apt install docker-compose-plugin` |
| Google Gemini API key | — | [Get one at Google AI Studio](https://aistudio.google.com/apikey) |
| OpenRouter API key | — | [Get one at openrouter.ai](https://openrouter.ai/keys) |
| Domain + TLS | Optional | Recommended for production; required for HTTPS microphone access |

---

## Step 1 — Clone the Repository

```bash
git clone https://github.com/your-username/savant.git
cd savant
```

---

## Step 2 — Configure Environment

```bash
cp .env.example .env
nano .env  # or use your preferred editor
```

Fill in at minimum:

```bash
GEMINI_API_KEY=AIzaSy...          # from Google AI Studio
OPENROUTER_API_KEY=sk-or-v1-...   # from openrouter.ai
HERMES_WEBHOOK_SECRET=<generate a strong random string>
REDIS_HOST=redis-container         # default — matches docker-compose service name
```

Generate a strong secret: `openssl rand -hex 32`

See [`docs/configuration.md`](configuration.md) for all available options.

---

## Step 3 — Build and Launch

```bash
docker compose build
docker compose up -d
```

This starts:
- `savant-backend` — FastAPI server on port 8000
- `hermes-v2` — Privileged VPS execution agent (ports 8642, 8644)
- `redis-container` — Ephemeral state

Watch logs during first boot:
```bash
docker compose logs -f savant-backend
```

---

## Step 4 — Verify Health

```bash
# Backend alive
curl http://localhost:8000/health

# Redis connected
curl http://localhost:8000/health/redis

# Sessions (should be empty first run)
curl http://localhost:8000/health/sessions
```

Expected responses:
```json
{"status": "ok"}
{"status": "ok", "ping": "PONG"}
{"sessions": []}
```

---

## Step 5 — Access the PWA

Open your browser and navigate to:
- With Caddy/TLS: `https://your-domain.com`
- Without TLS (local): `http://localhost:8000`

> **Note:** Chrome and Edge are recommended. The Web Audio API (microphone capture at 16kHz) works best on Chromium-based browsers. Firefox may require additional flags.

> **HTTPS requirement:** Browser microphone access requires HTTPS in production. For local testing, `localhost` is exempt.

---

## Step 6 — First-Run Onboarding

On first access, SAVANT will ask you to create an access code:

1. **Enter your access code** — this is a password you choose; it will be stored as a SHA-256 hash
2. **Answer onboarding questions:**
   - Your name (how SAVANT should address you)
   - Your preferred language for voice responses
   - Your timezone

These are stored in SQLite and used in every future session. You can change them later by voice: *"Update my timezone to Europe/Paris"*.

---

## Step 7 — Your First Voice Command

Once onboarded, try these to get started:

```
"What's the CPU and memory usage right now?"
"List all running Docker containers"
"Show me the last 50 lines of the nginx logs"
"What do you remember about my server?"
```

---

## Health Checks & Monitoring

```bash
# View backend logs
docker compose logs -f savant-backend

# View Hermes logs
docker compose logs -f hermes-v2

# Check active sessions
curl http://localhost:8000/health/sessions

# See the exact system prompt being used
curl -H "Authorization: Bearer $HERMES_WEBHOOK_SECRET" http://localhost:8000/health/prompt
```

---

## Resetting SAVANT

```bash
# Clear SQLite memory and sessions (keeps Docker state)
./scripts/reset_memory.sh

# Clear Hermes map/sessions/memories
./scripts/reset_hermes.sh

# Full reset to first-run state (requires sudo for root-owned backups)
sudo bash ./scripts/reset_all.sh
```

---

## Upgrading

```bash
git pull origin main
docker compose build
docker compose up -d
```

Your SQLite database (`../memory/savant.db`), logs, and skills are stored in bind-mounted directories outside the container — they survive rebuilds automatically.

---

## Troubleshooting

**Microphone not working**
- Ensure you're on HTTPS (or localhost)
- Check browser microphone permissions
- Try Chrome/Edge instead of Firefox

**"access_denied" on connection**
- Wrong access code — check the hash stored in SQLite:
  ```bash
  docker exec savant-backend python3 -c "from backend.memory.database import recall; print(recall('action_code'))"
  ```

**Hermes not responding**
- Check Hermes is running: `docker compose ps`
- Verify `HERMES_URL` in `.env` matches the service name
- Check Hermes logs: `docker compose logs hermes-v2`

**Redis connection failed**
- Verify `REDIS_HOST` matches the Docker service name
- Check Redis is running: `docker compose ps redis-container`
