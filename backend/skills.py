"""Skills system — markdown procedural docs SAVANT loads on demand.

A skill is a markdown file with a tiny front-matter header (name + description) and a
body of step-by-step guidance. Only the INDEX (names + descriptions) is injected into
the system prompt — cheap. SAVANT calls load_skill(name) to pull a full procedure into
context right before it needs it, and can save_skill(...) to capture a new procedure
after solving a hard problem so it's reusable next time.

Files live in SKILLS_DIR (bind-mounted /app/skills, read-write). Filenames are derived
from a sanitized slug, so a model-supplied name can never escape the directory.
"""

import os
import re
import logging

logger = logging.getLogger("savant")

SKILLS_DIR = os.getenv("SKILLS_DIR", "/app/skills")


def _slugify(name: str) -> str:
    """Lowercase kebab slug, [a-z0-9-] only — the only filename source (no traversal)."""
    s = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64]


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a leading `--- ... ---` front-matter block. Returns (meta, body)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta: dict[str, str] = {}
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def skills_index() -> str:
    """Compact index (names + descriptions only) for the system prompt, or '' if none."""
    try:
        files = sorted(f for f in os.listdir(SKILLS_DIR) if f.endswith(".md"))
    except Exception:
        return ""
    entries: list[str] = []
    for f in files:
        try:
            with open(os.path.join(SKILLS_DIR, f)) as fh:
                meta, _ = _parse_front_matter(fh.read(2000))
        except Exception:
            continue
        name = meta.get("name") or f[:-3]
        desc = meta.get("description") or ""
        entries.append(f"- {name}: {desc}" if desc else f"- {name}")
    if not entries:
        return ""
    return (
        "## SKILLS (load on demand)\n"
        "Before tackling one of these, call load_skill(name) to pull the full procedure "
        "into context, then follow it:\n" + "\n".join(entries)
    )


def load_skill(name: str) -> str | None:
    """Return a skill's body (without front-matter), or None if absent."""
    slug = _slugify(name)
    if not slug:
        return None
    path = os.path.join(SKILLS_DIR, slug + ".md")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            _, body = _parse_front_matter(fh.read())
        return body.strip() or None
    except Exception:
        return None


def save_skill(name: str, description: str, content: str) -> bool:
    """Write/overwrite a skill markdown file. Returns False on bad input or I/O error."""
    slug = _slugify(name)
    if not slug or not (content or "").strip():
        return False
    path = os.path.join(SKILLS_DIR, slug + ".md")
    # Defense in depth: the sanitized slug already prevents traversal; re-confirm the
    # resolved file stays inside SKILLS_DIR before writing.
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(SKILLS_DIR):
        return False
    try:
        os.makedirs(SKILLS_DIR, exist_ok=True)
        doc = (
            f"---\nname: {slug}\n"
            f"description: {(description or '').strip()[:200]}\n---\n\n"
            f"{content.strip()}\n"
        )
        with open(path, "w") as fh:
            fh.write(doc)
        return True
    except Exception:
        logger.warning("save_skill failed for slug=%s", slug)
        return False
