#!/usr/bin/env bash
# Reset SAVANT to first-run state: clears all memory/sessions/incidents rows
# and restarts the backend container. Does NOT delete the .db file.
#
# Usage:
#   ./scripts/reset_memory.sh
#   CONTAINER=my-backend ./scripts/reset_memory.sh   # override container name
set -euo pipefail

CONTAINER="${CONTAINER:-savant-backend}"
DB_PATH="/app/memory/savant.db"

echo ""
echo "=== SAVANT memory reset ==="
echo ""

# ── 1. Verify container is running ─────────────────────────────────────────
if ! docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    echo "ERROR: container '$CONTAINER' is not running." >&2
    exit 1
fi

# ── 2. Clear all tables ────────────────────────────────────────────────────
echo "Clearing tables: memory, sessions, incidents, action_log, workflows..."
docker exec -i "$CONTAINER" python3 - <<'PYEOF'
import sqlite3, os, sys

db = os.getenv("DB_PATH", "/app/memory/savant.db")
if not os.path.exists(db):
    print(f"  DB not found at {db} — nothing to clear.")
    sys.exit(0)

conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("DELETE FROM memory")
conn.execute("DELETE FROM sessions")
conn.execute("DELETE FROM incidents")

for table in ("action_log", "workflows"):
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists:
        conn.execute(f"DELETE FROM {table}")
        print(f"  ✓ {table:<12} cleared")
    else:
        print(f"  - {table:<12} skipped (table absent)")

conn.commit()
conn.close()
print("  ✓ memory     cleared")
print("  ✓ sessions   cleared")
print("  ✓ incidents  cleared")
PYEOF

# ── 3. Restart the container ───────────────────────────────────────────────
echo ""
echo "Restarting $CONTAINER..."
docker restart "$CONTAINER" > /dev/null
echo "  ✓ Container restarted"

# ── 4. Wait for the health endpoint ───────────────────────────────────────
echo ""
echo "Waiting for /health..."
for i in $(seq 1 15); do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        echo "  ✓ Backend is up"
        break
    fi
    sleep 1
    if [ "$i" -eq 15 ]; then
        echo "  WARNING: /health did not respond within 15 s — check logs." >&2
    fi
done

echo ""
echo "Reset complete. SAVANT is in first-run state."
echo ""
