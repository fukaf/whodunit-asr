"""Unit tests for the deterministic parts (no models, no network, no GPU).

Run:  python -m pytest tests -q
These cover the logic that actually decides speaker attribution, which is
where bugs hide. Model-level quality needs audio fixtures and is out of scope
for unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdts.config import MergeConfig, PipelineConfig
from sdts.fuse import TurnIndex, _join, fuse
from sdts.render import render_srt, render_txt
from sdts.types import Segment, TranscriptResult, Turn, Utterance, Word
from sdts.audio import plan_chunks
from sdts.config import AudioConfig


def W(s, e, t):
    return Word(start=s, end=e, text=t)


# -- interval index ---------------------------------------------------------
def test_overlap_lookup():
    idx = TurnIndex([Turn(0, 5, "A"), Turn(5, 10, "B")])
    hits = idx.overlapping(4.0, 6.0)
    assert [h[0].speaker for h in hits] == ["A", "B"]
    assert abs(hits[0][1] - 1.0) < 1e-6 and abs(hits[1][1] - 1.0) < 1e-6


def test_speaker_at_snaps_into_gaps():
    idx = TurnIndex([Turn(0, 5, "A"), Turn(5.4, 10, "B")])
    assert idx.speaker_at(5.2, tolerance=0.5) in ("A", "B")
    assert idx.speaker_at(5.2, tolerance=0.01) is None


def test_many_turns_do_not_blow_up():
    turns = [Turn(i * 0.5, i * 0.5 + 0.4, f"S{i % 3}") for i in range(2000)]
    idx = TurnIndex(turns)
    assert idx.speaker_at(500.2) is not None


# -- fusion ----------------------------------------------------------------
def test_segment_spanning_speaker_change_is_split():
    """The whole point of word-level fusion: one ASR segment, two speakers."""
    seg = Segment(0.0, 4.0, "hello there how are you", words=[
        W(0.0, 1.0, "hello"), W(1.0, 2.0, "there"),
        W(2.0, 3.0, "how"), W(3.0, 4.0, "are"),
    ])
    turns = [Turn(0.0, 2.0, "A"), Turn(2.0, 4.0, "B")]
    utts = fuse([seg], turns, MergeConfig(granularity="word"))
    assert [u.speaker for u in utts] == ["A", "B"]
    assert utts[0].text == "hello there"
    assert utts[1].text.startswith("how are")


def test_partial_overlap_prefers_dominant_speaker():
    seg = Segment(0.0, 1.0, "ok wow", words=[W(0.0, 0.5, "ok"), W(0.5, 1.0, "wow")])
    turns = [Turn(0.0, 0.9, "A"), Turn(0.9, 3.0, "B")]
    utts = fuse([seg], turns, MergeConfig())
    assert len(utts) == 1 and utts[0].speaker == "A"


def test_words_in_diarization_gap_are_recovered():
    """pyannote drops short back-channels; their words must not vanish."""
    seg = Segment(0.0, 3.0, "long answer yeah long answer", words=[
        W(0.0, 1.0, "long"), W(1.0, 2.0, "answer"), W(2.0, 2.2, "yeah"),
        W(2.2, 3.0, "long"),
    ])
    turns = [Turn(0.0, 1.9, "A"), Turn(2.4, 5.0, "A")]
    utts = fuse([seg], turns, MergeConfig(snap_tolerance=0.6))
    assert all(u.speaker == "A" for u in utts)
    assert "yeah" in " ".join(u.text for u in utts)


def test_short_pause_merges_same_speaker():
    segs = [Segment(0.0, 1.0, "first part", [W(0.0, 0.5, "first"), W(0.5, 1.0, "part")]),
            Segment(1.5, 2.5, "second part", [W(1.5, 2.0, "second"), W(2.0, 2.5, "part")])]
    turns = [Turn(0.0, 10.0, "A")]
    utts = fuse(segs, turns, MergeConfig(merge_gap=1.0))
    assert len(utts) == 1
    assert utts[0].text == "first part second part"


def test_one_word_leak_is_absorbed():
    """A single stray word mis-attributed to another speaker must not create a turn."""
    flat_words = [(W(0.0, 2.0, "so"), "A"), (W(2.0, 2.1, "um"), "B"),
                  (W(2.1, 4.0, "anyway"), "A")]
    from sdts.fuse import _build_utterances
    utts = _build_utterances(flat_words, MergeConfig(min_utterance=0.4, merge_gap=1.2))
    # Every utterance is speaker A: the B fragment was a one-word leak.
    assert {u.speaker for u in utts} == {"A"}
    assert "um" in " ".join(u.text for u in utts)


def test_real_short_answer_kept():
    """A genuine short back-channel by another speaker survives."""
    flat_words = [(W(0.0, 2.0, "do you agree"), "A"), (W(2.0, 3.2, "yes"), "B")]
    from sdts.fuse import _build_utterances
    utts = _build_utterances(flat_words, MergeConfig(min_utterance=0.4, merge_gap=1.2))
    assert [u.speaker for u in utts] == ["A", "B"]
    assert utts[1].text == "yes"


def test_long_pause_splits_same_speaker():
    segs = [Segment(0.0, 1.0, "first", [W(0.0, 1.0, "first")]),
            Segment(5.0, 6.0, "second", [W(5.0, 6.0, "second")])]
    turns = [Turn(0.0, 10.0, "A")]
    utts = fuse(segs, turns, MergeConfig(merge_gap=1.0))
    assert len(utts) == 2


def test_no_turns_degrades_to_single_speaker():
    segs = [Segment(0.0, 1.0, "hi")]
    utts = fuse(segs, [], MergeConfig())
    assert len(utts) == 1 and utts[0].speaker == "SPEAKER_00"


def test_segment_granularity_mode():
    """A sentence split 50/50 between two speakers is cut into two lines.

    Sentence granularity still refuses to cut on a brief interjection - that
    is covered by test_brief_interjection_does_not_split_sentence - but an even
    split is a genuine speaker change and must not be attributed to one person.
    """
    segs = [Segment(0.0, 4.0, "a b", [W(0.0, 2.0, "a"), W(2.0, 4.0, "b")])]
    turns = [Turn(0.0, 2.0, "A"), Turn(2.0, 4.0, "B")]
    utts = fuse(segs, turns, MergeConfig(granularity="segment"))
    assert [u.speaker for u in utts] == ["A", "B"]
    assert "".join(u.text for u in utts).replace(" ", "") == "ab"


# -- sentence granularity --------------------------------------------------
def test_brief_interjection_does_not_split_sentence():
    """A 10% 'はい' inside a 10 s sentence must not fracture it.

    Cutting on every momentary overlap would turn clean sentences into strings
    of half-words, which is worse than the occasional mis-attributed backchannel.
    """
    seg = Segment(0.0, 10.0, "あの件についてですが、はい、了解しました。")
    turns = [Turn(0.0, 9.0, "A"), Turn(9.0, 10.0, "B")]
    utts = fuse([seg], turns, MergeConfig(granularity="segment"))
    assert len(utts) == 1
    assert utts[0].speaker == "A"
    assert utts[0].text == "あの件についてですが、はい、了解しました。"


def test_sentence_punctuation_is_preserved():
    """Sentence mode keeps the ASR text verbatim - no re-joining from tokens."""
    seg = Segment(0.0, 6.0, "さて、次の議題です。まず確認します。")
    turns = [Turn(0.0, 6.0, "A")]
    utts = fuse([seg], turns, MergeConfig(granularity="segment"))
    assert utts[0].text == "さて、次の議題です。まず確認します。"


def test_split_sentence_loses_no_characters():
    """Whatever the cut, concatenating the parts must return the original."""
    text = "これは質問です。はい、答えます。"
    seg = Segment(0.0, 4.0, text)
    turns = [Turn(0.0, 1.6, "A"), Turn(1.6, 4.0, "B")]
    utts = fuse([seg], turns, MergeConfig(granularity="segment"))
    assert len(utts) == 2
    assert "".join(u.text for u in utts).replace(" ", "") == text.replace(" ", "")


def test_english_sentence_splits_on_space():
    seg = Segment(0.0, 4.0, "hello there how are you")
    turns = [Turn(0.0, 2.0, "A"), Turn(2.0, 4.0, "B")]
    utts = fuse([seg], turns, MergeConfig(granularity="segment"))
    assert [u.speaker for u in utts] == ["A", "B"]
    assert utts[0].text == "hello there"
    assert utts[1].text == "how are you"


# -- text joining ----------------------------------------------------------
def test_join_japanese_has_no_spaces():
    assert _join(["今日は", "いい天気ですね。"]) == "今日はいい天気ですね。"


def test_join_english_has_spaces():
    assert _join(["hello", "world"]) == "hello world"


def test_join_mixed():
    assert _join(["OK", "それでは"]) == "OKそれでは"


# -- chunking --------------------------------------------------------------
def test_plan_chunks_single():
    assert plan_chunks(100.0, AudioConfig(chunk_seconds=1800, chunk_overlap_seconds=15)) == [(0.0, 100.0)]


def test_plan_chunks_covers_whole_file():
    cfg = AudioConfig(chunk_seconds=60, chunk_overlap_seconds=10)
    chunks = plan_chunks(155.0, cfg)
    assert chunks[0][0] == 0.0
    assert chunks[-1][1] == 155.0
    for (s1, e1), (s2, _) in zip(chunks, chunks[1:]):
        assert s2 < e1, "chunks must overlap"


# -- rendering -------------------------------------------------------------
def test_render_txt_and_srt():
    res = TranscriptResult(
        utterances=[Utterance("SPEAKER_00", 0.0, 2.5, "こんにちは"),
                    Utterance("SPEAKER_01", 2.5, 65.25, "hello there")],
        language="ja", speakers=["SPEAKER_00", "SPEAKER_01"], duration=70.0,
    )
    from sdts.config import OutputConfig
    txt = render_txt(res, OutputConfig())
    assert txt.splitlines()[0] == "[00:00:00.000] SPEAKER_00: こんにちは"
    # fmt_time renders the utterance's *start*; 2.5 s -> "00:00:02.500".
    assert "[00:00:02.500] SPEAKER_01: hello there" in txt, txt
    # SRT uses start --> end, so the 65.25 s end must appear there.
    srt = render_srt(res)
    assert "00:00:02,500 --> 00:01:05,250" in srt, srt


def test_speaker_stats():
    res = TranscriptResult(
        utterances=[Utterance("A", 0.0, 4.0, "x"), Utterance("B", 4.0, 6.0, "y")],
        duration=6.0,
    )
    stats = res.speaker_stats()
    assert stats["A"]["share"] == round(4 / 6, 4)


# -- config ----------------------------------------------------------------
def test_config_roundtrip_and_unknown_key():
    cfg = PipelineConfig.from_dict({"asr": {"model": "large-v3"}, "output": {"formats": ["json"]}})
    assert cfg.asr.model == "large-v3"
    assert cfg.output.formats == ("json",)
    try:
        PipelineConfig.from_dict({"asr": {"nope": 1}})
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("should reject unknown keys")


# -- pyannote annotation conversion -----------------------------------------
def test_itertracks_yields_three_values():
    """Regression: pyannote's itertracks(yield_label=True) yields
    (segment, track, label). Unpacking two raised
    'ValueError: too many values to unpack (expected 2)' on real audio."""
    from pyannote.core import Annotation, Segment
    from sdts.diarize import Diarizer
    from sdts.config import DiarizationConfig

    ann = Annotation(uri="t")
    ann[Segment(0.0, 2.0), "A"] = "SPEAKER_00"
    ann[Segment(2.0, 4.0), "A"] = "SPEAKER_01"

    d = Diarizer.__new__(Diarizer)     # no model load
    d.cfg = DiarizationConfig()
    turns = d._to_turns(ann)

    assert [(t.speaker, t.start, t.end) for t in turns] == [
        ("SPEAKER_00", 0.0, 2.0), ("SPEAKER_01", 2.0, 4.0)]


def test_overlapping_same_speaker_tracks_are_merged():
    """The non-exclusive view can put one speaker on several overlapping
    tracks; fusion must not see two competing spans for the same person."""
    from pyannote.core import Annotation, Segment
    from sdts.diarize import Diarizer
    from sdts.config import DiarizationConfig

    ann = Annotation(uri="t")
    ann[Segment(2.0, 4.5), "A"] = "SPEAKER_01"
    ann[Segment(3.0, 6.0), "B"] = "SPEAKER_01"    # overlaps on another track
    ann[Segment(6.0, 8.0), "A"] = "SPEAKER_00"

    d = Diarizer.__new__(Diarizer)
    d.cfg = DiarizationConfig()
    turns = d._to_turns(ann)

    assert [(t.speaker, t.start, t.end) for t in turns] == [
        ("SPEAKER_01", 2.0, 6.0), ("SPEAKER_00", 6.0, 8.0)]


def test_empty_annotation_gives_no_turns():
    from pyannote.core import Annotation
    from sdts.diarize import Diarizer
    from sdts.config import DiarizationConfig

    d = Diarizer.__new__(Diarizer)
    d.cfg = DiarizationConfig()
    assert d._to_turns(Annotation(uri="t")) == []


def test_overlapping_turns_are_trimmed():
    """Regression: gap-filling makes one turn run into the next.

    On real audio a 0.75 s chair announcement overlapped the following
    question by 0.44 s, which split a name in half across two speakers
    ("岡田和" / "也さん"). Two speakers cannot hold the same instant in a
    transcript, so the earlier turn must be trimmed at the later one's start.
    """
    from pyannote.core import Annotation, Segment as PSeg
    from sdts.diarize import Diarizer
    from sdts.config import DiarizationConfig

    ann = Annotation(uri="t")
    ann[PSeg(0.0, 1.0), "A"] = "SPEAKER_00"
    ann[PSeg(0.7, 3.0), "A"] = "SPEAKER_01"      # overlaps 0.3 s

    d = Diarizer.__new__(Diarizer)
    d.cfg = DiarizationConfig()
    turns = d._to_turns(ann)

    assert [(t.speaker, t.start, t.end) for t in turns] == [
        ("SPEAKER_00", 0.0, 0.7), ("SPEAKER_01", 0.7, 3.0)]
    for a, b in zip(turns, turns[1:]):
        assert a.end <= b.start + 1e-9, "turns must not overlap"


def test_shorter_later_turn_wins_over_longer_earlier_one():
    """The later turn always wins a conflict, even when it is very short.

    A 0.2 s blip inside a long turn may be a back-channel or a genuine quick
    reply - the diarizer cannot tell. It keeps the turn and lets the fusion
    stage's min_utterance absorption decide, rather than deleting data here.
    """
    from pyannote.core import Annotation, Segment as PSeg
    from sdts.diarize import Diarizer
    from sdts.config import DiarizationConfig

    ann = Annotation(uri="t")
    ann[PSeg(0.0, 5.0), "A"] = "SPEAKER_00"
    ann[PSeg(1.0, 1.2), "B"] = "SPEAKER_01"      # entirely inside the first

    d = Diarizer.__new__(Diarizer)
    d.cfg = DiarizationConfig()
    turns = d._to_turns(ann)
    assert [(t.speaker, t.start, t.end) for t in turns] == [
        ("SPEAKER_00", 0.0, 1.0), ("SPEAKER_01", 1.0, 1.2)]


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
