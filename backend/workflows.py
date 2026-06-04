"""Workflow registry — voice-registered n8n (or any) webhooks.

The owner registers a webhook by voice (name + URL + description); SAVANT stores it
and can trigger it later, getting the response back in real time. Triggers fire a
DIRECT httpx call from the backend — NOT through Hermes — so it's fast and the result
comes straight back. Storage lives in SQLite (workflows table, see database.py).

This replaces the old Hermes-routed connector registry for the webhook use case.
The optional auth_header/auth_key cover authenticated endpoints; the key is stored for
the trigger call only and is NEVER echoed back to the owner or the panel.
"""

import json
import logging

import httpx

from backend.memory.database import (
    save_workflow as _db_save_workflow,
    get_workflow as _db_get_workflow,
    list_workflows as _db_list_workflows,
    delete_workflow as _db_delete_workflow,
)

logger = logging.getLogger("savant")

_TRIGGER_TIMEOUT_S = 30.0
_OUTPUT_CAP = 4000


def save_workflow(name: str, url: str, description: str = "",
                  auth_header: str | None = None, auth_key: str | None = None) -> dict:
    """Register/replace a webhook workflow. Returns a confirmation (never the key)."""
    name = (name or "").strip()
    url = (url or "").strip()
    if not name or not url:
        return {"status": "error", "output": "Workflow needs both a name and a URL."}
    if not url.lower().startswith(("http://", "https://")):
        return {"status": "error", "output": "Workflow URL must start with http:// or https://."}
    _db_save_workflow(name, url, description, auth_header, auth_key)
    return {"status": "ok", "output": f"Workflow « {name} » enregistré ({url})."}


def list_workflows() -> dict:
    """List registered workflows (name + description + URL only — never keys)."""
    rows = _db_list_workflows()
    if not rows:
        return {"status": "ok", "output": "Aucun workflow enregistré.", "count": 0, "names": []}
    lines = ["| Workflow | Description | URL |", "| --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['name']} | {r.get('description', '')} | {r['url']} |")
    return {
        "status": "ok",
        "output": "\n".join(lines),
        "count": len(rows),
        "names": [r["name"] for r in rows],
    }


def delete_workflow(name: str) -> dict:
    _db_delete_workflow(name)
    return {"status": "ok", "output": f"Workflow « {name} » supprimé."}


async def run_workflow(name: str, payload: dict | str | None = None) -> dict:
    """Trigger a registered webhook and return its response.

    Returns {"status": "ok"|"error", "output": <text>}. POSTs the payload as JSON
    (if any), otherwise GETs the URL. Auth header attached when configured."""
    name = (name or "").strip()
    wf = _db_get_workflow(name)
    if not wf:
        return {"status": "error",
                "output": f"Aucun workflow « {name} ». Demande-moi de l'enregistrer d'abord."}

    headers = {}
    if wf.get("auth_header") and wf.get("auth_key"):
        headers[wf["auth_header"]] = wf["auth_key"]

    # Accept a JSON string from the model and coerce it to an object when possible.
    body = payload
    if isinstance(payload, str):
        s = payload.strip()
        if s:
            try:
                body = json.loads(s)
            except Exception:
                body = {"text": s}
        else:
            body = None

    try:
        async with httpx.AsyncClient(timeout=_TRIGGER_TIMEOUT_S) as client:
            if body is not None:
                r = await client.post(wf["url"], headers=headers, json=body)
            else:
                r = await client.get(wf["url"], headers=headers)
            r.raise_for_status()
            text = (r.text or "").strip()
            # Pretty-print JSON responses; leave plain text as-is.
            try:
                text = json.dumps(r.json(), ensure_ascii=False, indent=2)
            except Exception:
                pass
            out = text[:_OUTPUT_CAP] if text else f"Workflow « {name} » déclenché (HTTP {r.status_code}, pas de corps)."
            return {"status": "ok", "output": out}
    except httpx.HTTPStatusError as e:
        logger.warning("Workflow '%s' HTTP error: %s", name, e)
        return {"status": "error",
                "output": f"Le workflow « {name} » a répondu en erreur (HTTP {e.response.status_code})."}
    except Exception as e:
        logger.warning("Workflow '%s' trigger failed: %s", name, str(e)[:120])
        return {"status": "error", "output": f"Échec du déclenchement de « {name} » : {str(e)[:120]}."}
