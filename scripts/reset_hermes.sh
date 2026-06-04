#!/usr/bin/env bash
# Reset Hermes to factory state — clears VPS map and sessions.
# Keeps: SOUL.md, skills/, config.yaml
# File ops run inside the container (hermes/ is root-owned on host).
set -euo pipefail

APP_DIR="/home/savant/app"

echo ""
echo "=== Hermes Reset ==="
echo ""

# Ensure hermes-v2 is running so we can docker exec into it
if ! docker inspect --format '{{.State.Running}}' hermes-v2 2>/dev/null | grep -q true; then
  echo "Starting hermes-v2 to run cleanup..."
  docker compose -f "$APP_DIR/docker-compose.yml" up -d hermes-v2
  sleep 5
fi

echo "Clearing VPS map, sessions, logs, memories (via docker exec)..."
docker exec hermes-v2 bash -c '
  rm -rf /opt/data/vps_map && mkdir -p /opt/data/vps_map
  find /opt/data -maxdepth 1 -type f \
    ! -name "SOUL.md" ! -name "config.yaml" -delete 2>/dev/null || true
  find /opt/data -maxdepth 1 -mindepth 1 -type d \
    ! -name "skills" ! -name "vps_map" \
    -exec rm -rf {} + 2>/dev/null || true
  echo "  ✓ Cleared"
'

echo "Restarting hermes-v2..."
docker compose -f "$APP_DIR/docker-compose.yml" restart hermes-v2

echo "Waiting 8s for startup..."
sleep 8

if curl -sf http://localhost:8643/health > /dev/null 2>&1; then
  echo "  ✓ Hermes is up and reset"
else
  echo "  ✗ Health check failed — logs:"
  docker logs hermes-v2 --tail 20
  exit 1
fi

echo ""
echo "=== Reset complete ==="
echo "  Kept    : SOUL.md, skills/, config.yaml"
echo "  Cleared : vps_map/, sessions, logs, memories"
echo "  Note    : VPS blueprint will be rebuilt on next SAVANT session"
echo ""
