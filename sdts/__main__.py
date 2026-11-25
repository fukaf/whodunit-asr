"""CLI: python -m sdts <audio> [options]

Examples
--------
  # Japanese meeting recording, 4 speakers known in advance
  python -m sdts meeting.mp3 --language ja --num-speakers 4

  # English interview, offline models, JSON + SRT
  python -m sdts interview.wav --language en --formats json,srt --device cpu

  # only diarize (skip ASR) to check speaker count before a long job
  python -m sdts call.mp3 --diarize-only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import PipelineConfig
from .pipeline import Pipeline, file_stem


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdts",
                                description="Speaker-diarized speech transcription")
    p.add_argument("audio", nargs="?", help="input audio/video file")
    p.add_argument("--config", help="YAML config file (CLI flags override it)")

    g = p.add_argument_group("models")
    g.add_argument("--asr-engine", choices=["faster-whisper", "sensevoice"])
    g.add_argument("--model", help="ASR model name (default large-v3-turbo)")
    g.add_argument("--diarization", dest="diarization_pipeline",
                   help="pyannote pipeline id (default speaker-diarization-community-1)")
    g.add_argument("--no-align", action="store_true", help="skip wav2vec2 forced alignment")
    g.add_argument("--align-model", help="override the CTC alignment model")

    g = p.add_argument_group("runtime")
    g.add_argument("--device", help="cuda | cpu | mps")
    g.add_argument("--compute-type", help="float16 | int8_float16 | int8 | float32")
    g.add_argument("--language", help="ja | en | ... (default: auto-detect per chunk)")
    g.add_argument("--initial-prompt", help="decoding hint (proper nouns, style)")
    g.add_argument("--chunk-seconds", type=float)
    g.add_argument("--chunk-overlap", type=float)

    g = p.add_argument_group("speakers")
    g.add_argument("--num-speakers", type=int)
    g.add_argument("--min-speakers", type=int)
    g.add_argument("--max-speakers", type=int)
    g.add_argument("--granularity", choices=["word", "segment"])

    g = p.add_argument_group("output")
    g.add_argument("--out-dir")
    g.add_argument("--formats", help="comma separated: json,txt,srt,md")
    g.add_argument("--diarize-only", action="store_true",
                   help="print the speaker timeline as JSON and exit")
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def apply_overrides(cfg: PipelineConfig, args: argparse.Namespace) -> PipelineConfig:
    if args.asr_engine:
        cfg.asr.engine = args.asr_engine
    if args.model:
        cfg.asr.model = args.model
    if args.diarization_pipeline:
        cfg.diarization.pipeline = args.diarization_pipeline
    if args.device:
        cfg.asr.device = args.device
        cfg.diarization.device = args.device
    if args.compute_type:
        cfg.asr.compute_type = args.compute_type
    if args.language:
        cfg.asr.language = args.language
    if args.initial_prompt is not None:
        cfg.asr.initial_prompt = args.initial_prompt
    if args.chunk_seconds:
        cfg.audio.chunk_seconds = args.chunk_seconds
    if args.chunk_overlap:
        cfg.audio.chunk_overlap_seconds = args.chunk_overlap
    if args.num_speakers:
        cfg.diarization.num_speakers = args.num_speakers
    if args.min_speakers:
        cfg.diarization.min_speakers = args.min_speakers
    if args.max_speakers:
        cfg.diarization.max_speakers = args.max_speakers
    if args.granularity:
        cfg.merge.granularity = args.granularity
    if args.no_align:
        cfg.align.enabled = False
    if args.align_model:
        cfg.align.model = args.align_model
    if args.out_dir:
        cfg.output.out_dir = args.out_dir
    if args.formats:
        cfg.output.formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.audio:
        build_parser().print_help()
        return 2

    cfg = PipelineConfig.load(args.config) if args.config else PipelineConfig()
    cfg = apply_overrides(cfg, args)
    if cfg.asr.device == "cpu" and cfg.asr.compute_type in ("float16",):
        cfg.asr.compute_type = "int8"

    pipeline = Pipeline(cfg)

    if args.diarize_only:
        from . import audio as audio_mod
        from .diarize import Diarizer
        arr = audio_mod.decode(args.audio, cfg.audio)
        turns = Diarizer(cfg.diarization, cfg.resolve_hf_token()).diarize_array(arr)
        out = [{"start": round(t.start, 3), "end": round(t.end, 3), "speaker": t.speaker}
               for t in turns]
        print(json.dumps({"num_speakers": len({t.speaker for t in turns}),
                          "turns": out}, ensure_ascii=False, indent=2))
        return 0

    result = pipeline.run(args.audio, num_speakers=cfg.diarization.num_speakers)

    from .render import write_all
    paths = write_all(result, cfg.output, file_stem(args.audio))

    print(f"\n{len(result.utterances)} utterances, "
          f"{len(result.speakers)} speakers, language={result.language}, "
          f"rtf={result.meta.get('rtf')}")
    for p in paths:
        print(f"  -> {p}")

    preview = result.utterances[:5]
    if preview:
        from .render import fmt_time
        print()
        for u in preview:
            print(f"  [{fmt_time(u.start, cfg.output.timestamp_format)}] "
                  f"{u.label}: {u.text[:70]}")
    return 0


if __name__ == "__main__":
    # Allow both `python -m sdts` and `python sdts/__main__.py`.
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from sdts.__main__ import main as _main
        sys.exit(_main())
    sys.exit(main())
