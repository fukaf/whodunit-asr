"""Versioned intermediate products of a transcription.

A finished job keeps the pieces that are expensive to recreate: the global
ASR segments, the reconciled speaker turns and one embedding vector per
speaker. Re-running fusion or identification then takes seconds instead of
repeating a 60-minute ASR/diarization pass.

The on-disk format is deliberately plain JSON. These files are an internal
cache, not a user-facing interchange format: `SCHEMA_VERSION` is bumped and the
reader refuses an unknown version rather than guessing at old data.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .types import Segment, TranscriptResult, Turn, Word

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"
SEGMENTS = "segments.json"
TURNS = "turns.json"
VECTORS = "speaker_vectors.json"


def _write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_artifacts(path: str | Path, *, segments: list[Segment], turns: list[Turn],
                   speaker_vectors: dict[str, Iterable[float]],
                   result: TranscriptResult, chunks: list[tuple[float, float]],
                   stride: float) -> Path:
    """Write one job's intermediate products; returns the artifacts directory."""
    from . import __version__  # local import: sdts/__init__ imports pipeline

    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": SCHEMA_VERSION,
        "sdts_version": __version__,
        "source": result.meta.get("source"),
        "duration": result.duration,
        "language": result.language,
        "speakers": result.speakers,
        "chunks": len(chunks),
        "stride": stride,
        "asr_engine": result.meta.get("asr_engine"),
        "asr_model": result.meta.get("asr_model"),
        "diarization": result.meta.get("diarization"),
    }
    _write(root / MANIFEST, manifest)
    _write(root / SEGMENTS, [asdict(s) for s in segments])
    _write(root / TURNS, [asdict(t) for t in turns])
    _write(root / VECTORS, {k: [round(float(v), 6) for v in vec]
                            for k, vec in speaker_vectors.items()})
    return root


def load_artifacts(path: str | Path) -> dict[str, Any]:
    """Load and validate artifacts written by `save_artifacts`."""
    root = Path(path).expanduser().resolve()
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported artifact schema {manifest.get('schema')!r}; "
            f"this build understands {SCHEMA_VERSION}")
    segments = json.loads((root / SEGMENTS).read_text(encoding="utf-8"))
    turns = json.loads((root / TURNS).read_text(encoding="utf-8"))
    vectors_raw = json.loads((root / VECTORS).read_text(encoding="utf-8")) \
        if (root / VECTORS).is_file() else {}
    vectors = {k: _normalise(v) for k, v in vectors_raw.items()}
    return {
        "root": root,
        "manifest": manifest,
        "segments": [segment_from_dict(d) for d in segments],
        "turns": [turn_from_dict(d) for d in turns],
        "vectors": vectors,
    }


def _normalise(values: Iterable[float]) -> list[float]:
    import numpy as np
    v = np.asarray(list(values), dtype=np.float32)
    norm = float(np.linalg.norm(v))
    if norm and norm != 1.0:
        v = v / norm
    return [float(x) for x in v]


def segment_from_dict(data: dict[str, Any]) -> Segment:
    words = [Word(start=w["start"], end=w["end"], text=w["text"],
                  score=w.get("score")) for w in data.get("words", [])]
    return Segment(start=float(data["start"]), end=float(data["end"]),
                   text=data.get("text", ""), words=words,
                   language=data.get("language"), avg_logprob=data.get("avg_logprob"))


def turn_from_dict(data: dict[str, Any]) -> Turn:
    return Turn(start=float(data["start"]), end=float(data["end"]),
                speaker=data["speaker"])


def normalise_result_json(path: str | Path, result: TranscriptResult) -> None:
    """Keep `speaker_display: null` for unnamed speakers on disk.

    `Utterance.to_dict()` substitutes the raw label when no display name is
    set. That is useful for txt/srt but wrong in the JSON, where the distinction
    between "unnamed" and "named SPEAKER_00" must survive a round trip.
    """
    p = Path(path)
    if not p.is_file():
        return
    dumped = json.loads(p.read_text(encoding="utf-8"))
    # Positional zip, not a dict keyed by speaker: one speaker owns many
    # utterances and each raw entry needs its own value.
    for raw, utt in zip(dumped.get("utterances", []), result.utterances):
        raw["speaker_display"] = utt.speaker_display
    p.write_text(json.dumps(dumped, ensure_ascii=False, indent=2), encoding="utf-8")
