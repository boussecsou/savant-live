import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException
from google import genai
from google.genai import types

from backend.memory.database import (
    init_db, is_first_run, load_memory, save_memory, get_memory_value,
    open_session, close_session, save_session_summary, get_recent_sessions,
    search_memory as db_search_memory, get_onboarding_status,
    save_incident, get_unresolved_incidents, resolve_incidents, count_unresolved_incidents,
    save_action_log, get_recent_action_context,
)
from backend.hermes.client import call_hermes
from backend.prompts import (
    _SYSTEM_BASE, _CODE_BLOCK, _ONBOARDING_INSTRUCTIONS, _ONBOARDING_KEYS,
    _SESSION_SUMMARY_INSTRUCTIONS, _HERMES_BLOCK, _NOTION_BLOCK, _WORKFLOWS_BLOCK,
    NOTION_ENABLED, _PAUSE_PHRASES, _RESUME_PHRASES, _REMEMBER_TRIGGERS,
    _ACTION_INTENT_PHRASES, match_phrase,
)
from backend.notion import notion_read, notion_fast_enabled
from backend.workflows import (
    save_workflow as wf_save, run_workflow as wf_run, list_workflows as wf_list,
)
from backend.memory.database import list_workflows as db_list_workflows
from backend.skills import skills_index, load_skill as sk_load, save_skill as sk_save
from backend.memory.database import recall as db_recall

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Persistent file logging — stdout (Docker logs) is preserved; we additionally
# write to /app/logs (bind-mounted to ../logs on the host so it survives rebuilds).
# Rotated backups are gzipped so old logs stay compact past a size threshold.
LOG_DIR = os.getenv("LOG_DIR", "/app/logs")
SESSION_LOG_DIR = os.path.join(LOG_DIR, "sessions")
_SESSION_LOG_GZIP_THRESHOLD = 256 * 1024  # gzip a session transcript past 256 KB


def _gzip_rotator(source: str, dest: str) -> None:
    """Rotator that gzips rotated app-log backups (savant.log.1 -> savant.log.1.gz)."""
    with open(source, "rb") as sf, gzip.open(f"{dest}.gz", "wb") as df:
        shutil.copyfileobj(sf, df)
    os.remove(source)


def _write_session_transcript(session_id, client_id: str, transcript: list[str]) -> None:
    """Persist a session's full transcript to logs/sessions/, gzipping large ones.

    Best-effort — wrapped by the caller so a write failure never breaks teardown.
    """
    if not transcript:
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sid = session_id if session_id is not None else client_id
    base = os.path.join(SESSION_LOG_DIR, f"session-{sid}-{ts}.log")
    body = "\n".join(transcript).encode("utf-8")
    if len(body) > _SESSION_LOG_GZIP_THRESHOLD:
        with gzip.open(base + ".gz", "wb") as f:
            f.write(body)
    else:
        with open(base, "wb") as f:
            f.write(body)


try:
    os.makedirs(SESSION_LOG_DIR, exist_ok=True)
    _file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "savant.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _file_handler.rotator = _gzip_rotator
    _file_handler.namer = lambda name: name  # _gzip_rotator appends the .gz suffix
    logging.getLogger().addHandler(_file_handler)
    logger.info("File logging active — %s/savant.log (10MB x5, gzip rotation)", LOG_DIR)
except Exception as _log_exc:  # never let logging setup break startup
    logger.warning("Could not set up file logging in %s: %s", LOG_DIR, _log_exc)

GEMINI_MODEL = "models/gemini-3.1-flash-live-preview"
AUDIO_MIME_TYPE = "audio/pcm;rate=16000"

# Blueprint loaded at session start — cached to avoid a Hermes call on every GoAway reconnect.
# Invalidated after any write/critical action or after 6 hours (infra state may have changed).
_blueprint_cache: str | None = None
_blueprint_cache_time: datetime | None = None
_BLUEPRINT_CACHE_TTL = timedelta(hours=6)

# Prompt-building constants now live in backend/prompts.py (imported above).
# Pause/resume phrase lists + match_phrase, _SYSTEM_BASE, _CODE_BLOCK,
# _ONBOARDING_INSTRUCTIONS, _ONBOARDING_KEYS, _SESSION_SUMMARY_INSTRUCTIONS,
# _HERMES_BLOCK, _NOTION_BLOCK, _WORKFLOWS_BLOCK, NOTION_ENABLED.

_MEMORY_TOOLS = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="save_memory",
            description="Upsert a key-value fact, organized in a namespace (e.g. infra, preferences, projects, people).",
            parameters=types.Schema(
                type="object",
                properties={
                    "key": types.Schema(type="string", description="snake_case identifier"),
                    "value": types.Schema(type="string", description="Value to store"),
                    "namespace": types.Schema(
                        type="string",
                        description="Category bucket, e.g. infra/preferences/projects/people. Defaults to general.",
                    ),
                },
                required=["key", "value"],
            ),
        ),
        types.FunctionDeclaration(
            name="recall",
            description=(
                "Retrieve relevant stored facts by meaning (ranked keyword search), optionally "
                "within a namespace. Use when the owner refers to something you may have saved before."
            ),
            parameters=types.Schema(
                type="object",
                properties={
                    "query": types.Schema(type="string", description="What you're trying to remember"),
                    "namespace": types.Schema(type="string", description="Optional namespace filter"),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_session_summary",
            description="Save 1-sentence session summary.",
            parameters=types.Schema(
                type="object",
                properties={
                    "summary": types.Schema(type="string", description="1-sentence summary"),
                },
                required=["summary"],
            ),
        ),
        types.FunctionDeclaration(
            name="execute_action",
            description="Run Notion write actions, VPS vision/scan scripts, or undo restore. NOT for direct VPS commands — use run_command chains for all VPS ops.",
            parameters=types.Schema(
                type="object",
                properties={
                    "instruction": types.Schema(type="string", description="Hermes instruction"),
                    "action_type": types.Schema(
                        type="string",
                        enum=["read", "write", "critical"],
                        description="Safety level",
                    ),
                    "description": types.Schema(type="string", description="1-line label shown to owner"),
                    "confirmed": types.Schema(
                        type="boolean",
                        description="True only after vocal confirm.",
                    ),
                },
                required=["instruction", "action_type", "description"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_command",
            description=(
                "Run a shell command DIRECTLY on the VPS and get the real output back, "
                "fast — no reasoning layer. Use for quick reads/inspection and simple ops. "
                "cwd and user persist across calls. For changes that need a backup + "
                "verification first, use execute_action instead."
            ),
            parameters=types.Schema(
                type="object",
                properties={
                    "command": types.Schema(type="string", description="Shell command to run on the VPS"),
                    "purpose": types.Schema(type="string", description="1-line reason, shown to the owner"),
                },
                required=["command"],
            ),
        ),
        types.FunctionDeclaration(
            name="search_memory",
            description="Search past memory + sessions. Use when owner asks about past.",
            parameters=types.Schema(
                type="object",
                properties={
                    "query": types.Schema(
                        type="string",
                        description="Free-text term, case-insensitive substring match",
                    ),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="show_data",
            description="Display data in owner's panel. format: report=sections+headers (default for action results), table=rows/metrics, markdown=freeform rich, list=bullets, json=payload, text=≤2 lines only.",
            parameters=types.Schema(
                type="object",
                properties={
                    "title": types.Schema(type="string", description="Panel title"),
                    "content": types.Schema(type="string", description="Content"),
                    "format": types.Schema(
                        type="string",
                        enum=["text", "table", "json", "report", "list", "markdown"],
                        description="Display format",
                    ),
                },
                required=["title", "content", "format"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_workflow",
            description=(
                "Register an n8n (or any) webhook workflow the owner gives by voice: a name, "
                "a URL, and a short description. Optional auth_header+auth_key for protected "
                "webhooks. Never read the key back aloud."
            ),
            parameters=types.Schema(
                type="object",
                properties={
                    "name": types.Schema(type="string", description="Short workflow name"),
                    "url": types.Schema(type="string", description="Webhook URL"),
                    "description": types.Schema(type="string", description="What the workflow does"),
                    "auth_header": types.Schema(type="string", description="Optional auth header name"),
                    "auth_key": types.Schema(type="string", description="Optional auth header value (secret)"),
                },
                required=["name", "url"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_workflow",
            description="Trigger a registered workflow by name and get its response back. Optional JSON payload.",
            parameters=types.Schema(
                type="object",
                properties={
                    "name": types.Schema(type="string", description="Registered workflow name"),
                    "payload": types.Schema(type="string", description="Optional JSON payload to POST"),
                },
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="list_workflows",
            description="List registered workflows (names + descriptions). Use when owner asks what workflows exist.",
            parameters=types.Schema(
                type="object",
                properties={},
            ),
        ),
        types.FunctionDeclaration(
            name="load_skill",
            description=(
                "Pull the full step-by-step procedure of a skill (from the SKILLS index) into "
                "context before doing a task it covers. Loads silently — don't read it aloud."
            ),
            parameters=types.Schema(
                type="object",
                properties={
                    "name": types.Schema(type="string", description="Skill name from the SKILLS index"),
                },
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="save_skill",
            description=(
                "Capture a reusable procedure as a new skill after solving a complex problem, "
                "so it's available next time. Give a short name, a one-line description, and the "
                "step-by-step content in markdown."
            ),
            parameters=types.Schema(
                type="object",
                properties={
                    "name": types.Schema(type="string", description="Short skill name (kebab-case)"),
                    "description": types.Schema(type="string", description="One-line summary"),
                    "content": types.Schema(type="string", description="Step-by-step procedure (markdown)"),
                },
                required=["name", "description", "content"],
            ),
        ),
    ]
)

_GOOGLE_SEARCH_TOOL = types.Tool(google_search=types.GoogleSearch())

_VPS_VISION_INSTRUCTION = "python3 /host/home/savant/scripts/vps_quick_vision.py"

# File-based VPS Vision lane: a host cron runs the scripts INSIDE the hermes-v2
# container (zero LLM tokens — the Hermes agent is never invoked) and writes the
# output to /host/home/savant/logs/vps_map/ (hermes-v2 sees this via /:/host).
# The backend reads from /app/logs/vps_map/ (already mounted via ../logs:/app/logs).
# Falls back to the Hermes path when the file is missing or stale.
_QUICK_VISION_FILE = "/app/logs/vps_map/QUICK_VISION.md"
_METRICS_FILE = "/app/logs/vps_map/METRICS.txt"
_QUICK_VISION_MAX_AGE_S = 15 * 60   # serve file if fresh, else fall back to Hermes
_METRICS_MAX_AGE_S = 60


def _read_fresh_file(path: str, max_age_s: float) -> str | None:
    """Return the file's content if it exists and is fresher than max_age_s,
    else None (→ caller falls back to Hermes). Never raises."""
    try:
        if time.time() - os.path.getmtime(path) > max_age_s:
            return None
        with open(path) as f:
            return f.read().strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LLM session summarizer (Gemini 2.0 Flash Lite via REST)
# ---------------------------------------------------------------------------

_LLM_SUMMARIZER_MODEL = "gemini-2.5-flash-lite"
_LLM_SUMMARIZER_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_LLM_SUMMARIZER_MODEL}:generateContent"
)
_LLM_SUMMARIZER_PROMPT = (
    "You are a technical memory extractor. Read this conversation and extract ONLY:\n"
    "- Technical decisions made\n"
    "- Bugs found or fixed\n"
    "- Task status (done / in progress / blocked)\n"
    "- Important infrastructure info\n"
    "- Any explicit preferences or instructions from the owner\n"
    "Max 15 bullet points. Be concise. Ignore small talk and greetings.\n"
    "Output plain bullet points only, no headers, no markdown. Use '• ' as bullet prefix.\n"
    "If nothing technical worth remembering, output a single line: NOTHING_TO_REMEMBER"
)


async def _llm_summarize_transcript(transcript: list[str]) -> str | None:
    """Summarize a session transcript via Gemini 2.0 Flash Lite REST.

    Returns the LLM summary string, or None if the call fails or the transcript
    is empty / contains nothing worth remembering. Caller decides the fallback.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not transcript:
        return None

    # Keep last 60 lines; drop micro-bursts (Gemini streams transcripts in tiny chunks).
    cleaned: list[str] = []
    for line in transcript[-60:]:
        s = (line or "").strip()
        if len(s) >= 3:
            cleaned.append(s)
    if not cleaned:
        return None

    convo = "\n".join(cleaned)
    payload = {
        "system_instruction": {"parts": [{"text": _LLM_SUMMARIZER_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": convo}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _LLM_SUMMARIZER_URL,
                headers={"x-goog-api-key": api_key},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("LLM summarizer call failed: %s", e)
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("LLM summarizer returned unexpected shape: %s", str(data)[:200])
        return None

    if not text or text.strip() == "NOTHING_TO_REMEMBER":
        return None
    return text


_LLM_FACTS_PROMPT = (
    "You extract DURABLE PERSONAL FACTS about the owner from a conversation, to store in "
    "long-term memory. Output STRICT JSON: an object of snake_case key → short value.\n"
    "Capture only durable facts: where they live (home_city), their name (owner_name only if "
    "clearly stated), preferences (preferred_editor, preferred_language), tools, ongoing "
    "projects (current_project), non-secret identifiers.\n"
    "NEVER include secrets (keys, tokens, passwords, access codes). Ignore transient chatter, "
    "VPS command output, and one-off questions.\n"
    "Max 8 keys. Each value <120 chars. If nothing durable, output exactly: {}"
)


async def _llm_extract_facts(transcript: list[str]) -> dict:
    """Extract durable personal facts as {key: value} via Gemini 2.0 Flash Lite REST.
    Best-effort: returns {} on any failure. Mirrors _llm_summarize_transcript."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not transcript:
        return {}
    cleaned = [s.strip() for s in transcript[-60:] if len((s or "").strip()) >= 3]
    if not cleaned:
        return {}
    payload = {
        "system_instruction": {"parts": [{"text": _LLM_FACTS_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": "\n".join(cleaned)}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _LLM_SUMMARIZER_URL, headers={"x-goog-api-key": api_key}, json=payload,
            )
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.warning("LLM facts extraction failed: %s", e)
        return {}
    # Strip markdown fences if the model wrapped the JSON.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        facts = json.loads(text)
    except Exception:
        return {}
    if not isinstance(facts, dict):
        return {}
    # Never overwrite onboarding/control keys; clamp sizes; drop empties.
    _protected = {"owner_name", "preferred_language", "timezone", "action_code", "paused"}
    out: dict = {}
    for k, v in list(facts.items())[:8]:
        if not isinstance(k, str) or not isinstance(v, (str, int, float)):
            continue
        key = re.sub(r"[^a-z0-9_]", "", k.strip().lower().replace(" ", "_"))[:40]
        val = str(v).strip()[:120]
        if key and val and key not in _protected:
            out[key] = val
    return out


# Strong refs to fire-and-forget background tasks so the event loop can't GC them
# mid-flight (changelog / monitoring-registry / Full Vision refresh would be lost).
_bg_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    """Create a tracked background task (kept referenced until it finishes)."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


# Count of verified WS sessions currently active. The background monitor only runs
# when this is 0 (in-session anomalies are handled live by _metrics_loop instead).
_active_sessions = 0
_BG_MONITOR_INTERVAL_S = 180.0


# F5 — Autonomous enriched monitoring. A critical alert is enriched (likely cause +
# suggested fix) by a cheap flash-lite pass and stored on the incident, then the live
# ALERTS.md is re-rendered. At login the Smart Briefing weaves open alerts (with cause/
# fix) into the greeting; resolving an incident drops it from ALERTS.md automatically.
_ALERTS_FILE = "/app/logs/vps_map/ALERTS.md"

_INCIDENT_ENRICH_PROMPT = (
    "You are a senior DevOps engineer. Given ONE VPS alert line, output STRICT JSON "
    '{"cause": "...", "fix": "..."} — cause = most likely root cause (<140 chars), '
    "fix = one concrete first action to resolve it (<140 chars). No prose, no markdown."
)


async def _enrich_incident(label: str, message: str) -> tuple[str, str]:
    """Best-effort, time-bounded enrichment of an alert via gemini-2.5-flash-lite.
    Returns (cause, fix); ('','') on any failure so the monitor never stalls."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return "", ""
    payload = {
        "system_instruction": {"parts": [{"text": _INCIDENT_ENRICH_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": f"{label}: {message}"}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 200},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _LLM_SUMMARIZER_URL, headers={"x-goog-api-key": api_key}, json=payload,
            )
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return "", ""
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
        return str(obj.get("cause", ""))[:300], str(obj.get("fix", ""))[:300]
    except Exception:
        return "", ""


def render_alerts_md() -> None:
    """Render unresolved incidents to the live ALERTS.md file (resolved ones drop out).
    Best-effort — never raises into the caller."""
    try:
        incs = get_unresolved_incidents(20)
        lines = ["# VPS ALERTS", ""]
        if not incs:
            lines.append("_No open alerts._")
        else:
            for i in incs:
                lines.append(f"## {i['type']} — {i['action_taken']}")
                lines.append(f"- When: {(i.get('date') or '')[:16].replace('T', ' ')}")
                if i.get("cause"):
                    lines.append(f"- Likely cause: {i['cause']}")
                if i.get("suggested_fix"):
                    lines.append(f"- Suggested fix: {i['suggested_fix']}")
                lines.append("")
        os.makedirs(os.path.dirname(_ALERTS_FILE), exist_ok=True)
        with open(_ALERTS_FILE, "w") as f:
            f.write("\n".join(lines))
    except Exception:
        logger.warning("render_alerts_md failed")


async def _store_enriched_incident(key: str, message: str, enrich: bool = True) -> None:
    """Enrich (optional) + persist a critical incident, then re-render ALERTS.md."""
    cause, fix = ("", "")
    if enrich:
        cause, fix = await _enrich_incident(key, message)
    save_incident(f"🔴 {key}", message, resolved=0, cause=cause, suggested_fix=fix)
    render_alerts_md()


async def _background_monitor() -> None:
    """When no session is active, poll VPS metrics every _BG_MONITOR_INTERVAL_S and
    store an incident for any CRITICAL anomaly. The incident is announced in the next
    Smart Briefing. Debounced per alert key so it can't spam the incidents table."""
    prev: dict | None = None
    last_alert: dict[str, float] = {}
    while True:
        try:
            await asyncio.sleep(_BG_MONITOR_INTERVAL_S)
            if _active_sessions > 0:
                continue  # a session is live — _metrics_loop owns alerting
            line = await _fetch_metrics_line("bgmon")
            if not line:
                continue
            cur = _parse_metrics(line)
            alerts = _evaluate_metrics_alerts(cur, prev)
            prev = cur
            now = asyncio.get_event_loop().time()
            for severity, key, message in alerts:
                if severity != "critical":
                    continue
                if now - last_alert.get(key, 0.0) < _ALERT_DEBOUNCE_S:
                    continue
                last_alert[key] = now
                try:
                    await _store_enriched_incident(key, message)
                    logger.info("Background monitor: stored incident — %s %s", key, message)
                except Exception:
                    logger.warning("Background monitor: save_incident failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Background monitor cycle error")


# Periodic security/vulnerability scan (the "analysis cron", self-contained — no host
# cron / sudo needed). Runs the scan, persists the report, and logs a CRITICAL verdict
# as an incident so SAVANT raises it in the next Smart Briefing.
_SECURITY_SCAN_INTERVAL_S = 6 * 3600


async def _security_loop() -> None:
    await asyncio.sleep(120)  # let the stack settle after boot
    while True:
        try:
            resp = await call_hermes(
                "Run: python3 /host/home/savant/scripts/vps_security_scan.py — return its output "
                "VERBATIM and also save the full output to /opt/data/vps_map/SECURITY_SCAN.md.",
                session_id="savant-security-scan", timeout=180.0,
            )
            out = resp.get("output", "")
            if resp.get("status", "ok") == "ok" and "Verdict" in out:
                vline = next((l for l in out.splitlines()
                              if any(s in l for s in ("🔴", "🟡", "🟢")) and "Verdict" not in l), "")
                if "🔴" in vline:
                    save_incident("🔴 security", vline.strip("- *#").strip()[:200]
                                  or "Critical security findings", resolved=0)
                    render_alerts_md()
                    logger.info("Security loop: critical findings stored as incident")
                else:
                    logger.info("Security loop: scan ok (%s)", vline.strip()[:60])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Security loop cycle error")
        try:
            await asyncio.sleep(_SECURITY_SCAN_INTERVAL_S)
        except asyncio.CancelledError:
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
        logger.info("Memory DB initialised")
    except Exception:
        logger.exception("Failed to initialise memory DB — continuing without it")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    app.state.gemini_client = genai.Client(api_key=api_key) if api_key else None
    if not app.state.gemini_client:
        logger.warning("GEMINI_API_KEY not set — /ws will reject connections")
    _bg_monitor_task = asyncio.create_task(_background_monitor())
    _sec_task = asyncio.create_task(_security_loop())
    try:
        yield
    finally:
        _bg_monitor_task.cancel()
        _sec_task.cancel()


app = FastAPI(title="SAVANT", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health + data endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/redis")
async def health_redis():
    r = await _get_redis()
    if r:
        await r.aclose()
        return {"redis": "ok"}
    return {"redis": "error"}


@app.get("/health/sessions")
async def sessions_endpoint():
    try:
        return {"sessions": get_recent_sessions(10)}
    except Exception as e:
        return {"sessions": [], "error": str(e)}


@app.get("/health/prompt")
async def prompt_endpoint():
    """Return the exact system prompt the backend assembles for Gemini Live.

    Reuses _make_live_config so what you see here is byte-for-byte what a fresh
    session receives (handle=None == first connect). Routed by Caddy under /health/*.
    """
    try:
        cfg = _make_live_config(None)
        text = cfg.system_instruction.parts[0].text
        return {"system_prompt": text, "length": len(text)}
    except Exception as e:
        return {"system_prompt": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Redis helper
# ---------------------------------------------------------------------------

_REDIS_HOST = os.getenv("REDIS_HOST", "redis-container")
_REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
_REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None


async def _get_redis() -> aioredis.Redis | None:
    """Return a live Redis connection, or None if Redis is unavailable."""
    try:
        r = aioredis.Redis(
            host=_REDIS_HOST, port=_REDIS_PORT, password=_REDIS_PASSWORD,
            decode_responses=True, socket_connect_timeout=2,
        )
        await r.ping()
        return r
    except Exception:
        return None


@asynccontextmanager
async def _redis():
    """Connection that is ALWAYS closed, even if the body raises — no leaks on
    exception paths. Yields None when Redis is unavailable."""
    r = await _get_redis()
    try:
        yield r
    finally:
        if r is not None:
            try:
                await r.aclose()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Control endpoints  (/control/* — proxied by Caddy to savant-backend:8000)
# These mutate session state (pause / stop), so they require the shared secret —
# the PWA drives pause/stop over the authenticated WS, not these admin routes.
# ---------------------------------------------------------------------------

_CONTROL_SECRET = os.getenv("HERMES_WEBHOOK_SECRET", "")


def _require_control_auth(authorization: str | None) -> None:
    """Raise 401 unless the Authorization header carries the shared control secret."""
    expected = f"Bearer {_CONTROL_SECRET}" if _CONTROL_SECRET else None
    if not expected or authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/control/stop")
async def control_stop(authorization: str | None = Header(None)):
    _require_control_auth(authorization)
    r = await _get_redis()
    if r:
        await r.set("savant:stop_flag", "1", ex=60)
        await r.aclose()
        return {"status": "ok"}
    return {"status": "redis_unavailable"}


@app.get("/control/status")
async def control_status():
    r = await _get_redis()
    if r:
        task = await r.get("savant:current_task")
        paused = await r.get("savant:paused")
        await r.aclose()
        return {"running": task is not None, "task": task, "paused": paused == "1"}
    return {"running": False, "task": None, "paused": False, "error": "redis_unavailable"}


@app.post("/control/pause")
async def control_pause(authorization: str | None = Header(None)):
    _require_control_auth(authorization)
    r = await _get_redis()
    if r:
        await r.set("savant:paused", "1")
        await r.aclose()
        return {"status": "paused"}
    return {"status": "redis_unavailable"}


@app.post("/control/resume")
async def control_resume(authorization: str | None = Header(None)):
    _require_control_auth(authorization)
    r = await _get_redis()
    if r:
        await r.delete("savant:paused")
        await r.aclose()
        return {"status": "resumed"}
    return {"status": "redis_unavailable"}


# ---------------------------------------------------------------------------
# WebSocket — Gemini Live bridge
# ---------------------------------------------------------------------------

# BCP-47 lookup for language names stored in DB (e.g. "French" → "fr-FR")
_LANGUAGE_CODE_MAP = {
    "french": "fr-FR", "français": "fr-FR", "francais": "fr-FR",
    "english": "en-US", "anglais": "en-US",
    "arabic": "ar-SA", "arabe": "ar-SA", "العربية": "ar-SA",
    "darija": "ar-MA", "moroccan arabic": "ar-MA",
    "spanish": "es-ES", "espagnol": "es-ES",
    "german": "de-DE", "allemand": "de-DE",
    "italian": "it-IT", "italien": "it-IT",
    "portuguese": "pt-PT", "portugais": "pt-PT",
}

_BASE_LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
        )
    ),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    # Server-side sliding compression — this is what keeps long sessions alive past
    # the token limit (no hard stop). target_tokens makes the budget explicit.
    context_window_compression=types.ContextWindowCompressionConfig(
        sliding_window=types.SlidingWindow(target_tokens=16000),
    ),
    # thinking_level MEDIUM — LOW made the model VERBALIZE tool calls (spoke
    # "execute_action(...)" aloud instead of calling it, so the action never ran) and
    # destabilised the voice. MEDIUM keeps tool-calling reliable; VAD 800ms keeps it snappy.
    thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
    generation_config=types.GenerationConfig(temperature=0.25),
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            # 800ms — snappy but stable; too low (700) caused premature end-of-turn that
            # made SAVANT restart/repeat. END_SENSITIVITY_LOW protects mid-sentence pauses.
            silence_duration_ms=800,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
        )
    ),
)


def _make_live_config(handle: str | None) -> types.LiveConnectConfig:
    try:
        first_run = is_first_run()
    except Exception:
        first_run = False

    try:
        action_code_exists = get_memory_value("action_code") is not None
    except Exception:
        action_code_exists = False

    try:
        memory_block = load_memory()
    except Exception:
        logger.warning("Could not load memory — using base system prompt only")
        memory_block = ""

    # Dynamic language code from DB — affects ASR transcription accuracy
    try:
        lang_raw = get_memory_value("preferred_language") or ""
        lang_code = _LANGUAGE_CODE_MAP.get(lang_raw.lower().strip(), "en-US")
    except Exception:
        lang_code = "en-US"

    parts = [_SYSTEM_BASE]
    ask = "ask owner to SET a code" if not action_code_exists else "ask owner for the code"
    parts.append(_CODE_BLOCK.format(ask=ask))
    if first_run:
        parts.append(_ONBOARDING_INSTRUCTIONS)
        # Resume support — tell SAVANT which keys already exist and which to ask next
        try:
            status = get_onboarding_status()
            if status["collected"]:
                collected_lines = [f"- {k}: {v}" for k, v in status["collected"].items()]
                missing = status["missing"]
                next_key = missing[0] if missing else None
                state_block = (
                    "## ONBOARDING STATE (resume)\n"
                    "Already saved:\n" + "\n".join(collected_lines) + "\n"
                    f"Missing: {', '.join(missing) if missing else '(none)'}\n"
                    + (f"Next to ask: {next_key}.\n" if next_key else "")
                    + "Do NOT re-ask saved keys. Resume from the next missing one only."
                )
                parts.append(state_block)
        except Exception:
            logger.warning("Could not compute onboarding status — using default flow")
    parts.append(_HERMES_BLOCK)
    # Workflow registry block — only when at least one workflow exists (token thrift).
    try:
        if db_list_workflows():
            parts.append(_WORKFLOWS_BLOCK)
    except Exception:
        pass
    # Skills index (names + descriptions only) — only when skills exist (token thrift).
    try:
        _skidx = skills_index()
        if _skidx:
            parts.append(_skidx)
    except Exception:
        pass
    if NOTION_ENABLED:
        parts.append(_NOTION_BLOCK)
    parts.append(_SESSION_SUMMARY_INSTRUCTIONS)
    # Adaptive tone (C6) — reflects the latest VPS health sampled by the metrics loop.
    try:
        if get_memory_value("vps_state") == "stress":
            parts.append(
                "## TONE\nVPS under stress — be short, urgent and focused; lead with what matters."
            )
        else:
            parts.append("## TONE\nVPS healthy — relaxed, natural, conversational.")
    except Exception:
        pass
    if memory_block:
        parts.append(memory_block)
    full_instruction = "\n\n".join(p for p in parts if p)

    return _BASE_LIVE_CONFIG.model_copy(
        update={
            "system_instruction": types.Content(
                parts=[types.Part(text=full_instruction)]
            ),
            "session_resumption": types.SessionResumptionConfig(handle=handle),
            "tools": [_MEMORY_TOOLS, _GOOGLE_SEARCH_TOOL],
            "speech_config": types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
                ),
                language_code=lang_code,
            ),
        }
    )


async def _inject_with_retry(session_ref: list, result_text: str, max_retries: int = 2) -> bool:
    # Retry ONLY on a closed/stale-session error (the GoAway reconnect swap window).
    # A send that raised for any OTHER reason may have already been delivered — blindly
    # retrying it would make SAVANT say the same line twice (the repetition bug). So a
    # non-connection error returns immediately without re-sending. session_ref[0] is read
    # fresh each attempt, so a short wait lets the reconnect loop install the new session.
    for attempt in range(max_retries):
        sess = session_ref[0]
        if sess is None:
            await asyncio.sleep(0.3)
            continue
        try:
            await sess.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=result_text)])],
                turn_complete=True,
            )
            return True
        except Exception as e:
            _msg = f"{type(e).__name__}: {e}".lower()
            _retryable = any(k in _msg for k in (
                "clos", "connect", "stale", "websocket", "eof", "not open",
            ))
            logger.warning("Inject attempt %d failed (%sretryable): %s",
                           attempt + 1, "" if _retryable else "non-", e)
            if _retryable and attempt < max_retries - 1:
                await asyncio.sleep(0.3)
                continue
            return False
    return False


# Affirmation tokens — gate write/critical execution on a REAL owner "yes" after the ask.
# Includes ASR-truncated / colloquial variants ("oi", "ui", "wai") because the speech
# recognizer regularly clips "oui" — a missed affirmation silently blocks the action.
_AFFIRM_PHRASES = [
    "oui", "ouais", "ouaip", "oui oui", "yes", "yep", "yeah", "yup", "ok", "okay",
    "d'accord", "daccord", "confirme", "confirmé", "je confirme", "vas-y", "vas y", "vasy",
    "fais-le", "fais le", "fais", "go", "c'est bon", "cest bon", "c'est parti", "parfait",
    "exact", "exactement", "tout à fait", "tout a fait", "carrément", "bien sûr", "bien sur",
    "oi", "ui", "ouai", "ouias", "wai", "voilà", "voila", "c'est ça", "cest ca",
    "do it", "yes please", "let's go", "lets go", "let's do it", "lets do it",
]

# Negation / refusal tokens — a write/critical confirm is NEVER honoured if the owner
# negated AFTER the ask (blocks an accidental fire when the owner said no). Kept distinct
# from stop phrases: this only gates the confirmation, it does not cancel a running action.
_NEGATION_PHRASES = [
    "non", "nan", "non non", "laisse tomber", "laisse béton", "laisse beton",
    "annule", "annuler", "oublie", "surtout pas", "pas ça", "pas ca",
    "arrête", "arrete", "stop", "n'importe quoi", "nimporte quoi",
]

# Spoken stop — owner verbally aborts a running action ("arrête", "annule"…). Matched
# ONLY while an action is in flight/queued, so over-matching is harmless (worst case it
# cancels something the owner was already abandoning). Mirrors the pause/resume trade-off.
_STOP_PHRASES = [
    "arrête", "arrete", "arrête l'action", "arrete l'action", "stop", "stop l'action",
    "annule", "annule l'action", "annuler", "laisse tomber", "laisse béton", "laisse beton",
    "oublie", "oublie ça", "oublie ca", "cancel", "abort",
]

# Idempotence window: an identical (action_type + normalized instruction) enqueued
# within this many seconds is treated as a duplicate re-emission and ignored. This is
# the safety net that kills the action-repetition loop even if Gemini re-issues a call.
_JOB_DEDUP_TTL_S = 12.0

# Suffix appended to every reconciler injection — forbids Gemini from re-calling an
# action already running/done, which is what reopened the turn loop in the first place.
_RECONCILER_GUARD = (
    "\n\n[Status updates only — acknowledge ONCE, briefly. Never repeat a sentence you "
    "already said. Do NOT re-call any action already running or completed.]"
)

_VPS_METRICS_INSTRUCTION = (
    "Run: python3 /host/home/savant/scripts/vps_metrics.py\n"
    "Return the script stdout VERBATIM as the 'output' field. "
    "DO NOT rephrase, DO NOT reformat, DO NOT add spaces around '|'. "
    "The output is a single line like: "
    "CPU:0%|LOAD:0.04|CORES:2|RAM:3.2/7.8GB(42%)|DISK:32/96GB(34%)|CONT:7r/0s|UPTIME:24d1h"
)
_METRICS_POLL_INTERVAL_S = 20.0
_METRICS_HERMES_TIMEOUT_S = 25.0

# Proactive-alert thresholds and behaviour. warning = visual only; critical = ONE terse
# vocal interruption + stored incident. Debounced so the same alert can't spam.
_ALERT_WARN_PCT = 80
_ALERT_CRIT_PCT = 90
_ALERT_DEBOUNCE_S = 300.0  # 5 min between repeats of the same alert key


def _parse_metrics(line: str) -> dict:
    """Extract numeric fields from a canonical metrics line. Missing fields are omitted.
    Line: CPU:0%|LOAD:0.04|CORES:2|RAM:3.2/7.8GB(42%)|DISK:32/96GB(34%)|CONT:7r/0s|UPTIME:24d1h"""
    out: dict = {}
    try:
        m = re.search(r"CPU:(\d+)%", line)
        if m:
            out["cpu"] = int(m.group(1))
        m = re.search(r"RAM:[\d.]+/[\d.]+GB\((\d+)%\)", line)
        if m:
            out["ram"] = int(m.group(1))
        m = re.search(r"DISK:[\d.]+/[\d.]+GB\((\d+)%\)", line)
        if m:
            out["disk"] = int(m.group(1))
        m = re.search(r"CONT:(\d+)r/(\d+)s", line)
        if m:
            out["cont_running"] = int(m.group(1))
            out["cont_stopped"] = int(m.group(2))
    except Exception:
        pass
    return out


def _evaluate_metrics_alerts(cur: dict, prev: dict | None) -> list[tuple]:
    """Return a list of (severity, key, message) where severity is 'critical'|'warning'.
    A container that newly stopped is always critical. RAM/DISK/CPU use percent bands."""
    alerts: list[tuple] = []
    for field, label in (("ram", "RAM"), ("disk", "Disque")):
        v = cur.get(field)
        if v is None:
            continue
        if v >= _ALERT_CRIT_PCT:
            alerts.append(("critical", field, f"{label} à {v}%"))
        elif v >= _ALERT_WARN_PCT:
            alerts.append(("warning", field, f"{label} à {v}%"))
    cpu = cur.get("cpu")
    if cpu is not None and cpu >= 95:
        alerts.append(("warning", "cpu", f"CPU à {cpu}%"))
    # A container went down since the last sample → critical.
    if prev is not None:
        cur_s = cur.get("cont_stopped")
        prev_s = prev.get("cont_stopped")
        if cur_s is not None and prev_s is not None and cur_s > prev_s:
            alerts.append(("critical", "container", f"{cur_s - prev_s} conteneur(s) arrêté(s)"))
    return alerts


def _normalize_metrics_line(raw: str) -> str | None:
    """Recover the canonical metrics line from Hermes output, which may add
    spaces around '|' or paraphrase 'CONT:7r/0s' as '7 running, 0 stopped'.
    Returns None if no CPU: token is found."""
    if not raw:
        return None
    # Try each line — Hermes sometimes wraps with prose
    for cand in raw.splitlines():
        s = cand.strip()
        if "CPU:" in s and "|" in s:
            # Strip spaces around '|'
            s = re.sub(r"\s*\|\s*", "|", s)
            # CONT paraphrase: "7 running, 0 stopped" → "7r/0s"
            s = re.sub(
                r"CONT:(\d+)\s*running\s*,\s*(\d+)\s*stopped",
                lambda m: f"CONT:{m.group(1)}r/{m.group(2)}s",
                s, flags=re.IGNORECASE,
            )
            # RAM paraphrase: "3.2/7.8GB (42%)" → "3.2/7.8GB(42%)"
            s = re.sub(r"GB\s*\(", "GB(", s)
            return s
    return None


async def _fetch_metrics_line(client_id: str) -> str | None:
    # FAST FILE lane: a host cron loop keeps METRICS.txt fresh (~20s) by running
    # vps_metrics.py inside hermes-v2 — zero LLM tokens. Read it directly.
    # If stale/missing: return None so the PWA shows the stale badge.
    # No Hermes fallback — it would overlap with the cron and cause CPU spikes.
    _mfile = _read_fresh_file(_METRICS_FILE, _METRICS_MAX_AGE_S)
    if _mfile:
        return _normalize_metrics_line(_mfile)
    return None


async def _metrics_loop(
    websocket, client_id: str, session_verified: asyncio.Event,
    inject_queue: asyncio.Queue,
) -> None:
    """Backend-driven metrics push + proactive alerting. Fires once immediately after
    verification, then every _METRICS_POLL_INTERVAL_S until WS disconnects.
    - Always pushes metrics_update (chips).
    - warning anomaly → {type:alert, level:warning} (visual only, no voice).
    - critical anomaly → visual alert + ONE terse vocal interruption via the reconciler
      + stored incident. Debounced per alert key. Adaptive tone via vps_state memory."""
    await session_verified.wait()
    logger.info("Metrics loop started — client=%s interval=%ss", client_id, _METRICS_POLL_INTERVAL_S)
    prev: dict | None = None
    last_alert: dict[str, float] = {}
    last_state: str | None = None
    while True:
        try:
            line = await _fetch_metrics_line(client_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            line = None
        if line:
            try:
                await websocket.send_text(json.dumps({"type": "metrics_update", "line": line}))
            except Exception:
                # WS closed — loop will exit naturally on next iteration
                return
            # ── Anomaly detection ───────────────────────────────────────────
            try:
                cur = _parse_metrics(line)
                alerts = _evaluate_metrics_alerts(cur, prev)
                prev = cur
                now = asyncio.get_event_loop().time()
                worst = "ok"
                for severity, key, message in alerts:
                    worst = "stress"
                    # Debounce repeats of the same alert key.
                    if now - last_alert.get(key, 0.0) < _ALERT_DEBOUNCE_S:
                        continue
                    last_alert[key] = now
                    icon = "🔴" if severity == "critical" else "⚠️"
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "alert", "level": severity,
                            "message": f"{icon} {message}", "key": key,
                        }))
                    except Exception:
                        pass
                    if severity == "critical":
                        # ONE terse vocal interruption — no blabla — via the reconciler.
                        inject_queue.put_nowait(
                            f"[ALERTE CRITIQUE] {message}. "
                            "Interromps l'utilisateur en UNE phrase courte et urgente, sans blabla."
                        )
                        try:
                            # Enrich + persist + re-render ALERTS.md off the loop so the
                            # ~10s enrichment never delays the next metrics poll.
                            _spawn_bg(_store_enriched_incident(key, message))
                        except Exception:
                            logger.warning("save_incident failed — client=%s", client_id)
                # ── Adaptive tone (C6): persist vps_state only on change ─────
                state = "stress" if worst == "stress" else "ok"
                if state != last_state:
                    last_state = state
                    try:
                        save_memory("vps_state", state)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Metrics anomaly check failed — client=%s", client_id)
        else:
            logger.debug("Metrics poll skipped (no valid line) — client=%s", client_id)
        try:
            await asyncio.sleep(_METRICS_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise


def _looks_structured(text: str) -> bool:
    """Heuristic: is this Hermes output worth auto-displaying in the DATA panel?
    True for JSON, Markdown tables/headers/code fences, or multi-item lists."""
    if not text:
        return False
    s = text.strip()
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        return True
    lines = [l for l in s.splitlines() if l.strip()]
    if len(lines) >= 6:
        return True
    # Markdown table: a row with >=2 pipes plus a separator row (---|---).
    if any(l.count("|") >= 2 for l in lines) and any(
        "-" in l and set(l.strip()) <= set("|-: ") for l in lines
    ):
        return True
    if any(l.lstrip().startswith("#") for l in lines) or "```" in s:
        return True
    bullets = sum(1 for l in lines if re.match(r"^\s*([-*•]|\d+[.)])\s+", l))
    return bullets >= 3


def _guess_format(text: str) -> str:
    """Pick the PWA show_data format for an auto-displayed structured result."""
    s = (text or "").strip()
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            json.loads(s)
            return "json"
        except Exception:
            pass
    return "markdown"


_ONBOARDING_ORDER = ["owner_name", "preferred_language", "timezone"]

# Tool names that Gemini sometimes vocalizes despite prompt instructions.
_TOOL_CALL_PREFIXES = (
    "execute_action(", "save_memory(", "save_session_summary(",
    "search_memory(", "show_data(",
    "run_command(", "run_workflow(", "save_workflow(", "list_workflows(",
    "load_skill(", "save_skill(", "recall(",
)


def _strip_tool_calls(text: str) -> str:
    """Truncate vocalized tool call syntax from Gemini output transcription.

    Gemini occasionally speaks tool-call parameters aloud even with the
    CALL TOOLS SILENTLY prompt rule. This strips everything from the first
    tool name onwards so only the natural-language part reaches the PWA/logs.
    """
    low = text.lower()
    for prefix in _TOOL_CALL_PREFIXES:
        idx = low.find(prefix)
        if idx != -1:
            return text[:idx].rstrip()
    return text


async def _send_onboarding_progress(websocket) -> None:
    """Push the onboarding step state to the PWA so the progress bar stays in sync
    with the server-driven voice flow. Best-effort — never raises."""
    try:
        st = get_onboarding_status()
        collected = st["collected"]
        missing = st["missing"]
        await websocket.send_text(json.dumps({
            "type": "onboarding",
            "total": len(_ONBOARDING_ORDER),
            "step": len(collected),
            "collected": [k for k in _ONBOARDING_ORDER if k in collected],
            "current": missing[0] if missing else None,
            "done": not missing,
        }))
    except Exception:
        pass


# F1 — Direct VPS execution (full-shell fast lane).
# Voice Gemini runs simple shell commands directly via the hermes-v2 exec server
# (:8644, nsenter, per-session cwd/user) — no Hermes LLM layer, ~instant. Only truly
# catastrophic patterns are blocked; everything else runs (owner trusts it). Ops that
# need backup + VERIFICATION still go through execute_action → Hermes.
_CATASTROPHIC_PATTERNS = (
    "rm -rf /", "rm -fr /", "mkfs", "wipefs", "dd if=", "dd of=/dev/",
    "> /dev/sd", "> /dev/nvme", "> /dev/vd", "fork bomb",
    "shutdown", "reboot", "halt", "poweroff",
)


def _is_catastrophic(cmd: str) -> bool:
    """True for a small, explicit set of irrecoverable/host-killing patterns."""
    low = " " + " ".join(cmd.strip().lower().split()) + " "
    despaced = low.replace(" ", "")
    if ":(){:|:&};:" in despaced:  # classic fork bomb, any spacing
        return True
    return any(p in low for p in _CATASTROPHIC_PATTERNS)


async def _run_command_bg(
    websocket, client_id: str, command: str, inject_queue: asyncio.Queue,
) -> None:
    """Execute a shell command directly on the VPS via the :8644 exec server, push the
    result to the PWA, and hand a human-facing summary to the reconciler (idle-gated, so
    it never cuts SAVANT mid-sentence). Fast lane — no Hermes, no backup/verify."""
    from backend.console import _exec_vps  # reuse the console exec client (lazy import)
    t0 = time.monotonic()
    res = await _exec_vps(command, f"voice-{client_id}")
    dur = time.monotonic() - t0
    out = res.get("stdout") or ""
    err = res.get("stderr") or ""
    code = res.get("code", 0)
    try:
        await websocket.send_text(json.dumps({
            "type": "command_result", "command": command,
            "stdout": out, "stderr": err, "code": code,
            "cwd": res.get("cwd", "/"), "user": res.get("user", "root"),
        }))
    except Exception:
        pass
    try:
        save_action_log(uuid.uuid4().hex[:12], "command", command,
                        "ok" if code == 0 else "error", round(dur, 1), 0)
    except Exception:
        pass
    body = (out or err or "(no output)").strip()[:1500]
    inject_queue.put_nowait(
        f"[COMMAND OUTPUT — `{command}` exit={code} — source of truth, "
        f"summarize briefly in the owner's language]\n{body}"
    )


async def _run_workflow_bg(
    websocket, name: str, payload, inject_queue: asyncio.Queue,
) -> None:
    """Trigger a registered webhook directly (no Hermes), push the result to the panel,
    and hand a one-line summary to the reconciler (idle-gated)."""
    res = await wf_run(name, payload)
    out = res.get("output", "") or ""
    _is_complex = (
        out.strip()[:1] in ("{", "[")   # JSON
        or "|" in out                    # markdown table
        or "\n" in out.strip()           # multi-line
        or len(out) > 200                # long text
    )
    try:
        _fmt = "json" if out.strip()[:1] in ("{", "[") else ("table" if "|" in out else ("markdown" if len(out.splitlines()) > 3 else "text"))
        await websocket.send_text(json.dumps({
            "type": "show_data", "title": f"Workflow · {name}",
            "content": out, "format": _fmt,
        }))
    except Exception:
        pass
    if _is_complex:
        inject_queue.put_nowait(
            f"[WORKFLOW RESULT — {name} — {res.get('status')} — full output shown in panel. "
            f"Give a SHORT 1-2 sentence vocal summary/interpretation — never recite raw data]\n"
            f"{out[:800]}"
        )
    else:
        inject_queue.put_nowait(
            f"[WORKFLOW RESULT — {name} — {res.get('status')} — source of truth, "
            f"summarize in one short line]\n{out[:1500]}"
        )


async def _run_hermes_bg(
    session_ref: list, websocket, action_id: str, description: str,
    instruction: str, action_type: str, client_id: str, gemini_idle: asyncio.Event,
    inject_queue: asyncio.Queue,
) -> None:
    """Run a Hermes call in background; hand the result to the reconciler when done.
    All Gemini injections go through inject_queue (a single serialized reconciler gates
    them on gemini_idle and forbids action re-emission), so background tasks never cut
    SAVANT off mid-sentence and never reopen a turn that re-triggers the same call.
    Checks Redis STOP_FLAG before and after the call; tracks CURRENT_TASK in Redis.
    For write/critical actions: runs backup-scope skill first, then the actual command."""

    async def _inject_cancelled(reason: str) -> None:
        try:
            inject_queue.put_nowait(f"[ACTION CANCELLED] {description}: {reason}")
        except Exception:
            pass

    # Part 3.2: time the action for the adaptive action_log, and default the verified
    # flag. final_status/_verified are finalised below and persisted in the finally.
    t_start = time.monotonic()
    final_status = "error"
    _verified = False
    _is_gateway_error = False  # 502/503/504 from Hermes — action may have run but response lost

    # Check STOP_FLAG before starting; set CURRENT_TASK in Redis (connection always closed)
    async with _redis() as r0:
        if r0:
            stop = await r0.get("savant:stop_flag")
            if stop:
                logger.info("Hermes task cancelled by STOP_FLAG before start — action_id=%s", action_id)
                await _inject_cancelled("action stopped before execution.")
                return
            await r0.set("savant:current_task", description[:120], ex=300)

    # Run backup-scope skill before any write or critical action
    if action_type in ("write", "critical"):
        # Part 3.2 — Phase 1 narration: tell SAVANT to say it secures first (real phase,
        # the backup call below actually runs). Phase, never an outcome → anti-spec safe.
        try:
            inject_queue.put_nowait(
                "[INTERNAL] Action sensible en cours. Dis en 1 courte phrase naturelle que "
                "tu sécurises d'abord (sauvegarde) avant d'agir. N'annonce aucun résultat."
            )
        except Exception:
            pass
        # Self-contained backup (the backup-scope skill may be absent): identify the
        # files this action will change and zip them BEFORE changing anything.
        # CRITICAL: write to the HOST path via the /host mount so backups PERSIST
        # (the container-local /home/savant/backups is ephemeral and lost on rebuild).
        backup_instruction = (
            "Before the following change, create a safety backup. Identify the files/dirs it will "
            "affect and zip them to /host/home/savant/backups/hermes/ with a timestamped name "
            f"(create it first: mkdir -p /host/home/savant/backups/hermes/).\n{instruction}\n"
            "If the action targets a user/group/account (useradd, userdel, passwd, etc.) and has no "
            "obvious file target, back up /host/etc/passwd, /host/etc/shadow and /host/etc/group. "
            "Use zip or `tar czf`. The archive MUST end up under /host/home/savant/backups/hermes/. "
            "Return ONLY the backup archive path and the list of files included."
        )
        try:
            async def _backup_chunk(text: str) -> None:
                try:
                    await websocket.send_text(json.dumps({
                        "type": "hermes_step",
                        "action_id": f"{action_id}-bak",
                        "text": text,
                    }))
                except Exception:
                    pass

            backup_resp = await call_hermes(
                backup_instruction,
                session_id=f"savant-{client_id}-{action_id}-bak",
                on_chunk=_backup_chunk,
            )
            backup_output = backup_resp.get("output", "")
            backup_ok = backup_resp.get("status", "ok") == "ok"
            if backup_ok and backup_output:
                # Extract backup path from output (look for the zip path pattern)
                backup_path = None
                for token in backup_output.split():
                    if "/home/savant/backups/hermes/" in token and ".zip" in token:
                        backup_path = token.strip('",\'"')
                        break
                async with _redis() as rb:
                    if rb:
                        await rb.set("savant:last_backup", backup_path or "backup_created", ex=86400)
                logger.info("Backup completed before action — action_id=%s path=%s", action_id, backup_path)
            else:
                logger.warning("Backup returned non-ok status — action_id=%s output=%s", action_id, backup_output[:100])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Backup call failed — continuing with action — action_id=%s", action_id)

    result_output = "Action failed."
    success = False
    _il = instruction.lower()
    # UNDO: restore the auto-backup taken before the last write/critical. The backup path
    # lives in Redis (savant:last_backup) — Hermes only gets a concrete restore instruction.
    _is_undo = any(s in _il for s in (
        "restore last backup", "annuler la dernière action", "annule la dernière action",
        "undo last", "undo the last", "reviens en arrière",
    ))

    # Language prefix — read once from DB, prepended to all Hermes instructions so
    # the LLM formats its output in the owner's preferred language.
    try:
        _lang = get_memory_value("preferred_language") or ""
    except Exception:
        _lang = ""
    _lang_prefix = (
        f"[LANG: {_lang}] Respond and format all output in {_lang}.\n\n" if _lang else ""
    )

    # Tell the executor its output is rendered in a UI panel, so structured results
    # come back as clean Markdown (tables/lists/headers). Skip vision scripts (already
    # Markdown) to avoid double-wrapping their own report.
    if _is_undo:
        _bpath = None
        async with _redis() as _ru:
            if _ru:
                _bpath = await _ru.get("savant:last_backup")
        if _bpath and _bpath not in ("backup_created", ""):
            _hermes_instruction = _lang_prefix + (
                f"Undo the last change by restoring this backup archive: {_bpath}. "
                "Extract it back to the original locations (the archive preserves the file paths under "
                "/host…). Then VERIFY the restore took effect and report what was restored."
            )
        else:
            _hermes_instruction = _lang_prefix + (
                "There is no recorded backup to undo the last action. Do NOT change anything; "
                "reply that you cannot undo (no backup available). VERIFICATION: NOT_VERIFIED — no backup."
            )
    elif "vps_quick_vision" in instruction or "vps_full_vision" in instruction:
        _hermes_instruction = _lang_prefix + instruction
    elif "notion" in instruction.lower():
        # Notion — make search forgiving: voice mishears names, so query broadly and
        # surface the closest matches instead of failing on an exact-match miss.
        _hermes_instruction = _lang_prefix + (
            f"{instruction}\n\n"
            "When searching Notion: use the Notion search tool with the main distinctive KEYWORD "
            "(not the exact phrase), do a fuzzy/partial title match, and return the closest page "
            "titles. If nothing matches the exact term, return the nearest titles you DID find so "
            "the caller can suggest one — only report a truly empty search when the tool returns 0 results. "
            "Format any structured output as clean Markdown for the UI panel."
        )
    else:
        # Part 3.2 — adaptive memory: prepend a short [CONTEXT] block of the last few
        # actions so Hermes adapts instead of re-inventing. ONLY on the generic task
        # branch — vision/notion/undo stay clean (deterministic instructions).
        _ctx = get_recent_action_context(limit=3)
        _hermes_instruction = _lang_prefix + (
            (f"{_ctx}\n\n" if _ctx else "")
            + f"{instruction}\n\n"
            "Act as a senior DevOps engineer: check current state if relevant, use professional "
            "commands and flags (prefer reload over restart, validate config before applying, "
            "verify outcome with real command output). "
            "Format structured output as clean Markdown for the UI panel. Keep prose short."
        )
    # Verify-after-write: state-changing actions must prove they took effect. The
    # executor performs the change, then re-reads the real state and ends with a
    # VERIFICATION marker line. We trust the marker, not the LLM's self-report.
    if action_type in ("write", "critical"):
        _hermes_instruction += (
            "\n\n[VERIFY — MANDATORY] This is a state-changing action. After performing it, "
            "VERIFY the new state with an explicit command and include that command's real output "
            "as evidence. End your reply with EXACTLY ONE line, either:\n"
            "'VERIFICATION: VERIFIED' (the change is confirmed in the real system), or\n"
            "'VERIFICATION: NOT_VERIFIED — <short reason>' (could not confirm / failed).\n"
            "Never write VERIFIED without evidence. Example: after `userdel x`, run "
            "`getent passwd x` and require empty output before VERIFIED."
        )
    # Notion FAST READ lane: read-only Notion goes straight to the Notion REST API
    # (~1-3s) instead of the slow Hermes+MCP loop (~1-2min). None → fall back to Hermes.
    _used_fast_notion = False
    if action_type == "read" and notion_fast_enabled() and "notion" in instruction.lower():
        _fast = await notion_read(instruction)
        if _fast is not None:
            result_output = _fast.get("output", "")
            success = _fast.get("status", "ok") == "ok"
            _used_fast_notion = True
            logger.info("Notion fast-read used — action_id=%s", action_id)
    # Quick Vision FAST FILE lane: a host cron keeps QUICK_VISION.md fresh (written by
    # the script running inside hermes-v2, zero LLM tokens). Serve it directly instead
    # of asking the Hermes agent to run the script. Stale/missing → fall back to Hermes.
    _used_fast_file = False
    if not _used_fast_notion and "vps_quick_vision" in instruction:
        _vfile = _read_fresh_file(_QUICK_VISION_FILE, _QUICK_VISION_MAX_AGE_S)
        if _vfile:
            result_output = _vfile
            success = True
            _used_fast_file = True
            logger.info("Quick Vision fast-file used — action_id=%s", action_id)
    try:
        if _used_fast_notion or _used_fast_file:
            pass  # result already obtained via direct Notion API / cron file
        else:
            if action_type in ("write", "critical"):
                # Part 3.2 — Phase 2 narration: backup done, launching the real action
                # now (real phase — the call below runs). Phase, never an outcome.
                try:
                    inject_queue.put_nowait(
                        "[INTERNAL] Sauvegarde prête. Dis en 1 courte phrase naturelle que tu "
                        "lances maintenant l'action. N'annonce aucun résultat."
                    )
                except Exception:
                    pass
            async def _main_chunk(text: str) -> None:
                try:
                    await websocket.send_text(json.dumps({
                        "type": "hermes_step",
                        "action_id": action_id,
                        "text": text,
                    }))
                except Exception:
                    pass

            resp = await call_hermes(
                _hermes_instruction,
                session_id=f"savant-{client_id}-{action_id}",
                on_chunk=_main_chunk,
            )
            result_output = resp.get("output", "")
            success = resp.get("status", "ok") == "ok"
            if not success:
                _low = result_output.lower()
                _is_gateway_error = (
                    "502" in result_output or "bad gateway" in _low or
                    "503" in result_output or "504" in result_output or
                    "service unavailable" in _low
                )
        # For state-changing actions, success requires a verified marker — this kills the
        # "Hermes says deleted but the user still exists" class of bug.
        if action_type in ("write", "critical"):
            _vlines = [l for l in result_output.splitlines()
                       if l.strip().upper().startswith("VERIFICATION:")]
            # Strict: the value after 'VERIFICATION:' must be EXACTLY 'VERIFIED' (first token),
            # not just contain the word — a chatty 'VERIFICATION: … VERIFIED …' is NOT a pass.
            def _verif_token(line: str) -> str:
                _after = line.split(":", 1)[1].strip().upper() if ":" in line else ""
                return re.split(r"[\s—\-:]+", _after, 1)[0] if _after else ""
            _verified = any(_verif_token(l) == "VERIFIED" for l in _vlines)
            _body_wo = "\n".join(
                l for l in result_output.splitlines()
                if not l.strip().upper().startswith("VERIFICATION:")
            ).strip()
            if _verified and success:
                result_output = _body_wo
            else:
                success = False
                _reason = "no verification evidence returned"
                for l in _vlines:
                    _idx = l.upper().find("NOT_VERIFIED")
                    if _idx != -1:
                        _reason = l[_idx + len("NOT_VERIFIED"):].lstrip(" —-:").strip() or _reason
                result_output = (
                    (_body_wo + "\n\n" if _body_wo else "") + f"NOT VERIFIED — {_reason}"
                ).strip()
            logger.info(
                "Verify-after-write: action_id=%s verified=%s", action_id, _verified,
            )
    except asyncio.CancelledError:
        logger.info("Hermes task cancelled — action_id=%s", action_id)
        final_status = "cancelled"
        raise
    except Exception:
        logger.exception("Hermes call failed — action_id=%s client=%s", action_id, client_id)
    finally:
        async with _redis() as r1:
            if r1:
                await r1.delete("savant:current_task")
        # Part 3.2 — persist to the adaptive action_log (best-effort; a logging
        # failure must never break the action). Runs once per action that started.
        if final_status != "cancelled":
            final_status = "ok" if success else "error"
        try:
            save_action_log(
                action_id, action_type, description, final_status,
                time.monotonic() - t_start, int(_verified),
            )
        except Exception:
            logger.warning("save_action_log failed — action_id=%s", action_id)

    # Cache successful blueprint loads so GoAway reconnects skip the Hermes call.
    if success and result_output and (
        "vps_quick_vision" in instruction
        or "BLUEPRINT.md" in instruction
        or "VPS_VISION.md" in instruction
    ):
        global _blueprint_cache, _blueprint_cache_time
        _blueprint_cache = result_output
        _blueprint_cache_time = datetime.now()
        logger.info("Blueprint cached — action_id=%s", action_id)

    # Check STOP_FLAG after call — discard result if stop was set during execution
    _stopped = False
    async with _redis() as r2:
        if r2:
            _stopped = bool(await r2.get("savant:stop_flag"))
    if _stopped:
        logger.info("Hermes result discarded (STOP_FLAG set during execution) — action_id=%s", action_id)
        await _inject_cancelled("action stopped mid-execution.")
        return

    # Append to Hermes Changelog for write/critical actions
    if action_type in ("write", "critical"):
        _ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _status_str = "ok" if success else "error"
        _entry = f"## {_ts} — [{action_type}] {description} | Result: {_status_str}"
        _changelog_instr = (
            f"Append this line to /opt/data/vps_map/HERMES_CHANGELOG.md "
            f"(create file if missing, one blank line between entries):\n{_entry}\n"
            "Use: python3 -c \""
            "import os; p='/opt/data/vps_map/HERMES_CHANGELOG.md'; "
            "open(p,'a').write('" + _entry.replace("'", "\\'") + r"\n')" + "\""
        )
        # If the action installed/enabled a service, register it for monitoring (C5).
        _il = instruction.lower()
        _installed_service = success and any(
            kw in _il for kw in (
                "apt install", "apt-get install", "docker run", "docker compose up",
                "systemctl enable", "systemctl start", "useradd", "npm install -g",
            )
        )
        _registry_instr = (
            "A VPS action just completed. If it installed/enabled a new service, container "
            "or daemon, upsert a monitoring entry into /opt/data/vps_map/MONITORING_REGISTRY.json "
            "(a JSON object keyed by service name; each value {type, check, added}). Create the "
            "file with {} if missing; preserve existing entries; do nothing if no new service. "
            f"Action was: {description}\nInstruction: {instruction[:300]}"
        )
        # Chain changelog → registry → Full Vision refresh so they don't fight for Hermes capacity.
        async def _post_action_chain() -> None:
            await call_hermes(_changelog_instr, session_id="savant-changelog", timeout=30.0)
            if _installed_service:
                try:
                    await call_hermes(_registry_instr, session_id="savant-monitor-registry", timeout=45.0)
                except Exception:
                    logger.warning("Monitoring registry upsert failed — action_id=%s", action_id)
            await call_hermes(
                "python3 /host/home/savant/scripts/vps_full_vision.py",
                session_id="savant-vision-refresh",
                timeout=120.0,
            )
        _spawn_bg(_post_action_chain())

    # Auto show_data — vision reports always, plus any structured read/write result.
    # SAVANT still speaks a 1-line summary; the panel shows the full rendered Markdown.
    _is_vision = "vps_quick_vision" in instruction
    _is_full_vision = "vps_full_vision" in instruction or "VPS_FULL_VISION" in instruction
    _is_security = "vps_security_scan" in instruction or "SECURITY_SCAN" in instruction
    _auto_title: str | None = None
    _auto_content = ""
    _auto_format = "markdown"
    if success and result_output:
        if _is_vision or _is_full_vision or _is_security:
            _auto_title = ("VPS Quick Vision" if _is_vision else
                           "VPS Full Vision — INDEX" if _is_full_vision else "VPS Security Scan")
            _auto_content = result_output if (_is_vision or _is_security) else "\n".join(
                l for l in result_output.splitlines()
                if not l.startswith("<!-- SECTION") and not l.startswith("<!-- END")
            )[:3000]
            _auto_format = "markdown"
            # Log a critical security verdict as an incident so it surfaces in the next briefing.
            if _is_security and "🔴" in result_output.splitlines()[0:6].__str__():
                try:
                    _vl = next((l for l in result_output.splitlines() if "Verdict" not in l and "🔴" in l), "")
                    save_incident("🔴 security", _vl.strip("- *#").strip()[:200] or "Critical security findings", resolved=0)
                    render_alerts_md()
                except Exception:
                    pass
        elif _looks_structured(result_output):
            _auto_title = (description or "Résultat")[:60]
            _auto_content = result_output[:3000]
            _auto_format = _guess_format(result_output)
    if _auto_title is not None:
        try:
            await websocket.send_text(json.dumps({
                "type": "show_data",
                "title": _auto_title,
                "content": _auto_content,
                "format": _auto_format,
            }))
        except Exception:
            pass

    try:
        await websocket.send_text(json.dumps({
            "type": "hermes_done",
            "action_id": action_id,
            "description": description,
            "output": result_output[:300],
            "success": success,
            "uncertain": _is_gateway_error,
        }))
    except Exception:
        logger.warning("hermes_done WS send failed — action_id=%s", action_id)

    # Whether the result was rendered in the PWA panel (drives the recap hint below).
    _panel_shown = _auto_title is not None

    # Gemini [INTERNAL] for vision results — tell it to summarize, not recite.
    _vision_gemini_hint = ""
    if success and _is_vision:
        _vision_gemini_hint = (
            "[INTERNAL] Quick Vision loaded. ONE sentence status. Mention ⚠️/🔴 only."
        )
    elif success and _is_full_vision:
        _vision_gemini_hint = (
            "[INTERNAL] Full Vision INDEX. 2 sentences max. Never recite table."
        )
    elif success and _is_security:
        _vision_gemini_hint = (
            "[INTERNAL] Security scan in the panel. MANDATORY: state the verdict (🟢/🟡/🔴) "
            "and the TOP 1-2 issues in one sentence. Offer to fix one if 🔴/⚠️. Never stay silent."
        )

    try:
        # Explicit SUCCESS/FAILED marker + panel hint so SAVANT always acknowledges the
        # outcome out loud (fixes "panel shows data but SAVANT doesn't confirm it worked").
        # Handed to the reconciler (inject_queue), which gates on gemini_idle and appends
        # the no-re-emission guard before sending a single coalesced turn.
        if _is_gateway_error:
            _status_marker = "UNCERTAIN"
        else:
            _status_marker = "SUCCESS" if success else "FAILED"
        _body = result_output[:1500] if result_output else ""
        if success and not _body.strip():
            _body = "(completed — no items returned)"
        _hint = ""
        if _vision_gemini_hint:
            _hint = f"\n\n{_vision_gemini_hint}"
        elif _is_gateway_error:
            _hint = (
                "\n\n[INTERNAL] Hermes returned a network error (502/503). The action MAY have "
                "succeeded before the error. Tell the owner in ONE sentence to verify manually."
            )
        elif success and _panel_shown:
            _hint = (
                "\n\n[INTERNAL] Data is now visible in the panel. "
                "MANDATORY: say ONE short sentence summarising the key finding or confirming it worked. "
                "Do NOT recite rows or repeat details. Never stay silent after a panel update."
            )
        elif success:
            _hint = "\n\n[INTERNAL] MANDATORY: confirm it worked in ONE short sentence. Never stay silent."
        else:
            _hint = "\n\n[INTERNAL] This failed — say so plainly and the gist of why."
        _result_text = (
            f"[ACTION RESULT — {_status_marker} — source of truth, overrides anything you said] "
            f"{description}:\n{_body}{_hint}"
        )
        inject_queue.put_nowait(_result_text)
    except Exception:
        logger.warning(
            "Result injection outer error — action_id=%s", action_id
        )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Console Live mode — fully isolated handler (separate Gemini session + lean prompt).
    # Delegate and return; the voice handler below is never entered for console connections.
    if websocket.query_params.get("mode") == "console":
        from backend.console import handle_console_session
        await handle_console_session(websocket)
        return
    await websocket.accept()
    client_id = (
        f"{websocket.client.host}:{websocket.client.port}"
        if websocket.client
        else "unknown"
    )
    logger.info("WS opened — client=%s", client_id)

    gemini_client: genai.Client | None = websocket.app.state.gemini_client
    if gemini_client is None:
        logger.error("Gemini client not initialised — closing WS")
        await websocket.close(code=1011, reason="GEMINI_API_KEY not configured")
        return

    cmd_queue: asyncio.Queue[tuple[str, bytes | None]] = asyncio.Queue()
    pwa_done = asyncio.Event()
    session_handle: str | None = None

    # Identity gate — always locked at connection start, unlocked on code validation.
    session_verified = asyncio.Event()

    # Session recording state.
    session_transcript: list[str] = []
    session_summary_saved = False
    try:
        current_session_id: int | None = open_session()
    except Exception:
        current_session_id = None

    first_session_opened = False
    active_hermes_tasks: set[asyncio.Task] = set()

    # Strict FIFO Hermes execution — one action at a time, results injected in
    # submission order so a fast read (verification) can't overtake a slow write
    # (deletion that backs up first) and contradict it.
    hermes_action_queue: asyncio.Queue = asyncio.Queue()
    hermes_busy: list = [False]  # True while the worker is running an action

    # Signals Gemini is not generating audio — safe to inject [ACTION RESULT].
    # Cleared on first audio chunk of a turn; set again on turn_complete.
    gemini_idle: asyncio.Event = asyncio.Event()
    gemini_idle.set()
    # Set while a run_command fast-path is executing. Audio/eot are buffered;
    # flushed back to cmd_queue when the command completes.
    exec_running: asyncio.Event = asyncio.Event()
    _pending_user_turns: list[tuple] = []
    # Monotonic counter of audio chunks received from Gemini — used to detect
    # whether a turn produced actual speech (vs an empty FunctionResponse ack turn).
    audio_chunk_count: list = [0]

    # Timestamp of last audio frame received from browser — used by auto-pause watcher.
    last_audio_time: list[float] = [asyncio.get_event_loop().time()]

    async def _pwa_reader() -> None:
        """Long-lived task: read WebSocket frames → put commands into cmd_queue."""
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    logger.info("WS disconnected — client=%s", client_id)
                    break
                if msg["type"] != "websocket.receive":
                    continue
                if msg.get("bytes"):
                    last_audio_time[0] = asyncio.get_event_loop().time()
                    await cmd_queue.put(("audio", msg["bytes"]))
                elif msg.get("text"):
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "end_of_turn":
                        await cmd_queue.put(("eot", None))
                        logger.info("end_of_turn received — client=%s", client_id)
                    elif data.get("type") == "action_code":
                        if session_verified.is_set():
                            continue  # gate already open — drop re-submissions silently
                        raw = data.get("value", "")
                        if raw:
                            try:
                                stored = get_memory_value("action_code")
                                if stored is None:
                                    save_memory("action_code", raw)
                                    await cmd_queue.put(("action_code_set", None))
                                    logger.info("action_code: set — client=%s", client_id)
                                elif stored == raw:
                                    await cmd_queue.put(("action_code_valid", None))
                                    logger.info("action_code: match — client=%s", client_id)
                                else:
                                    await cmd_queue.put(("action_code_invalid", None))
                                    logger.warning("action_code: mismatch — client=%s", client_id)
                            except Exception:
                                logger.exception("Failed to check action_code — client=%s", client_id)
                    elif data.get("type") == "hermes_stop_confirm":
                        await cmd_queue.put(("hermes_stop_confirm", None))
                    elif data.get("type") == "stop_action":
                        await cmd_queue.put(("stop_requested", None))
                    elif data.get("type") == "text_command":
                        text = (data.get("text") or "").strip()
                        if text and session_verified.is_set():
                            await cmd_queue.put(("text_command", text))
                            logger.info("text_command received — client=%s len=%d", client_id, len(text))
                    # NOTE: metrics polling is now backend-driven (_metrics_loop task
                    # started below). The PWA no longer sends refresh_metrics — the
                    # backend pushes metrics_update every _METRICS_POLL_INTERVAL_S
                    # autonomously, so polling is robust to Hermes latency.
        except WebSocketDisconnect:
            pass
        finally:
            pwa_done.set()

    async def _auto_pause_watcher() -> None:
        """Automatically pause SAVANT if no user audio received for 180 seconds."""
        while True:
            await asyncio.sleep(10)
            if not session_verified.is_set():
                continue
            if savant_paused[0]:
                continue
            if asyncio.get_event_loop().time() - last_audio_time[0] >= 180:
                savant_paused[0] = True
                _rp = await _get_redis()
                if _rp:
                    await _rp.set("savant:paused", "1")
                    await _rp.aclose()
                try:
                    await websocket.send_text(json.dumps({"type": "savant_paused", "paused": True}))
                except Exception:
                    pass
                logger.info("Auto-pause: no audio for 180s — client=%s", client_id)

    pwa_reader_task = asyncio.create_task(_pwa_reader())
    auto_pause_task = asyncio.create_task(_auto_pause_watcher())

    # Mutable reference to the current Gemini session — updated on each GoAway reconnect
    # so in-flight _run_hermes_bg tasks can inject results into the new session.
    session_ref: list = [None]

    # ── Coordination state (idempotence + Job Ledger + single injector) ─────────
    # job_inflight: fingerprint → action_id for actions queued/running right now.
    # job_recent:   fingerprint → completion time, kept for _JOB_DEDUP_TTL_S.
    # Together they suppress duplicate re-emissions of the same action.
    job_inflight: dict[str, str] = {}
    job_recent: dict[str, float] = {}
    # job_ledger: action_id → {id, description, type, status} — mirrored to the PWA
    # as chips (queued → running → done) so progress is visible without narration.
    job_ledger: dict[str, dict] = {}
    # inject_queue: every backend→Gemini status text funnels through one reconciler.
    inject_queue: asyncio.Queue = asyncio.Queue()

    # Confirmation integrity: a write/critical only executes after a REAL ask→owner-turn→confirm
    # cycle. pending_confirm maps an action fingerprint → the user_turn_count when it was asked;
    # a confirmed=true call is honoured only if the owner spoke in a later turn. This blocks
    # premature/self-confirmed execution AND suppresses repeated confirmation questions.
    pending_confirm: dict[str, int] = {}
    # Full details of each pending write/critical ask, keyed by the same fingerprint as
    # pending_confirm: {fp: {"instruction","description","action_label"}}. Lets the backend
    # EXECUTE a confirmed action on its own (Prong 2) when Gemini fails to re-emit the
    # execute_action(confirmed=true) tool call — the action no longer depends on the model.
    pending_action_detail: dict[str, dict] = {}
    user_turn_count: list[int] = [0]
    # user_turn_count when the owner last AFFIRMED (oui/yes/ok…). Drives the backend
    # confirm fallback (Prong 2). The Gemini-re-emit gate (Prong 1) no longer REQUIRES it —
    # it accepts a confirm once the owner spoke a new turn after the ask and did not negate.
    last_affirm_turn: list[int] = [0]
    # user_turn_count when the owner last NEGATED (non/annule…). A confirm is rejected if
    # the owner negated AFTER the ask — blocks an accidental fire on an explicit refusal.
    last_negate_turn: list[int] = [0]
    # True if an execute_action tool call was emitted during the turn currently completing.
    # Drives the "phantom action" net: SAVANT sometimes narrates a lookup ("je regarde…") but
    # never emits the call, so nothing runs. Set True at the top of the execute_action handler
    # (any call counts, even deduped/awaiting); checked then reset to False at the end of each
    # turn_complete.
    exec_called_since_user: list[bool] = [False]
    # Loop-time of the last confirmed write/critical fire (either prong, set in _fire_action).
    # A confirmed=true re-emit arriving right after a fire is a duplicate of the just-fired
    # action (Prong 2 cleared pending_confirm and the instruction text often drifts so the
    # exact-fingerprint dedup misses) — we must NOT re-ask, which was cutting SAVANT off.
    _last_confirmed_fire_t: list[float] = [0.0]

    def _action_fp(action_type: str, instruction: str) -> str:
        norm = " ".join((instruction or "").lower().split())
        return hashlib.sha1(f"{action_type}|{norm}".encode()).hexdigest()[:12]

    def _is_duplicate_action(action_type: str, instruction: str) -> bool:
        """True if an identical action is already inflight or completed within the TTL."""
        now = asyncio.get_event_loop().time()
        for _k in [k for k, t in job_recent.items() if now - t > _JOB_DEDUP_TTL_S]:
            job_recent.pop(_k, None)
        fp = _action_fp(action_type, instruction)
        return fp in job_inflight or fp in job_recent

    async def _push_job_update() -> None:
        try:
            await websocket.send_text(json.dumps({
                "type": "job_update",
                "jobs": [
                    {"id": j["id"], "description": j["description"], "status": j["status"]}
                    for j in job_ledger.values()
                ],
            }))
        except Exception:
            pass

    async def _enqueue_action(action_id: str, description: str,
                              instruction: str, action_type: str) -> None:
        """Register the action (idempotence + ledger) and put it on the FIFO worker.
        Caller must have already checked _is_duplicate_action()."""
        fp = _action_fp(action_type, instruction)
        job_inflight[fp] = action_id
        job_ledger[action_id] = {
            "id": action_id, "description": description,
            "type": action_type, "status": "queued", "fp": fp,
        }
        hermes_action_queue.put_nowait({
            "action_id": action_id, "description": description,
            "instruction": instruction, "action_type": action_type, "fp": fp,
        })
        await _push_job_update()

    _FIRE_INFRA_KEYWORDS = {
        "docker", "systemctl", "service", "caddy", "nginx",
        "ufw", "iptables", "cron", "mount", "fdisk",
        "useradd", "userdel", "groupadd", "apt", "dpkg",
    }

    async def _fire_action(action_id: str, description: str,
                           instruction: str, action_label: str) -> None:
        """Commit a confirmed write/critical action: invalidate the blueprint cache if it
        touches infra, notify the PWA, enqueue on the FIFO worker, and inject the locking
        [INTERNAL] so Gemini does not re-emit. Shared by the Gemini-re-emit gate (Prong 1)
        and the backend confirm fallback (Prong 2) so both paths behave identically.
        Caller must have cleared pending_confirm and checked _is_duplicate_action()."""
        global _blueprint_cache
        # Mark the fire time so a confirmed=true re-emit racing this fire is deduped, not re-asked.
        _last_confirmed_fire_t[0] = asyncio.get_event_loop().time()
        if any(f" {kw} " in f" {instruction.lower()} " for kw in _FIRE_INFRA_KEYWORDS):
            _blueprint_cache = None
        await websocket.send_text(json.dumps({
            "type": "hermes_running", "action_id": action_id, "description": description,
        }))
        _queued_behind = hermes_action_queue.qsize() + (1 if hermes_busy[0] else 0)
        await _enqueue_action(action_id, description, instruction, action_label)
        inject_queue.put_nowait(
            f"[INTERNAL] Action '{description}' is confirmed and NOW RUNNING. "
            "Do NOT call execute_action again for this task — it is already queued. "
            "Say one natural short sentence (e.g. 'Je m\\'en occupe.') "
            "then wait silently for [ACTION RESULT]."
        )
        if _queued_behind > 0:
            inject_queue.put_nowait(
                f"[INTERNAL] '{description}' is queued behind the running task. "
                "Tell the owner briefly you'll do it right after "
                "(e.g. 'Je fais ça juste après.'). Do NOT report any result yet."
            )

    async def _reconciler() -> None:
        """Single serialized injector. Coalesces all pending status texts, waits until
        SAVANT is idle, then sends ONE user turn with the no-re-emission guard. This
        replaces the scattered send_client_content calls that reopened turns and made
        Gemini re-issue the same execute_action (the repetition bug)."""
        while True:
            text = await inject_queue.get()
            batch = [text]
            try:
                while True:
                    batch.append(inject_queue.get_nowait())
            except asyncio.QueueEmpty:
                pass
            try:
                await asyncio.wait_for(gemini_idle.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("reconciler: idle wait timed out — injecting anyway — client=%s", client_id)
            combined = "\n\n".join(batch) + _RECONCILER_GUARD
            if session_ref[0] is not None:
                if not await _inject_with_retry(session_ref, combined):
                    logger.warning("reconciler: injection failed — client=%s", client_id)
            for _ in batch:
                inject_queue.task_done()

    async def _action_heartbeat(start_turn: int, action_type: str = "read") -> None:
        """Keep SAVANT alive during a running action. While the owner stays SILENT
        (no new turn since the action started) and SAVANT isn't speaking, slip ONE short
        varied 'still working' line every ~5s so the wait never feels dead. The moment the
        owner speaks (engaged → async chat), STOP — never interject. Capped at 3.
        For write/critical the phase narration ('je sécurise', 'je lance') already fills
        the gap, so the generic heartbeat is a DEEP fallback (first fire at ~15s); reads
        (no phases) start at ~7s. The worker cancels this the instant the action completes,
        so a 'still working' line never coalesces with the result."""
        _fired = 0
        _delay = 15.0 if action_type in ("write", "critical") else 7.0
        while _fired < 3:
            try:
                await asyncio.sleep(_delay)
            except asyncio.CancelledError:
                raise
            _delay = 5.0
            if user_turn_count[0] != start_turn:
                return  # owner spoke since the action began — they're engaged, don't fill
            if not gemini_idle.is_set():
                continue  # SAVANT is mid-sentence — wait for a gap
            inject_queue.put_nowait(
                "[INTERNAL] The action is still running in the background. Say ONE short, "
                "varied, natural waiting line (e.g. 'c'est en route', 'je m'en occupe', "
                "'presque là') — never the same twice, never announce any result."
            )
            _fired += 1

    async def _do_stop(internal_inject: str | None = None) -> int:
        """Kill all in-flight + queued Hermes actions. Shared by the orb stop
        (stop_requested) and the spoken stop (transcript match in _gemini_receiver).
        Sets savant:stop_flag so an action whose Hermes call already returned during
        the cancel window discards its result instead of narrating it. Returns the
        number of actions affected (drained + in-flight) so callers can no-op silently."""
        # Set the Redis stop_flag FIRST — the post-call guard in _run_hermes_bg reads it.
        try:
            _rc = await _get_redis()
            if _rc:
                await _rc.set("savant:stop_flag", "1", ex=60)
                await _rc.aclose()
        except Exception:
            pass
        # Drain the FIFO queue so queued (not-yet-started) actions never run.
        _drained = 0
        while not hermes_action_queue.empty():
            try:
                _dropped = hermes_action_queue.get_nowait()
                hermes_action_queue.task_done()
                _drained += 1
                _dfp = _dropped.get("fp")
                if _dfp is not None:
                    job_inflight.pop(_dfp, None)
                job_ledger.pop(_dropped.get("action_id"), None)
            except asyncio.QueueEmpty:
                break
        if _drained:
            await _push_job_update()
        _affected = _drained + len(active_hermes_tasks)
        if _affected and internal_inject:
            _sess = session_ref[0]
            if _sess is not None:
                try:
                    await _sess.send_client_content(
                        turns=[types.Content(role="user", parts=[types.Part(
                            text=internal_inject
                        )])],
                        turn_complete=True,
                    )
                except Exception:
                    pass
        for _t in list(active_hermes_tasks):
            _t.cancel()
        try:
            await websocket.send_text(json.dumps({"type": "stop_ack"}))
        except Exception:
            pass
        return _affected

    async def _hermes_worker() -> None:
        """Serial FIFO executor. Runs ONE Hermes action fully to completion before
        starting the next, so [ACTION RESULT] injections arrive in submission order.
        Each action runs as a child task tracked in active_hermes_tasks so a stop can
        cancel the in-flight action without killing the worker (asyncio.wait does not
        propagate the child's cancellation; it only propagates worker cancellation)."""
        while True:
            item = await hermes_action_queue.get()
            hermes_busy[0] = True
            gemini_idle.clear()  # action starting — result waits until SAVANT finishes its launch line
            _aid = item["action_id"]
            _fp = item.get("fp")
            if _aid in job_ledger:
                job_ledger[_aid]["status"] = "running"
                await _push_job_update()
            child = asyncio.create_task(_run_hermes_bg(
                session_ref, websocket, _aid,
                item["description"], item["instruction"], item["action_type"],
                client_id, gemini_idle=gemini_idle, inject_queue=inject_queue,
            ))
            active_hermes_tasks.add(child)
            child.add_done_callback(active_hermes_tasks.discard)
            _hb = asyncio.create_task(_action_heartbeat(user_turn_count[0], item["action_type"]))  # fallback filler
            try:
                await asyncio.wait({child})
            finally:
                _hb.cancel()
                await asyncio.gather(_hb, return_exceptions=True)
                hermes_busy[0] = False
                hermes_action_queue.task_done()
                # Release the dedup fingerprint into the recent window and close the ledger entry.
                if _fp is not None:
                    job_inflight.pop(_fp, None)
                    job_recent[_fp] = asyncio.get_event_loop().time()
                if _aid in job_ledger:
                    job_ledger[_aid]["status"] = "cancelled" if child.cancelled() else "done"
                    await _push_job_update()
                    job_ledger.pop(_aid, None)

    hermes_worker_task = asyncio.create_task(_hermes_worker())
    reconciler_task = asyncio.create_task(_reconciler())
    # Metrics loop is created here (after inject_queue exists) so it can push terse
    # critical alerts through the reconciler. It still self-gates on session_verified.
    metrics_loop_task = asyncio.create_task(
        _metrics_loop(websocket, client_id, session_verified, inject_queue)
    )

    # Pause flag — shared between _audio_sender and _gemini_receiver.
    # Set via save_memory('paused','1'); cleared via save_memory('paused','0').
    savant_paused: list = [False]
    # pause_pending guards against double-firing between the Gemini tool-call path
    # and the transcript fallback (a pause may be deferred for ~15s before the flag
    # flips, so savant_paused[0] alone can't tell us a pause is already in flight).
    pause_pending: list = [False]
    # Load initial paused state from Redis (may have been set via /control/pause)
    _rp_init = await _get_redis()
    if _rp_init:
        savant_paused[0] = (await _rp_init.get("savant:paused")) == "1"
        await _rp_init.aclose()

    try:
        while not pwa_done.is_set():
            go_away = asyncio.Event()

            try:
                async with gemini_client.aio.live.connect(
                    model=GEMINI_MODEL, config=_make_live_config(session_handle)
                ) as session:
                    session_ref[0] = session  # update ref for any running hermes tasks
                    gemini_idle.set()  # reset to idle on each new/resumed session
                    logger.info(
                        "Gemini Live session opened — client=%s handle=%s",
                        client_id,
                        session_handle[:8] + "…" if session_handle else "new",
                    )

                    if not first_session_opened:
                        first_session_opened = True
                        # Log the exact system prompt sent to Gemini (also visible via
                        # GET /health/prompt) so what the model receives is inspectable.
                        try:
                            _sp = _make_live_config(session_handle).system_instruction.parts[0].text
                            logger.info(
                                "System prompt for client=%s (%d chars):\n%s",
                                client_id, len(_sp), _sp,
                            )
                        except Exception:
                            logger.warning("Could not log system prompt — client=%s", client_id)
                        try:
                            action_code_exists = get_memory_value("action_code") is not None
                        except Exception:
                            action_code_exists = False
                        if action_code_exists:
                            trigger = "[INTERNAL] Ask code (field). Silent until verified."
                        else:
                            trigger = "[INTERNAL] No code set. Ask owner to set one (field)."
                        await session.send_client_content(
                            turns=[types.Content(role="user", parts=[types.Part(text=trigger)])],
                            turn_complete=True,
                        )
                    elif session_verified.is_set():
                        # GoAway reconnect with identity already confirmed — override
                        # _IDENTITY_VERIFICATION_BLOCK in the refreshed system prompt.
                        try:
                            await session.send_client_content(
                                turns=[types.Content(role="user", parts=[types.Part(
                                    text="[INTERNAL] Reconnect. Verified — continue."
                                )])],
                                turn_complete=True,
                            )
                        except Exception:
                            logger.warning("send_client_content failed on GoAway reconnect — client=%s", client_id)

                    async def _audio_sender() -> None:
                        """Drain cmd_queue and forward commands to the active session."""
                        global _blueprint_cache, _blueprint_cache_time, _active_sessions
                        while True:
                            kind, payload = await cmd_queue.get()
                            if kind == "audio":
                                if session_verified.is_set():
                                    if exec_running.is_set():
                                        _pending_user_turns.append((kind, payload))
                                    else:
                                        await session.send_realtime_input(
                                            audio=types.Blob(
                                                data=payload, mime_type=AUDIO_MIME_TYPE
                                            )
                                        )
                            elif kind == "eot":
                                if session_verified.is_set():
                                    if exec_running.is_set():
                                        _pending_user_turns.append((kind, None))
                                    else:
                                        await session.send_client_content(turn_complete=True)
                            elif kind == "action_code_set":
                                if not session_verified.is_set():
                                    _active_sessions += 1
                                session_verified.set()
                                await websocket.send_text(json.dumps({"type": "session_verified"}))
                                _rc = await _get_redis()
                                if _rc:
                                    await _rc.delete("savant:stop_flag")
                                    await _rc.delete("savant:paused")
                                    await _rc.aclose()
                                # A freshly verified session always starts listening — never
                                # inherit a stale pause left in Redis by a prior session.
                                savant_paused[0] = False
                                pause_pending[0] = False
                                try:
                                    await websocket.send_text(json.dumps({"type": "savant_paused", "paused": False}))
                                except Exception:
                                    pass
                                # Kick off the onboarding progress bar (step 0 of 3)
                                await _send_onboarding_progress(websocket)
                                try:
                                    await session.send_client_content(
                                        turns=[types.Content(role="user", parts=[types.Part(
                                            text="[INTERNAL] Code set. 2-word ack, then Q1: 'What should I call you?'"
                                        )])],
                                        turn_complete=True,
                                    )
                                except Exception:
                                    logger.warning("send_client_content failed after action_code_set — client=%s", client_id)
                            elif kind == "action_code_valid":
                                if not session_verified.is_set():
                                    _active_sessions += 1
                                session_verified.set()
                                await websocket.send_text(json.dumps({"type": "session_verified"}))
                                _rc = await _get_redis()
                                if _rc:
                                    await _rc.delete("savant:stop_flag")
                                    await _rc.delete("savant:paused")
                                    await _rc.aclose()
                                # A freshly verified session always starts listening — never
                                # inherit a stale pause left in Redis by a prior session.
                                savant_paused[0] = False
                                pause_pending[0] = False
                                try:
                                    await websocket.send_text(json.dumps({"type": "savant_paused", "paused": False}))
                                except Exception:
                                    pass
                                # FIX5: a newly verified session always gets a FRESH Quick Vision —
                                # never serve a stale cache at login. The cache only accelerates
                                # GoAway reconnects (which keep session_verified and don't re-enter here).
                                _blueprint_cache = None
                                _blueprint_cache_time = None

                                # ── Smart Briefing (C2) — what changed since last session + open alerts ──
                                _brief_bits: list[str] = []
                                try:
                                    for _s in get_recent_sessions(4):
                                        if current_session_id and _s.get("id") == current_session_id:
                                            continue
                                        _sum = (_s.get("summary") or "").strip()
                                        for _tag in ("[llm] ", "[gemini] ", "[auto] "):
                                            if _sum.lower().startswith(_tag):
                                                _sum = _sum[len(_tag):]
                                                break
                                        _sum = " ".join(p.strip() for p in _sum.splitlines() if p.strip())
                                        if _sum:
                                            _brief_bits.append(f"Dernière session: {_sum[:240]}")
                                            break
                                except Exception:
                                    pass
                                _incident_ids: list[int] = []
                                try:
                                    _incs = get_unresolved_incidents(5)
                                    if _incs:
                                        _incident_ids = [int(i["id"]) for i in _incs]
                                        # Weave the enriched context (cause/fix) so SAVANT can
                                        # explain and offer to fix, not just announce.
                                        _parts = []
                                        for i in _incs:
                                            _p = f"{i['type']} {i['action_taken']}"
                                            if i.get("cause"):
                                                _p += f" (cause probable: {i['cause']})"
                                            if i.get("suggested_fix"):
                                                _p += f" → {i['suggested_fix']}"
                                            _parts.append(_p)
                                        _inc_txt = "; ".join(_parts)
                                        _brief_bits.append(
                                            f"{len(_incs)} alerte(s) VPS en attente: {_inc_txt[:400]}"
                                        )
                                except Exception:
                                    pass
                                # Part 2-B: keep the briefing to ONE short woven clause — no recap
                                # offer in the first turn (recap is owner-initiated now).
                                _briefing = ""
                                if _brief_bits:
                                    _briefing = (
                                        "BRIEFING (weave as ONE short natural clause into your single "
                                        "greeting sentence, never a list, only if it fits): "
                                        + " | ".join(_brief_bits) + "\n"
                                    )

                                # Token-free Quick Vision: read the cron-written file directly. Fresh →
                                # show it + greet in one short turn (no execute_action, no Gemini tool
                                # call). Missing/stale → greet then load via execute_action, which hits
                                # the fast-file lane (file or Hermes fallback).
                                _qv = _read_fresh_file(_QUICK_VISION_FILE, _QUICK_VISION_MAX_AGE_S)
                                if _qv:
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "show_data",
                                            "title": "VPS Quick Vision",
                                            "content": _qv,
                                            "format": "markdown",
                                        }))
                                    except Exception:
                                        pass
                                    _flags = "\n".join(
                                        l for l in _qv.splitlines() if "⚠️" in l or "🔴" in l
                                    )[:300]
                                    _status_hint = f"Flags: {_flags}" if _flags else "No ⚠️/🔴 — all green."
                                    _bp_inject = (
                                        "[INTERNAL] Identity OK. VPS status is already on screen. "
                                        f"{_briefing}"
                                        "Greet the owner by name in ONE short sentence (Bonjour/Bonsoir per "
                                        "local time). Add a 4-5 word flag ONLY if there's a ⚠️/🔴, else nothing. "
                                        "Then STOP and wait — no list, no recap unless asked.\n"
                                        f"{_status_hint}"
                                    )
                                    logger.info("Quick Vision file served at login — client=%s", client_id)
                                else:
                                    _bp_inject = (
                                        "[INTERNAL] Identity OK. "
                                        f"{_briefing}"
                                        "Greet the owner by name in ONE short sentence (Bonjour/Bonsoir per "
                                        "local time), then call execute_action(read, description='VPS Quick "
                                        f"Vision', instruction={_VPS_VISION_INSTRUCTION!r}). "
                                        "On [ACTION RESULT]: ONE short sentence of VPS status (⚠️/🔴 only), "
                                        "then STOP and wait. No list, no recap unless asked."
                                    )
                                    logger.info("Quick Vision file miss at login — load via action — client=%s", client_id)
                                try:
                                    await session.send_client_content(
                                        turns=[types.Content(role="user", parts=[types.Part(text=_bp_inject)])],
                                        turn_complete=True,
                                    )
                                    # Incidents are now being announced — mark them resolved so they
                                    # aren't repeated next login.
                                    if _incident_ids:
                                        try:
                                            resolve_incidents(_incident_ids)
                                        except Exception:
                                            logger.warning("resolve_incidents failed — client=%s", client_id)
                                except Exception:
                                    logger.warning("send_client_content failed after action_code_valid — client=%s", client_id)
                            elif kind == "action_code_invalid":
                                await websocket.send_text(json.dumps({"type": "access_denied"}))
                                try:
                                    await session.send_client_content(
                                        turns=[types.Content(role="user", parts=[types.Part(
                                            text="[INTERNAL] Wrong code. Refuse, re-ask."
                                        )])],
                                        turn_complete=True,
                                    )
                                except Exception:
                                    logger.warning("send_client_content failed after action_code_invalid — client=%s", client_id)
                            elif kind == "hermes_stop_confirm":
                                # Owner confirmed stopping a running Hermes action — set Redis flag + cancel
                                try:
                                    _rc = await _get_redis()
                                    if _rc:
                                        await _rc.set("savant:stop_flag", "1", ex=60)
                                        await _rc.aclose()
                                except Exception:
                                    pass
                                while not hermes_action_queue.empty():
                                    try:
                                        hermes_action_queue.get_nowait()
                                        hermes_action_queue.task_done()
                                    except asyncio.QueueEmpty:
                                        break
                                for _t in list(active_hermes_tasks):
                                    _t.cancel()
                                try:
                                    await session.send_client_content(
                                        turns=[types.Content(role="user", parts=[types.Part(
                                            text="[INTERNAL] Stopped."
                                        )])],
                                        turn_complete=True,
                                    )
                                except Exception:
                                    pass
                                try:
                                    await websocket.send_text(json.dumps({"type": "stop_ack"}))
                                except Exception:
                                    pass
                            elif kind == "text_command":
                                # A typed command is a real owner turn — count it (and detect an
                                # affirmation) so a typed "oui" can confirm a pending write/critical.
                                user_turn_count[0] += 1
                                if isinstance(payload, str) and match_phrase(payload, _AFFIRM_PHRASES):
                                    last_affirm_turn[0] = user_turn_count[0]
                                elif isinstance(payload, str) and match_phrase(payload, _NEGATION_PHRASES):
                                    last_negate_turn[0] = user_turn_count[0]
                                    if pending_confirm:
                                        pending_confirm.clear()
                                        pending_action_detail.clear()
                                try:
                                    await session.send_client_content(
                                        turns=[types.Content(role="user", parts=[types.Part(text=payload)])],
                                        turn_complete=True,
                                    )
                                except Exception:
                                    logger.warning("send_client_content failed for text_command — client=%s", client_id)
                            elif kind == "stop_requested":
                                # Orb stop — kill all actions and discard any in-flight result.
                                await _do_stop("[INTERNAL] Cancelling. Say: 'Stopping all actions.'")

                    async def _gemini_receiver() -> None:
                        """Forward Gemini responses to the PWA; handle GoAway/resumption/tool calls."""
                        nonlocal session_handle, session_summary_saved
                        global _blueprint_cache
                        _savant_buf: list[str] = []
                        _user_buf: list[str] = []
                        while True:
                            async for response in session.receive():
                                if response.session_resumption_update:
                                    upd = response.session_resumption_update
                                    if upd.resumable and upd.new_handle:
                                        session_handle = upd.new_handle
                                        # DEBUG, not INFO: Gemini emits a resumption update ~1/s,
                                        # which flooded savant.log (~10k lines/day) and buried real events.
                                        logger.debug(
                                            "Session handle updated — client=%s handle=%s…",
                                            client_id,
                                            session_handle[:8],
                                        )

                                if response.go_away:
                                    logger.warning(
                                        "GoAway received — client=%s time_left=%s",
                                        client_id,
                                        response.go_away.time_left,
                                    )
                                    go_away.set()
                                    return

                                if response.tool_call:
                                    fn_responses = []
                                    onboarding_just_completed = False
                                    language_switch_value: str | None = None
                                    onboarding_next_inject: str | None = None
                                    write_confirm_inject: str | None = None
                                    hermes_queue_inject: str | None = None
                                    _pause_deferred_inject: bool = False
                                    invalid_reask_inject: str | None = None
                                    for fc in response.tool_call.function_calls:
                                        if fc.name == "save_memory":
                                            key = fc.args.get("key", "")
                                            value = fc.args.get("value", "").strip()
                                            _namespace = (fc.args.get("namespace") or "general").strip() or "general"
                                            _is_onboarding = key in _ONBOARDING_KEYS
                                            if key == "preferred_language":
                                                _clean = value.replace(" ", "").replace("-", "")
                                                _invalid = not _clean or not _clean.isalpha()
                                                _invalid_hint = (
                                                    "language name must be letters only (e.g. English, French, Arabic, Darija)"
                                                )
                                            else:
                                                _invalid = _is_onboarding and (not value or value.isdigit())
                                                _invalid_hint = "answer was empty or not valid"
                                            if _invalid:
                                                # FunctionResponse stays minimal — never put instructions here (vocalized in AUDIO mode)
                                                fn_responses.append(types.FunctionResponse(
                                                    name="save_memory", id=fc.id,
                                                    response={"result": "invalid"},
                                                ))
                                                invalid_reask_inject = (
                                                    f"Last answer rejected ({_invalid_hint}). "
                                                    f"Re-ask the same question ({key}) warmly, briefly rephrased."
                                                )
                                            elif key:
                                                try:
                                                    save_memory(key, value, _namespace)
                                                    # Completion: all 3 onboarding keys now present
                                                    if _is_onboarding:
                                                        # Advance the PWA progress bar (reflects the value just saved)
                                                        await _send_onboarding_progress(websocket)
                                                        try:
                                                            _st = get_onboarding_status()
                                                            if not _st["missing"]:
                                                                onboarding_just_completed = True
                                                        except Exception:
                                                            pass
                                                    if not onboarding_just_completed:
                                                        if key == "preferred_language":
                                                            language_switch_value = value
                                                        elif _is_onboarding:
                                                            # Build next-question inject dynamically from remaining missing keys
                                                            try:
                                                                _miss = get_onboarding_status()["missing"]
                                                                _nk = _miss[0] if _miss else None
                                                            except Exception:
                                                                _nk = None
                                                            _next_prompt = {
                                                                "preferred_language": (
                                                                    "Detect language from owner's answer. "
                                                                    "Ask Q2 in that language: 'What language should I respond in?'"
                                                                ),
                                                                "timezone": "Ask: 'What's your timezone? (e.g. Europe/Paris)'",
                                                                "owner_name": "Ask: 'What should I call you?'",
                                                            }.get(_nk)
                                                            if _next_prompt:
                                                                onboarding_next_inject = _next_prompt
                                                    elif key == "paused":
                                                        _is_paused = value in ("1", "true", "yes")
                                                        if not _is_paused:
                                                            # Resume: unblock audio immediately
                                                            pause_pending[0] = False
                                                            savant_paused[0] = False
                                                            _rr = await _get_redis()
                                                            if _rr:
                                                                await _rr.delete("savant:paused")
                                                                await _rr.aclose()
                                                            try:
                                                                await websocket.send_text(json.dumps({
                                                                    "type": "savant_paused",
                                                                    "paused": False,
                                                                }))
                                                            except Exception:
                                                                pass
                                                        elif not pause_pending[0]:
                                                            # Pause: defer flag until after SAVANT finishes speaking
                                                            pause_pending[0] = True
                                                            _pause_deferred_inject = True
                                                    logger.info(
                                                        "Tool: save_memory(key=%s) — client=%s",
                                                        key, client_id,
                                                    )
                                                except Exception:
                                                    logger.exception(
                                                        "Tool save_memory failed — client=%s", client_id
                                                    )
                                                fn_responses.append(types.FunctionResponse(
                                                    name="save_memory", id=fc.id,
                                                    response={"result": "saved" if _is_onboarding else "ok"},
                                                ))
                                            else:
                                                fn_responses.append(types.FunctionResponse(
                                                    name="save_memory", id=fc.id,
                                                    response={"result": "ok"},
                                                ))
                                        elif fc.name == "save_session_summary":
                                            gemini_summary = fc.args.get("summary", "").strip()
                                            try:
                                                llm_summary = await _llm_summarize_transcript(session_transcript)
                                                if llm_summary:
                                                    final_summary = f"[llm] {llm_summary}"
                                                elif gemini_summary:
                                                    final_summary = f"[gemini] {gemini_summary}"
                                                else:
                                                    final_summary = ""
                                                if final_summary:
                                                    if current_session_id:
                                                        close_session(current_session_id, final_summary)
                                                    else:
                                                        save_session_summary(final_summary)
                                                    session_summary_saved = True
                                                    logger.info(
                                                        "Tool: save_session_summary (%s, %d chars) — client=%s",
                                                        "llm" if llm_summary else "gemini",
                                                        len(final_summary), client_id,
                                                    )
                                            except Exception:
                                                logger.exception(
                                                    "Tool save_session_summary failed — client=%s", client_id
                                                )
                                            fn_responses.append(
                                                types.FunctionResponse(
                                                    name="save_session_summary",
                                                    id=fc.id,
                                                    response={"result": "ok"},
                                                )
                                            )
                                        elif fc.name == "execute_action":
                                            # The model DID emit the call this turn — disarm the phantom-action net.
                                            exec_called_since_user[0] = True
                                            instruction = fc.args.get("instruction", "")
                                            action_type = fc.args.get("action_type", "read")
                                            description = fc.args.get("description", "Executing action")
                                            action_id = uuid.uuid4().hex[:8]
                                            logger.info(
                                                "Tool: execute_action(type=%s) %s — client=%s",
                                                action_type, description[:50], client_id,
                                            )

                                            # FIX3: destructive/irreversible ops force the CRITICAL flow
                                            # even if the model under-classified them as a plain write.
                                            _DESTRUCTIVE_MARKERS = (
                                                "userdel", "deluser", "rm -rf", "rm -fr", "drop table",
                                                "drop database", "mkfs", "dd if=", "docker rm -f",
                                                "docker rmi", "truncate", "delete user", "shutdown", "reboot",
                                                "systemctl disable", "systemctl stop", "systemctl mask",
                                                "iptables -f", "iptables -x", "ufw disable", "ufw reset",
                                                "git reset --hard", "fdisk", "parted", "wipefs",
                                                "chown -r /", "chmod -r 000", "killall", "pkill -9",
                                            )
                                            if action_type == "write" and any(
                                                m in instruction.lower() for m in _DESTRUCTIVE_MARKERS
                                            ):
                                                action_type = "critical"
                                                logger.info(
                                                    "Destructive write escalated to critical — %s — client=%s",
                                                    description[:40], client_id,
                                                )

                                            if action_type in ("critical", "write"):
                                                action_label = action_type
                                                confirmed = fc.args.get("confirmed", False)
                                                # Confirmation fingerprint uses a CONSTANT label (not action_label)
                                                # so a write→critical escalation between the ask and the confirm
                                                # produces the SAME key — otherwise the confirm misses the pending
                                                # entry and the gate re-asks (the triple-confirmation loop).
                                                _cfp = _action_fp("act", instruction)
                                                _irrev = (
                                                    " Say it is IRREVERSIBLE."
                                                    if action_label == "critical" else ""
                                                )
                                                # NB: no leading [INTERNAL] — the sender adds it (avoids double prefix).
                                                _ask_text = (
                                                    f"{action_label} pending: '{description}'. "
                                                    f"Restate this action in ONE sentence IN THE OWNER'S preferred_language "
                                                    f"and ask them to confirm.{_irrev} "
                                                    f"Say it ONCE, in a single language, then WAIT. "
                                                    f"After the owner says yes, re-call execute_action with the EXACT SAME "
                                                    f"instruction and description and confirmed=true."
                                                )
                                                # Genuine confirmation = confirmed=true AND the owner RESPONDED in a turn
                                                # AFTER the ask (user_turn_count advanced) AND did not negate since. We no
                                                # longer require an exact "yes" keyword — ASR clips "oui" → "oi" and silently
                                                # blocked real confirmations. Same-turn self-confirm is still blocked (the
                                                # turn count has not advanced) and an explicit refusal still blocks it.
                                                # Match the exact fingerprint; fall back to the most recent pending ask when
                                                # the model rephrased the instruction (kills the drift re-ask loop).
                                                def _confirm_ok(_turn: int) -> bool:
                                                    return user_turn_count[0] > _turn and last_negate_turn[0] <= _turn
                                                _matched_fp = None
                                                if confirmed:
                                                    if _cfp in pending_confirm and _confirm_ok(pending_confirm[_cfp]):
                                                        _matched_fp = _cfp
                                                    elif pending_confirm:
                                                        _recent = max(pending_confirm, key=pending_confirm.get)
                                                        if _confirm_ok(pending_confirm[_recent]):
                                                            _matched_fp = _recent
                                                if _matched_fp is None and _is_duplicate_action(action_label, instruction):
                                                    # Already enqueued/in-flight — e.g. the backend confirm fallback
                                                    # (Prong 2) just fired this action and Gemini is now ALSO re-emitting
                                                    # confirmed=true. Do NOT re-ask; acknowledge it's already running.
                                                    pending_confirm.pop(_cfp, None)
                                                    pending_action_detail.pop(_cfp, None)
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="execute_action", id=fc.id,
                                                        response={"status": "already_running"},
                                                    ))
                                                    logger.info(
                                                        "execute_action dedup: already running (backend/re-emit) — %s — client=%s",
                                                        description[:40], client_id,
                                                    )
                                                elif _matched_fp is None:
                                                    # Initial ask OR confirm without a real 'yes' → ask and WAIT; never execute.
                                                    # Guard: if the worker is already busy, OR a confirmed=true arrives right
                                                    # after a confirmed fire (Prong 2 cleared pending_confirm and the text
                                                    # drifted so the fingerprint dedup missed), this is a duplicate of the
                                                    # just-fired action — respond already_running and NEVER re-ask. The
                                                    # spurious re-ask was the double-confirmation that also cut SAVANT off
                                                    # mid-sentence.
                                                    _recent_confirmed_fire = (
                                                        asyncio.get_event_loop().time() - _last_confirmed_fire_t[0]
                                                    ) < 60.0
                                                    if hermes_busy[0] or _recent_confirmed_fire:
                                                        fn_responses.append(types.FunctionResponse(
                                                            name="execute_action", id=fc.id,
                                                            response={"status": "already_running"},
                                                        ))
                                                        inject_queue.put_nowait(
                                                            "[INTERNAL] An action is already running. "
                                                            "Do NOT call execute_action again. "
                                                            "Wait silently for [ACTION RESULT]."
                                                        )
                                                        logger.info(
                                                            "confirmed re-emit after fire — deduped, no re-ask "
                                                            "(busy=%s) — %s — client=%s",
                                                            hermes_busy[0], description[:40], client_id,
                                                        )
                                                    else:
                                                        fn_responses.append(types.FunctionResponse(
                                                            name="execute_action", id=fc.id,
                                                            response={"status": "awaiting_confirmation"},
                                                        ))
                                                        if _cfp in pending_confirm and user_turn_count[0] <= pending_confirm[_cfp]:
                                                            # Same ask re-emitted with no new owner turn → stay silent (kills the repeat).
                                                            logger.info(
                                                                "confirm re-ask suppressed — %s — client=%s",
                                                                description[:40], client_id,
                                                            )
                                                        else:
                                                            pending_confirm[_cfp] = user_turn_count[0]
                                                            # Store full details so the backend can execute this on
                                                            # its own (Prong 2) if Gemini never re-emits confirmed=true.
                                                            pending_action_detail[_cfp] = {
                                                                "instruction": instruction,
                                                                "description": description,
                                                                "action_label": action_label,
                                                            }
                                                            write_confirm_inject = _ask_text
                                                            if confirmed:
                                                                logger.info(
                                                                    "confirm without owner 'yes' blocked, re-asking — %s — client=%s",
                                                                    description[:40], client_id,
                                                                )
                                                elif _is_duplicate_action(action_label, instruction):
                                                    # Confirmed but identical to an in-flight/recent action — ignore the re-emission.
                                                    pending_confirm.pop(_matched_fp, None)
                                                    pending_action_detail.pop(_matched_fp, None)
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="execute_action", id=fc.id,
                                                        response={"status": "already_running"},
                                                    ))
                                                    logger.info(
                                                        "execute_action dedup: duplicate %s ignored — %s — client=%s",
                                                        action_label, description[:40], client_id,
                                                    )
                                                else:
                                                    # Action fires — clear ALL pending confirms so no orphan
                                                    # fingerprint (from instruction drift) triggers a phantom re-ask.
                                                    pending_confirm.clear()
                                                    pending_action_detail.clear()
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="execute_action", id=fc.id,
                                                        response={"status": "pending"},
                                                    ))
                                                    await _fire_action(action_id, description, instruction, action_label)
                                            else:  # read
                                                # Idempotence: a re-emitted identical read is ignored, which
                                                # breaks the inject→re-emit→enqueue loop (the repetition bug).
                                                if _is_duplicate_action("read", instruction):
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="execute_action", id=fc.id,
                                                        response={"status": "already_running"},
                                                    ))
                                                    logger.info(
                                                        "execute_action dedup: duplicate read ignored — %s — client=%s",
                                                        description[:40], client_id,
                                                    )
                                                else:
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="execute_action", id=fc.id,
                                                        response={"status": "pending"},
                                                    ))
                                                    await websocket.send_text(json.dumps({
                                                        "type": "hermes_running",
                                                        "action_id": action_id,
                                                        "description": description,
                                                    }))
                                                    # Enqueue on the strict FIFO worker (one action at a time, in order)
                                                    _queued_behind = hermes_action_queue.qsize() + (1 if hermes_busy[0] else 0)
                                                    await _enqueue_action(action_id, description, instruction, "read")
                                                    if _queued_behind > 0:
                                                        hermes_queue_inject = (
                                                            f"'{description}' is queued behind the running task. "
                                                            "Tell the owner briefly you'll do it right after "
                                                            "(e.g. 'Je fais ça juste après.'). Do NOT report any result yet."
                                                        )

                                        elif fc.name == "run_command":
                                            command = (fc.args.get("command") or "").strip()
                                            purpose = (fc.args.get("purpose") or "").strip()
                                            if not command:
                                                fn_responses.append(types.FunctionResponse(
                                                    name="run_command", id=fc.id,
                                                    response={"status": "empty"},
                                                ))
                                            elif _is_catastrophic(command):
                                                fn_responses.append(types.FunctionResponse(
                                                    name="run_command", id=fc.id,
                                                    response={"status": "blocked"},
                                                ))
                                                # Routed through the reconciler (idle-gated, no-re-emit guard)
                                                hermes_queue_inject = (
                                                    f"The command `{command}` is catastrophic and was BLOCKED. "
                                                    "Tell the owner it's too dangerous to run directly; if they truly "
                                                    "want it they must confirm explicitly and you run it via "
                                                    "execute_action (critical) so it is backed up first. Do NOT retry run_command."
                                                )
                                            else:
                                                _silent = purpose.lstrip().lower().startswith("[internal]")
                                                await websocket.send_text(json.dumps({
                                                    "type": "command_running",
                                                    "command": command,
                                                    "purpose": purpose,
                                                    "silent": _silent,
                                                }))
                                                exec_running.set()
                                                try:
                                                    from backend.console import _exec_vps
                                                    _rc_t0 = time.monotonic()
                                                    _rc_res = await asyncio.wait_for(
                                                        _exec_vps(command, f"voice-{client_id}"),
                                                        timeout=12.0,
                                                    )
                                                    _rc_dur = time.monotonic() - _rc_t0
                                                    _rc_out = (_rc_res.get("stdout") or _rc_res.get("stderr") or "(no output)").strip()
                                                    _rc_err = (_rc_res.get("stderr") or "")
                                                    _rc_code = _rc_res.get("code", 0)
                                                    _rc_cwd = _rc_res.get("cwd", "/")
                                                    _rc_user = _rc_res.get("user", "root")
                                                    try:
                                                        await websocket.send_text(json.dumps({
                                                            "type": "command_result",
                                                            "command": command,
                                                            "stdout": _rc_out,
                                                            "stderr": _rc_err,
                                                            "code": _rc_code,
                                                            "cwd": _rc_cwd,
                                                            "user": _rc_user,
                                                            "silent": _silent,
                                                        }))
                                                    except Exception:
                                                        pass
                                                    try:
                                                        save_action_log(
                                                            uuid.uuid4().hex[:12], "command", command,
                                                            "ok" if _rc_code == 0 else "error",
                                                            round(_rc_dur, 1), 0,
                                                        )
                                                    except Exception:
                                                        pass
                                                    _rc_lines = [_l for _l in _rc_out.splitlines() if _l.strip()]
                                                    if not _silent and len(_rc_lines) >= 3:
                                                        _rc_fmt = "table" if ("\t" in _rc_out or bool(re.search(r"\s{3,}", _rc_out))) else ("markdown" if len(_rc_lines) > 5 else "text")
                                                        try:
                                                            await websocket.send_text(json.dumps({
                                                                "type": "show_data",
                                                                "title": command,
                                                                "content": _rc_out[:4000],
                                                                "format": _rc_fmt,
                                                            }))
                                                        except Exception:
                                                            pass
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="run_command", id=fc.id,
                                                        response={
                                                            "stdout": _rc_out[:1500],
                                                            "exit_code": _rc_code,
                                                            "cwd": _rc_cwd,
                                                        },
                                                    ))
                                                except asyncio.TimeoutError:
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="run_command", id=fc.id,
                                                        response={"status": "running"},
                                                    ))
                                                    _spawn_bg(_run_command_bg(
                                                        websocket, client_id, command, inject_queue,
                                                    ))
                                                except Exception as _rc_exc:
                                                    logger.warning("run_command fast-path error: %s", _rc_exc)
                                                    fn_responses.append(types.FunctionResponse(
                                                        name="run_command", id=fc.id,
                                                        response={"status": "error", "output": str(_rc_exc)[:200]},
                                                    ))
                                                finally:
                                                    # Drain queue BEFORE clearing exec_running to close the race
                                                    # window where a new audio frame could arrive between clear()
                                                    # and the list copy, causing it to be both sent directly and
                                                    # re-queued from the snapshot.
                                                    _held = _pending_user_turns[:]
                                                    _pending_user_turns.clear()
                                                    exec_running.clear()
                                                    if _held:
                                                        inject_queue.put_nowait(
                                                            "[QUEUED WHILE EXECUTING — address the owner's "
                                                            "question/comment below first, then report the command result]"
                                                        )
                                                        for _held_kind, _held_pl in _held:
                                                            cmd_queue.put_nowait((_held_kind, _held_pl))

                                        elif fc.name == "save_workflow":
                                            _r = wf_save(
                                                fc.args.get("name", ""), fc.args.get("url", ""),
                                                fc.args.get("description", ""),
                                                fc.args.get("auth_header") or None,
                                                fc.args.get("auth_key") or None,
                                            )
                                            fn_responses.append(types.FunctionResponse(
                                                name="save_workflow", id=fc.id,
                                                response={"status": _r["status"], "detail": _r["output"]},
                                            ))

                                        elif fc.name == "list_workflows":
                                            _r = wf_list()
                                            await websocket.send_text(json.dumps({
                                                "type": "show_data", "title": "Workflows",
                                                "content": _r["output"],
                                                "format": "table" if _r.get("count", 0) > 0 else "text",
                                            }))
                                            fn_responses.append(types.FunctionResponse(
                                                name="list_workflows", id=fc.id,
                                                response={
                                                    "status": _r["status"],
                                                    "count": _r.get("count", 0),
                                                    "names": _r.get("names", []),
                                                },
                                            ))

                                        elif fc.name == "run_workflow":
                                            _wf_name = (fc.args.get("name") or "").strip()
                                            fn_responses.append(types.FunctionResponse(
                                                name="run_workflow", id=fc.id,
                                                response={"status": "running"},
                                            ))
                                            await websocket.send_text(json.dumps({
                                                "type": "command_running",
                                                "command": f"workflow · {_wf_name}",
                                                "purpose": "",
                                            }))
                                            _spawn_bg(_run_workflow_bg(
                                                websocket, _wf_name, fc.args.get("payload"), inject_queue,
                                            ))

                                        elif fc.name == "load_skill":
                                            _sk_name = (fc.args.get("name") or "").strip()
                                            _sk_body = sk_load(_sk_name)
                                            if _sk_body:
                                                fn_responses.append(types.FunctionResponse(
                                                    name="load_skill", id=fc.id,
                                                    response={"status": "loaded"},
                                                ))
                                                # Body is procedural guidance — inject (idle-gated), never in the
                                                # FunctionResponse (which is vocalized in AUDIO mode).
                                                inject_queue.put_nowait(
                                                    f"[SKILL LOADED — {_sk_name}] Follow this procedure now; "
                                                    f"do NOT read it aloud:\n{_sk_body[:3000]}"
                                                )
                                            else:
                                                fn_responses.append(types.FunctionResponse(
                                                    name="load_skill", id=fc.id,
                                                    response={"status": "not_found"},
                                                ))

                                        elif fc.name == "save_skill":
                                            _ok = sk_save(
                                                fc.args.get("name", ""),
                                                fc.args.get("description", ""),
                                                fc.args.get("content", ""),
                                            )
                                            fn_responses.append(types.FunctionResponse(
                                                name="save_skill", id=fc.id,
                                                response={"status": "ok" if _ok else "error"},
                                            ))

                                        elif fc.name == "recall":
                                            _rq = (fc.args.get("query") or "").strip()
                                            _rns = (fc.args.get("namespace") or "").strip() or None
                                            _hits: list[dict] = []
                                            try:
                                                _hits = db_recall(_rq, limit=5, namespace=_rns)
                                            except Exception:
                                                logger.exception("recall failed — client=%s", client_id)
                                            if _hits:
                                                _disp = "\n".join(
                                                    f"• [{h['namespace']}] {h['key']}: {h['value']}" for h in _hits
                                                )
                                                try:
                                                    await websocket.send_text(json.dumps({
                                                        "type": "show_data",
                                                        "title": f"Recall: {_rq}"[:60],
                                                        "content": _disp, "format": "text",
                                                    }))
                                                except Exception:
                                                    pass
                                            fn_responses.append(types.FunctionResponse(
                                                name="recall", id=fc.id,
                                                response={"status": "ok", "matches": _hits, "total": len(_hits)},
                                            ))
                                            logger.info(
                                                "Tool: recall(q=%s ns=%s) — %d hits — client=%s",
                                                _rq[:40], _rns, len(_hits), client_id,
                                            )

                                        elif fc.name == "search_memory":
                                            query = (fc.args.get("query") or "").strip()
                                            mem_hits: list[dict] = []
                                            sess_hits: list[dict] = []
                                            try:
                                                results = db_search_memory(query, limit=3)
                                                mem_hits = results.get("memory", [])
                                                sess_hits = results.get("sessions", [])
                                            except Exception:
                                                logger.exception(
                                                    "search_memory failed — client=%s", client_id
                                                )

                                            # Build a compact display payload + a structured response for Gemini.
                                            display_lines: list[str] = []
                                            if mem_hits:
                                                display_lines.append("KEY FACTS")
                                                for m in mem_hits:
                                                    display_lines.append(
                                                        f"• {m['key']}: {m['value']}  [{m['updated_at'][:10]}]"
                                                    )
                                            if sess_hits:
                                                if display_lines:
                                                    display_lines.append("")
                                                display_lines.append("PAST SESSIONS")
                                                for s in sess_hits:
                                                    body = (s["summary"] or "").strip()
                                                    for tag in ("[llm] ", "[gemini] ", "[auto] "):
                                                        if body.lower().startswith(tag):
                                                            body = body[len(tag):]
                                                            break
                                                    body = " ".join(
                                                        p.strip() for p in body.splitlines() if p.strip()
                                                    )
                                                    display_lines.append(f"[{s['date']}] {body}")
                                            if not display_lines:
                                                display_lines.append(f"No memory or session matches for '{query}'.")

                                            display_content = "\n".join(display_lines)
                                            try:
                                                await websocket.send_text(json.dumps({
                                                    "type": "show_data",
                                                    "title": f"Memory search: {query}"[:60],
                                                    "content": display_content,
                                                    "format": "text",
                                                }))
                                            except Exception:
                                                pass

                                            fn_responses.append(types.FunctionResponse(
                                                name="search_memory", id=fc.id,
                                                response={
                                                    "status": "ok",
                                                    "query": query,
                                                    "memory_matches": mem_hits,
                                                    "session_matches": sess_hits,
                                                    "total": len(mem_hits) + len(sess_hits),
                                                },
                                            ))
                                            logger.info(
                                                "Tool: search_memory(q=%s) — %d mem + %d sess — client=%s",
                                                query[:40], len(mem_hits), len(sess_hits), client_id,
                                            )
                                        elif fc.name == "show_data":
                                            title = fc.args.get("title", "Data")
                                            content = fc.args.get("content", "")
                                            fmt = fc.args.get("format", "text")
                                            await websocket.send_text(json.dumps({
                                                "type": "show_data",
                                                "title": title,
                                                "content": content,
                                                "format": fmt,
                                            }))
                                            fn_responses.append(types.FunctionResponse(
                                                name="show_data", id=fc.id,
                                                response={"status": "ok"},
                                            ))
                                    if fn_responses:
                                        await session.send_tool_response(
                                            function_responses=fn_responses
                                        )
                                    if write_confirm_inject:
                                        try:
                                            await session.send_client_content(
                                                turns=[types.Content(role="user", parts=[types.Part(
                                                    text=f"[INTERNAL] {write_confirm_inject}"
                                                )])],
                                                turn_complete=True,
                                            )
                                        except Exception:
                                            logger.exception(
                                                "Write confirm injection failed — client=%s", client_id
                                            )
                                    if hermes_queue_inject:
                                        # Route through the reconciler so it is coalesced, idle-gated,
                                        # and carries the no-re-emission guard (no extra turn reopen).
                                        inject_queue.put_nowait(f"[INTERNAL] {hermes_queue_inject}")
                                    if invalid_reask_inject:
                                        try:
                                            await session.send_client_content(
                                                turns=[types.Content(role="user", parts=[types.Part(
                                                    text=f"[INTERNAL] {invalid_reask_inject}"
                                                )])],
                                                turn_complete=True,
                                            )
                                        except Exception:
                                            logger.exception(
                                                "Invalid re-ask injection failed — client=%s", client_id
                                            )
                                    if _pause_deferred_inject:
                                        try:
                                            await session.send_client_content(
                                                turns=[types.Content(role="user", parts=[types.Part(
                                                    text="[INTERNAL] Say 'D\\'accord, je patiente.' then silent."
                                                )])],
                                                turn_complete=True,
                                            )
                                        except Exception:
                                            logger.warning("Pause inject failed — client=%s", client_id)
                                        # Snapshot chunk counter — we'll wait for it to grow before considering SAVANT spoke
                                        _start_chunk = audio_chunk_count[0]
                                        gemini_idle.clear()

                                        async def _deferred_pause_flag(start_chunk: int = _start_chunk) -> None:
                                            # Loop: each turn_complete unblocks gemini_idle. Only stop when
                                            # the turn actually produced audio (chunk counter grew).
                                            deadline = asyncio.get_event_loop().time() + 15.0
                                            while True:
                                                remaining = deadline - asyncio.get_event_loop().time()
                                                if remaining <= 0:
                                                    break
                                                try:
                                                    await asyncio.wait_for(gemini_idle.wait(), timeout=remaining)
                                                except asyncio.TimeoutError:
                                                    break
                                                if audio_chunk_count[0] > start_chunk:
                                                    break  # SAVANT actually spoke — done
                                                # Empty turn (FunctionResponse ack with no audio) — reset and wait again
                                                gemini_idle.clear()
                                            # R1: if the owner already resumed while we were waiting,
                                            # pause_pending was cleared — do NOT re-pause (would drop audio).
                                            if not pause_pending[0]:
                                                return
                                            savant_paused[0] = True
                                            _rr2 = await _get_redis()
                                            if _rr2:
                                                await _rr2.set("savant:paused", "1")
                                                await _rr2.aclose()
                                            try:
                                                await websocket.send_text(json.dumps({
                                                    "type": "savant_paused", "paused": True,
                                                }))
                                            except Exception:
                                                pass

                                        _dp = asyncio.create_task(_deferred_pause_flag())
                                        active_hermes_tasks.add(_dp)
                                        _dp.add_done_callback(active_hermes_tasks.discard)
                                    if onboarding_next_inject:
                                        try:
                                            await session.send_client_content(
                                                turns=[types.Content(role="user", parts=[types.Part(
                                                    text=f"[INTERNAL] {onboarding_next_inject}"
                                                )])],
                                                turn_complete=True,
                                            )
                                        except Exception:
                                            logger.exception(
                                                "Onboarding next-question injection failed — client=%s", client_id
                                            )
                                    if language_switch_value:
                                        try:
                                            _next_missing = get_onboarding_status()["missing"]
                                            _next_key = _next_missing[0] if _next_missing else None
                                            _next_prompt = {
                                                "owner_name": "Ask: what should I call you?",
                                                "timezone": "Ask: what's your timezone? (e.g. Europe/Paris)",
                                            }.get(_next_key, "")
                                            await session.send_client_content(
                                                turns=[types.Content(role="user", parts=[types.Part(
                                                    text=(
                                                        f"[INTERNAL] Language={language_switch_value}. Switch permanently. "
                                                        + (f"In {language_switch_value}, {_next_prompt}" if _next_prompt else "")
                                                    )
                                                )])],
                                                turn_complete=True,
                                            )
                                        except Exception:
                                            logger.exception(
                                                "Language switch injection failed — client=%s", client_id
                                            )
                                    if onboarding_just_completed:
                                        try:
                                            mem = load_memory()
                                            _owner = get_memory_value("owner_name") or "there"
                                            await session.send_client_content(
                                                turns=[types.Content(role="user", parts=[types.Part(
                                                    text=(
                                            f"[INTERNAL] Onboarding complete. Profile:\n{mem}\n\n"
                                            f"Say a brief casual transition like: 'Parfait {_owner}, je suis prêt.' "
                                            "Then call execute_action(read, description='VPS Quick Vision', "
                                            f"instruction={_VPS_VISION_INSTRUCTION!r}). "
                                            "On [ACTION RESULT]: ONE sentence VPS status, then ask what they plan. "
                                            "Full Vision is being generated in background."
                                        )
                                                )])],
                                                turn_complete=True,
                                            )
                                            # Full Vision generated in background after onboarding
                                            _spawn_bg(call_hermes(
                                                "python3 /host/home/savant/scripts/vps_full_vision.py",
                                                session_id="savant-full-vision-init",
                                            ))
                                            logger.info("Full Vision background task launched after onboarding — client=%s", client_id)
                                        except Exception:
                                            logger.exception(
                                                "Memory refresh after onboarding failed — client=%s", client_id
                                            )

                                if response.data:
                                    gemini_idle.clear()  # Gemini is generating audio
                                    audio_chunk_count[0] += 1
                                    if not savant_paused[0]:
                                        await websocket.send_bytes(response.data)

                                sc = response.server_content
                                if sc:
                                    # Gemini detected the owner speaking and stopped its own turn —
                                    # tell the PWA to flush buffered audio NOW (true barge-in, echo-immune
                                    # because the model decides, not a client RMS threshold).
                                    if getattr(sc, "interrupted", False):
                                        gemini_idle.set()
                                        try:
                                            await websocket.send_text(json.dumps({"type": "interrupted"}))
                                        except Exception:
                                            pass
                                    if sc.output_transcription and sc.output_transcription.text:
                                        text = _strip_tool_calls(sc.output_transcription.text)
                                        if text:
                                            _savant_buf.append(text)
                                            # HARD SAFETY NET: a "paused but speaking" state must never
                                            # exist. If SAVANT produces a substantive turn while paused
                                            # (Gemini resumed conversationally without us catching the
                                            # resume phrase), auto-resume NOW so the audio is heard instead
                                            # of silently dropped. Detect at the first substantive chunk so
                                            # almost no audio is lost. The pause ack ("D'accord.") never
                                            # triggers this — savant_paused is only set True AFTER that turn.
                                            if savant_paused[0] and len("".join(_savant_buf).strip()) > 15:
                                                pause_pending[0] = False
                                                savant_paused[0] = False
                                                _rrp = await _get_redis()
                                                if _rrp:
                                                    await _rrp.delete("savant:paused")
                                                    await _rrp.aclose()
                                                try:
                                                    await websocket.send_text(json.dumps({
                                                        "type": "savant_paused", "paused": False,
                                                    }))
                                                except Exception:
                                                    pass
                                                logger.info("Auto-resume: SAVANT spoke while paused — client=%s", client_id)
                                            await websocket.send_text(
                                                json.dumps({
                                                    "type": "transcript",
                                                    "role": "assistant",
                                                    "text": text,
                                                })
                                            )
                                    if sc.input_transcription and sc.input_transcription.text:
                                        text = sc.input_transcription.text
                                        _user_buf.append(text)
                                        await websocket.send_text(
                                            json.dumps({
                                                "type": "transcript",
                                                "role": "user",
                                                "text": text,
                                            })
                                        )
                                        # Immediate resume: clear pause as soon as the resume phrase is
                                        # recognised, BEFORE the model's audio for this turn arrives.
                                        # Otherwise the flag is still True when response.data flows and the
                                        # whole resume turn gets dropped — owner sees text but hears nothing.
                                        if savant_paused[0] or pause_pending[0]:
                                            if match_phrase("".join(_user_buf), _RESUME_PHRASES):
                                                pause_pending[0] = False
                                                savant_paused[0] = False
                                                _rru = await _get_redis()
                                                if _rru:
                                                    await _rru.delete("savant:paused")
                                                    await _rru.aclose()
                                                try:
                                                    await websocket.send_text(json.dumps({
                                                        "type": "savant_paused", "paused": False,
                                                    }))
                                                except Exception:
                                                    pass
                                                logger.info("Resume via input transcription (immediate) — client=%s", client_id)
                                    if sc.turn_complete:
                                        gemini_idle.set()  # Turn complete — Gemini is idle
                                        _savant_line = "".join(_savant_buf)  # capture before clear (phantom-action net)
                                        if _savant_buf:
                                            session_transcript.append(f"SAVANT: {_savant_line}")
                                            _savant_buf.clear()
                                        _user_line = ""
                                        if _user_buf:
                                            _user_line = "".join(_user_buf)
                                            session_transcript.append(f"User: {_user_line}")
                                            _user_buf.clear()
                                            # The owner actually spoke this turn — drives the
                                            # confirmation gate (a confirm must follow a real user turn).
                                            user_turn_count[0] += 1
                                            # Affirmation / negation tracking (ASR-tolerant). An affirm wins over a
                                            # negation in the same line ("non mais oui vas-y" = yes). Negation blocks a
                                            # pending confirm and clears it (the owner refused whatever was pending).
                                            _is_affirm = match_phrase(_user_line, _AFFIRM_PHRASES)
                                            _is_negate = (not _is_affirm) and match_phrase(_user_line, _NEGATION_PHRASES)
                                            if _is_affirm:
                                                last_affirm_turn[0] = user_turn_count[0]
                                            elif _is_negate:
                                                last_negate_turn[0] = user_turn_count[0]
                                                if pending_confirm:
                                                    pending_confirm.clear()
                                                    pending_action_detail.clear()
                                                    logger.info("pending confirm cleared by owner negation — client=%s", client_id)
                                            # Prong 2 — backend-driven confirm fallback. The owner affirmed and a recent
                                            # write/critical ask is pending, but Gemini may NOT re-emit confirmed=true
                                            # (it sometimes just speaks). Execute the STORED action ourselves so a
                                            # confirmation never silently fails. Queues behind a running read (no busy
                                            # guard). A later Gemini re-emit is deduped to a no-op.
                                            if _is_affirm and pending_confirm and pending_action_detail:
                                                _fp_recent = max(pending_confirm, key=pending_confirm.get)
                                                _det = pending_action_detail.get(_fp_recent)
                                                _ask_turn = pending_confirm.get(_fp_recent, 0)
                                                if (_det is not None
                                                        and user_turn_count[0] - _ask_turn == 1
                                                        and last_negate_turn[0] <= _ask_turn
                                                        and not _is_duplicate_action(_det["action_label"], _det["instruction"])):
                                                    pending_confirm.clear()
                                                    pending_action_detail.clear()
                                                    _aid_fb = uuid.uuid4().hex[:8]
                                                    logger.info(
                                                        "backend confirm fallback fired — %s — client=%s",
                                                        _det["description"][:40], client_id,
                                                    )
                                                    await _fire_action(
                                                        _aid_fb, _det["description"],
                                                        _det["instruction"], _det["action_label"],
                                                    )
                                            # Spoken stop ("arrête l'action", "annule"…). Only acts when an
                                            # action is actually in flight/queued, so over-matching is a no-op.
                                            # This is the ONLY path that cancels a verbally-aborted action —
                                            # without it the abandoned action runs to completion and narrates
                                            # its (now-wrong) result.
                                            if (active_hermes_tasks or not hermes_action_queue.empty()) \
                                                    and match_phrase(_user_line, _STOP_PHRASES):
                                                _n = await _do_stop()
                                                logger.info(
                                                    "Spoken stop — cancelled %d action(s) — client=%s",
                                                    _n, client_id,
                                                )
                                        # Backend fallback for hands-free pause/resume: don't rely solely
                                        # on Gemini calling save_memory('paused',...) — match the owner's
                                        # transcript directly so the PWA log/overlay always fire.
                                        if _user_line:
                                            if savant_paused[0] or pause_pending[0]:
                                                if match_phrase(_user_line, _RESUME_PHRASES):
                                                    pause_pending[0] = False
                                                    savant_paused[0] = False
                                                    _rr3 = await _get_redis()
                                                    if _rr3:
                                                        await _rr3.delete("savant:paused")
                                                        await _rr3.aclose()
                                                    try:
                                                        await websocket.send_text(json.dumps({
                                                            "type": "savant_paused", "paused": False,
                                                        }))
                                                    except Exception:
                                                        pass
                                                    logger.info("Resume via transcript fallback — client=%s", client_id)
                                            elif match_phrase(_user_line, _PAUSE_PHRASES):
                                                pause_pending[0] = True
                                                savant_paused[0] = True
                                                _rr3 = await _get_redis()
                                                if _rr3:
                                                    await _rr3.set("savant:paused", "1")
                                                    await _rr3.aclose()
                                                try:
                                                    await websocket.send_text(json.dumps({
                                                        "type": "savant_paused", "paused": True,
                                                    }))
                                                except Exception:
                                                    pass
                                                logger.info("Pause via transcript fallback — client=%s", client_id)
                                            # Proactive-memory safety net: the owner asked to remember
                                            # something (or stated a durable fact) → force the save
                                            # in-session so it's never lost, even if Gemini doesn't
                                            # initiate it. Goes through the reconciler (idle-gated,
                                            # ordered). Skipped while paused (handled by the if above).
                                            elif match_phrase(_user_line, _REMEMBER_TRIGGERS):
                                                inject_queue.put_nowait(
                                                    "[INTERNAL] The owner just gave a durable fact to remember. "
                                                    "Call save_memory(key, value) NOW with a clear snake_case key "
                                                    "(e.g. home_city) capturing it, then confirm in ONE short sentence."
                                                )
                                                logger.info("Remember-trigger → forcing save_memory — client=%s", client_id)
                                        # Phantom-action net: SAVANT announced a lookup ('je regarde…')
                                        # but emitted NO execute_action this turn → nothing runs and no
                                        # data will reach the panel. Force the call. Mirrors the
                                        # remember-trigger net above; goes through the idle-gated
                                        # reconciler. Heavily guarded to avoid false positives: no action
                                        # in flight, none completed in the last ~15s (so a result-ack turn
                                        # never re-triggers), no pending write-confirm (write/critical must
                                        # only ask and wait), not paused, session verified.
                                        _recent_action = any(
                                            asyncio.get_event_loop().time() - _t < 15.0
                                            for _t in job_recent.values()
                                        )
                                        if (
                                            session_verified.is_set()
                                            and not savant_paused[0]
                                            and not exec_called_since_user[0]
                                            and _savant_line
                                            and match_phrase(_savant_line, _ACTION_INTENT_PHRASES)
                                            and not hermes_busy[0]
                                            and hermes_action_queue.empty()
                                            and not job_inflight
                                            and not _recent_action
                                            and not pending_confirm
                                        ):
                                            inject_queue.put_nowait(
                                                "[INTERNAL] You said you would fetch/look something up but you did NOT "
                                                "call execute_action — nothing is running and no data will arrive. Call "
                                                "execute_action(read, …) NOW for what you just announced. Do not announce "
                                                "it again, just call it."
                                            )
                                            logger.info("Phantom action net → forcing execute_action — client=%s", client_id)
                                        # Re-arm the net for the next turn (this turn's call, if any, counted above).
                                        exec_called_since_user[0] = False
                                        await websocket.send_text(
                                            json.dumps({"type": "turn_complete"})
                                        )

                    sender_task = asyncio.create_task(_audio_sender())
                    receiver_task = asyncio.create_task(_gemini_receiver())

                    done, _ = await asyncio.wait(
                        [sender_task, receiver_task, pwa_reader_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in done:
                        if task is not pwa_reader_task and not task.cancelled():
                            exc = task.exception()
                            if exc:
                                logger.error(
                                    "Gemini task error — client=%s: %s", client_id, exc
                                )

                    for task in (sender_task, receiver_task):
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass

            except Exception:
                if pwa_done.is_set():
                    break
                logger.exception("Gemini session error — client=%s", client_id)
                await asyncio.sleep(1)
                continue

            if pwa_done.is_set():
                break
            if go_away.is_set():
                logger.info(
                    "Reconnecting after GoAway — client=%s handle=%s…",
                    client_id,
                    session_handle[:8] if session_handle else "none",
                )
                continue
            break

    except Exception:
        logger.exception("Unexpected error in WS handler — client=%s", client_id)
    finally:
        # Release the active-session slot so the background monitor can resume.
        if session_verified.is_set():
            global _active_sessions
            _active_sessions = max(0, _active_sessions - 1)

        # Persist the full transcript to a per-session file (gzipped if large).
        try:
            _write_session_transcript(current_session_id, client_id, session_transcript)
        except Exception:
            logger.exception("Failed to write session transcript file — client=%s", client_id)

        # Auto-save session transcript if SAVANT didn't call save_session_summary
        if not session_summary_saved and session_transcript:
            try:
                llm_summary = await _llm_summarize_transcript(session_transcript)
                if llm_summary:
                    final_summary = f"[llm] {llm_summary}"
                else:
                    lines = session_transcript[:8]
                    final_summary = f"[auto] {(' | '.join(lines))[:300]}"
                if current_session_id:
                    close_session(current_session_id, final_summary)
                else:
                    save_session_summary(final_summary)
                logger.info(
                    "Auto-saved session summary (%s) — client=%s",
                    "llm" if llm_summary else "auto", client_id,
                )
            except Exception:
                logger.exception("Failed to auto-save session — client=%s", client_id)

        # A3: extract durable personal facts into discrete memory keys (proactive memory).
        # Best-effort — never blocks/raises out of the close path.
        if session_transcript:
            try:
                _facts = await _llm_extract_facts(session_transcript)
                for _k, _v in _facts.items():
                    save_memory(_k, _v)
                if _facts:
                    logger.info("Extracted %d durable fact(s) to memory — client=%s",
                                len(_facts), client_id)
            except Exception:
                logger.warning("Fact extraction failed — client=%s", client_id)

        # Stop the FIFO worker and cancel any still-running Hermes background tasks
        hermes_worker_task.cancel()
        if active_hermes_tasks:
            for _t in list(active_hermes_tasks):
                _t.cancel()
            await asyncio.gather(*list(active_hermes_tasks), return_exceptions=True)
            active_hermes_tasks.clear()
            logger.info("Hermes background tasks cancelled — client=%s", client_id)
        try:
            await hermes_worker_task
        except (asyncio.CancelledError, Exception):
            pass

        reconciler_task.cancel()
        pwa_reader_task.cancel()
        auto_pause_task.cancel()
        metrics_loop_task.cancel()
        # Await all cancelled tasks so their CancelledError is consumed (no
        # "Task was destroyed but it is pending!" warnings, clean teardown).
        await asyncio.gather(
            reconciler_task, pwa_reader_task, auto_pause_task, metrics_loop_task,
            return_exceptions=True,
        )
        logger.info("WS handler done — client=%s", client_id)
