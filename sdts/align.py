"""Stage 4: forced alignment to word-level timestamps.

Whisper's own word timestamps come from cross-attention heuristics: they are
good enough for subtitle chunking and consistently 100-300 ms off at segment
edges. When you are trying to decide "was this word before or after the speaker
change", that error is the dominant source of mis-attribution.

WhisperX aligns the recognised text to a CTC model (wav2vec2) with a Viterbi
pass, which pins every word to the acoustic evidence. That is what turns
speaker attribution from a 300 ms coin flip into a ~30 ms decision.

Default models used per language (from whisperx.alignment):
  en -> WAV2VEC2_ASR_BASE_960H            (torchaudio bundle)
  ja -> jonatasgrosman/wav2vec2-large-xlsr-53-japanese  (HF, Apache-2.0)

Note: WhisperX's own repo is BSD-2-Clause, and its pyannote integration is
optional - we do NOT use it, we call the aligner only and keep diarization
in our own stage so speaker numbering is under our control.
"""
from __future__ import annotations

import logging
import re

import numpy as np

from .config import AlignConfig
from .types import Segment, Word

log = logging.getLogger(__name__)

_JP_PUNCT = re.compile(r"[\s　、。！？…「」『』（）・,\.!\?\-—―~〜\"'“”‘’]")


class Aligner:
    def __init__(self, cfg: AlignConfig, device: str = "cuda"):
        self.cfg = cfg
        self.device = device
        self._cache: dict[str, tuple] = {}

    def _load(self, language: str):
        if language in self._cache:
            return self._cache[language]
        from whisperx import alignment as wx_align
        import torch

        model_name = self.cfg.model or wx_align.DEFAULT_ALIGN_MODELS_HF.get(language)
        if model_name is None and language in wx_align.DEFAULT_ALIGN_MODELS_TORCH:
            model_name = wx_align.DEFAULT_ALIGN_MODELS_TORCH[language]
        if model_name is None:
            raise ValueError(
                f"no default wav2vec2 alignment model for language {language!r}. "
                "Pass align.model explicitly (any CTC wav2vec2 model on HF works)."
            )
        device = self.device
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        log.info("loading aligner %s for %s", model_name, language)
        model, meta = wx_align.load_align_model(language_code=language,
                                                device=device,
                                                model_name=model_name)
        self._ensure_nltk_data(language)
        self._cache[language] = (model, meta)
        return model, meta

    @staticmethod
    def _ensure_nltk_data(language: str) -> None:
        """whisperx's align() calls nltk.sent_tokenize for EVERY language.

        That is not obvious: Japanese output gets split by the *English* Punkt
        model. Without the data, `align()` raises mid-run and every word
        timestamp silently falls back to segment-level timing - which is only
        visible as a warning after the whole file has been transcribed. Fail
        fast, at load time, with the actual fix.
        """
        import nltk
        try:
            nltk.data.find("tokenizers/punkt_tab")
            return
        except LookupError:
            pass
        try:
            nltk.download("punkt_tab", quiet=True, raise_on_error=True)
        except Exception:                       # noqa: BLE001 - network/proxy
            pass
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError as exc:
            raise RuntimeError(
                "word alignment needs NLTK's 'punkt_tab' data, which is not "
                "installed and could not be downloaded (corporate proxy or "
                "offline machine). Either run:\n"
                "  python -m nltk.downloader punkt_tab\n"
                "or set align.enabled=false to skip alignment and accept "
                "segment-level timestamps."
            ) from exc

    def align(self, segments: list[Segment], audio: np.ndarray,
              language: str) -> list[Segment]:
        if not segments:
            return segments
        model, meta = self._load(language)
        payload = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in segments if s.text
        ]
        if not payload:
            return segments

        import torch
        result = self._call_align(model, meta, payload, audio, language)

        by_index = {i: w for i, w in result}
        out: list[Segment] = []
        pos = 0
        for s in segments:
            if not s.text:
                out.append(s)
                continue
            words = by_index.get(pos, [])
            pos += 1
            if words:
                s = Segment(start=words[0].start,
                            end=max(words[-1].end, words[0].end),
                            text=s.text, words=words,
                            language=s.language, avg_logprob=s.avg_logprob)
            out.append(s)
        return out

    def _call_align(self, model, meta, payload, audio, language):
        from whisperx.alignment import align

        try:
            res = align(payload, model, meta, audio, self.device,
                        interpolate_method=self.cfg.interpolate_method,
                        return_char_alignments=False)
        except Exception as exc:                      # noqa: BLE001
            # Never let alignment kill a job: degrade to segment-level timing.
            log.warning("alignment failed (%s); falling back to segment timing", exc)
            return []

        rows = []
        for idx, seg in enumerate(res.get("segments", [])):
            words: list[Word] = []
            for w in seg.get("words", []):
                if "start" not in w or "end" not in w or w.get("start") is None:
                    continue
                text = w.get("word", "")
                if language in ("ja", "zh"):
                    text = _JP_PUNCT.sub("", text)
                if not text:
                    continue
                words.append(Word(start=float(w["start"]), end=float(w["end"]),
                                  text=text, score=w.get("score")))
            if words:
                rows.append((idx, words))
        return rows

    def word_timestamps_from_ctc(self, segment: Segment) -> list[Word]:
        """Coarse fallback: spread segment duration over words by length.

        Only used when an engine returns neither word timings nor a working
        aligner. Better than nothing for display, useless for attribution -
        the merge stage knows the difference and will prefer turn centroids.
        """
        tokens = segment.text.split()
        if not tokens:
            return []
        total = sum(len(t) + 1 for t in tokens)
        t, out = segment.start, []
        for tok in tokens:
            share = (len(tok) + 1) / total
            dur = segment.duration * share
            out.append(Word(start=t, end=min(segment.end, t + dur), text=tok))
            t += dur
        return out
