"""Integration test for the orchestration path with models stubbed out.

Verifies the plumbing that unit tests cannot reach: chunk iteration, global
time offsetting, core-vs-overlap ownership, cross-chunk speaker reconciliation,
and the final transcript shape. No GPU, no downloads.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from sdts.config import PipelineConfig
from sdts.pipeline import Pipeline
from sdts.types import Segment, Turn, Word


class FakeDiarizer:
    """Two speakers per chunk, named per chunk like pyannote would."""

    def diarize_array(self, arr, sample_rate=16000):
        dur = len(arr) / sample_rate
        return [
            Turn(0.0, dur / 2, "SPEAKER_00"),
            Turn(dur / 2, dur, "SPEAKER_01"),
        ]


class FakeEngine:
    name = "fake"

    def transcribe(self, arr, language=None):
        dur = len(arr) / 16000
        half = dur / 2
        # Two sentences, one per speaker half - what whisper actually emits on
        # a turn-taking conversation. Sentence-level fusion then yields one
        # utterance each instead of splitting a single sentence down the middle.
        first = Segment(0.5, half - 0.2, "hello there",
                        [Word(0.5, 1.0, "hello"), Word(1.0, 1.6, "there")])
        second = Segment(half + 0.2, min(dur, half + 2.0), "world again",
                         [Word(half + 0.2, half + 0.8, "world"),
                          Word(half + 0.8, half + 1.4, "again")])
        return [first, second], "en"


class FakeAligner:
    def align(self, segments, arr, language):
        return segments


def make_pipeline(tmp_wav: Path, chunk_seconds=10.0, overlap=2.0) -> Pipeline:
    cfg = PipelineConfig()
    cfg.audio.chunk_seconds = chunk_seconds
    cfg.audio.chunk_overlap_seconds = overlap
    p = Pipeline(cfg)
    p._engine = FakeEngine()
    p._diarizer = FakeDiarizer()
    p._aligner = FakeAligner()
    # Force the embedding-based reconciliation path to be a no-op so we can
    # assert on the relabelling behaviour deterministically.
    p._cluster_embedding = lambda *a, **k: None
    return p


def test_short_file_single_chunk():
    import tempfile
    import audio_fixtures
    with tempfile.TemporaryDirectory() as td:
        wav = audio_fixtures.tone(Path(td) / "a.wav", seconds=8.0)
        result = make_pipeline(wav).run(wav)
        assert len(result.utterances) >= 2, result.to_dict()
        assert result.language == "en"
        assert result.speakers == ["SPEAKER_00", "SPEAKER_01"]
        # Times must be global and monotonic, not chunk-relative.
        starts = [u.start for u in result.utterances]
        assert starts == sorted(starts)
        assert all(u.end > u.start for u in result.utterances)


def test_long_file_chunk_offsets():
    import tempfile
    import audio_fixtures
    with tempfile.TemporaryDirectory() as td:
        # 25 s file / 10 s chunks with 2 s overlap -> 3 chunks
        wav = audio_fixtures.tone(Path(td) / "b.wav", seconds=25.0)
        result = make_pipeline(wav, chunk_seconds=10.0, overlap=2.0).run(wav)
        assert result.meta["chunks"] == 3
        # Second chunk starts at 8 s, so its words must be beyond 8 s.
        assert max(u.start for u in result.utterances) > 16.0
        # No utterance may exceed the source duration.
        assert all(u.end <= result.duration + 0.01 for u in result.utterances)


def test_duration_and_meta():
    import tempfile
    import audio_fixtures
    with tempfile.TemporaryDirectory() as td:
        wav = audio_fixtures.tone(Path(td) / "c.wav", seconds=5.0)
        result = make_pipeline(wav).run(wav)
        assert abs(result.duration - 5.0) < 0.05
        assert result.meta["asr_engine"] == "faster-whisper"   # from config, not the stub
        assert "rtf" in result.meta
        stats = result.speaker_stats()
        assert set(stats) == set(result.speakers)


# -- progress hook ---------------------------------------------------------

def test_on_stage_reports_every_phase_in_order():
    """The hook is what the web UI's progress bar is built on, so its contract
    (which stages, in which order, monotonic chunk counter) is worth pinning."""
    import tempfile
    import audio_fixtures
    with tempfile.TemporaryDirectory() as td:
        wav = audio_fixtures.tone(Path(td) / "p.wav", seconds=25.0)
        seen: list[tuple[str, int, int]] = []
        result = make_pipeline(wav, chunk_seconds=10.0, overlap=2.0).run(
            wav, on_stage=lambda stage, done, total: seen.append((stage, done, total)))

        stages = [s for s, _, _ in seen]
        assert stages[0] == "probe" and stages[-1] == "done"
        # Chunks are reported before and after each one, so 3 chunks -> 6 events.
        assert stages.count("chunk") == 2 * result.meta["chunks"]
        assert "reconcile" in stages and "fuse" in stages
        # The chunk counter must never go backwards: the UI draws it directly.
        chunk_counts = [d for s, d, _ in seen if s == "chunk"]
        assert chunk_counts == sorted(chunk_counts)
        assert chunk_counts[-1] == result.meta["chunks"]


def test_on_stage_failure_does_not_kill_the_run():
    """A broken listener must not cost the caller a completed transcription."""
    import tempfile
    import audio_fixtures
    with tempfile.TemporaryDirectory() as td:
        wav = audio_fixtures.tone(Path(td) / "q.wav", seconds=5.0)

        def boom(stage, done, total):
            raise RuntimeError("listener exploded")

        result = make_pipeline(wav).run(wav, on_stage=boom)
        assert result.utterances, result.to_dict()


def test_cancel_is_raised_out_of_the_pipeline():
    """`pipeline._emit` swallows listener errors but re-raises a registered
    cancellation signal - that is the only way a run can stop on purpose."""
    import tempfile

    import pytest

    from sdts.pipeline import _emit, register_cancel_exception

    class Stop(RuntimeError):
        pass

    register_cancel_exception(Stop)
    with pytest.raises(Stop):
        _emit(lambda *a: (_ for _ in ()).throw(Stop("stop")), "chunk", 0, 1)
    # Unregistered exception types keep the swallow-and-continue behaviour.
    _emit(lambda *a: (_ for _ in ()).throw(RuntimeError("nope")), "chunk", 0, 1)


if __name__ == "__main__":
    import traceback
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
