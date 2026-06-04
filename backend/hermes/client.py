"""Hermes v2 client — SAVANT execution layer."""
import json
import os
from typing import Callable, Awaitable, Optional
import httpx
import logging

logger = logging.getLogger(__name__)

HERMES_URL = os.getenv("HERMES_URL", "http://hermes-v2:8642")
HERMES_SECRET = os.getenv("HERMES_WEBHOOK_SECRET", "")


def _parse_output(text: str) -> dict:
    """Apply the savant-executor JSON contract check. Returns {"status", "output"}."""
    stripped = (text or "").strip()
    # Strip markdown code fences that models sometimes wrap JSON in (```json { } ```)
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        end = len(lines) - 1 if lines and lines[-1].strip() == "```" else len(lines)
        stripped = "\n".join(lines[1:end]).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            inner_status = parsed.get("status", "ok")
            inner_output = parsed.get("output", text)
            if inner_status not in ("ok", "error"):
                inner_status = "ok"
            return {"status": inner_status, "output": str(inner_output)}
        except json.JSONDecodeError:
            pass
    return {"status": "ok", "output": text}


async def call_hermes(
    instruction: str,
    session_id: str = "savant",
    timeout: float = 600.0,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> dict:
    """Send an instruction to Hermes v2 via the OpenAI-compatible gateway API.

    With on_chunk: streams SSE tokens; closing the TCP connection on
    asyncio.CancelledError gives real server-side cancellation.

    Returns {"status": "ok"|"error", "output": str}.
    """
    _headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {HERMES_SECRET}",
        "X-Hermes-Session-Id": session_id,
    }
    _body = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": instruction}],
        "stream": True,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            async with client.stream(
                "POST",
                f"{HERMES_URL}/v1/chat/completions",
                headers=_headers,
                json=_body,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "event-stream" in content_type:
                    # SSE streaming — real cancellation: closing the loop drops the
                    # TCP connection and Hermes stops generating.
                    chunks: list[str] = []
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            break
                        try:
                            delta = json.loads(raw)["choices"][0]["delta"].get("content") or ""
                            if delta:
                                chunks.append(delta)
                                if on_chunk:
                                    await on_chunk(delta)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
                    text = "".join(chunks)
                else:
                    # Gateway returned non-streaming JSON — read full body and parse.
                    await response.aread()
                    data = response.json()
                    text = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    if on_chunk and text:
                        await on_chunk(text)
        return _parse_output(text)
    except httpx.TimeoutException:
        logger.error("Hermes timeout (%.1fs) — instruction: %.50s", timeout, instruction)
        return {"status": "error", "output": f"Hermes timeout after {timeout:.0f}s"}
    except Exception as exc:
        logger.error("Hermes error: %s", exc)
        return {"status": "error", "output": str(exc)}
    # asyncio.CancelledError is BaseException — not caught above, propagates naturally.
    # The `async with client.stream(...)` context manager closes the TCP connection,
    # which signals Hermes to stop generating (server-side kill).
