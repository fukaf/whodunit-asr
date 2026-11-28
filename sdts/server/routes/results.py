"""`/api/jobs/{id}/...`: read a finished transcript, rename clusters, export."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ...config import OutputConfig
from ...labels import result_from_dict
from ...render import render_markdown, render_srt, render_txt, write_all
from ..deps import get_job, job_result, write_job_result

router = APIRouter()


def _speakers_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per raw cluster, with the display name currently in effect."""
    order: list[str] = []
    for utt in data.get("utterances", []):
        if utt["speaker"] not in order:
            order.append(utt["speaker"])
    ident = (data.get("meta") or {}).get("identification") or {}
    matches = {m.get("speaker"): m for m in ident.get("matches", [])}
    out = []
    for speaker in sorted(order):
        utts = [u for u in data["utterances"] if u["speaker"] == speaker]
        seconds = sum(float(u["end"]) - float(u["start"]) for u in utts)
        display = next((u.get("speaker_display") for u in utts
                        if u.get("speaker_display") not in (None, speaker)), None)
        match = matches.get(speaker) or {}
        out.append({
            "id": speaker,
            "display_name": display,
            "turns": len(utts),
            "seconds": round(seconds, 1),
            "decision": match.get("decision"),
            "score": match.get("score"),
            "candidates": match.get("candidates", []),
        })
    return out


def _rewrite_text_formats(job, data: dict[str, Any]) -> list[str]:
    """Re-render txt/srt/md from the edited JSON, non-JSON outputs only.

    `write_all` also rewrites the JSON, and it does so through
    `Utterance.to_dict()`, which substitutes the raw label when
    `speaker_display` is None - turning "not named yet" into "named
    SPEAKER_01". The JSON on disk is the source of truth here, so only the
    derived text formats are regenerated.
    """
    if not job.out_dir or not job.stem:
        return []
    cfg = OutputConfig(out_dir=job.out_dir, formats=("txt", "srt", "md"))
    written = write_all(result_from_dict(data), cfg, job.stem)
    return [str(p) for p in written]


@router.get("/{job_id}/summary")
def summary(request: Request, job_id: str):
    """Header facts and speaker cards, for **every** job state.

    The job page opens immediately after `POST /api/jobs`, while the job is
    still queued. Returning 409 here used to leave the page stuck at `...`:
    it never learned the job id was valid and never subscribed to SSE. Results
    are filled in once the job is done; until then the payload still carries
    the live job state, which is exactly what the progress bar needs.
    """
    job = get_job(request, job_id)
    ready = (job.status == "done" and bool(job.result_path)
             and Path(job.result_path).is_file())
    data = job_result(job) if ready else {}
    return {
        "job": job.public(),
        "result_ready": ready,
        "language": data.get("language"),
        "duration": data.get("duration"),
        "speakers": _speakers_payload(data) if ready else [],
        "speaker_stats": (data.get("speaker_stats") or {}) if ready else {},
        "meta": (data.get("meta") or {}) if ready else dict(job.meta or {}),
        "utterances": len(data.get("utterances", [])) if ready else 0,
    }


@router.post("/{job_id}/rename")
def rename(request: Request, job_id: str, body: dict[str, Any] = Body(...)):
    """Set the display name of one raw cluster and re-render the text outputs.

    Session-only on purpose: persisting a voice is an explicit decision from
    the registry page, never a side effect of typing a name in the transcript.
    """
    job = get_job(request, job_id)
    speaker = body.get("speaker")
    name = body.get("name", "")
    if not isinstance(speaker, str) or not speaker:
        raise HTTPException(400, "`speaker` is required")
    if not isinstance(name, str):
        raise HTTPException(400, "`name` must be a string")
    data = job_result(job)
    if speaker not in {u["speaker"] for u in data.get("utterances", [])}:
        raise HTTPException(404, f"no such speaker in this job: {speaker}")

    clean = name.strip()
    for utt in data["utterances"]:
        if utt["speaker"] == speaker:
            utt["speaker_display"] = clean or None
    write_job_result(job, data)
    _rewrite_text_formats(job, data)
    return {"speakers": _speakers_payload(data)}


@router.get("/{job_id}/export")
def export(request: Request, job_id: str, fmt: str = "txt"):
    job = get_job(request, job_id)
    data = job_result(job)
    if fmt == "json":
        return PlainTextResponse(json.dumps(data, ensure_ascii=False, indent=2),
                                 media_type="application/json")
    cfg = OutputConfig()
    result = result_from_dict(data)
    if fmt == "txt":
        return PlainTextResponse(render_txt(result, cfg),
                                 media_type="text/plain; charset=utf-8")
    if fmt == "srt":
        return PlainTextResponse(render_srt(result), media_type="application/x-subrip")
    if fmt == "md":
        return PlainTextResponse(render_markdown(result, cfg),
                                 media_type="text/markdown; charset=utf-8")
    raise HTTPException(400, "fmt must be one of json, txt, srt, md")


@router.delete("/{job_id}")
def delete_job(request: Request, job_id: str, purge_files: bool = False):
    """Forget a job. `purge_files=true` also deletes its uploaded audio.

    Default is "remove from the list only": deleting someone's recording
    because they tidied the job list would be a nasty surprise on a tool whose
    whole selling point is that the audio never leaves the machine.
    """
    import shutil

    from ..deps import manager

    mgr = manager(request)
    job = get_job(request, job_id)
    if job.status == "running":
        raise HTTPException(409, "cancel the job before deleting it")
    removed: list[str] = []
    jobs_dir = mgr.data_dir / "jobs"
    (jobs_dir / f"{job.id}.json").unlink(missing_ok=True)
    if purge_files:
        upload_dir = mgr.data_dir / "uploads" / job.id
        if upload_dir.is_dir():
            shutil.rmtree(upload_dir, ignore_errors=True)
            removed.append(str(upload_dir))
    with mgr._lock:                      # noqa: SLF001 - same package, deliberate
        mgr._jobs.pop(job.id, None)      # noqa: SLF001
        bus = mgr._buses.pop(job.id, None)  # noqa: SLF001
        mgr._cancels.pop(job.id, None)   # noqa: SLF001
    if bus is not None:
        bus.close()
    return {"deleted": job.id, "purged": removed}
