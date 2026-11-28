"""Evaluation of a transcript against a human-corrected reference.

DER comes from `pyannote.metrics` (already a dependency); WER uses a small
pure-Python Levenshtein implementation so the web extra does not need another
package. For ja/zh the "word" unit is a character, which is the usual CER
convention and avoids pretending whitespace tokenisation works.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from .types import TranscriptResult, Utterance


def utterances_from(source: dict[str, Any] | TranscriptResult | Iterable[Utterance]) -> list[Utterance]:
    if isinstance(source, TranscriptResult):
        return source.utterances
    if isinstance(source, dict):
        raws = source.get("utterances", [])
        out = []
        for raw in raws:
            out.append(Utterance(
                speaker=raw.get("speaker") or raw.get("speaker_display") or "UNKNOWN",
                start=float(raw.get("start", 0.0)), end=float(raw.get("end", 0.0)),
                text=raw.get("text", ""),
            ))
        return out
    return list(source)


def _tokens(text: str, character: bool) -> list[str]:
    text = unicodedata.normalize("NFKC", text or "").strip()
    if character:
        return [c for c in text if not c.isspace()]
    text = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return text.split()


def levenshtein(a: list[str], b: list[str]) -> tuple[int, int, int]:
    """Returns (substitutions, deletions, insertions).

    The edit counts come from a backtrace over the full matrix. Recording the
    best incoming operation while filling the matrix looks tempting but is
    wrong: a cell's local winner can later be superseded by a better path.
    """
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j - 1] + cost,
                           dp[i - 1][j] + 1, dp[i][j - 1] + 1)
    s = d = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            cost = 0 if a[i - 1] == b[j - 1] else 1
            if dp[i][j] == dp[i - 1][j - 1] + cost:
                if cost:
                    s += 1
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            d += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return s, d, ins


def word_error_rate(reference: str, hypothesis: str, *, character: bool = False) -> dict[str, Any]:
    ref, hyp = _tokens(reference, character), _tokens(hypothesis, character)
    if not ref:
        return {"wer": None, "substitutions": 0, "deletions": 0,
                "insertions": len(hyp), "reference_tokens": 0,
                "unit": "character" if character else "word"}
    s, d, i = levenshtein(ref, hyp)
    return {
        "wer": round((s + d + i) / len(ref), 4),
        "substitutions": s, "deletions": d, "insertions": i,
        "reference_tokens": len(ref),
        "unit": "character" if character else "word",
    }


def diarization_error_rate(reference: list[Utterance],
                           hypothesis: list[Utterance]) -> dict[str, Any]:
    try:
        from pyannote.core import Annotation, Segment as PYSegment
        from pyannote.metrics.diarization import DiarizationErrorRate
    except ImportError:
        return {"der": None, "error": "pyannote.metrics is unavailable"}

    def annotation(utts: list[Utterance]) -> "Annotation":
        ann = Annotation()
        for u in utts:
            start, end = max(0.0, u.start), max(0.0, u.end)
            if end <= start:
                continue
            ann[PYSegment(start, end)] = u.speaker or "UNKNOWN"
        return ann

    ref, hyp = annotation(reference), annotation(hypothesis)
    if not len(ref) and not len(hyp):
        return {"der": 0.0, "components": {}}
    metric = DiarizationErrorRate(collar=0.0)
    try:
        der = float(metric(ref, hyp))
    except Exception as exc:  # noqa: BLE001 - malformed annotations are a result, not a crash
        return {"der": None, "error": str(exc)}
    details = (metric.results_[-1][1] if getattr(metric, "results_", None) else {})
    components = {k: round(float(v), 4) for k, v in details.items()
                  if isinstance(v, (int, float))}
    return {"der": round(der, 4), "components": components}


def evaluate(reference: dict[str, Any] | TranscriptResult | Iterable[Utterance],
             hypothesis: dict[str, Any] | TranscriptResult | Iterable[Utterance],
             *, language: str | None = None) -> dict[str, Any]:
    """Compare a gold transcript with a machine transcript."""
    ref = sorted(utterances_from(reference), key=lambda u: u.start)
    hyp = sorted(utterances_from(hypothesis), key=lambda u: u.start)
    der = diarization_error_rate(ref, hyp)
    character = bool(language and language.lower().startswith(("ja", "zh")))
    wer = word_error_rate(
        " ".join(u.text for u in ref), " ".join(u.text for u in hyp),
        character=character)
    return {
        "der": der.get("der"),
        "der_components": der.get("components", {}),
        "der_error": der.get("error"),
        "wer": wer["wer"],
        "wer_components": {k: v for k, v in wer.items() if k != "wer"},
        "reference_utterances": len(ref),
        "hypothesis_utterances": len(hyp),
    }
