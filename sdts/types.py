"""Normalised data model shared by every stage.

Stage boundaries exchange these objects, never engine-specific dicts. That is
what makes it possible to swap faster-whisper for WhisperX or SenseVoice, or
pyannote for NeMo, without touching the merge or render layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable


@dataclass
class Word:
    start: float
    end: float
    text: str
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}
        if self.score is not None:
            d["score"] = round(self.score, 4)
        return d


@dataclass
class Segment:
    """One ASR segment. `words` may be empty when alignment is disabled."""

    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    language: str | None = None
    avg_logprob: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Turn:
    """A speaker turn from diarization: who owned the floor over [start, end]."""

    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Utterance:
    """Final deliverable unit: one speaker, one stretch of time, one string."""

    speaker: str
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    speaker_display: str | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def label(self) -> str:
        return self.speaker_display or self.speaker

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "speaker_display": self.label,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
            "words": [w.to_dict() for w in self.words],
        }


@dataclass
class TranscriptResult:
    utterances: list[Utterance] = field(default_factory=list)
    language: str | None = None
    speakers: list[str] = field(default_factory=list)
    duration: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    # -- views -------------------------------------------------------------
    def speaker_stats(self) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for utt in self.utterances:
            s = stats.setdefault(utt.label, {"turns": 0, "speech_seconds": 0.0,
                                             "words": 0, "chars": 0})
            s["turns"] += 1
            s["speech_seconds"] += utt.duration
            s["words"] += len(utt.words) or len(utt.text.split())
            s["chars"] += len(utt.text)
        for s in stats.values():
            s["speech_seconds"] = round(s["speech_seconds"], 2)
            s["share"] = round(s["speech_seconds"] / self.duration, 4) if self.duration else 0.0
        return stats

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": round(self.duration, 3),
            "speakers": self.speakers,
            "speaker_stats": self.speaker_stats(),
            "meta": self.meta,
            "utterances": [u.to_dict() for u in self.utterances],
        }


def total_word_count(segments: Iterable[Segment]) -> int:
    return sum(len(s.words) for s in segments)
