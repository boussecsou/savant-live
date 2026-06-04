#!/usr/bin/env bash
# SAVANT metrics refresher — writes a fresh metrics snapshot every ~20s.
# Writes to /host/home/savant/logs/vps_map/ (hermes-v2 /:/host mount),
# readable by savant-backend at /app/logs/vps_map/ (../logs:/app/logs already mounted).
# Launched by @reboot line in savant-vps-cron.
# flock prevents overlapping runs if docker exec takes longer than 20s.
set -u
LOCK=/tmp/metrics_refresher.lock
while true; do
  flock -n "$LOCK" docker exec hermes-v2 sh -c \
    "mkdir -p /host/home/savant/logs/vps_map && python3 /host/home/savant/scripts/vps_metrics.py > /host/home/savant/logs/vps_map/METRICS.txt 2>/dev/null" \
    2>/dev/null || true
  sleep 20
done
