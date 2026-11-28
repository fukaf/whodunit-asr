"""Cheap re-running of the tail of a finished job.

ASR and diarization dominate the cost of a long recording. Fusion, display-name
assignment and export do not. Once a job has artifacts, a threshold sweep only
needs the latter, so a 60-minute job can be re-evaluated in seconds without
touching its original result or the voice registry.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from ..artifacts import load_artifacts, normalise_result_json
from ..config import PipelineConfig
from ..fuse import fuse
from ..identify import identify_speakers
from ..render import write_all
from ..types import TranscriptResult
from .jobs import Job, JobManager, apply_overrides

MAX_RUNS = 24


class _NoEmbedder:
    """Replay uses vectors saved by the original run; no model, no audio."""

    def embed_audio(self, audio, sample_rate=16000):
        return None

    def embed_turns(self, turns, chunk_audio, chunks, stride, **kwargs):
        return None


def _dummy_config(mgr: JobManager, job: Job) -> PipelineConfig:
    """Use the exact config the job ran with, including the gallery path."""
    return mgr._build_config(job)  # noqa: SLF001 - same package, deliberate


def list_replays(mgr: JobManager, job_id: str) -> list[dict[str, Any]]:
    root = mgr.data_dir / "replays" / job_id
    if not root.is_dir():
        return []
    runs = []
    for path in sorted(root.glob("*.json")):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(runs, key=lambda r: r.get("created_at", ""))


def run_replay(mgr: JobManager, job: Job, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Execute one or more config variants against a job's saved artifacts."""
    if job.status != "done":
        raise ValueError("only a finished job can be replayed")
    if not runs:
        raise ValueError("no replay runs were supplied")
    if len(runs) > MAX_RUNS:
        raise ValueError(f"at most {MAX_RUNS} runs per sweep")

    artifacts = load_artifacts(mgr.data_dir / "artifacts" / job.id)
    base_cfg = _dummy_config(mgr, job)
    # Replay always applies the job's own overrides first, then this run's.
    cfg_defaults = apply_overrides(base_cfg, job.config or {})
    segments, turns, vectors = artifacts["segments"], artifacts["turns"], artifacts["vectors"]
    manifest = artifacts["manifest"]
    out_root = mgr.data_dir / "replays" / job.id
    out_root.mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, Any]] = []
    for index, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            raise ValueError("each replay run must be an object")
        name = str(run.get("name") or f"run-{index}").strip()[:80] or f"run-{index}"
        overrides = run.get("config") or {}
        if not isinstance(overrides, dict):
            raise ValueError(f"run {name}: config must be an object")
        cfg = apply_overrides(PipelineConfig.from_dict(cfg_defaults.to_dict()), overrides)
        # Sweeps must never write to the registry, even if the source config
        # enabled adaptation (accepted matches, drift votes, pending centroids).
        cfg.identification.adaptation.enabled = False
        run_id = secrets.token_hex(4)
        run_dir = out_root / run_id
        cfg.output.out_dir = str(run_dir)
        cfg.output.formats = ("json", "txt", "srt", "md")

        utterances = [u for u in fuse(segments, turns, cfg.merge) if u.text.strip()]
        speakers = sorted({u.speaker for u in utterances})
        result = TranscriptResult(
            utterances=utterances, language=manifest.get("language"),
            speakers=speakers, duration=float(manifest.get("duration") or 0.0),
            meta={
                "source": manifest.get("source"),
                "replay_of": job.id,
                "replay_run": name,
                "chunks": manifest.get("chunks"),
                "asr_engine": manifest.get("asr_engine"),
                "asr_model": manifest.get("asr_model"),
                "diarization": manifest.get("diarization"),
            },
        )
        if cfg.identification.enabled:
            result.meta["identification"] = identify_speakers(
                result, turns=turns, embedder=_NoEmbedder(), cfg=cfg.identification,
                chunk_audio=[], chunks=[], stride=0.0,
                speaker_vectors=vectors, readonly=True)

        written = write_all(result, cfg.output, run_id)
        json_path = next((p for p in written if p.suffix == ".json"), None)
        if json_path is not None:
            normalise_result_json(json_path, result)

        ident = result.meta.get("identification") or {}
        summary = {
            "id": run_id,
            "name": name,
            "created_at": _utcnow(),
            "job_id": job.id,
            "config": overrides,
            "result_path": str(json_path) if json_path else None,
            "out_dir": str(run_dir),
            "utterances": len(utterances),
            "speakers": speakers,
            "identification_mode": ident.get("mode"),
            "decisions": [
                {"speaker": m.get("speaker"), "decision": m.get("decision"),
                 "best": m.get("best"), "score": m.get("score")}
                for m in ident.get("matches", [])
            ],
        }
        (out_root / f"{run_id}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries.append(summary)

    return {"runs": summaries, "job_id": job.id}


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
