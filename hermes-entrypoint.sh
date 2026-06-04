#!/bin/bash
# Run Hermes gateway as root — bypasses gosu in the original entrypoint.
# HERMES_ALLOW_ROOT_GATEWAY=1 is set in hermes-agent.env and permits root execution.
set -e

HERMES_HOME="${HERMES_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"

# Bootstrap essential directory structure (mirrors what the original entrypoint does)
mkdir -p "$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,skins,plans,workspace,home,vps_map}

# Bootstrap config files on first boot only (our volume already has SOUL.md + config.yaml)
[ -f "$HERMES_HOME/.env" ]       || cp "$INSTALL_DIR/.env.example"           "$HERMES_HOME/.env"       2>/dev/null || true
[ -f "$HERMES_HOME/config.yaml" ] || cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml" 2>/dev/null || true
[ -f "$HERMES_HOME/SOUL.md" ]    || cp "$INSTALL_DIR/docker/SOUL.md"         "$HERMES_HOME/SOUL.md"    2>/dev/null || true

# Sync bundled skills (user edits in our volume are preserved — manifest-based merge)
[ -d "$INSTALL_DIR/skills" ] && python3 "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null || true

# Activate venv and exec hermes as root (full root access to VPS)
source "${INSTALL_DIR}/.venv/bin/activate"

# Enable the native MCP client (Notion etc.). The `mcp` package is an optional
# Hermes dependency not present in the base image. The venv ships without pip, so
# install via uv (present in the image) into the active venv. Idempotent + non-fatal.
uv pip install mcp >/dev/null 2>&1 || \
    echo "[entrypoint] warning: could not install mcp package — MCP servers disabled"

# SAVANT Console Live — fast non-LLM exec helper for the hands-on console mode.
# Internal docker network only (port 8644 not published), bearer-authed with the
# Hermes secret. Backgrounded so the gateway below still becomes the main process.
CONSOLE_EXEC="/host/home/savant/app/scripts/console_exec_server.py"
[ -f "$CONSOLE_EXEC" ] && python3 "$CONSOLE_EXEC" >>"$HERMES_HOME/logs/console_exec.log" 2>&1 &

exec hermes "$@"
