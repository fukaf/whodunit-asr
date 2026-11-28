"""Reusable speaker embedding extraction.

The same wespeaker model is used for two things:
  * cross-chunk label reconciliation (already in pipeline.py);
  * speaker identity matching / enrollment (this feature).

Keeping it in one place means the model is loaded once and the crop logic is
shared. Tests inject a deterministic MockEmbedder instead of downloading the
real model.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np

from .types import Turn

log = logging.getLogger(__name__)

EMBEDDING_MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"


def chunk_of(t: float, chunks: Sequence[tuple[float, float]], stride: float) -> int | None:
    """Return the chunk index whose core range owns timestamp t."""
    for i, (start, end) in enumerate(chunks):
        core_end = end if i == len(chunks) - 1 else min(end, start + stride)
        if start <= t < core_end:
            return i
    return None


class SpeakerEmbedder:
    """Lazy wrapper around pyannote Inference for wespeaker embeddings."""

    def __init__(self, device: str = "cuda", token: str | None = None, model=None):
        self.device = device
        self.token = token
        self._model = model
        self._resolved = model is not None

    def _load(self):
        if self._resolved:
            return self._model
        from pyannote.audio import Inference, Model
        import torch

        device = torch.device(self.device)
        if device.type == "cuda" and not torch.cuda.is_available():
            device = torch.device("cpu")
        try:
            model = Model.from_pretrained(EMBEDDING_MODEL, token=self.token)
            self._model = Inference(model, window="whole", device=device)
        except Exception as exc:  # noqa: BLE001 - offline install, gated model, etc.
            log.warning("speaker embedding model unavailable (%s); identity features disabled", exc)
            self._model = None
        self._resolved = True
        return self._model

    def embed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray | None:
        """Embed one 16 kHz mono float32 waveform. Returns L2-normalised vector."""
        if audio is None or audio.size == 0:
            return None
        model = self._load()
        if model is None:
            return None
        import torch

        try:
            out = model({"waveform": torch.from_numpy(audio).unsqueeze(0),
                         "sample_rate": sample_rate})
            vec = np.asarray(out, dtype=np.float32).reshape(-1)
        except Exception as exc:  # noqa: BLE001
            log.debug("embedding failed: %s", exc)
            return None
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else None

    def embed_turns(self, turns: Sequence[Turn],
                    chunk_audio: Sequence[np.ndarray],
                    chunks: Sequence[tuple[float, float]],
                    stride: float,
                    max_turns: int = 5,
                    min_duration: float = 0.3) -> np.ndarray | None:
        """Mean embedding of the longest few turns belonging to one speaker."""
        vecs: list[np.ndarray] = []
        for turn in sorted(turns, key=lambda t: -t.duration)[:max_turns]:
            if turn.duration < min_duration:
                continue
            idx = chunk_of(turn.start, chunks, stride)
            if idx is None or idx >= len(chunk_audio):
                continue
            c_start = chunks[idx][0]
            arr = chunk_audio[idx]
            a = max(0, int((turn.start - c_start) * 16000))
            b = min(len(arr), int((turn.end - c_start) * 16000))
            if b - a < int(min_duration * 16000):
                continue
            vec = self.embed_audio(np.asarray(arr[a:b], dtype=np.float32))
            if vec is not None:
                vecs.append(vec)
        if not vecs:
            return None
        mean = np.mean(vecs, axis=0)
        norm = float(np.linalg.norm(mean))
        return mean / norm if norm else None
