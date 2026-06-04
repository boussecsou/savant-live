#!/usr/bin/env bash
# Full SAVANT reset — first-run state.
# Clears: memory/sessions/incidents/action_log/workflows (SQLite),
#         Redis savant:* keys, backups, session logs, alerts, Hermes state.
# Keeps:  skills/, SOUL.md, config.yaml
# Usage:  bash /home/savant/app/scripts/reset_all.sh
set -euo pipefail

APP_DIR="/home/savant/app"
BACKUP_DIR="/home/savant/backups"
LOG_DIR="/home/savant/logs"

echo ""
echo "=== Full SAVANT Reset ==="
echo ""

# ── Step 1: SQLite (memory, sessions, incidents, action_log, workflows) ────
echo "Step 1/5 — Reset SQLite (incl. workflows)..."
bash "$APP_DIR/scripts/reset_memory.sh"

# ── Step 2: Redis — flush all savant:* keys ────────────────────────────────
echo ""
echo "Step 2/5 — Flush Redis savant:* keys..."
if docker inspect --format '{{.State.Running}}' redis-container 2>/dev/null | grep -q true; then
    COUNT=$(docker exec redis-container redis-cli KEYS "savant:*" 2>/dev/null | wc -l)
    if [ "$COUNT" -gt 0 ]; then
        docker exec redis-container redis-cli KEYS "savant:*" \
            | xargs -r docker exec -i redis-container redis-cli DEL > /dev/null
        echo "  ✓ $COUNT Redis key(s) deleted"
    else
        echo "  - No savant:* keys found"
    fi
else
    echo "  WARNING: redis-container not running — skipping Redis flush" >&2
fi

# ── Step 3: Backups ────────────────────────────────────────────────────────
echo ""
echo "Step 3/5 — Purge backups ($BACKUP_DIR)..."
if [ -d "$BACKUP_DIR" ]; then
    FILE_COUNT=$(find "$BACKUP_DIR" -mindepth 1 | wc -l)
    sudo find "${BACKUP_DIR:?}" -mindepth 1 -delete
    echo "  ✓ $FILE_COUNT backup file(s) deleted"
else
    echo "  - Backup directory not found — nothing to purge"
fi

# ── Step 4: Session logs + ALERTS.md ──────────────────────────────────────
echo ""
echo "Step 4/5 — Purge session logs and alerts..."
SESSION_LOG_DIR="$LOG_DIR/sessions"
ALERTS_FILE="$LOG_DIR/vps_map/ALERTS.md"

if [ -d "$SESSION_LOG_DIR" ]; then
    LOG_COUNT=$(find "$SESSION_LOG_DIR" -type f | wc -l)
    rm -f "$SESSION_LOG_DIR"/*.log "$SESSION_LOG_DIR"/*.log.gz 2>/dev/null || true
    echo "  ✓ $LOG_COUNT session log(s) deleted"
else
    echo "  - Session log directory not found — skipping"
fi

if [ -f "$ALERTS_FILE" ]; then
    rm -f "$ALERTS_FILE"
    echo "  ✓ ALERTS.md deleted"
else
    echo "  - ALERTS.md not found — skipping"
fi

# ── Step 5: Hermes ─────────────────────────────────────────────────────────
echo ""
echo "Step 5/5 — Reset Hermes..."
bash "$APP_DIR/scripts/reset_hermes.sh"

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=== All done ==="
echo "  Cleared : memory, sessions, incidents, action_log, workflows (SQLite)"
echo "  Cleared : Redis savant:* keys"
echo "  Cleared : backups ($BACKUP_DIR)"
echo "  Cleared : session logs, ALERTS.md"
echo "  Cleared : Hermes vps_map, sessions, memories"
echo "  Kept    : skills/, SOUL.md, config.yaml"
echo "  Next session will trigger onboarding + full VPS scan."
echo ""
