"""Threshold calibration from labelled same/different-speaker pairs."""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from .config import PipelineConfig
from .embedding import SpeakerEmbedder


def calibrate(pairs_path: str | Path, out_path: str | Path,
               config: PipelineConfig | None = None) -> int:
    cfg = config or PipelineConfig()
    rows = list(csv.DictReader(Path(pairs_path).open(encoding="utf-8")))
    if not rows:
        print("no pairs found")
        return 1

    embedder = SpeakerEmbedder(cfg.diarization.device, cfg.resolve_hf_token())
    cache: dict[str, np.ndarray] = {}

    def vector(path: str) -> np.ndarray | None:
        key = str(Path(path).expanduser().resolve())
        if key not in cache:
            arr = audio_mod.decode(key, cfg.audio)
            cache[key] = embedder.embed_audio(arr, cfg.audio.sample_rate)
        return cache[key]

    scores: list[tuple[float, int]] = []
    for row in rows:
        a, b = vector(row["file_a"]), vector(row["file_b"])
        if a is None or b is None:
            continue
        scores.append((float(np.dot(a, b)), int(row["same_speaker"])))
    pos = [s for s, y in scores if y == 1]
    neg = [s for s, y in scores if y == 0]
    if not pos or not neg:
        print("need both positive and negative pairs")
        return 1

    candidates = np.unique(np.concatenate([
        np.linspace(-1, 1, 2001), np.asarray(pos), np.asarray(neg)]))
    eer, eer_gap = 0.0, 9.0
    best_far, best_frr, best_far_threshold = 1.0, 1.0, 0.0
    for thr in candidates:
        far = sum(s >= thr for s in neg) / len(neg)
        frr = sum(s < thr for s in pos) / len(pos)
        if abs(far - frr) < eer_gap:
            eer_gap, eer = abs(far - frr), (far + frr) / 2
        if far <= 0.01 and frr < best_frr:
            best_far, best_frr, best_far_threshold = far, frr, float(thr)

    reject = min(best_far_threshold, eer)
    text = (
        "identification:\n"
        f"  accept_threshold: {best_far_threshold:.4f}\n"
        f"  reject_threshold: {reject:.4f}\n"
        "  margin: 0.10\n"
    )
    Path(out_path).write_text(text, encoding="utf-8")
    print(f"pairs={len(scores)} positives={len(pos)} negatives={len(neg)} "
          f"EER={eer:.4f} FAR@accept={best_far:.4f} FRR@accept={best_frr:.4f}")
    print(f"wrote {out_path}")
    return 0
