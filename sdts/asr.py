"""Stage 2: speech recognition.

Engines are registered behind one interface so the rest of the pipeline never
imports a specific ASR library. Default is faster-whisper (CTranslate2):
same Whisper weights, 4x faster than openai/whisper on both CPU and GPU, and
quantisation that actually works (int8 on CPU is ~40% less memory).

The alternative engine is SenseVoiceSmall, hooked in as the "simpler + cheaper
licence path" option required by spec item 4.
"""
from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from .config import AsrConfig
from .device import resolve_device, resolve_float
from .types import Segment, Word

log = logging.getLogger(__name__)


class AsrEngine(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> tuple[list[Segment], str | None]:
        """audio: 16 kHz mono float32. Returns (segments, detected_language)."""
        ...


# --------------------------------------------------------------------------
# faster-whisper (default)
# --------------------------------------------------------------------------
class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self, cfg: AsrConfig):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        # Resolve the device once and reuse it for both the model and the
        # compute type: float16 has no CPU kernel, so a "cuda" config on a
        # CPU-only torch build would otherwise fail at load time.
        self.device = resolve_device(cfg.device)
        self.compute_type = resolve_float(cfg.compute_type)
        if self.device == "cpu" and self.compute_type in ("float16", "half"):
            self.compute_type = "int8"   # faster than float32 on CPU, no quality cliff
        log.info("loading faster-whisper %s (%s/%s)", cfg.model, self.device, self.compute_type)
        self.model = WhisperModel(
            cfg.model,
            device=self.device,
            compute_type=self.compute_type,
            # Keep everything under one cache dir so air-gapped deploys can
            # rsync it once and never touch the network again.
            download_root=None,
        )

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> tuple[list[Segment], str | None]:
        cfg = self.cfg
        seg_iter, info = self.model.transcribe(
            audio,
            language=language or cfg.language,
            beam_size=cfg.beam_size,
            vad_filter=cfg.vad_filter,
            # word_timestamps gives us a fallback to coarse word timing even
            # when the aligner is disabled or fails on a language.
            word_timestamps=True,
            initial_prompt=cfg.initial_prompt,
            condition_on_previous_text=cfg.condition_on_previous_text,
            no_repeat_ngram_size=cfg.no_repeat_ngram_size,
        )
        segments: list[Segment] = []
        for s in seg_iter:
            words = [
                Word(start=float(w.start), end=float(w.end), text=w.word.strip(),
                     score=float(w.probability) if w.probability is not None else None)
                for w in (s.words or [])
                if w.word and w.word.strip()
            ]
            segments.append(Segment(
                start=float(s.start), end=float(s.end), text=s.text.strip(),
                words=words, language=getattr(info, "language", None),
                avg_logprob=getattr(s, "avg_logprob", None),
            ))
        return segments, getattr(info, "language", None)


# --------------------------------------------------------------------------
# SenseVoiceSmall (alternative: multilingual, non-autoregressive, very fast)
# --------------------------------------------------------------------------
class SenseVoiceEngine:
    """Alibaba SenseVoiceSmall via FunASR.

    Useful when you need Japanese/English/Cantonese in one model at ~15x the
    speed of large-v3 and do not want a gated-download dependency on HF.
    Whisper remains the default because its Japanese punctuation and long-form
    stability are better out of the box.

    Licence caveat: the FunASR *code* is MIT, but the model weights ship under
    the "FunASR Model Open Source License" (see docs/ARCHITECTURE.md). Free to
    use, modify and redistribute including commercially, with an attribution
    requirement and a community-conduct clause. Read it before shipping.
    """

    name = "sensevoice"

    def __init__(self, cfg: AsrConfig):
        from funasr import AutoModel

        self.cfg = cfg
        self.model = AutoModel(
            model="iic/SenseVoiceSmall",
            device=cfg.device if cfg.device != "cuda" else "cuda:0",
            disable_update=True,
            trust_remote_code=False,
        )

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> tuple[list[Segment], str | None]:
        import re

        lang = (language or self.cfg.language or "auto")
        res = self.model.generate(
            input=audio,
            cache={},
            language=lang,
            use_itn=True,      # inverse text normalisation (numbers, dates)
            batch_size_s=60,
            merge_vad=True,
        )
        # SenseVoice emits rich tags like <|ja|><|NEUTRAL|><|Speech|><|woitn|>;
        # strip them, they are metadata not speech.
        tag = re.compile(r"<\|[^|]*\|>")
        segments: list[Segment] = []
        for item in res:
            text = tag.sub("", item.get("text", "")).strip()
            if not text:
                continue
            ts = item.get("timestamp") or []
            start = float(ts[0][0]) / 1000.0 if ts else 0.0
            end = float(ts[-1][1]) / 1000.0 if ts else start
            # SenseVoice does not emit word timings reliably; the aligner
            # downstream is what recovers them.
            segments.append(Segment(start=start, end=end, text=text, words=[]))
        detected = lang if lang != "auto" else None
        return segments, detected


_ENGINES: dict[str, type] = {
    "faster-whisper": FasterWhisperEngine,
    "sensevoice": SenseVoiceEngine,
}


def build_engine(cfg: AsrConfig) -> AsrEngine:
    try:
        klass = _ENGINES[cfg.engine]
    except KeyError:
        raise ValueError(f"unknown asr engine {cfg.engine!r}; "
                         f"available: {sorted(_ENGINES)}") from None
    return klass(cfg)
