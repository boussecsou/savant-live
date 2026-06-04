"""Direct Notion API fast-read lane for SAVANT.

Reading Notion through the Hermes agent loop + MCP is slow (~1-2 min). For READ-only
intents (search pages, fetch a page's content) we hit the Notion REST API directly from
the backend → ~1-3s. Anything that fails here returns None so the caller transparently
falls back to the (slow but capable) Hermes path. Never used for writes.

Requires NOTION_API_KEY in the backend env (same token Hermes uses).
"""

import os
import re
import logging

import httpx

logger = logging.getLogger(__name__)

_NOTION_TOKEN = os.getenv("NOTION_API_KEY", "").strip()
_NOTION_VERSION = "2022-06-28"
_BASE = "https://api.notion.com/v1"

# Words that signal the owner wants a page's CONTENT (not just a list of matches).
_CONTENT_HINTS = (
    "contenu", "content", "lis", "lire", "read", "explique", "résume", "resume",
    "summary", "détail", "detail", "what are", "what's in", "qu'est-ce", "ouvre", "open",
)
# Noise to strip when deriving a search query from a free-text instruction.
_NOISE = (
    "in notion", "dans notion", "notion", "search", "cherche", "recherche", "find",
    "fetch", "page", "la page", "the page", "lis", "lire", "read", "contenu", "content",
    "of", "de", "du", "des", "the", "my", "mes", "mon", "ma", "tasks", "task", "tâches",
    "explique", "résume", "resume", "moi", "me", "give", "donne", "list", "liste",
)


def notion_fast_enabled() -> bool:
    return bool(_NOTION_TOKEN)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_NOTION_TOKEN}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _derive_query(instruction: str) -> str:
    q = instruction.lower()
    q = re.sub(r"[\"'«»“”]", " ", q)
    for n in _NOISE:
        q = re.sub(rf"\b{re.escape(n)}\b", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:80]


def _title_of(obj: dict) -> str:
    props = obj.get("properties", {}) or {}
    for v in props.values():
        if isinstance(v, dict) and v.get("type") == "title":
            rt = v.get("title", [])
            if rt:
                return "".join(t.get("plain_text", "") for t in rt).strip() or "(untitled)"
    # databases carry their title at the top level
    rt = obj.get("title", [])
    if rt:
        return "".join(t.get("plain_text", "") for t in rt).strip() or "(untitled)"
    return "(untitled)"


def _blocks_to_md(blocks: list) -> str:
    lines: list[str] = []
    for b in blocks:
        bt = b.get("type", "")
        data = b.get(bt, {}) or {}
        txt = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
        if bt.startswith("heading_"):
            lvl = bt.split("_")[-1]
            lines.append(f"{'#' * (int(lvl) + 1)} {txt}")
        elif bt == "bulleted_list_item":
            lines.append(f"- {txt}")
        elif bt == "numbered_list_item":
            lines.append(f"1. {txt}")
        elif bt == "to_do":
            done = "x" if data.get("checked") else " "
            lines.append(f"- [{done}] {txt}")
        elif bt == "quote":
            lines.append(f"> {txt}")
        elif bt == "code":
            lines.append(f"```\n{txt}\n```")
        elif bt == "divider":
            lines.append("---")
        elif txt:
            lines.append(txt)
    return "\n".join(l for l in lines if l is not None)


async def notion_read(instruction: str) -> dict | None:
    """Try a fast direct Notion read. Returns {"status","output"} on success, or None
    to signal the caller to fall back to the Hermes path."""
    if not _NOTION_TOKEN:
        return None
    query = _derive_query(instruction)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            sr = await client.post(
                f"{_BASE}/search",
                headers=_headers(),
                json={"query": query, "page_size": 8,
                      "sort": {"direction": "descending", "timestamp": "last_edited_time"}},
            )
            sr.raise_for_status()
            results = sr.json().get("results", [])
            if not results:
                # Genuinely empty — report it (don't fall back; Hermes would also find nothing).
                return {"status": "ok",
                        "output": f"Aucune page Notion trouvée pour « {query} ». "
                                  "Elles ne sont peut-être pas partagées avec l'intégration."}

            want_content = any(h in instruction.lower() for h in _CONTENT_HINTS)
            if want_content:
                top = next((r for r in results if r.get("object") == "page"), results[0])
                pid = top.get("id")
                title = _title_of(top)
                br = await client.get(
                    f"{_BASE}/blocks/{pid}/children",
                    headers=_headers(), params={"page_size": 100},
                )
                br.raise_for_status()
                body = _blocks_to_md(br.json().get("results", []))
                out = f"# {title}\n\n{body[:4000]}" if body else \
                    f"# {title}\n\n(page vide ou contenu non textuel)"
                return {"status": "ok", "output": out}

            # Otherwise: a clean list of matches (title + url).
            rows = ["| Page | Lien |", "| --- | --- |"]
            for r in results[:8]:
                rows.append(f"| {_title_of(r)} | {r.get('url', '')} |")
            return {"status": "ok", "output": "\n".join(rows)}
    except Exception as e:
        logger.warning("Notion fast-read failed (%s) — falling back to Hermes", str(e)[:80])
        return None
