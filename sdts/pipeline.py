"""Orchestration: the end-to-end pipeline and the chunk-reconciliation logic.

The interesting engineering is here. For files longer than `chunk_seconds` we
process windows with overlap, and then we have to answer: is the "SPEAKER_02"
in window 3 the same person as "SPEAKER_00" in window 4? pyannote numbers
speakers per call, so the labels are meaningless across chunks.

Solution: pyannote's own embedding model (`pyannote/wespeaker-voxceleb-resnet34-LM`,
CC-BY-4.0) embeds a sample of each local cluster; we then do constrained
hierarchical clustering across all local clusters with a distance threshold.
This is simple (~50 lines), deterministic, and avoids re-running diarization
on the full file, which is what makes the memory footprint flat.

Boundary policy: each chunk owns [start, start + stride) exclusively; the
overlap region is only used to give diarization and ASR enough right-context.
A word is kept by the chunk whose core range contains its midpoint.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import numpy as np

from . import audio as audio_mod
from .align import Aligner
from .asr import build_engine
from .config import PipelineConfig
from .diarize import Diarizer
from .embedding import SpeakerEmbedder
from .fuse import _join, fuse
from .identify import identify_speakers
from .types import Segment, TranscriptResult, Turn, Utterance, Word

log = logging.getLogger(__name__)


class Pipeline:
    """Lazy-loading pipeline. Models are built on first use, then reused."""

    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self._engine = None
        self._diarizer = None
        self._aligner = None
        self._embedder = None

    # -- model handles -----------------------------------------------------
    @property
    def engine(self):
        if self._engine is None:
            self._engine = build_engine(self.cfg.asr)
        return self._engine

    @property
    def diarizer(self):
        if self._diarizer is None:
            self._diarizer = Diarizer(self.cfg.diarization, self.cfg.resolve_hf_token())
        return self._diarizer

    @property
    def aligner(self):
        if self._aligner is None:
            self._aligner = Aligner(self.cfg.align, self.cfg.asr.device)
        return self._aligner

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = SpeakerEmbedder(
                self.cfg.diarization.device, self.cfg.resolve_hf_token())
        return self._embedder

    # -- main entry --------------------------------------------------------
    def run(self, path: str | Path, num_speakers: int | None = None) -> TranscriptResult:
        t0 = time.perf_counter()
        info = audio_mod.probe(path)
        log.info("input %s: %.1fs, %d Hz, %d ch, %s",
                 info.path.name, info.duration, info.sample_rate, info.channels, info.codec)

        chunks = audio_mod.plan_chunks(info.duration, self.cfg.audio)
        stride = (self.cfg.audio.chunk_seconds - self.cfg.audio.chunk_overlap_seconds)

        local_segments: list[Segment] = []
        local_turns: list[Turn] = []
        chunk_audio: list[np.ndarray] = []
        detected_lang: str | None = None

        for idx, (c_start, c_end) in enumerate(chunks):
            log.info("chunk %d/%d  [%.1f, %.1f]", idx + 1, len(chunks), c_start, c_end)
            arr = audio_mod.decode(path, self.cfg.audio, c_start, c_end)
            core_end = c_end if idx == len(chunks) - 1 else min(c_end, c_start + stride)

            turns = self.diarizer.diarize_array(arr, self.cfg.audio.sample_rate)
            turns = [Turn(t.start + c_start, t.end + c_start, f"L{idx}_{t.speaker}")
                     for t in turns]

            segments, lang = self.engine.transcribe(arr, self.cfg.asr.language)
            detected_lang = detected_lang or lang
            segments = [Segment(s.start + c_start, s.end + c_start, s.text, s.words,
                                s.language, s.avg_logprob) for s in segments]
            segments = [s for s in segments if c_start <= (s.start + s.end) / 2 < core_end]
            turns = [t for t in turns if c_start <= (t.start + t.end) / 2 < core_end]

            if self.cfg.align.enabled and segments:
                lang_for_align = (lang or self.cfg.asr.language or "en")
                # align() works in chunk-local time; shift to global afterwards.
                aligned = self.aligner.align(segments, arr, lang_for_align)
                for s in aligned:
                    for w in s.words:
                        w.start += c_start
                        w.end += c_start
                    s.start += c_start
                    s.end += c_start
                segments = aligned

            local_segments.extend(segments)
            local_turns.extend(turns)
            chunk_audio.append(arr)

        turns = self._reconcile_speakers(local_turns, chunk_audio, chunks, stride, num_speakers)
        utterances = fuse(local_segments, turns, self.cfg.merge)
        utterances = [u for u in utterances if u.text.strip()]

        speakers = sorted({u.speaker for u in utterances})
        result = TranscriptResult(
            utterances=utterances,
            language=detected_lang,
            speakers=speakers,
            duration=info.duration,
            meta={
                "source": str(info.path.expanduser().resolve()),
                "codec": info.codec,
                "sample_rate": info.sample_rate,
                "channels": info.channels,
                "chunks": len(chunks),
                "asr_engine": self.cfg.asr.engine,
                "asr_model": self.cfg.asr.model,
                "diarization": self.cfg.diarization.pipeline,
                "diarization_mode": self.cfg.diarization.mode,
                "elapsed_seconds": round(time.perf_counter() - t0, 2),
                "rtf": round((time.perf_counter() - t0) / max(info.duration, 1e-6), 4),
            },
        )
        if self.cfg.identification.enabled:
            result.meta["identification"] = identify_speakers(
                result, turns=turns, embedder=self.embedder,
                cfg=self.cfg.identification, chunk_audio=chunk_audio,
                chunks=chunks, stride=stride)
        return result

    # -- cross-chunk speaker reconciliation --------------------------------
    def _reconcile_speakers(self, turns: list[Turn], chunk_audio: list[np.ndarray],
                            chunks: list[tuple[float, float]], stride: float,
                            num_speakers: int | None) -> list[Turn]:
        local_labels = sorted({t.speaker for t in turns})
        if len(chunks) == 1 or len(local_labels) <= 1:
            return self._relabel(turns, {lab: f"SPEAKER_{i:02d}" for i, lab in enumerate(local_labels)})

        embeddings: dict[str, np.ndarray] = {}
        by_idx = {lab: [] for lab in local_labels}
        for t in turns:
            by_idx[t.speaker].append(t)

        for lab, lab_turns in by_idx.items():
            if not lab_turns:
                continue
            vec = self._cluster_embedding(lab_turns, chunk_audio, chunks, stride)
            if vec is not None:
                embeddings[lab] = vec

        present = [lab for lab in local_labels if lab in embeddings]
        if not present:
            # No speaker embeddings available (model download blocked, offline
            # install without the embedding weights, or all turns too short).
            # Deterministic fallback: within each chunk, order the local
            # clusters by their first appearance and map that position onto a
            # global SPEAKER_0i. Wrong when persons speak in a different order
            # in different chunks, so warn loudly instead of pretending.
            log.warning(
                "speaker embeddings unavailable: cross-chunk speaker identity "
                "cannot be verified. Labels are assigned by order of first "
                "appearance within each chunk and may not match across chunks. "
                "Set --num-speakers to improve stability.")
            first_seen: dict[str, float] = {}
            for t in sorted(turns, key=lambda x: x.start):
                first_seen.setdefault(t.speaker, t.start)
            per_chunk: dict[int, list[str]] = {}
            for lab, start_ in first_seen.items():
                chunk_idx = int(lab.split("_", 1)[0][1:])
                per_chunk.setdefault(chunk_idx, []).append(lab)
            mapping: dict[str, str] = {}
            global_order: dict[str, int] = {}
            for chunk_idx in sorted(per_chunk):
                for position, lab in enumerate(sorted(per_chunk[chunk_idx],
                                                     key=lambda x: first_seen[x])):
                    if position not in global_order:
                        global_order[position] = len(global_order)
                    mapping[lab] = f"SPEAKER_{global_order[position]:02d}"
            return self._relabel(turns, mapping)

        mapping = self._cluster_embeddings(embeddings, present, num_speakers)
        # Labels with no embedding keep their local identity.
        for lab in local_labels:
            mapping.setdefault(lab, lab)
        canon = {}
        for i, target in enumerate(sorted(set(mapping.values()))):
            canon[target] = f"SPEAKER_{i:02d}"
        return self._relabel(turns, {k: canon[v] for k, v in mapping.items()})

    def _cluster_embedding(self, lab_turns: list[Turn], chunk_audio: list[np.ndarray],
                           chunks: list[tuple[float, float]], stride: float) -> np.ndarray | None:
        """Mean embedding of up to N longest turns belonging to one local cluster."""
        return self.embedder.embed_turns(lab_turns, chunk_audio, chunks, stride)

    def _cluster_embeddings(self, embeddings: dict[str, np.ndarray],
                            present: list[str],
                            num_speakers: int | None) -> dict[str, str]:
        """Agglomerative clustering on cosine distance with a fixed threshold."""
        thr = 0.35                       # tuned for wespeaker on 16k speech
        clusters = {lab: [lab] for lab in present}
        centroids = {lab: embeddings[lab] for lab in present}

        def dist(a: str, b: str) -> float:
            return 1.0 - float(np.dot(centroids[a], centroids[b]))

        while len(clusters) > 1:
            keys = list(clusters)
            best, best_d = None, thr
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    d = dist(keys[i], keys[j])
                    if d < best_d:
                        best, best_d = (keys[i], keys[j]), d
            if best is None:
                break
            if num_speakers and len(clusters) <= num_speakers:
                break
            a, b = best
            clusters[a].extend(clusters.pop(b))
            centroids.pop(b)
            v = np.mean([embeddings[x] for x in clusters[a]], axis=0)
            n = np.linalg.norm(v)
            centroids[a] = v / n if n else v

        return {lab: root for root, members in clusters.items() for lab in members}

    @staticmethod
    def _relabel(turns: list[Turn], mapping: dict[str, str]) -> list[Turn]:
        return [Turn(t.start, t.end, mapping.get(t.speaker, t.speaker)) for t in turns]


def _chunk_of(t: float, chunks: list[tuple[float, float]], stride: float) -> int | None:
    for i, (s, e) in enumerate(chunks):
        core_end = e if i == len(chunks) - 1 else min(e, s + stride)
        if s <= t < core_end:
            return i
    return None


def file_stem(path: str | Path) -> str:
    p = Path(path)
    digest = hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:6]
    return f"{p.stem}_{digest}"
