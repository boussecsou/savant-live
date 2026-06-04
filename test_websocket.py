"""
WebSocket integration test for the SAVANT /ws endpoint.

Usage:
    python3 test_websocket.py [ws://host:port/ws]

Generates test_audio.wav if absent, sends it to the server in real-time
PCM chunks, prints every server message, and exits on turn_complete or
after TIMEOUT_SEC seconds.
"""

import asyncio
import json
import math
import struct
import sys
import wave
from pathlib import Path

import websockets

WAV_PATH = Path(__file__).parent / "test_audio.wav"
DEFAULT_URL = "ws://localhost:8000/ws"
CHUNK_BYTES = 3200      # 100 ms of 16-bit mono 16 kHz  (16000 × 2 × 0.1)
TIMEOUT_SEC = 15        # hard upper bound


# ---------------------------------------------------------------------------
# WAV generation
# ---------------------------------------------------------------------------

def generate_test_wav(
    path: Path,
    duration_s: float = 3.0,
    freq_hz: float = 440.0,
    sample_rate: int = 16_000,
) -> None:
    n_samples = int(sample_rate * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)       # 16-bit
        wf.setframerate(sample_rate)
        for i in range(n_samples):
            v = int(32_767 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            wf.writeframes(struct.pack("<h", v))
    print(f"[wav] generated {path}  ({duration_s}s @ {freq_hz} Hz, {sample_rate} Hz mono)")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

async def run(url: str) -> None:
    if not WAV_PATH.exists():
        generate_test_wav(WAV_PATH)

    print(f"[ws]  connecting to {url} …")
    async with websockets.connect(url) as ws:
        print("[ws]  connected\n")

        stop_event = asyncio.Event()

        # --- sender: stream PCM audio then signal end-of-turn ---------------
        async def send_audio() -> None:
            with wave.open(str(WAV_PATH), "rb") as wf:
                assert wf.getnchannels() == 1,      "WAV must be mono"
                assert wf.getsampwidth() == 2,      "WAV must be 16-bit"
                assert wf.getframerate() == 16_000, "WAV must be 16 kHz"

                n_samples_per_chunk = CHUNK_BYTES // wf.getsampwidth()
                chunk_duration = n_samples_per_chunk / wf.getframerate()

                sent_bytes = 0
                while True:
                    raw = wf.readframes(n_samples_per_chunk)
                    if not raw:
                        break
                    await ws.send(raw)
                    sent_bytes += len(raw)
                    # pace at real-time so Gemini VAD sees natural timing
                    await asyncio.sleep(chunk_duration)

            print(f"[send] audio sent ({sent_bytes} bytes) — signalling end_of_turn")
            await ws.send(json.dumps({"type": "end_of_turn"}))

        # --- receiver: print every frame, exit on turn_complete -------------
        async def receive_all() -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    print(f"[recv] audio chunk: {len(message):>6} bytes")
                else:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        print(f"[recv] text  → {message}")
                        continue

                    kind = data.get("type", "?")
                    if kind == "transcript":
                        role = data.get("role", "?")
                        text = data.get("text", "")
                        print(f"[recv] transcript [{role}] → {text}")
                    elif kind == "turn_complete":
                        print("[recv] turn_complete — closing")
                        stop_event.set()
                        return
                    else:
                        print(f"[recv] JSON  → {json.dumps(data, ensure_ascii=False)}")

        send_task = asyncio.create_task(send_audio())
        recv_task = asyncio.create_task(receive_all())

        try:
            await asyncio.wait_for(
                asyncio.gather(send_task, recv_task, return_exceptions=True),
                timeout=TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            print(f"\n[ws]  timeout after {TIMEOUT_SEC}s — closing")
        finally:
            send_task.cancel()
            recv_task.cancel()
            for t in (send_task, recv_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    print("[ws]  connection closed cleanly")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asyncio.run(run(target))
