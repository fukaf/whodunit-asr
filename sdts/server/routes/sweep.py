"""`/api/jobs/{id}/replay`: threshold sweeps without re-running ASR."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..deps import get_job, manager
from ..replay import list_replays, run_replay

router = APIRouter()


@router.post("/{job_id}/replay")
def replay(request: Request, job_id: str, body: dict[str, Any] = Body(...)):
    job = get_job(request, job_id)
    raw_runs = body.get("runs")
    if raw_runs is None:
        raw_runs = [{"name": body.get("name") or "default",
                     "config": body.get("config") or {}}]
    if not isinstance(raw_runs, list) or not raw_runs:
        raise HTTPException(400, "`runs` must be a non-empty list")
    try:
        return run_replay(manager(request), job, raw_runs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(409, "this job has no saved artifacts (created before 0.3.0?)") from None


@router.get("/{job_id}/replays")
def replays(request: Request, job_id: str):
    get_job(request, job_id)
    return {"runs": list_replays(manager(request), job_id)}


@router.get("/{job_id}/replays/{run_id}/result")
def replay_result(request: Request, job_id: str, run_id: str):
    get_job(request, job_id)
    record = next((r for r in list_replays(manager(request), job_id)
                   if r["id"] == run_id), None)
    if record is None:
        raise HTTPException(404, f"unknown replay run: {run_id}")
    path = Path(record["result_path"])
    if not path.is_file():
        raise HTTPException(410, "replay result is gone")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/{job_id}/replays/{run_id}/export")
def replay_export(request: Request, job_id: str, run_id: str, fmt: str = "txt"):
    get_job(request, job_id)
    record = next((r for r in list_replays(manager(request), job_id)
                   if r["id"] == run_id), None)
    if record is None:
        raise HTTPException(404, f"unknown replay run: {run_id}")
    file = Path(record["out_dir"]) / f"{run_id}.{fmt}"
    if fmt not in {"json", "txt", "srt", "md"} or not file.is_file():
        raise HTTPException(404, f"no {fmt} export for this run")
    media = {"json": "application/json", "txt": "text/plain; charset=utf-8",
             "srt": "application/x-subrip", "md": "text/markdown; charset=utf-8"}[fmt]
    return PlainTextResponse(file.read_text(encoding="utf-8"), media_type=media)
