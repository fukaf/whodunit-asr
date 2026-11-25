"""Stage 1: audio ingest and normalisation.

Whisper wants 16 kHz mono float32. pyannote wants 16 kHz mono. Users hand you
48 kHz stereo MP4 with a video stream, or a 44.1 kHz MP3, or a 8 kHz phone
recording. Normalise once, in one place, and every downstream stage can assume
the contract.

Design note: decoding and resampling is done by ffmpeg, not torchaudio or
torchcodec. ffmpeg is the only component that reliably handles every container
and codec people actually send, and it keeps this stage free of torch -
which matters because torch version pinning is the single biggest source of
install pain in this stack.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import AudioConfig


class AudioError(RuntimeError):
    pass


@dataclass
class AudioInfo:
    path: Path
    duration: float
    sample_rate: int
    channels: int
    codec: str
    bit_rate: int | None = None


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise AudioError(
            "ffmpeg/ffprobe not found on PATH. Install ffmpeg (>=5) - pyannote's "
            "torchcodec and this ingestion stage both depend on it."
        )


def probe(path: str | Path) -> AudioInfo:
    require_ffmpeg()
    path = Path(path)
    if not path.exists():
        raise AudioError(f"input not found: {path}")
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise AudioError(f"no audio stream in {path}")
    st = streams[0]
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    if duration <= 0:
        raise AudioError(f"could not determine duration of {path}")
    return AudioInfo(
        path=path,
        duration=duration,
        sample_rate=int(st.get("sample_rate", 0)),
        channels=int(st.get("channels", 0)),
        codec=st.get("codec_name", "unknown"),
        bit_rate=int(st["bit_rate"]) if st.get("bit_rate") else None,
    )


def decode(path: str | Path, cfg: AudioConfig,
           start: float | None = None, end: float | None = None) -> np.ndarray:
    """Decode (optionally a slice) to mono float32 @ cfg.sample_rate."""
    require_ffmpeg()
    src = Path(path).expanduser()
    if not src.is_file():
        # ffmpeg's own "No such file" message points at a mangled path and is
        # easy to misread (Windows backslashes), so fail with the real reason.
        raise AudioError(f"audio file not found: {src.resolve() if src.parent.exists() else src}")
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    # Seek before -i for fast, frame-accurate-enough seeking on long files.
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if end is not None:
        if start is not None:
            cmd += ["-t", f"{max(0.0, end - start):.3f}"]
        else:
            cmd += ["-to", f"{end:.3f}"]
    cmd += [
        "-vn", "-sn", "-dn",                 # drop video/subtitle/data streams
        "-ac", "1",                          # downmix: pyannote averages channels
        "-ar", str(cfg.sample_rate),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:500]}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise AudioError(f"decoded zero samples from {path}")
    return audio


def write_wav(audio: np.ndarray, path: str | Path, sample_rate: int = 16000) -> Path:
    """Write float32 mono to a 16-bit PCM wav via ffmpeg (no soundfile needed)."""
    require_ffmpeg()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y",
           "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
           "-acodec", "pcm_s16le", str(path)]
    proc = subprocess.run(cmd, input=audio.astype(np.float32).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg encode failed: {proc.stderr.decode('utf-8', 'ignore')[:500]}")
    return path


def plan_chunks(duration: float, cfg: AudioConfig) -> list[tuple[float, float]]:
    """Return [(start, end)] covering the file with configurable overlap."""
    if duration <= cfg.chunk_seconds:
        return [(0.0, duration)]
    stride = cfg.chunk_seconds - cfg.chunk_overlap_seconds
    if stride <= 0:
        raise AudioError("chunk_overlap_seconds must be smaller than chunk_seconds")
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(start + cfg.chunk_seconds, duration)
        chunks.append((start, end))
        if end >= duration:
            break
        start += stride
    return chunks
