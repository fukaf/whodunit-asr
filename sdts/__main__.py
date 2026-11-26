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


def build_subcommand_parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="sdts", description="Speaker-diarized speech transcription")
    sub = top.add_subparsers(dest="cmd", required=True)

    labels = sub.add_parser("labels", help="post-hoc speaker labelling")
    lsub = labels.add_subparsers(dest="labels_cmd", required=True)
    p = lsub.add_parser("init", help="generate labels JSON, clips and review.html")
    p.add_argument("result")
    p.add_argument("--clips", type=int, default=5)
    p.add_argument("--clip-seconds", type=float, default=8.0)
    p.add_argument("--registry")
    p = lsub.add_parser("apply", help="apply labels and re-render outputs")
    p.add_argument("result")
    p.add_argument("--labels", required=True)
    p.add_argument("--registry")
    p.add_argument("--config")
    p = lsub.add_parser("set", help="set one session alias")
    p.add_argument("result")
    p.add_argument("--speaker", required=True)
    p.add_argument("--name", required=True)

    reg = sub.add_parser("registry", help="manage the local voice registry")
    rsub = reg.add_subparsers(dest="registry_cmd", required=True)
    p = rsub.add_parser("list")
    p.add_argument("--registry")
    p = rsub.add_parser("show")
    p.add_argument("speaker_id")
    p.add_argument("--registry")
    p = rsub.add_parser("rename")
    p.add_argument("speaker_id")
    p.add_argument("--name", required=True)
    p.add_argument("--registry")
    p = rsub.add_parser("delete")
    p.add_argument("speaker_id")
    p.add_argument("--registry")
    p = rsub.add_parser("adapt-queue")
    p.add_argument("--registry")
    p = rsub.add_parser("adapt-approve")
    p.add_argument("centroid_id")
    p.add_argument("--registry")

    p = sub.add_parser("enroll", help="register a voice from a sample file")
    p.add_argument("--name", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--consent", default="persistent")
    p.add_argument("--registry")
    p.add_argument("--config")

    p = sub.add_parser("calibrate", help="derive thresholds from labelled pairs")
    p.add_argument("--pairs", required=True)
    p.add_argument("--out", default="identification.yaml")
    p.add_argument("--config")
    return top


def _dispatch_subcommand(argv: list[str]) -> int:
    args = build_subcommand_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        datefmt="%H:%M:%S")
    from . import audio as audio_mod
    from .config import PipelineConfig
    from .embedding import SpeakerEmbedder
    from .registry import SpeakerRegistry

    if args.cmd == "labels":
        from .labels import apply_labels, init_labels, set_label
        if args.labels_cmd == "init":
            paths = init_labels(args.result, clip_count=args.clips,
                                clip_seconds=args.clip_seconds, registry=args.registry)
            for key, value in paths.items():
                print(f"{key}: {value}")
            return 0
        if args.labels_cmd == "apply":
            embedder = None
            if args.config:
                cfg = PipelineConfig.load(args.config)
                embedder = SpeakerEmbedder(cfg.diarization.device, cfg.resolve_hf_token())
            out = apply_labels(args.result, args.labels, registry_path=args.registry,
                               embedder=embedder)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        paths = set_label(args.result, args.speaker, args.name)
        for p in paths:
            print(f"-> {p}")
        return 0

    if args.cmd == "registry":
        cfg = PipelineConfig()
        path = args.registry or cfg.identification.registry_path
        reg = SpeakerRegistry(path)
        try:
            if args.registry_cmd == "list":
                rows = [{k: (len(v) if isinstance(v, bytes) else v)
                         for k, v in r.items()} for r in reg.list_speakers(True)]
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif args.registry_cmd == "show":
                row = reg.get_speaker(args.speaker_id)
                if not row:
                    print(f"unknown speaker: {args.speaker_id}")
                    return 1
                print(json.dumps({k: (len(v) if isinstance(v, bytes) else v)
                                  for k, v in row.items()}, ensure_ascii=False, indent=2))
                print(json.dumps([{k: (len(v) if isinstance(v, bytes) else v)
                                   for k, v in c.items()}
                                  for c in reg.list_centroids(args.speaker_id)],
                                 ensure_ascii=False, indent=2))
            elif args.registry_cmd == "rename":
                reg.rename_speaker(args.speaker_id, args.name)
            elif args.registry_cmd == "delete":
                reg.delete_speaker(args.speaker_id)
            elif args.registry_cmd == "adapt-queue":
                print(json.dumps([{k: (len(v) if isinstance(v, bytes) else v)
                                   for k, v in c.items()} for c in reg.list_centroids(status="pending")],
                                 ensure_ascii=False, indent=2))
            else:
                reg.approve_centroid(args.centroid_id)
        finally:
            reg.close()
        return 0

    if args.cmd == "enroll":
        cfg = PipelineConfig.load(args.config) if args.config else PipelineConfig()
        arr = audio_mod.decode(args.audio, cfg.audio)
        vec = SpeakerEmbedder(cfg.diarization.device, cfg.resolve_hf_token()).embed_audio(
            arr, cfg.audio.sample_rate)
        if vec is None:
            print("embedding failed; nothing registered")
            return 1
        reg = SpeakerRegistry(args.registry or cfg.identification.registry_path)
        try:
            sid = reg.add_speaker(args.name, vec, consent_scope=args.consent,
                                  source_file=args.audio)
        finally:
            reg.close()
        print(sid)
        return 0

    from .calibrate import calibrate
    return calibrate(args.pairs, args.out,
                      config=PipelineConfig.load(args.config) if args.config else None)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in {"labels", "registry", "enroll", "calibrate"}:
        return _dispatch_subcommand(argv)
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
