"""
SQLite long-term memory for SAVANT.

DB path inside Docker:  /app/memory/savant.db  (mounted from host ../memory/)
DB path on host:        /home/savant/memory/savant.db

Override with DB_PATH env var for local dev outside Docker.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "/app/memory/savant.db")

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _connect():
    """Yield a SQLite connection with WAL mode and row_factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema creation + seeding
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    summary     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memory (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    type         TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    resolved     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id   TEXT,
    action_type TEXT,
    description TEXT,
    status      TEXT,
    duration_s  REAL,
    verified    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflows (
    name        TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    auth_header TEXT,
    auth_key    TEXT,
    created_at  TEXT NOT NULL
);
"""

def init_db() -> None:
    """Create tables if they don't exist. Idempotent — safe to call on every startup."""
    with _connect() as conn:
        conn.executescript(_DDL)
        _migrate(conn)


def _migrate(conn) -> None:
    """Add columns introduced after the initial schema. Idempotent — each ALTER is
    wrapped so a 'duplicate column' on an already-migrated DB is a no-op."""
    _alters = (
        "ALTER TABLE incidents ADD COLUMN cause TEXT",
        "ALTER TABLE incidents ADD COLUMN suggested_fix TEXT",
        "ALTER TABLE memory ADD COLUMN namespace TEXT DEFAULT 'general'",
    )
    for stmt in _alters:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # column already exists


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_memory_value(key: str) -> str | None:
    """Return the stored value for *key*, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM memory WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


_ONBOARDING_REQUIRED_KEYS = ("owner_name", "preferred_language", "timezone")


def is_first_run() -> bool:
    """Return True if ANY required onboarding key is missing.

    Onboarding is considered complete only when all 3 keys exist:
    owner_name, preferred_language, timezone. This lets SAVANT detect
    a half-finished onboarding on reconnect and resume from the next
    missing question.
    """
    with _connect() as conn:
        for key in _ONBOARDING_REQUIRED_KEYS:
            row = conn.execute(
                "SELECT value FROM memory WHERE key = ?", (key,)
            ).fetchone()
            if row is None or not (row["value"] or "").strip():
                return True
    return False


def get_onboarding_status() -> dict:
    """Return {"collected": {key: value}, "missing": [key,...]} for the 3 required keys."""
    collected: dict[str, str] = {}
    missing: list[str] = []
    with _connect() as conn:
        for key in _ONBOARDING_REQUIRED_KEYS:
            row = conn.execute(
                "SELECT value FROM memory WHERE key = ?", (key,)
            ).fetchone()
            val = (row["value"] if row else "") or ""
            if val.strip():
                collected[key] = val
            else:
                missing.append(key)
    return {"collected": collected, "missing": missing}


def load_memory() -> str:
    """Return a formatted string suitable for injection into the system prompt.

    Loads the 40 most recently updated memory keys and the last 3 sessions that
    have a usable summary (LLM-generated or Gemini 1-sentence — never [auto]).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at, namespace FROM memory "
            "WHERE key NOT IN ('action_code', 'paused') "
            "ORDER BY updated_at DESC LIMIT 40"
        ).fetchall()
        # Guarantee identity keys are present even if pushed past the 40-row window —
        # they must always be 'known', no matter how much else has been stored.
        have = {r["key"] for r in rows}
        missing_ident = [k for k in _IDENTITY_KEYS if k not in have]
        ident_rows = []
        if missing_ident:
            qmarks = ",".join("?" for _ in missing_ident)
            ident_rows = conn.execute(
                f"SELECT key, value, updated_at, namespace FROM memory WHERE key IN ({qmarks})",
                tuple(missing_ident),
            ).fetchall()
        # Fetch a wider window so we can filter [auto]/empty and still get 3.
        sessions = conn.execute(
            "SELECT started_at, summary FROM sessions WHERE summary != '' "
            "ORDER BY id DESC LIMIT 15"
        ).fetchall()

    lines = ["## SAVANT persistent memory\n"]

    # Identity block — always first, always present.
    ident = list(ident_rows) + [r for r in rows if r["key"] in _IDENTITY_KEYS]
    seen_ident: set[str] = set()
    ident_lines = []
    for r in ident:
        if r["key"] in seen_ident:
            continue
        seen_ident.add(r["key"])
        ident_lines.append(f"- {r['key']}: {r['value']}")
    if ident_lines:
        lines.append("### Identity")
        lines.extend(ident_lines)

    # Remaining facts grouped by namespace (cap 15 per namespace for token thrift).
    groups: dict[str, list] = {}
    for r in rows:
        if r["key"] in _IDENTITY_KEYS:
            continue
        ns = (r["namespace"] or "general")
        groups.setdefault(ns, []).append(r)
    for ns in sorted(groups):
        lines.append(f"\n### {ns}")
        for r in groups[ns][:15]:
            date = r["updated_at"][:10]
            lines.append(f"- {r['key']}: {r['value']}  [updated: {date}]")

    # Keep only sessions with LLM or Gemini-generated summaries; skip [auto] noise.
    usable: list[tuple[str, str]] = []
    for s in sessions:
        raw = (s["summary"] or "").strip()
        if not raw or raw.lower().startswith("[auto]"):
            continue
        # Strip the type prefix (e.g. "[llm] " or "[gemini] ") and normalize whitespace.
        if raw.lower().startswith("[llm]"):
            body = raw[5:].strip()
        elif raw.lower().startswith("[gemini]"):
            body = raw[8:].strip()
        else:
            body = raw
        # Collapse multi-line bullets onto one compact line: "• X • Y • Z".
        body = " ".join(part.strip() for part in body.splitlines() if part.strip())
        if not body:
            continue
        usable.append((s["started_at"][:10], body))
        if len(usable) >= 3:
            break

    if usable:
        lines.append("\n### Recent session history (last 3)")
        for date, body in usable:
            lines.append(f"[{date}] Session: {body}")

    return "\n".join(lines)


_IDENTITY_KEYS = ("owner_name", "preferred_language", "timezone")


def save_memory(key: str, value: str, namespace: str = "general") -> None:
    """Upsert a key-value pair into the memory table, in a namespace (default 'general')."""
    ns = (namespace or "general").strip() or "general"
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO memory (key, value, updated_at, namespace) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at,
                                           namespace = excluded.namespace
            """,
            (key, value, _now(), ns),
        )


def open_session() -> int:
    """Create a new session row with empty summary. Returns the row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, ended_at, summary) VALUES (?, NULL, '')",
            (_now(),),
        )
        return cur.lastrowid


def close_session(session_id: int, summary: str) -> None:
    """Set the summary and end time for an existing session."""
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, summary = ? WHERE id = ?",
            (_now(), summary, session_id),
        )


def save_session_summary(summary: str, started_at: str | None = None) -> int:
    """Insert a completed session summary row. Returns the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (started_at, ended_at, summary) VALUES (?, ?, ?)",
            (started_at or _now(), _now(), summary),
        )
        return cur.lastrowid


def search_memory(query: str, limit: int = 3) -> dict:
    """Search memory keys and session summaries for substrings of *query*.

    Returns a dict with two lists:
      - 'memory':   up to *limit* matching memory key/value pairs (most recent first)
      - 'sessions': up to *limit* matching session summaries (most recent first)

    The search uses SQL LIKE on the value/summary columns (LIKE is ASCII
    case-insensitive in SQLite). Query is never interpolated into SQL —
    parameterised binding only — and LIKE wildcards (percent, underscore,
    backslash) in the user query are escaped via ESCAPE so they match literally.
    """
    q = (query or "").strip()
    if not q:
        return {"memory": [], "sessions": []}
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{esc}%"
    with _connect() as conn:
        mem_rows = conn.execute(
            "SELECT key, value, updated_at FROM memory "
            "WHERE key NOT IN ('action_code', 'paused') AND "
            "(value LIKE ? ESCAPE '\\' OR key LIKE ? ESCAPE '\\') "
            "ORDER BY updated_at DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        sess_rows = conn.execute(
            "SELECT started_at, summary FROM sessions "
            "WHERE summary LIKE ? ESCAPE '\\' AND summary != '' "
            "ORDER BY started_at DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()

    return {
        "memory": [
            {"key": r["key"], "value": r["value"], "updated_at": r["updated_at"]}
            for r in mem_rows
        ],
        "sessions": [
            {"date": (r["started_at"] or "")[:10], "summary": r["summary"]}
            for r in sess_rows
        ],
    }


def recall(query: str, limit: int = 5, namespace: str | None = None) -> list[dict]:
    """Ranked keyword retrieval over memory — approximates 'search by meaning' without
    embeddings. Scores each fact by how many query terms appear in its key+value (plus a
    bonus for a full-query substring match), optionally filtered to a namespace. Returns
    the top *limit* as dicts {key, value, namespace, updated_at}, best first."""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = [t for t in re.split(r"\W+", q) if len(t) >= 2] or [q]
    with _connect() as conn:
        if namespace:
            rows = conn.execute(
                "SELECT key, value, updated_at, namespace FROM memory "
                "WHERE key NOT IN ('action_code','paused') AND namespace = ?",
                ((namespace or "general").strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, value, updated_at, namespace FROM memory "
                "WHERE key NOT IN ('action_code','paused')"
            ).fetchall()
    scored: list[tuple[int, sqlite3.Row]] = []
    for r in rows:
        hay = f"{r['key']} {r['value']}".lower()
        score = sum(hay.count(t) for t in terms)
        if q in hay:
            score += 2
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: (x[0], x[1]["updated_at"]), reverse=True)
    return [
        {"key": r["key"], "value": r["value"],
         "namespace": r["namespace"] or "general", "updated_at": r["updated_at"]}
        for _, r in scored[:limit]
    ]


def get_recent_sessions(n: int = 10) -> list[dict]:
    """Return the last *n* session summaries as a list of dicts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, started_at, ended_at, summary FROM sessions "
            "ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Incidents — proactive VPS alerts. Written by the background monitor when no
# session is active (and by the live metrics loop), announced in the next
# Smart Briefing, then marked resolved. resolved=0 means "not yet announced".
# ---------------------------------------------------------------------------

def save_incident(itype: str, detail: str, resolved: int = 0,
                  cause: str | None = None, suggested_fix: str | None = None) -> int:
    """Record a VPS incident/alert. `itype` is a short severity label
    (e.g. '🔴 RAM', '⚠️ DISK'); `detail` is the human-readable message. Optional
    `cause`/`suggested_fix` carry the enriched context. Returns the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (date, type, action_taken, resolved, cause, suggested_fix) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), itype[:80], detail[:500], 1 if resolved else 0,
             (cause or None) and cause[:300], (suggested_fix or None) and suggested_fix[:300]),
        )
        return cur.lastrowid


def get_unresolved_incidents(limit: int = 10) -> list[dict]:
    """Return unresolved incidents (most recent first) for the Smart Briefing."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, date, type, action_taken, resolved, cause, suggested_fix FROM incidents "
            "WHERE resolved = 0 ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_incidents(ids: list[int]) -> None:
    """Mark the given incidents as resolved (announced). No-op on empty list."""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with _connect() as conn:
        conn.execute(
            f"UPDATE incidents SET resolved = 1 WHERE id IN ({placeholders})",
            tuple(ids),
        )


def count_unresolved_incidents() -> int:
    """Fast count of unresolved incidents."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE resolved = 0"
        ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Action log — adaptive memory of past Hermes actions. Written deterministically
# by the backend after each action (never by Hermes), so it is reliable. Prepended
# as a short [CONTEXT] block to future task instructions so Hermes adapts instead
# of re-inventing from scratch. See main.py _run_hermes_bg.
# ---------------------------------------------------------------------------

def save_action_log(
    action_id: str,
    action_type: str,
    description: str,
    status: str,
    duration_s: float,
    verified: int = 0,
) -> None:
    """Insert one completed action into the action_log table. Best-effort —
    callers wrap this so a logging failure never breaks the action itself."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO action_log "
            "(action_id, action_type, description, status, duration_s, verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                action_type,
                (description or "")[:200],
                status,
                round(float(duration_s or 0.0), 1),
                1 if verified else 0,
                _now(),
            ),
        )


def get_recent_action_context(limit: int = 3) -> str:
    """Return the last *limit* actions as a concise multi-line string for
    prepending to a Hermes instruction, or '' when there is no history.
    Format: `YYYY-MM-DD HH:MM | type | "desc" | status | Ns | verified`."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT action_type, description, status, duration_s, verified, created_at "
                "FROM action_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["[CONTEXT: dernières actions]"]
    for r in rows:
        when = (r["created_at"] or "").replace("T", " ")[:16]
        dur = f"{r['duration_s']:.0f}s" if r["duration_s"] is not None else "?"
        parts = [
            when,
            r["action_type"] or "?",
            f"\"{(r['description'] or '')[:60]}\"",
            r["status"] or "?",
            dur,
        ]
        if r["verified"]:
            parts.append("verified")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workflow registry — voice-registered n8n (or any) webhooks. Triggered directly
# by the backend (backend/workflows.py), NOT through Hermes. auth_key is stored for
# the trigger call only and is NEVER returned by list_workflows / echoed anywhere.
# ---------------------------------------------------------------------------

def save_workflow(
    name: str,
    url: str,
    description: str = "",
    auth_header: str | None = None,
    auth_key: str | None = None,
) -> None:
    """Upsert a webhook workflow keyed by name."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO workflows (name, url, description, auth_header, auth_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET url = excluded.url,
                                            description = excluded.description,
                                            auth_header = excluded.auth_header,
                                            auth_key = excluded.auth_key
            """,
            (name.strip(), url.strip(), (description or "").strip(),
             (auth_header or None), (auth_key or None), _now()),
        )


def get_workflow(name: str) -> dict | None:
    """Return the full workflow row (incl. auth) for triggering, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, url, description, auth_header, auth_key FROM workflows WHERE name = ?",
            (name.strip(),),
        ).fetchone()
    return dict(row) if row else None


def list_workflows() -> list[dict]:
    """Return all workflows as name/description/url only — NEVER the auth key."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, url, description FROM workflows ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_workflow(name: str) -> None:
    """Remove a workflow by name. No-op if absent."""
    with _connect() as conn:
        conn.execute("DELETE FROM workflows WHERE name = ?", (name.strip(),))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
