#!/usr/bin/env python3
"""
Standalone voice-ID feasibility test using Resemblyzer.
Records two 5-second mic samples, computes cosine similarity between
their speaker embeddings, and reports score + RAM usage + latency.

Usage:
    pip install resemblyzer sounddevice numpy psutil
    python3 scripts/test_voice_id.py
"""

import time
import sys

import numpy as np
import psutil
import sounddevice as sd

SAMPLE_RATE = 16000
DURATION = 5  # seconds per recording


def record(label: str) -> np.ndarray:
    input(f"\n[{label}] Press ENTER then speak for {DURATION} seconds...")
    print("  Recording...", end="", flush=True)
    audio = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    print(" done.")
    return audio.flatten()


def main() -> None:
    print("=== Resemblyzer voice-ID feasibility test ===")

    # Lazy import so the install error is clear
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
    except ImportError:
        sys.exit("resemblyzer not installed — run: pip install resemblyzer")

    process = psutil.Process()

    # ── Load encoder ──────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    encoder = VoiceEncoder()
    load_ms = (time.perf_counter() - t0) * 1000
    ram_after_load = process.memory_info().rss / 1024 / 1024

    print(f"\n  Encoder loaded in {load_ms:.0f} ms | RAM: {ram_after_load:.1f} MB")

    # ── Record samples ────────────────────────────────────────────────────────
    raw1 = record("Sample 1 — speak a sentence")
    raw2 = record("Sample 2 — speak the same or a different sentence")

    # ── Preprocess ────────────────────────────────────────────────────────────
    wav1 = preprocess_wav(raw1, source_sr=SAMPLE_RATE)
    wav2 = preprocess_wav(raw2, source_sr=SAMPLE_RATE)

    # ── Embed ─────────────────────────────────────────────────────────────────
    t1 = time.perf_counter()
    emb1 = encoder.embed_utterance(wav1)
    emb2 = encoder.embed_utterance(wav2)
    embed_ms = (time.perf_counter() - t1) * 1000
    ram_after_embed = process.memory_info().rss / 1024 / 1024

    # ── Cosine similarity ─────────────────────────────────────────────────────
    score = float(np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2)))

    # ── Report ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 45)
    print(f"  Cosine similarity : {score:.4f}")
    print(f"  Embedding latency : {embed_ms:.0f} ms  (both samples)")
    print(f"  RAM usage         : {ram_after_embed:.1f} MB")
    print("=" * 45)

    if score >= 0.85:
        verdict = "SAME speaker (high confidence)"
    elif score >= 0.75:
        verdict = "Likely same speaker"
    elif score >= 0.60:
        verdict = "Uncertain"
    else:
        verdict = "DIFFERENT speakers"

    print(f"  Verdict           : {verdict}")
    print()


if __name__ == "__main__":
    main()
