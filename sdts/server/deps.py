"""Small shared helpers for the route modules.

Every route needs the same three things - the current JobManager, its settings,
and a job looked up by id - so they live here once instead of being copied into
each router.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from ..labels import load_result
from .jobs import Job, JobManager


def manager(request: Request) -> JobManager:
    return request.app.state.jobs


def settings(request: Request) -> Any:
    return request.app.state.settings


def get_job(request: Request, job_id: str) -> Job:
    try:
        return manager(request).get(job_id)
    except KeyError:
        raise HTTPException(404, f"unknown job: {job_id}") from None


def job_result(job: Job) -> dict[str, Any]:
    if job.status != "done" or not job.result_path:
        raise HTTPException(409, f"job is {job.status}, no result yet")
    path = Path(job.result_path)
    if not path.is_file():
        raise HTTPException(410, f"result file is gone: {path.name}")
    return load_result(path)


def write_job_result(job: Job, data: dict[str, Any]) -> None:
    if not job.result_path:
        raise HTTPException(409, "job has no result file")
    Path(job.result_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
