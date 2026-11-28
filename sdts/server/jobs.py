"""Job queue: one audio file, one worker thread, one append-only event log.

Why a thread and not a process pool: the models are loaded lazily and pinned to
one GPU. A second concurrent transcription would not go faster, it would
compete for the same 24 GB of VRAM, so the queue is deliberately `max_workers
= 1` and further submissions wait in `queued`.

State is mirrored to `<data_dir>/jobs/<id>.json` so a job survives a page
reload (and a server restart, in the sense that its result is still readable);
the worker thread itself is not resumed.
"""
from __future__ import annotations

import json
import logging
import platform
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import PipelineConfig
from ..pipeline import Pipeline, file_stem
from ..render import write_all
from .cancel import JobCancelled, raise_if_cancelled
from .events import EventBus

log = logging.getLogger(__name__)

PipelineFactory = Callable[[PipelineConfig], Pipeline]

# Audio/video containers the browser's file picker offers. The server does not
# trust this list for validation - ffprobe is the authority - it only drives
# the `accept` attribute.
ALLOWED_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma",
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts",
}

QUEUED, RUNNING, DONE, FAILED, CANCELLED = (
    "queued", "running", "done", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    source: str                       # absolute path of the audio on this machine
    source_name: str
    config: dict[str, Any]            # the `overrides` block, already validated
    config_path: str | None = None
    status: str = QUEUED
    stage: str = "queued"
    stage_done: int = 0
    stage_total: int = 1
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result_path: str | None = None
    out_dir: str | None = None
    stem: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------
    @property
    def progress(self) -> float:
        """0..1 over the whole job. Chunking dominates; the rest is a tail."""
        if self.status == DONE:
            return 1.0
        if self.stage == "chunk" and self.stage_total:
            # Chunks are ~90 % of wall time; leave the last 10 % for the
            # reconcile/fuse/identify tail so the bar keeps moving at the end.
            return round(0.9 * (self.stage_done / self.stage_total), 4)
        if self.stage in ("reconcile", "fuse", "identify"):
            return 0.9
        return 0.0

    def public(self) -> dict[str, Any]:
        """What the browser is allowed to see. No paths except the out dir."""
        return {
            "id": self.id,
            "source_name": self.source_name,
            "status": self.status,
            "stage": self.stage,
            "stage_done": self.stage_done,
            "stage_total": self.stage_total,
            "progress": self.progress,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "stem": self.stem,
            "out_dir": self.out_dir,
            "meta": self.meta,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "source_name": self.source_name,
            "config": self.config, "config_path": self.config_path,
            "status": self.status, "stage": self.stage,
            "stage_done": self.stage_done, "stage_total": self.stage_total,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
            "result_path": self.result_path, "out_dir": self.out_dir,
            "stem": self.stem, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class JobManager:
    """Owns the queue, the workers and the on-disk job records."""

    def __init__(self, data_dir: str | Path, *,
                 pipeline_factory: PipelineFactory | None = None,
                 base_config_path: str | Path | None = None,
                 registry_path_hint: str | None = None,
                 max_workers: int = 1):
        self.data_dir = Path(data_dir).expanduser().resolve()
        (self.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "out").mkdir(parents=True, exist_ok=True)
        self._factory = pipeline_factory or (lambda cfg: Pipeline(cfg))
        self.base_config_path = str(base_config_path) if base_config_path else None
        self.registry_path_hint = registry_path_hint
        self._jobs: dict[str, Job] = {}
        self._buses: dict[str, EventBus] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="sdts-job")
        self._load_existing()

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        with self._lock:
            for flag in self._cancels.values():
                flag.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _load_existing(self) -> None:
        for path in sorted((self.data_dir / "jobs").glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job.from_dict(data)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("skipping unreadable job record %s (%s)", path.name, exc)
                continue
            if job.status in (QUEUED, RUNNING):
                # The thread died with the previous process. Do not pretend it
                # is still working: a job that never finished is a failure the
                # user must be able to retry.
                job.status = FAILED
                job.error = "server stopped while this job was running"
                job.stage = "failed"
                self._write(job)
            self._jobs[job.id] = job
            self._buses[job.id] = EventBus()
            self._buses[job.id].close()

    # -- reads -------------------------------------------------------------
    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.public() for j in jobs[:limit]]

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def bus(self, job_id: str) -> EventBus:
        self.get(job_id)
        with self._lock:
            return self._buses[job_id]

    # -- writes ------------------------------------------------------------
    def _write(self, job: Job) -> None:
        path = self.data_dir / "jobs" / f"{job.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    def _publish(self, job: Job, event: str, data: dict[str, Any] | None = None) -> None:
        payload = {"job_id": job.id, **(data or {})}
        with self._lock:
            bus = self._buses.get(job.id)
        if bus is not None:
            bus.publish(event, payload)

    def _set_stage(self, job: Job, stage: str, done: int, total: int) -> None:
        with self._lock:
            job.stage, job.stage_done, job.stage_total = stage, done, total
        self._publish(job, "progress", {"stage": stage, "done": done, "total": total,
                                        "progress": job.progress})
        self._write(job)

    # -- creating work -----------------------------------------------------
    def add_local_file(self, path: str | Path, *, config: dict[str, Any],
                       config_path: str | None = None,
                       source_name: str | None = None) -> Job:
        src = Path(path).expanduser()
        if not src.is_absolute():
            raise ValueError("source path must be absolute")
        src = src.resolve()
        if not src.is_file():
            raise FileNotFoundError(str(src))
        if src.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"unsupported container: {src.suffix or '(none)'}")
        return self._enqueue(src, src.name if source_name is None else source_name,
                             config, config_path)

    def add_upload(self, filename: str, stream, *, config: dict[str, Any],
                   config_path: str | None = None) -> Job:
        """Stream an uploaded file into `<data_dir>/uploads/<id>/`."""
        suffix = Path(filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise ValueError(f"unsupported container: {suffix or '(none)'}")
        job_id = secrets.token_hex(6)
        folder = self.data_dir / "uploads" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        # Keep the original name for display, but never trust it as a path:
        # `Path(name).name` strips any directory component a client sent.
        safe = Path(filename).name or f"upload{suffix}"
        dest = folder / safe
        with dest.open("wb") as fh:
            shutil.copyfileobj(stream, fh, length=1024 * 1024)
        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ValueError("uploaded file is empty")
        return self._enqueue(dest, safe, config, config_path, job_id=job_id)

    def _enqueue(self, src: Path, display_name: str, config: dict[str, Any],
                 config_path: str | None, job_id: str | None = None) -> Job:
        job_id = job_id or secrets.token_hex(6)
        job = Job(id=job_id, source=str(src), source_name=display_name,
                  config=dict(config), config_path=config_path)
        with self._lock:
            self._jobs[job.id] = job
            self._buses[job.id] = EventBus()
            self._cancels[job.id] = threading.Event()
        self._write(job)
        self._publish(job, "created", {"job": job.public()})
        self._pool.submit(self._run, job.id)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._lock:
            flag = self._cancels.get(job_id)
        if flag is None or job.status in (DONE, FAILED, CANCELLED):
            return job
        flag.set()
        if job.status == QUEUED:
            # Not started yet: no worker will ever pick it up.
            with self._lock:
                job.status = CANCELLED
                job.stage = CANCELLED
                job.finished_at = _now()
            self._publish(job, "cancelled", {})
            self._publish(job, "end", {"status": job.status})
            self._buses[job.id].close()
            self._write(job)
        else:
            self._publish(job, "cancelling", {
                "note": "the running chunk has to finish first"})
        return job

    # -- the worker --------------------------------------------------------
    def _build_config(self, job: Job) -> PipelineConfig:
        path = job.config_path or self.base_config_path
        cfg = PipelineConfig.load(path) if path and Path(path).is_file() else PipelineConfig()
        return apply_overrides(cfg, job.config)

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._lock:
            flag = self._cancels.get(job_id)
        if flag is not None and flag.is_set():
            job.status, job.stage, job.finished_at = CANCELLED, CANCELLED, _now()
            self._write(job)
            return
        job.status, job.stage, job.started_at = RUNNING, "starting", _now()
        self._write(job)
        self._publish(job, "started", {"job": job.public()})

        def on_stage(stage: str, done: int, total: int) -> None:
            raise_if_cancelled(flag)          # between chunks: cheap and safe
            self._set_stage(job, stage, done, total)

        try:
            cfg = self._build_config(job)
            out_dir = self.data_dir / "out"
            cfg.output.out_dir = str(out_dir)
            pipeline = self._factory(cfg)
            result = pipeline.run(job.source, on_stage=on_stage)
            stem = file_stem(job.source)
            write_all(result, cfg.output, stem)
            result_json = out_dir / f"{stem}.json"
            # `Utterance.to_dict()` falls back to the raw label when
            # `speaker_display` is unset, so a freshly written JSON makes every
            # unnamed speaker look as if someone had named it "SPEAKER_00".
            # `labels.apply_labels` patches this too; the workbench has to do
            # the same, or the rename form starts pre-filled with the raw id.
            if result_json.is_file():
                dumped = json.loads(result_json.read_text(encoding="utf-8"))
                for raw, utt in zip(dumped.get("utterances", []), result.utterances):
                    raw["speaker_display"] = utt.speaker_display
                result_json.write_text(json.dumps(dumped, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            job.result_path = str(result_json)
            job.out_dir = str(out_dir)
            job.stem = stem
            job.meta = {
                "duration": round(result.duration, 2),
                "language": result.language,
                "speakers": result.speakers,
                "utterances": len(result.utterances),
                "speaker_stats": result.speaker_stats(),
                "rtf": result.meta.get("rtf"),
                "elapsed_seconds": result.meta.get("elapsed_seconds"),
                "identification": result.meta.get("identification"),
                "server": {"python": platform.python_version(),
                           "config": job.config_path or self.base_config_path},
            }
            with self._lock:
                job.status, job.stage, job.stage_done = DONE, "done", job.stage_total or 1
                job.finished_at = _now()
            self._publish(job, "result", {"job": job.public()})
        except JobCancelled:
            with self._lock:
                job.status, job.stage, job.finished_at = CANCELLED, CANCELLED, _now()
            self._publish(job, "cancelled", {})
        except Exception as exc:  # noqa: BLE001 - any failure becomes job state
            log.exception("job %s failed", job.id)
            with self._lock:
                job.status, job.stage = FAILED, "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _now()
            self._publish(job, "failed", {"error": job.error})
        finally:
            self._write(job)
            with self._lock:
                bus = self._buses.get(job.id)
            if bus is not None:
                bus.publish("end", {"job_id": job.id, "status": job.status})
                bus.close()


def apply_overrides(cfg: PipelineConfig, overrides: dict[str, Any]) -> PipelineConfig:
    """Map the browser's flat JSON onto config fields, through a whitelist.

    The UI is not allowed to set arbitrary attributes: `hf_token` would leak
    into the job record, `registry_path` would let a request point at any file
    on disk. Only the knobs the forms actually expose are reachable here.
    """
    audio = overrides.get("audio") or {}
    asr = overrides.get("asr") or {}
    diar = overrides.get("diarization") or {}
    merge = overrides.get("merge") or {}
    ident = overrides.get("identification") or {}

    if "chunk_seconds" in audio:
        cfg.audio.chunk_seconds = float(audio["chunk_seconds"])
    if "chunk_overlap_seconds" in audio:
        cfg.audio.chunk_overlap_seconds = float(audio["chunk_overlap_seconds"])
    if cfg.audio.chunk_overlap_seconds >= cfg.audio.chunk_seconds:
        raise ValueError("chunk_overlap_seconds must be smaller than chunk_seconds")

    for key in ("engine", "model", "device", "compute_type", "language",
                "initial_prompt", "beam_size"):
        if key in asr and asr[key] is not None:
            setattr(cfg.asr, key, asr[key])
    if cfg.asr.language == "":
        cfg.asr.language = None

    for key in ("device", "pipeline", "num_speakers", "min_speakers",
                "max_speakers", "mode", "min_duration_off"):
        if key in diar and diar[key] is not None:
            setattr(cfg.diarization, key, diar[key])
    if cfg.diarization.mode not in ("exclusive", "regular"):
        raise ValueError("diarization.mode must be 'exclusive' or 'regular'")

    for key in ("granularity", "merge_gap", "snap_tolerance", "min_part_share"):
        if key in merge and merge[key] is not None:
            setattr(cfg.merge, key, merge[key])
    if cfg.merge.granularity not in ("segment", "word"):
        raise ValueError("merge.granularity must be 'segment' or 'word'")

    if "enabled" in ident:
        cfg.identification.enabled = bool(ident["enabled"])
    for key in ("mode", "accept_threshold", "reject_threshold", "margin"):
        if key in ident and ident[key] is not None:
            setattr(cfg.identification, key, ident[key])
    if cfg.identification.mode not in ("off", "review", "auto"):
        raise ValueError("identification.mode must be 'off', 'review' or 'auto'")

    if cfg.asr.device == "cpu" and cfg.asr.compute_type in ("float16",):
        cfg.asr.compute_type = "int8"
    return cfg
