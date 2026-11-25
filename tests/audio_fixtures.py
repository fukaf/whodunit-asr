"""Synthetic audio fixtures for tests. Requires ffmpeg on PATH."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from sdts import audio as audio_mod


def tone(path: Path, seconds: float = 5.0, sample_rate: int = 16000,
         freq: float = 440.0, amplitude: float = 0.2) -> Path:
    """Alternating tone/silence, 1.5 s on / 0.5 s off, mono float32."""
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    gate = ((t % 2.0) < 1.5).astype(np.float32)
    sig = (amplitude * np.sin(2 * np.pi * freq * t) * gate).astype(np.float32)
    return audio_mod.write_wav(sig, path, sample_rate)


def silence(path: Path, seconds: float = 3.0, sample_rate: int = 16000) -> Path:
    sig = np.zeros(int(sample_rate * seconds), dtype=np.float32)
    return audio_mod.write_wav(sig, path, sample_rate)
