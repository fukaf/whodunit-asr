"""Closed-set speaker identification against a local registry.

Runs after fuse(): it maps raw ``SPEAKER_0i`` labels to registered display
names by comparing cluster embeddings with stored centroids. Nothing here
touches the ASR text or the diarization labels themselves.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from .config import IdentificationConfig
from .embedding import chunk_of
from .registry import SpeakerRegistry
from .types import TranscriptResult, Turn

log = logging.getLogger(__name__)


def _turn_vector(turn: Turn, embedder, chunk_audio, chunks, stride) -> np.ndarray | None:
    if turn.duration < 0.3:
        return None
    idx = chunk_of(turn.start, chunks, stride)
    if idx is None or idx >= len(chunk_audio):
        return None
    start = chunks[idx][0]
    arr = chunk_audio[idx]
    a = max(0, int((turn.start - start) * 16000))
    b = min(len(arr), int((turn.end - start) * 16000))
    if b - a < 4800:
        return None
    return embedder.embed_audio(np.asarray(arr[a:b], dtype=np.float32))


def identify_speakers(result: TranscriptResult, *, turns: list[Turn], embedder,
                      cfg: IdentificationConfig, chunk_audio, chunks,
                      stride: float,
                      speaker_vectors: dict[str, Any] | None = None,
                      collector: dict[str, Any] | None = None,
                      readonly: bool = False) -> dict[str, Any]:
    """Apply registry matches to utterances and return a meta summary.

    `speaker_vectors` lets a replay reuse the vectors computed by the original
    run. `collector` records freshly computed vectors for the artifact writer.
    `readonly` suppresses registry writes, so threshold sweeps cannot pollute
    score history or trigger adaptation.
    """
    meta: dict[str, Any] = {
        "mode": cfg.mode,
        "registry": str(Path(cfg.registry_path).expanduser()),
        "thresholds": {
            "accept": cfg.accept_threshold,
            "reject": cfg.reject_threshold,
            "margin": cfg.margin,
        },
        "matches": [],
    }
    if not cfg.enabled or cfg.mode == "off":
        return meta
    try:
        registry = SpeakerRegistry(cfg.registry_path)
    except (OSError, sqlite3.DatabaseError) as exc:
        log.warning("speaker registry unavailable (%s); identification skipped", exc)
        meta["error"] = str(exc)
        return meta

    try:
        candidates = registry.active_candidates()
        if not candidates:
            log.info("speaker registry is empty; identification skipped")
            meta["note"] = "empty registry"
            return meta

        condition = {
            "sample_rate": result.meta.get("sample_rate"),
            "codec": result.meta.get("codec"),
            "channels": result.meta.get("channels"),
        }
        source_file = result.meta.get("source")
        mapping: dict[str, str | None] = {}
        drift_checked: set[str] = set()

        for speaker in result.speakers:
            speaker_turns = [t for t in turns if t.speaker == speaker]
            if speaker_vectors is not None and speaker in speaker_vectors:
                raw_vec = np.asarray(speaker_vectors[speaker], dtype=np.float32)
                norm = float(np.linalg.norm(raw_vec))
                vec = raw_vec / norm if norm else None
            else:
                vec = embedder.embed_turns(speaker_turns, chunk_audio, chunks, stride)
            if collector is not None:
                collector.setdefault("speaker_vectors", {})[speaker] = (
                    [float(x) for x in vec] if vec is not None else None)
            if vec is None:
                log.warning("no embedding for %s; leaving unlabelled", speaker)
                meta["matches"].append({"speaker": speaker, "decision": "no_embedding"})
                mapping[speaker] = None
                continue

            scores = []
            for cand in candidates:
                score = max(float(np.dot(vec, c)) for c in cand["vectors"])
                scores.append((score, cand))
            scores.sort(key=lambda item: -item[0])
            best_score, best = scores[0]
            second_score = scores[1][0] if len(scores) > 1 else None
            margin_ok = second_score is None or (best_score - second_score >= cfg.margin)

            if best_score >= cfg.accept_threshold and margin_ok:
                decision, display = "accept", best["display_name"]
            elif best_score >= cfg.reject_threshold:
                decision, display = "review", None
            else:
                decision, display = "unknown", None
                if cfg.unknown_label and cfg.mode == "auto":
                    if cfg.unknown_label not in {c["display_name"] for c in candidates}:
                        display = cfg.unknown_label

            record: dict[str, Any] = {
                "speaker": speaker,
                "decision": decision,
                "best": best["display_name"],
                "score": round(best_score, 4),
                "second": round(second_score, 4) if second_score is not None else None,
                "candidates": [{"name": c["display_name"], "score": round(s, 4)}
                               for s, c in scores[:3]],
            }

            # In review mode nothing is written automatically; a human applies
            # the labels through `labels apply` after inspecting candidates.
            mapping[speaker] = display if cfg.mode == "auto" else None

            adapt = cfg.adaptation
            if (not readonly and decision == "accept" and adapt.enabled
                    and best_score >= cfg.accept_threshold + adapt.update_margin
                    and margin_ok and cfg.mode == "auto"):
                pending = []
                for t in speaker_turns:
                    tv = _turn_vector(t, embedder, chunk_audio, chunks, stride)
                    if tv is None:
                        continue
                    cid = registry.update_buffer(
                        best["id"], tv, t.duration, adapt.min_turns, adapt.min_seconds,
                        source_file=source_file, condition=condition)
                    if cid:
                        pending.append(cid)
                if pending:
                    record["pending_adapt"] = pending
                    active = registry.list_centroids(best["id"], "active")
                    for old in active[:max(0, len(active) - cfg.max_centroids)]:
                        registry.delete_centroid(old["id"])

            if not readonly and decision == "accept":
                registry.add_score(best["id"], best_score)
                if (best["id"] not in drift_checked
                        and registry.drift_flag(best["id"], adapt.drift_window,
                                                adapt.drift_threshold)):
                    record["drift"] = "needs_re_enrollment"
                    log.warning("speaker %s scores are drifting; re-enrollment advised",
                                best["display_name"])
                # One vote per recording: several clusters of the same person
                # must not fill the window and fake a drift on their own.
                drift_checked.add(best["id"])

            meta["matches"].append(record)

        for utt in result.utterances:
            display = mapping.get(utt.speaker)
            if display:
                utt.speaker_display = display
    finally:
        registry.close()
    return meta
