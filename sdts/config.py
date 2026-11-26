"""Configuration objects for the SDTS pipeline.

Everything here is plain dataclasses so the config can be built from code,
from a YAML file, or from CLI flags without extra dependencies.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    # Fixed-length chunking for long inputs. 30 min keeps GPU memory flat and
    # lets a crashed chunk be retried instead of losing the whole job.
    chunk_seconds: float = 1800.0
    # Overlap between chunks. Must be > the longest plausible single utterance,
    # otherwise a word spoken at the boundary gets cut in half.
    chunk_overlap_seconds: float = 15.0


@dataclass
class AsrConfig:
    engine: str = "faster-whisper"
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "float16"       # cpu -> "int8"
    language: str | None = None         # None -> auto detect per chunk
    beam_size: int = 5
    vad_filter: bool = True
    # Decoding hints. For Japanese business audio this measurably cuts
    # hallucination on silence and keeps proper nouns stable.
    initial_prompt: str | None = None
    # Keep prior transcript as context. Without it Whiper loses the writing
    # style after the initial prompt and emits almost no Japanese punctuation
    # (measured: 4% -> 99% of sentences punctuated on a 28 min hearing).
    # faster-whisper defaults to True too. Repetition loops are guarded by
    # no_repeat_ngram_size below.
    condition_on_previous_text: bool = True
    # Safety net for the context carry-over above: forbid repeated n-grams.
    # 0 disables it; 0 means "no guard" in CTranslate2 as well.
    no_repeat_ngram_size: int = 0


@dataclass
class DiarizationConfig:
    pipeline: str = "pyannote/speaker-diarization-community-1"
    device: str = "cuda"
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    # "exclusive" reassigns overlapped speech to one speaker and is what you
    # want when reconciling with transcripts. "regular" keeps overlaps.
    mode: str = "exclusive"
    # Fill silent gaps shorter than this inside one speaker's turn. pyannote
    # splits on every pause, so parliamentary / interview audio comes back as
    # hundreds of 1-2 s fragments. Measured on a 28 min committee hearing:
    # 0.5 -> 232 turns, 1.0 -> 101 turns, both keeping all 4 speakers.
    # Above ~1.5 s real back-channels start getting absorbed, so 1.0 is the
    # point where output becomes readable without losing participants.
    # Set to None to keep pyannote's own default.
    min_duration_off: float | None = 1.0


@dataclass
class AlignConfig:
    # Off by default: CTC alignment exists only to sharpen per-WORD timings.
    # Sentence-level output ("who said what, when they started") does not need
    # it, and it adds a wav2vec2 download plus an NLTK data dependency.
    # Turn on together with merge.granularity="word" if you need word timings.
    enabled: bool = False
    # None -> take whisperx's DEFAULT_ALIGN_MODELS_* table for the language.
    model: str | None = None
    interpolate_method: str = "nearest"


@dataclass
class MergeConfig:
    # Whisper segments are already sentence-shaped (it cuts on punctuation), so
    # "segment" gives one utterance per sentence with its original punctuation.
    # "word" additionally needs CTC alignment and yields sub-second fragments.
    granularity: str = "segment"        # segment | word
    # A word whose start falls outside every speaker turn by this much is
    # still assigned to the nearest turn (pyannote drops short turns).
    snap_tolerance: float = 0.5
    # Consecutive turns of the same speaker separated by a pause shorter than
    # this collapse into one utterance.
    merge_gap: float = 1.2
    # Minimum utterance length; shorter fragments get merged into neighbours.
    min_utterance: float = 0.4
    # In "segment" mode, a sentence is split only where a second speaker holds
    # at least this share of it. Below the threshold the sentence goes wholly
    # to the dominant speaker, so a brief "はい" does not fracture the sentence.
    min_part_share: float = 0.25


@dataclass
class OutputConfig:
    out_dir: str = "out"
    formats: tuple[str, ...] = ("json", "srt", "txt")
    timestamp_format: str = "hms"       # hms | seconds


@dataclass
class AdaptationConfig:
    """Controlled growth of the persistent voice gallery."""

    enabled: bool = False
    mode: str = "review"                # review | auto
    update_margin: float = 0.05         # T_update = accept_threshold + this
    min_turns: int = 3
    min_seconds: float = 30.0
    drift_window: int = 5
    drift_threshold: float = 0.60


@dataclass
class IdentificationConfig:
    """Closed-set identification against a local speaker registry."""

    enabled: bool = False
    registry_path: str = "~/.sdts/registry.db"
    mode: str = "review"                # off | review | auto
    unknown_label: str | None = None    # None -> keep SPEAKER_0i
    accept_threshold: float = 0.70      # uncalibrated placeholder
    reject_threshold: float = 0.55      # uncalibrated placeholder
    margin: float = 0.10                # only when >=2 registered candidates
    max_centroids: int = 6
    adaptation: AdaptationConfig = field(default_factory=AdaptationConfig)


@dataclass
class PipelineConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    align: AlignConfig = field(default_factory=AlignConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    identification: IdentificationConfig = field(default_factory=IdentificationConfig)
    hf_token: str | None = None
    work_dir: str | None = None

    # -- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineConfig":
        sections = {
            "audio": AudioConfig,
            "asr": AsrConfig,
            "diarization": DiarizationConfig,
            "align": AlignConfig,
            "merge": MergeConfig,
            "output": OutputConfig,
            "identification": IdentificationConfig,
        }
        kwargs: dict[str, Any] = {}
        for name, klass in sections.items():
            raw = data.get(name) or {}
            valid = {f.name for f in dataclasses.fields(klass)}
            unknown = set(raw) - valid
            if unknown:
                raise ValueError(f"unknown keys in config.{name}: {sorted(unknown)}")
            if name == "output" and "formats" in raw:
                raw = {**raw, "formats": tuple(raw["formats"])}
            if name == "identification" and "adaptation" in raw:
                raw = {**raw, "adaptation": AdaptationConfig(**(raw["adaptation"] or {}))}
            kwargs[name] = klass(**raw)
        for scalar in ("hf_token", "work_dir"):
            if scalar in data:
                kwargs[scalar] = data[scalar]
        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        import yaml  # imported lazily so the core does not hard-depend on it
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})

    def resolve_hf_token(self) -> str:
        """Token lookup order: config -> HF_TOKEN -> HUGGING_FACE_HUB_TOKEN ->
        the credential saved by `huggingface-cli login` / `hf auth login`.

        The last step matters: after logging in, the token lives in the HF
        token store and pyannote itself would find it, but looking only at
        environment variables raises a confusing "gated model" error on a
        machine that is in fact authenticated.
        """
        import os
        token = (self.hf_token
                 or os.environ.get("HF_TOKEN")
                 or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
        if not token:
            token = _stored_hf_token()
        if not token:
            raise RuntimeError(
                "pyannote models are gated. Either run `hf auth login` "
                "(`huggingface-cli login` on older tooling), or set `hf_token` in "
                "the config or export HF_TOKEN - after accepting the user "
                "agreement at "
                "https://hf.co/pyannote/speaker-diarization-community-1"
            )
        return token


def _stored_hf_token() -> str | None:
    """Read the token that `hf auth login` saved, without needing `huggingface_hub`."""
    import os
    from pathlib import Path

    # huggingface_hub >= 1.0 writes to HF_HOME/stored_tokens; older versions
    # used a single `token` file.
    home = os.environ.get("HF_HOME")
    bases = [Path(home)] if home else []
    bases.append(Path.home() / ".cache" / "huggingface")
    bases.append(Path.home() / ".huggingface")

    for base in bases:
        for name in ("stored_tokens", "token"):
            f = base / name
            if not f.is_file():
                continue
            try:
                text = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not text:
                continue
            # stored_tokens is an ini-ish file: `hf.co = hf_xxx`
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "[")):
                    continue
                value = line.split("=", 1)[-1].strip().strip('"')
                if value:
                    return value
    return None
