#!/usr/bin/env bash
# Deploy Hermes v2 as SAVANT's execution arm.
# Prerequisites: OPENROUTER_API_KEY and HERMES_WEBHOOK_SECRET must be set in /home/savant/app/.env
# Usage: bash /home/savant/app/scripts/deploy_hermes_savant.sh
set -euo pipefail

APP_DIR="/home/savant/app"
HERMES_DIR="$APP_DIR/hermes"

echo ""
echo "=== Hermes v2 deploy ==="
echo ""

# ── 1. Directories ──────────────────────────────────────────────────────────
echo "Creating directories..."
mkdir -p "$HERMES_DIR/skills" "$HERMES_DIR/vps_map"

# ── 2. config.yaml ──────────────────────────────────────────────────────────
echo "Writing config.yaml..."
cat > "$HERMES_DIR/config.yaml" << 'EOF'
provider: openrouter
model: google/gemini-2.5-pro
terminal:
  backend: local
  approval: off
agent:
  reasoning_effort: high
api:
  enabled: true
  port: 8642
display:
  background_process_notifications: none
EOF

# ── 3. .env for Hermes ───────────────────────────────────────────────────────
echo "Writing hermes/.env..."
OPENROUTER_KEY=$(grep -oP 'OPENROUTER_API_KEY=\K.*' "$APP_DIR/.env" 2>/dev/null || echo "")
HERMES_SECRET=$(grep -oP 'HERMES_WEBHOOK_SECRET=\K.*' "$APP_DIR/.env" 2>/dev/null || echo "changeme")

if [[ -z "$OPENROUTER_KEY" ]]; then
    echo "  WARNING: OPENROUTER_API_KEY not found in $APP_DIR/.env — Hermes will not work without it."
fi

cat > "$APP_DIR/hermes-agent.env" << EOF
OPENROUTER_API_KEY=$OPENROUTER_KEY
API_SERVER_ENABLED=true
API_SERVER_KEY=$HERMES_SECRET
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642
HERMES_ALLOW_ROOT_GATEWAY=1
HERMES_WEBHOOK_SECRET=$HERMES_SECRET
EOF
chmod 600 "$APP_DIR/hermes-agent.env"

# ── 4. SOUL.md ───────────────────────────────────────────────────────────────
echo "Writing SOUL.md..."
cat > "$HERMES_DIR/SOUL.md" << 'EOF'
# Soul
You are SAVANT's execution arm on a Linux VPS.
Be terse. One sentence max per response unless more is needed.
Never explain unless asked.
At the start of each gateway session, read /opt/data/vps_map/BLUEPRINT.md.
If the file is missing or empty, run the vps-map skill first before anything else.
Always run backup-scope skill before any write action.
Always update vps-map skill after any infrastructure change.
Return JSON when called via API: {"status": "...", "output": "...", "section_updated": "..."|null}
EOF

# ── 5. Skills ────────────────────────────────────────────────────────────────
echo "Writing skills..."

cat > "$HERMES_DIR/skills/vps-map.md" << 'EOF'
---
name: vps-map
description: Build and update VPS blueprint. Use at first session and after any infrastructure change.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [vps, infrastructure, docker]
    category: devops
---
# VPS Map

## When to Use
- First session ever
- After any docker, network, or filesystem change

## Procedure
Run these commands and collect output:
```
docker ps -a --format '{{json .}}'
ss -tulpn | grep LISTEN
df -h
free -m
uname -a
ls /home /root /var/www 2>/dev/null
```
Build/update sections in /home/savant/app/hermes/vps_map/:
- docker.json  ← docker ps output
- network.json ← open ports
- storage.json ← disk + RAM
- system.json  ← uname + top-level dirs

Update last_updated.txt with current timestamp.
Return: {"status": "ok", "sections_updated": [...]}

## Rules
- Update ONLY the section relevant to the action that triggered this skill
- Full rebuild only on first session or explicit request
- Never read /root contents without explicit instruction

## Pitfalls
- docker.sock must be accessible
- /home/savant/app/hermes/vps_map/ must exist (create if absent)
EOF

cat > "$HERMES_DIR/skills/backup-scope.md" << 'EOF'
---
name: backup-scope
description: Backup files affected by an action before executing it. Always run before write actions.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [backup, safety]
    category: devops
---
# Backup Scope

## When to Use
Before ANY action that modifies: docker-compose files, Caddyfile, savant.db, app source code.

## Procedure
1. Detect scope from action description:
   - "docker" action    → /home/savant/app/docker-compose.yml
   - "caddy" action     → /root/Caddyfile (VPS host path)
   - "memory/db" action → /home/savant/memory/savant.db
   - "code" action      → git diff output only
2. mkdir -p /home/savant/backups/hermes
3. zip /home/savant/backups/hermes/$(date +%Y-%m-%d_%Hh%M)_<target>.zip <file>
4. find /home/savant/backups/hermes -name "*.zip" -mtime +7 -delete
5. Return: {"status": "ok", "backup_path": "..."}

## Rules
- Never backup .env files (contain secrets)
- savant.db backup is safe (SQLite binary, no plaintext secrets)
- Never backup .env files (contain secrets)

## Pitfalls
- If zip fails, abort the action and return an error
EOF

cat > "$HERMES_DIR/skills/savant-executor.md" << 'EOF'
---
name: savant-executor
description: Rules for executing instructions received from SAVANT via API. Always active.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [savant, api, execution]
    category: devops
---
# SAVANT Executor

## When to Use
Every time an instruction arrives via the API (session_id contains "savant").

## Rules
- Response format ALWAYS: {"status": "ok|error", "output": "...", "section_updated": "..."|null}
- output max 500 chars
- Before any write action → run backup-scope skill
- After any infra change → run vps-map skill (relevant section only)
- Never expose secrets in output
- Never expose secrets in output

## Pitfalls
- If backup-scope fails, do NOT proceed with the write action
- Always return valid JSON, never plain text
EOF

echo "  ✓ 3 skills written"

# ── 6. Pull + start ──────────────────────────────────────────────────────────
echo ""
echo "Pulling nousresearch/hermes-agent (may take a moment)..."
docker compose -f "$APP_DIR/docker-compose.yml" pull hermes-v2

echo "Starting hermes-v2..."
docker compose -f "$APP_DIR/docker-compose.yml" up -d hermes-v2

echo "Waiting 8 s for startup..."
sleep 8

echo "Health check..."
if curl -sf http://localhost:8643/health > /dev/null 2>&1; then
    echo "  ✓ Hermes is up"
else
    echo "  ✗ Health check failed — check logs:"
    docker logs hermes-v2 --tail 20
    exit 1
fi

# ── 7. Curator cleanup ───────────────────────────────────────────────────────
echo "Removing default skills..."
docker exec hermes-v2 hermes curator unlink --all 2>/dev/null && echo "  ✓ Default skills removed" || echo "  (skipped — curator not available or no default skills)"

# ── 8. Test API ──────────────────────────────────────────────────────────────
echo "Testing API..."
RESPONSE=$(curl -s -X POST http://localhost:8643/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $HERMES_SECRET" \
  -d '{"message": "ping", "session_id": "savant-test"}')
echo "  Response: ${RESPONSE:0:200}"

echo ""
echo "=== Deploy complete ==="
echo "  Container : hermes-v2"
echo "  API       : http://localhost:8643/api/v1/chat"
echo "  Secret    : set in $HERMES_DIR/.env"
echo ""
