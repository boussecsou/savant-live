"""
SAVANT — Console Live (isolated, removable, hands-on).

Separate Gemini Live session with its own LEAN prompt. Typed commands execute
DIRECTLY on the VPS via the hermes-v2 exec helper (no Hermes LLM → fast).
Gemini Live narrates output in voice + suggests the next command.

Features:
- Persistent CWD and user context across commands (server-side state in exec helper)
- User switching via `su <user>` or `sudo su <user>` — user/cwd preserved
- `/savant <text>` command: routes a text message directly to the voice SAVANT session
  without executing a shell command (the main WS must be provided by the caller)

Removal: delete this file + 2-line guard in main.py /ws + PWA console block +
entrypoint line. The voice SAVANT is completely untouched.
"""
import asyncio
import json
import logging
import os
import time
import uuid

import httpx
from google.genai import types

from backend.memory.database import get_memory_value, save_action_log

logger = logging.getLogger("savant")

_CONSOLE_EXEC_BASE = os.getenv("CONSOLE_EXEC_BASE", "http://hermes-v2:8644")
_CONSOLE_EXEC_URL = f"{_CONSOLE_EXEC_BASE}/exec"
_CONSOLE_RESET_URL = f"{_CONSOLE_EXEC_BASE}/reset"
_CONSOLE_EXEC_SECRET = os.getenv("HERMES_WEBHOOK_SECRET", "")
_CONSOLE_MODEL = "models/gemini-3.1-flash-live-preview"
_METRICS_FILE = "/app/logs/vps_map/METRICS.txt"
_METRICS_POLL_S = 20.0
_EXEC_HTTP_TIMEOUT_S = 35.0
_IDLE_WAIT_S = 25.0   # max wait for Gemini to finish speaking before injecting output

_DESTRUCTIVE = (
    "rm ", "rmdir", "unlink", "shred", "mkfs", "dd if=", "fdisk", "parted", "wipefs",
    "truncate", "> /etc", "> /boot", "tee /etc", "kill ", "killall", "pkill",
    "shutdown", "reboot", "halt", "poweroff", "userdel", "deluser",
    "chown -r /", "chmod -r", "chmod 000",
    "systemctl stop", "systemctl disable", "systemctl mask", "systemctl restart",
    "iptables", "ufw disable", "ufw reset",
    "docker rm", "docker stop", "docker kill", "docker rmi",
    "docker compose down", "docker-compose down", "docker system prune",
    "drop table", "drop database", "delete from", "git reset --hard", "git clean",
)

_CONSOLE_PROMPT = (
    "You are SAVANT in VPS CONSOLE mode. The owner types raw shell commands executed on "
    "the VPS. You receive the real output inside [OUTPUT ...] messages.\n"
    "When you receive [OUTPUT]: describe what it shows in ONE concise sentence (owner's "
    "language), then suggest ONE useful follow-up command. Be fast, concrete, terse.\n"
    "The owner can also speak directly to you. Respond to voice questions with ONE natural "
    "sentence in the owner's language. Never narrate commands the owner is about to type — "
    "wait for [OUTPUT].\n"
    "NEVER invent or guess output. Never read secrets aloud."
)


def _is_destructive(cmd: str) -> bool:
    low = " " + cmd.strip().lower() + " "
    return any(m in low for m in _DESTRUCTIVE)


def _read_metrics() -> str | None:
    try:
        if time.time() - os.path.getmtime(_METRICS_FILE) > 60:
            return None
        with open(_METRICS_FILE) as f:
            return f.read().strip() or None
    except Exception:
        return None


async def _exec_vps(cmd: str, console_session_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=_EXEC_HTTP_TIMEOUT_S) as client:
            r = await client.post(
                _CONSOLE_EXEC_URL,
                headers={
                    "Authorization": f"Bearer {_CONSOLE_EXEC_SECRET}",
                    "X-Console-Session-Id": console_session_id,
                },
                json={"cmd": cmd},
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning("Console exec failed: %s", e)
        return {"stdout": "", "stderr": f"exec error: {e}", "code": 1,
                "cwd": "/", "user": "root"}


async def _reset_console_session(console_session_id: str) -> None:
    """Ask the exec server to drop server-side state for this session."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                _CONSOLE_RESET_URL,
                headers={
                    "Authorization": f"Bearer {_CONSOLE_EXEC_SECRET}",
                    "X-Console-Session-Id": console_session_id,
                },
                json={},
            )
    except Exception:
        pass  # best-effort


def _console_config(handle: str | None) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
            )
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(target_tokens=8000),
        ),
        thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
        generation_config=types.GenerationConfig(temperature=0.2),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        system_instruction=types.Content(parts=[types.Part(text=_CONSOLE_PROMPT)]),
    )


async def handle_console_session(websocket, main_ws_send=None) -> None:
    """Self-contained Console Live WS handler. Reached via /ws?mode=console.

    main_ws_send: optional async callable(text: str) that injects a text_command
    into the live voice SAVANT session so /savant <msg> works cross-mode.
    Not used currently (voice WS is closed when console opens); kept as extension
    point for future side-channel injection.
    """
    await websocket.accept()
    client_id = uuid.uuid4().hex[:8]
    # Stable ID for exec-server session state — survives Gemini reconnects
    console_session_id = f"console-{client_id}"

    gemini_client = websocket.app.state.gemini_client
    if gemini_client is None:
        await websocket.send_text(json.dumps({"type": "error", "message": "no gemini client"}))
        await websocket.close()
        return

    logger.info("Console session opened — client=%s", client_id)
    verified = asyncio.Event()
    pwa_done = asyncio.Event()
    gemini_idle = asyncio.Event()
    gemini_idle.set()   # starts idle; cleared on audio, set on turn_complete
    session_ref: list = [None]
    pending_cmd: list = [None]
    session_handle: list = [None]

    async def _inject(text: str) -> None:
        """Inject a user turn, waiting for Gemini to finish speaking first."""
        try:
            await asyncio.wait_for(gemini_idle.wait(), timeout=_IDLE_WAIT_S)
        except asyncio.TimeoutError:
            pass   # inject anyway
        sess = session_ref[0]
        if sess is None or pwa_done.is_set():
            return
        try:
            await sess.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=text)])],
                turn_complete=True,
            )
        except Exception:
            logger.warning("Console inject failed — client=%s", client_id)

    async def _do_exec(cmd: str) -> None:
        t0 = time.monotonic()
        res = await _exec_vps(cmd, console_session_id)
        dur = time.monotonic() - t0
        out = res.get("stdout", "") or ""
        err = res.get("stderr", "") or ""
        code = res.get("code", 0)
        new_cwd = res.get("cwd", "/")
        new_user = res.get("user", "root")
        try:
            await websocket.send_text(json.dumps({
                "type": "console_output", "cmd": cmd,
                "stdout": out, "stderr": err, "code": code,
                "cwd": new_cwd, "user": new_user,
            }))
        except Exception:
            pass
        try:
            save_action_log(uuid.uuid4().hex[:12], "console", cmd,
                            "ok" if code == 0 else "error", round(dur, 1), 0)
        except Exception:
            pass
        body = (out or err or "(no output)")[:1500]
        await _inject(
            f"[OUTPUT of `{cmd}`] exit={code}\n{body}"
        )

    async def _reader() -> None:
        try:
            while True:
                frame = await websocket.receive()
                # Binary frame = PCM audio from mic — forward to Gemini console session
                if frame.get("bytes"):
                    sess = session_ref[0]
                    if sess:
                        try:
                            await sess.send(input=types.LiveClientRealtimeInput(
                                media_chunks=[types.Blob(
                                    data=frame["bytes"], mime_type="audio/pcm;rate=16000"
                                )]
                            ))
                        except Exception:
                            pass
                    continue
                raw = frame.get("text") or ""
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "action_code":
                    stored = None
                    try:
                        stored = get_memory_value("action_code")
                    except Exception:
                        pass
                    if stored and msg.get("value") == stored:
                        verified.set()
                        await websocket.send_text(json.dumps({"type": "session_verified"}))
                        logger.info("Console verified — client=%s", client_id)
                    else:
                        await websocket.send_text(json.dumps({"type": "access_denied"}))
                elif not verified.is_set():
                    continue
                elif mtype == "command":
                    cmd = (msg.get("text") or "").strip()
                    if not cmd:
                        continue

                    # /savant <message>: route text to SAVANT voice AI (via Gemini inject)
                    if cmd.lower().startswith("/savant "):
                        text_msg = cmd[8:].strip()
                        if text_msg:
                            await _inject(f"[SAVANT MESSAGE from console owner] {text_msg}")
                            try:
                                await websocket.send_text(json.dumps({
                                    "type": "console_savant_echo", "text": text_msg,
                                }))
                            except Exception:
                                pass
                        continue

                    if pending_cmd[0] and cmd.lower() in ("non", "no", "annule", "cancel"):
                        pending_cmd[0] = None
                        continue
                    if _is_destructive(cmd):
                        pending_cmd[0] = cmd
                        await websocket.send_text(json.dumps({"type": "console_confirm", "cmd": cmd}))
                        await _inject(
                            f"[INTERNAL] Dangerous command: `{cmd}`. Ask 'tu es sûr ?' in ONE line."
                        )
                    else:
                        asyncio.create_task(_do_exec(cmd))
                elif mtype == "confirm":
                    if pending_cmd[0]:
                        cmd = pending_cmd[0]
                        pending_cmd[0] = None
                        asyncio.create_task(_do_exec(cmd))
                elif mtype == "ping":
                    pass
        except Exception:
            logger.info("Console reader ended — client=%s", client_id)
        finally:
            pwa_done.set()

    async def _metrics() -> None:
        await verified.wait()
        while not pwa_done.is_set():
            line = _read_metrics()
            if line:
                try:
                    await websocket.send_text(json.dumps({"type": "metrics_update", "line": line}))
                except Exception:
                    break
            await asyncio.sleep(_METRICS_POLL_S)

    reader_task = asyncio.create_task(_reader())
    metrics_task = asyncio.create_task(_metrics())

    try:
        await asyncio.wait_for(verified.wait(), timeout=120.0)
    except asyncio.TimeoutError:
        logger.info("Console verify timeout — client=%s", client_id)
        for t in (reader_task, metrics_task):
            t.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
        return

    try:
        while not pwa_done.is_set():
            try:
                async with gemini_client.aio.live.connect(
                    model=_CONSOLE_MODEL, config=_console_config(session_handle[0]),
                ) as session:
                    session_ref[0] = session
                    gemini_idle.set()

                    async def _receiver() -> None:
                        async for response in session.receive():
                            if pwa_done.is_set():
                                return
                            if response.data:
                                gemini_idle.clear()   # Gemini is speaking
                                try:
                                    await websocket.send_bytes(response.data)
                                except Exception:
                                    return
                            upd = getattr(response, "session_resumption_update", None)
                            if upd and upd.resumable and upd.new_handle:
                                session_handle[0] = upd.new_handle
                            if getattr(response, "go_away", None):
                                return
                            sc = response.server_content
                            if sc:
                                if sc.output_transcription and sc.output_transcription.text:
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript", "role": "assistant",
                                            "text": sc.output_transcription.text,
                                        }))
                                    except Exception:
                                        return
                                if sc.input_transcription and sc.input_transcription.text:
                                    try:
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript", "role": "user",
                                            "text": sc.input_transcription.text,
                                        }))
                                    except Exception:
                                        return
                                if sc.turn_complete:
                                    gemini_idle.set()   # Gemini finished speaking
                                    try:
                                        await websocket.send_text(json.dumps({"type": "turn_complete"}))
                                    except Exception:
                                        return

                    recv_task = asyncio.create_task(_receiver())
                    done, _ = await asyncio.wait(
                        {recv_task, reader_task}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not recv_task.done():
                        recv_task.cancel()
                    if reader_task in done:
                        break
            except Exception as e:
                if pwa_done.is_set():
                    break
                logger.warning("Console session error, reconnecting — client=%s: %s", client_id, e)
                await asyncio.sleep(0.5)
    finally:
        session_ref[0] = None
        for t in (reader_task, metrics_task):
            t.cancel()
        await asyncio.gather(reader_task, metrics_task, return_exceptions=True)
        await _reset_console_session(console_session_id)
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Console session closed — client=%s", client_id)
