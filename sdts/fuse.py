"""Stage 5: fusing transcript with speaker turns.

This is the part that most tutorials get wrong. The naive approach -
`whisperx.assign_word_speakers` - takes each word, finds overlapping diarization
segments, and picks the one with the largest intersection. It fails in three
predictable ways:

  1. Whisper segments often span a speaker change. Assigning "the segment" to one
     speaker loses the other half of the conversation.
  2. pyannote drops short back-channels entirely ("うん", "yeah"). Those words
     overlap nothing and get assigned None.
  3. When ASR and diarization disagree on a boundary by 200 ms, the tie is
     resolved by noise, and you get a phantom speaker switch mid-sentence.

So instead: assign per word, with a three-tier strategy, then rebuild
utterances from runs of the same speaker.
"""
from __future__ import annotations

import bisect
import logging

from .config import MergeConfig
from .types import Segment, Turn, Utterance, Word

log = logging.getLogger(__name__)


class TurnIndex:
    """Interval index over speaker turns. O(log n) lookup, sorted by start."""

    def __init__(self, turns: list[Turn]):
        self.turns = sorted(turns, key=lambda t: (t.start, t.end))
        self._starts = [t.start for t in self.turns]
        self._max_end_prefix: list[float] = []
        running = float("-inf")
        for t in self.turns:
            running = max(running, t.end)
            self._max_end_prefix.append(running)

    def __len__(self) -> int:
        return len(self.turns)

    def overlapping(self, start: float, end: float) -> list[tuple[Turn, float]]:
        """Return (turn, overlap_seconds) for every turn intersecting the span."""
        if end < start:
            start, end = end, start
        hi = bisect.bisect_left(self._starts, end)
        out: list[tuple[Turn, float]] = []
        # Walk backwards only while some earlier turn can still reach `start`.
        i = hi - 1
        while i >= 0 and self._max_end_prefix[i] > start:
            t = self.turns[i]
            ov = min(t.end, end) - max(t.start, start)
            if ov > 0:
                out.append((t, ov))
            i -= 1
        out.reverse()
        return out

    def speaker_at(self, t: float, tolerance: float = 0.5) -> str | None:
        """Speaker active at instant `t`; falls back to nearest turn."""
        hits = self.overlapping(t, t + 1e-6)
        if hits:
            return max(hits, key=lambda x: x[1])[0].speaker
        # Gap between turns: snap to the closest one within tolerance.
        best, best_d = None, tolerance
        i = bisect.bisect_left(self._starts, t)
        for j in (i - 1, i):
            if 0 <= j < len(self.turns):
                turn = self.turns[j]
                d = min(abs(t - turn.start), abs(t - turn.end))
                if d <= best_d:
                    best, best_d = turn, d
        return best.speaker if best else None


def _assign_word(word: Word, index: TurnIndex, cfg: MergeConfig) -> str | None:
    """Four-tier word attribution.

    Tier 1: weighted overlap by duration - the honest measure when the word
            genuinely straddles a boundary.
    Tier 2: turn containing the word midpoint - robust for very short words
            (Japanese particles) where overlap arithmetic is unstable.
    Tier 3: dominant overlap regardless of margin - keeps hard boundary cases
            from being discarded.
    Tier 4: nearest turn within snap_tolerance - recovers words that fell into
            a diarization gap (dropped back-channels).
    """
    span = max(word.end - word.start, 1e-3)
    hits = index.overlapping(word.start, word.end)
    if hits:
        weighted = max(hits, key=lambda x: x[1])
        # Demand a clear winner, else prefer the midpoint owner.
        if weighted[1] / span >= 0.6:
            return weighted[0].speaker
        mid = (word.start + word.end) / 2.0
        mid_hits = index.overlapping(mid, mid + 1e-6)
        if mid_hits:
            return max(mid_hits, key=lambda x: x[1])[0].speaker
        return weighted[0].speaker
    return index.speaker_at((word.start + word.end) / 2.0, cfg.snap_tolerance)


def _divide_text(seg: Segment, parts: list[tuple[float, float, str]]
                 ) -> list[tuple[str, float, float, str]]:
    """Give each time part its share of the original sentence text.

    Split points come from two sources, in order of reliability:

    1. **Word timings**, when the ASR produced them (whisper does by default).
       The cut lands exactly on the first word that belongs to the next part,
       so "hello | world" splits correctly even when the midpoint of the
       sentence falls inside "hello".
    2. **Character share by duration**, when there are no words. Japanese has
       no spaces so a char-proportional cut is nudged to the nearest
       punctuation mark, which keeps the break on a natural pause.

    Concatenating the parts always reproduces the original text exactly.
    """
    if not parts:
        # The sentence fell entirely into a gap between diarization turns (or
        # there were no turns at all). Keep the text whole; _fill_gaps will
        # attribute it to the nearest labelled neighbour in time.
        log.debug("segment at %.2fs has no overlapping turn; deferring to gap fill",
                  seg.start)
        return [(seg.text, seg.start, seg.end, None)]

    if len(parts) == 1:
        a, b, sp = parts[0]
        return [(seg.text, a, b, sp)]

    char_cuts = _char_cuts_from_words(seg, parts)
    if char_cuts is None:
        char_cuts = _char_cuts_from_duration(seg, parts)

    out: list[tuple[str, float, float, str]] = []
    prev = 0
    for (a, b, sp), end in zip(parts, char_cuts):
        text = "".join(seg.text[prev:end]).strip()
        prev = end
        if text:
            out.append((text, a, b, sp))
    if not out:
        a, b, sp = parts[0]
        out.append((seg.text, a, b, sp))
    return out


def _char_cuts_from_words(seg: Segment,
                          parts: list[tuple[float, float, str]]) -> list[int] | None:
    """Character index where each part's text begins, derived from word timings.

    Each word is assigned to the part whose time span covers it, then the text
    is cut at the first word belonging to the next part. Using the part's own
    start time (not the boundary estimate) avoids off-by-one-word effects where
    a word straddles the boundary.
    """
    if not seg.words:
        return None

    text = seg.text
    # Locate each word in the text once, in order, so repeated words map to
    # successive occurrences instead of always the first.
    located: list[tuple[float, int, int]] = []
    pos = 0
    for w in seg.words:
        token = w.text.strip()
        if not token:
            continue
        idx = text.find(token, pos)
        if idx < 0:
            continue
        located.append((w.start, idx, idx + len(token)))
        pos = idx + len(token)
    if not located:
        return None

    bounds = [round(a, 6) for a, _b, _sp in parts]
    cuts: list[int] = []
    for i in range(len(parts) - 1):
        boundary = bounds[i + 1]
        # First positioned word at or after the next part's start.
        cut = None
        for start, idx, _end in located:
            if start >= boundary and idx > 0:
                cut = idx
                break
            if idx >= len(text):
                break
        if cut is None:
            return None                 # no word explains this boundary
        cuts.append(cut)
    cuts.append(len(text))
    # Cuts must be non-decreasing for the text slices to be correct.
    if any(cuts[i] > cuts[i + 1] for i in range(len(cuts) - 1)):
        return None
    return cuts


def _char_cuts_from_duration(seg: Segment,
                             parts: list[tuple[float, float, str]]) -> list[int]:
    """Character index where each part starts, by share of segment duration."""
    total = max(sum(b - a for a, b, _ in parts), 1e-6)
    chars = list(seg.text)
    cuts: list[int] = []
    acc = 0.0
    prev = 0
    for i, (a, b, _sp) in enumerate(parts):
        acc += b - a
        if i == len(parts) - 1:
            cuts.append(len(chars))
            break
        target = int(round(len(chars) * acc / total))
        end = _snap_to_break(chars, target, prev)
        cuts.append(end)
        prev = end
    return cuts


_BREAK_CHARS = set("、。，．！？!?,.)）」』】 ")


def _snap_to_break(chars: list[str], target: int, low: int, window: int = 8) -> int:
    """Move a split index to the nearest punctuation/space inside a window."""
    best, best_dist = None, None
    hi = min(len(chars), target + window)
    lo = max(low + 1, target - window)
    for i in range(lo, hi):
        if chars[i - 1] in _BREAK_CHARS:
            d = abs(i - target)
            if best_dist is None or d < best_dist:
                best, best_dist = i, d
    if best is not None:
        return best
    return max(low + 1, min(target, len(chars)))


def _assign_segment(seg: Segment, index: TurnIndex) -> str | None:
    """Segment-level attribution: total overlap, then midpoint (tiers 1-2)."""
    hits = index.overlapping(seg.start, seg.end)
    if hits:
        return max(hits, key=lambda x: x[1])[0].speaker
    return index.speaker_at((seg.start + seg.end) / 2.0)


def _split_segment(seg: Segment, index: TurnIndex,
                   cfg: MergeConfig) -> list[tuple[float, float, str]]:
    """Split one ASR sentence at speaker changes.

    Whisper cuts on pauses, not on speaker changes, so a single "sentence" can
    contain two people (the end of a question plus the start of an answer).
    Assigning the whole thing to whoever overlaps most mislabels the rest.
    Instead, cut wherever the dominant speaker changes and both sides carry at
    least `min_part_share` of the segment, so a brief interruption does not
    fragment the sentence.

    Returns [(start, end, speaker)] in order, covering the whole segment.
    """
    hits = index.overlapping(seg.start, seg.end)
    if not hits:
        fallback = index.speaker_at((seg.start + seg.end) / 2.0)
        if fallback is None:
            return []
        return [(seg.start, seg.end, fallback)]

    hits = sorted(hits, key=lambda x: x[0].start)
    span = max(seg.end - seg.start, 1e-6)

    # Keep only speakers with a meaningful share of the sentence.
    shares: dict[str, float] = {}
    for turn, ov in hits:
        shares[turn.speaker] = shares.get(turn.speaker, 0.0) + ov
    dominant = sorted(shares.items(), key=lambda kv: -kv[1])

    if len(dominant) == 1 or dominant[1][1] / span < cfg.min_part_share:
        return [(seg.start, seg.end, dominant[0][0])]

    # Walk the overlaps, cutting where the leading speaker changes.
    pieces: list[list] = []          # [start, end, speaker]
    for turn, _ov in hits:
        a = max(turn.start, seg.start)
        b = min(turn.end, seg.end)
        if b <= a:
            continue
        if pieces and pieces[-1][2] == turn.speaker:
            pieces[-1][1] = max(pieces[-1][1], b)
        else:
            pieces.append([a, b, turn.speaker])

    if not pieces:
        return [(seg.start, seg.end, dominant[0][0])]

    # One piece carrying < min_part_share is noise: fold it into its neighbour.
    merged: list[list] = []
    for p in pieces:
        if merged and (p[1] - p[0]) / span < cfg.min_part_share:
            merged[-1][1] = max(merged[-1][1], p[1])
        else:
            merged.append(p)

    return [(a, b, sp) for a, b, sp in merged]


def fuse(segments: list[Segment], turns: list[Turn],
         cfg: MergeConfig) -> list[Utterance]:
    index = TurnIndex(turns)
    if not len(index):
        log.warning("no speaker turns available; emitting single-speaker transcript")
        return _single_speaker(segments)

    flat: list[tuple[Word, str | None]] = []
    for seg in segments:
        if not seg.text:
            continue
        if cfg.granularity == "word" and seg.words:
            for w in seg.words:
                flat.append((w, _assign_word(w, index, cfg)))
        else:
            # Sentence granularity: the ASR segment is the atom, so its
            # punctuation survives. It is cut only when the sentence genuinely
            # spans two speakers, and the text is divided at the cut point.
            parts = _split_segment(seg, index, cfg)
            for piece_text, a, b, speaker in _divide_text(seg, parts):
                flat.append((Word(a, b, piece_text), speaker))

    if not flat:
        return []

    # Second pass: fill remaining None by nearest labelled neighbour in time.
    flat.sort(key=lambda x: (x[0].start, x[0].end))
    flat = _fill_gaps(flat, cfg)

    return _build_utterances(flat, cfg)


def _fill_gaps(flat: list[tuple[Word, str | None]],
               cfg: MergeConfig) -> list[tuple[Word, str | None]]:
    labelled = [(i, w, s) for i, (w, s) in enumerate(flat) if s]
    if not labelled:
        return [(w, "SPEAKER_00") for w, _ in flat]
    for i, (w, s) in enumerate(flat):
        if s:
            continue
        mid = (w.start + w.end) / 2.0
        best = min(labelled, key=lambda x: min(abs(mid - x[1].start),
                                               abs(mid - x[1].end)))
        flat[i] = (w, best[2])
    return flat


def _build_utterances(flat: list[tuple[Word, str | None]],
                      cfg: MergeConfig) -> list[Utterance]:
    utts: list[Utterance] = []
    cur_speaker: str | None = None
    cur_words: list[Word] = []

    def flush() -> None:
        nonlocal cur_speaker, cur_words
        if not cur_words or cur_speaker is None:
            cur_speaker, cur_words = None, []
            return
        utts.append(Utterance(
            speaker=cur_speaker,
            start=cur_words[0].start,
            end=cur_words[-1].end,
            text=_join([w.text for w in cur_words]),
            words=list(cur_words),
        ))
        cur_speaker, cur_words = None, []

    for word, speaker in flat:
        if speaker != cur_speaker:
            # Speaker changed: close the utterance. Never merge across a speaker
            # change - that is the whole point of diarization. Spurious
            # sub-word jitter is handled by the min_utterance absorption pass
            # below, which is the only place allowed to override a boundary.
            flush()
            cur_speaker = speaker
            cur_words = [word]
        else:
            gap = word.start - cur_words[-1].end if cur_words else 0.0
            if gap > cfg.merge_gap:
                flush()
                cur_speaker = speaker
                cur_words = [word]
            else:
                cur_words.append(word)
    flush()

    # Absorb sub-minimum fragments. A fragment of the SAME speaker is simply
    # rejoined to its neighbour (this undoes spurious splits). A fragment of a
    # DIFFERENT speaker is absorbed only when it is shorter than
    # min_utterance / 2 - that is a one-word leak (a stray "um" attributed to
    # the wrong person), not a real turn. After absorbing, re-check: the
    # fragment may still now be below the threshold, so run to a fixed point.
    merged: list[Utterance] = []
    floor = cfg.min_utterance
    # Timestamps come from float arithmetic on millisecond values, so exact
    # boundary comparisons need slack or a 0.2000000000000002 fragment fails
    # a `< 0.2` test. 10 ms is below any perceptible speech unit.
    eps = 0.01
    for u in utts:
        if not merged:
            merged.append(u)
            continue
        prev = merged[-1]
        gap = u.start - prev.end
        same = u.speaker == prev.speaker
        tiny = u.duration < floor / 2 - eps
        # Absorb when: same speaker and the previous block is below the floor,
        # or the incoming fragment is so small it cannot be a real turn.
        # Note `u` is *not* appended when absorbed, so the caller's next
        # iteration continues against the enlarged `prev`; a chain of
        # micro-fragments therefore collapses into one utterance.
        if gap <= cfg.merge_gap and ((same and prev.duration < floor - eps) or tiny):
            prev.text = _join([prev.text, u.text])
            prev.words.extend(u.words)
            prev.start = min(prev.start, u.start)
            prev.end = max(prev.end, u.end)
            continue
        merged.append(u)
    return merged


def _join(parts: list[str]) -> str:
    """Join tokens respecting languages without spaces (ja/zh) vs with (en)."""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not out:
            out = p
        elif _needs_space(out[-1], p[0]):
            out += " " + p
        else:
            out += p
    return out


def _needs_space(prev: str, nxt: str) -> bool:
    # No space when either side is CJK punctuation or a CJK character.
    cjk = lambda c: "\u3000" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef"
    if cjk(prev) or cjk(nxt):
        return False
    return not (prev in ",.!?;:" and nxt == " ")


def _single_speaker(segments: list[Segment]) -> list[Utterance]:
    utts = []
    for s in segments:
        if not s.text.strip():
            continue
        utts.append(Utterance(speaker="SPEAKER_00", start=s.start, end=s.end,
                              text=s.text.strip(), words=list(s.words)))
    return utts
