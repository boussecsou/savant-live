#!/usr/bin/env python3
"""
SAVANT Console Live — exec helper. Runs INSIDE the hermes-v2 container (the only
place with full VPS access: privileged, pid:host, /host mount). Gives the backend a
FAST, non-LLM command path for the hands-on Console mode — distinct from the Hermes
agent gateway (no reasoning, no skills, ~instant).

Security:
- Binds 0.0.0.0:8644 but the port is NOT published in docker-compose, so it is only
  reachable from inside the n8n docker network (backend → http://hermes-v2:8644).
- Bearer-authenticated with the same secret as the Hermes gateway (API_SERVER_KEY /
  HERMES_WEBHOOK_SECRET). It adds NO new capability — hermes-v2 already runs as root.
- The destructive-command confirmation gate lives in the backend (Console handler).

Session state (CWD + current user) is tracked server-side, keyed by the session_id
header sent by the backend. This gives persistent cd / su between commands without
requiring a long-lived PTY.

Launched by hermes-entrypoint.sh:
    python3 /host/home/savant/app/scripts/console_exec_server.py &
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CONSOLE_EXEC_PORT", "8644"))
_SECRETS = {
    s for s in (
        os.environ.get("CONSOLE_EXEC_SECRET"),
        os.environ.get("API_SERVER_KEY"),
        os.environ.get("HERMES_WEBHOOK_SECRET"),
    ) if s
}
EXEC_TIMEOUT_S = 30
OUTPUT_CAP = 20_000  # chars per stream
SESSION_TTL_S = 3600  # drop idle session state after 1h

# Per-session state: {session_id: {"cwd": str, "user": str|None, "ts": float}}
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _gc_sessions() -> None:
    """Evict sessions idle longer than SESSION_TTL_S."""
    now = time.time()
    with _sessions_lock:
        stale = [k for k, v in _sessions.items() if now - v["ts"] > SESSION_TTL_S]
        for k in stale:
            del _sessions[k]


def _get_session(session_id: str) -> dict:
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {"cwd": "/", "user": None, "ts": time.time()}
        else:
            _sessions[session_id]["ts"] = time.time()
        return dict(_sessions[session_id])


def _update_session(session_id: str, cwd: str | None, user: str | None) -> None:
    with _sessions_lock:
        if session_id in _sessions:
            if cwd is not None:
                _sessions[session_id]["cwd"] = cwd
            if user is not None:
                _sessions[session_id]["user"] = user
            _sessions[session_id]["ts"] = time.time()


def _run(cmd: str, session_id: str) -> dict:
    """Run a command in the HOST namespace via nsenter, preserving CWD and user."""
    state = _get_session(session_id)
    cwd = state["cwd"]
    user = state["user"]  # None = root

    # Detect user-switching commands (su -, sudo su, su <user>).
    # We extract the target user and remember it; the actual command resolves who we are.
    # We also detect `cd` to update the tracked CWD.
    stripped = cmd.strip()

    # Build the shell payload:
    # 1. cd to current tracked directory
    # 2. optionally wrap in `su - <user> -c` if a non-root user is active
    # 3. append a sentinel to read the resulting $PWD and $USER after execution
    sentinel = "__SAVANT_STATE__"
    inner = f"{stripped}; echo '{sentinel}'\"$PWD\"'|'\"$USER\""

    if user:
        # Run as a different user via su; use -c with a login shell env
        shell_payload = f"cd {_quote(cwd)} && su - {_quote(user)} -c {_quote(inner)}"
    else:
        shell_payload = f"cd {_quote(cwd)} && {inner}"

    full = ["nsenter", "-t", "1", "-m", "-u", "-n", "-i", "--", "bash", "-lc", shell_payload]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=EXEC_TIMEOUT_S)
        stdout_raw = r.stdout or ""
        stderr_out = (r.stderr or "")[:OUTPUT_CAP]
        code = r.returncode

        # Extract sentinel line to update session state
        new_cwd = cwd
        new_user = user
        clean_stdout = stdout_raw
        if sentinel in stdout_raw:
            lines = stdout_raw.split("\n")
            clean_lines = []
            for line in lines:
                if line.startswith(sentinel):
                    rest = line[len(sentinel):]
                    parts = rest.split("|", 1)
                    if len(parts) == 2:
                        candidate_cwd = parts[0].strip()
                        candidate_user = parts[1].strip()
                        if candidate_cwd:
                            new_cwd = candidate_cwd
                        # root stays as None (clean state)
                        new_user = None if candidate_user == "root" else candidate_user
                else:
                    clean_lines.append(line)
            # Remove trailing blank left by sentinel removal
            while clean_lines and clean_lines[-1] == "":
                clean_lines.pop()
            clean_stdout = "\n".join(clean_lines)
        else:
            # Sentinel never appeared (command may have exited early or su failed)
            # Try to detect su/cd manually from command text for partial state update
            new_cwd, new_user = _infer_state(stripped, cwd, user, code)

        _update_session(session_id, new_cwd, new_user)
        _gc_sessions()

        return {
            "stdout": clean_stdout[:OUTPUT_CAP],
            "stderr": stderr_out,
            "code": code,
            "cwd": new_cwd,
            "user": new_user or "root",
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"timeout after {EXEC_TIMEOUT_S}s", "code": 124,
                "cwd": cwd, "user": user or "root"}
    except Exception as e:
        return {"stdout": "", "stderr": f"exec error: {e}", "code": 1,
                "cwd": cwd, "user": user or "root"}


def _infer_state(cmd: str, cwd: str, user: str | None, code: int) -> tuple[str, str | None]:
    """Best-effort state update for commands where sentinel was not emitted."""
    # Only update state on success
    if code != 0:
        return cwd, user
    low = cmd.lstrip()
    if low.startswith("cd "):
        # Can't know the resolved path without running pwd; reset to /
        return "/", user
    return cwd, user


def _quote(s: str) -> str:
    """Single-quote a shell argument, escaping inner single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — health probe
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/reset":
            # allow a GET reset for test convenience (auth still required via header)
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if not _SECRETS or token not in _SECRETS:
                self._send(401, {"error": "unauthorized"})
                return
            sid = self.headers.get("X-Console-Session-Id", "")
            if sid:
                with _sessions_lock:
                    _sessions.pop(sid, None)
            self._send(200, {"status": "reset"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/exec", "/reset"):
            self._send(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not _SECRETS or token not in _SECRETS:
            self._send(401, {"error": "unauthorized"})
            return

        if self.path == "/reset":
            sid = self.headers.get("X-Console-Session-Id", "")
            if sid:
                with _sessions_lock:
                    _sessions.pop(sid, None)
            self._send(200, {"status": "reset"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            cmd = (payload.get("cmd") or "").strip()
            session_id = (self.headers.get("X-Console-Session-Id") or
                          payload.get("session_id") or "default")
        except Exception:
            self._send(400, {"error": "bad request"})
            return
        if not cmd:
            self._send(400, {"error": "empty cmd"})
            return
        self._send(200, _run(cmd, session_id))

    def log_message(self, *args):  # silence default stderr logging
        return


def main():
    if not _SECRETS:
        print("console_exec_server: no secret in env (API_SERVER_KEY/HERMES_WEBHOOK_SECRET) — not starting")
        return
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"console_exec_server listening on :{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
