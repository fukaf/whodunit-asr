"""Stage 3: speaker diarization.

Uses pyannote.audio 4.x with `speaker-diarization-community-1`.

Why this pipeline: on the pyannote published benchmark it beats the older
speaker-diarization-3.1 on every corpus they report (e.g. AMI-IHM DER 17.0% vs
18.8%, AliMeeting 20.3% vs 24.5%, CALLHOME 26.7% vs 28.5%), it is fully
self-contained so it can be vendored for offline use, and it exposes an
`exclusive_speaker_diarization` view that has already resolved overlap - which
is exactly the right input for a "one speaker per span" transcript.

Licence: weights are CC-BY-4.0 (attribution required) AND gated - you must
accept the user agreement on HF and pass a token. The *library* is MIT.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .config import DiarizationConfig
from .device import resolve_device
from .types import Turn

log = logging.getLogger(__name__)


class Diarizer:
    def __init__(self, cfg: DiarizationConfig, hf_token: str):
        from pyannote.audio import Pipeline
        import torch

        self.cfg = cfg
        self._torch = torch
        log.info("loading diarization pipeline %s", cfg.pipeline)
        try:
            self.pipeline = Pipeline.from_pretrained(cfg.pipeline, token=hf_token)
        except TypeError:
            # pyannote <4.0 used use_auth_token; keep the fallback so an
            # older pinned environment still runs.
            self.pipeline = Pipeline.from_pretrained(cfg.pipeline, use_auth_token=hf_token)

        self._apply_tuning()

        device = torch.device(resolve_device(cfg.device))
        log.info("diarization device: %s", device)
        self.pipeline.to(device)

    def _apply_tuning(self) -> None:
        """Push config overrides into pyannote's hyper-parameters.

        `instantiate` must be called before inference and only accepts keys the
        loaded pipeline exposes. A pipeline version that renames or drops
        `min_duration_off` would otherwise crash here, so a failed override is
        a warning rather than an exception - segmentation quality degrades but
        the run still produces output.
        """
        if self.cfg.min_duration_off is None:
            return
        try:
            self.pipeline.instantiate(
                {"segmentation": {"min_duration_off": float(self.cfg.min_duration_off)}})
            log.info("fill-gap threshold min_duration_off=%.2fs", self.cfg.min_duration_off)
        except (KeyError, ValueError, AttributeError) as exc:
            log.warning("could not set min_duration_off (pipeline may have changed): %s", exc)

    def diarize(self, wav_path: str | Path, num_speakers: int | None = None) -> list[Turn]:
        kw = {}
        n = num_speakers or self.cfg.num_speakers
        if n:
            kw["num_speakers"] = int(n)
        else:
            if self.cfg.min_speakers:
                kw["min_speakers"] = int(self.cfg.min_speakers)
            if self.cfg.max_speakers:
                kw["max_speakers"] = int(self.cfg.max_speakers)

        output = self.pipeline(str(wav_path), **kw)
        turns = self._to_turns(self._pick_view(output))
        turns.sort(key=lambda t: (t.start, t.end))
        return turns

    def _to_turns(self, annotation) -> list[Turn]:
        """Convert a pyannote Annotation into Turns.

        `itertracks(yield_label=True)` yields **(segment, track, label)** -
        three values, in every pyannote version. The track name is an internal
        id used to allow overlapping speech; for a single-speaker-per-span
        transcript we only need the label.

        The exclusive view guarantees non-overlapping spans, but the plain
        `speaker_diarization` view does not, so the same speaker can appear on
        several overlapping tracks. Those are merged afterwards so the fusion
        stage never sees duplicate competing spans for one speaker.
        """
        raw = [
            (float(seg.start), float(seg.end), str(label))
            for seg, _track, label in annotation.itertracks(yield_label=True)
            if seg.end > seg.start
        ]
        if not raw:
            return []

        # Merge same-speaker spans that touch or overlap. Sorting by start then
        # sweeping once keeps this O(n log n).
        raw.sort()
        merged: list[tuple[float, float, str]] = []
        for start, end, speaker in raw:
            if merged and merged[-1][2] == speaker and start <= merged[-1][1]:
                p_start, p_end, p_speaker = merged[-1]
                merged[-1] = (p_start, max(p_end, end), p_speaker)
            else:
                merged.append((start, end, speaker))

        # Different speakers can still overlap: the exclusive view resolves
        # overlap *between tracks*, but filling short gaps (min_duration_off)
        # extends a turn into the next one. For a "who said this" transcript
        # two people cannot hold the same instant, so the later turn wins and
        # the earlier one is trimmed. Without this a single sentence is split
        # down the middle of a name, producing two half-sentences for two
        # speakers at overlapping times.
        trimmed: list[tuple[float, float, str]] = []
        for i, (start, end, speaker) in enumerate(merged):
            if i + 1 < len(merged):
                next_start = merged[i + 1][0]
                if end > next_start:
                    end = next_start
            if end > start:
                trimmed.append((start, end, speaker))
            elif trimmed:
                # Fully swallowed: drop it rather than emit a zero-length turn.
                continue
        return [Turn(start=s, end=e, speaker=sp) for s, e, sp in trimmed]

    def _pick_view(self, output):
        """pyannote 4.x returns an object; 3.x returned an Annotation."""
        if hasattr(output, "exclusive_speaker_diarization"):
            if self.cfg.mode == "exclusive":
                return output.exclusive_speaker_diarization
            return output.speaker_diarization
        # 3.x style Annotation - no exclusive view available.
        return output

    def diarize_array(self, audio: np.ndarray, sample_rate: int = 16000) -> list[Turn]:
        """In-memory variant. Avoids a temp wav for chunked processing."""
        waveform = self._torch.from_numpy(audio).unsqueeze(0)
        kw = {}
        n = self.cfg.num_speakers
        if n:
            kw["num_speakers"] = int(n)
        output = self.pipeline({"waveform": waveform, "sample_rate": sample_rate}, **kw)
        turns = self._to_turns(self._pick_view(output))
        turns.sort(key=lambda t: (t.start, t.end))
        return turns
