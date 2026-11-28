"""`/api/jobs`: create, watch (SSE), cancel, and read a transcription."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..deps import get_job, job_result, manager, settings
from ..events import format_sse

router = APIRouter()

KEEPALIVE_SECONDS = 15.0


def _parse_config(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, f"config is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "config must be a JSON object")
    return data


@router.get("")
def list_jobs(request: Request, limit: int = Query(50, ge=1, le=200)):
    return {"jobs": manager(request).list_jobs(limit)}


@router.post("", status_code=201)
async def create_job(request: Request):
    """Create a job from either multipart (`file`) or JSON (`source`).

    Two inputs on purpose: a browser can only send a file it has been handed,
    while a long recording already on the server should not be re-uploaded
    just to pick it up.
    """
    st = settings(request)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(400, "multipart body needs a `file` part")
        config = _parse_config(form.get("config") if isinstance(form.get("config"), str) else None)
        source_name = getattr(upload, "filename", None)
        stream = upload.file
        try:
            job = manager(request).add_upload(
                source_name or "upload", stream, config=config,
                config_path=st.base_config_path)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from None
        return {"job": job.public()}

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    config = body.get("config") or {}
    if not isinstance(config, dict):
        raise HTTPException(400, "`config` must be an object")
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(400, "`source` must be a path to a file on this machine")
    try:
        job = manager(request).add_local_file(
            source, config=config,
            config_path=body.get("config_path") or st.base_config_path)
    except FileNotFoundError:
        raise HTTPException(404, f"no such file: {source}") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"job": job.public()}


@router.get("/{job_id}")
def show_job(request: Request, job_id: str):
    return {"job": get_job(request, job_id).public()}


@router.get("/{job_id}/events")
def job_events(request: Request, job_id: str):
    get_job(request, job_id)
    bus = manager(request).bus(job_id)
    sub = bus.subscribe()

    def stream():
        last = time.monotonic()
        while True:
            batch = sub.drain(timeout=1.0)
            if not batch:
                if bus.closed:
                    return
                if time.monotonic() - last >= KEEPALIVE_SECONDS:
                    last = time.monotonic()
                    yield ": keepalive\n\n"
                continue
            for item in batch:
                last = time.monotonic()
                yield format_sse(item)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@router.post("/{job_id}/cancel")
def cancel_job(request: Request, job_id: str):
    return {"job": manager(request).cancel(job_id).public()}


@router.get("/{job_id}/result")
def get_result(request: Request, job_id: str):
    return job_result(get_job(request, job_id))


@router.get("/{job_id}/audio")
def job_audio(request: Request, job_id: str):
    """Serve the source audio so the page can play a segment in place.

    Range requests matter here: without them the browser downloads the whole
    file before the playhead moves, which on a two-hour meeting is a minute of
    silence after every click. Starlette's FileResponse handles ranges.
    """
    from fastapi.responses import FileResponse

    job = get_job(request, job_id)
    path = Path(job.source)
    if not path.is_file():
        raise HTTPException(410, "source audio is no longer on disk")
    return FileResponse(path, filename=path.name, content_disposition_type="inline")


@router.get("/{job_id}/clip")
def job_clip(request: Request, job_id: str, start: float = Query(..., ge=0),
             seconds: float = Query(10.0, gt=0, le=120)):
    """Decode `[start, start+seconds)` to a WAV and return it.

    Used by the "listen to this turn" button. Returning WAV keeps the browser
    side dumb - ffmpeg already did the resampling the pipeline needs - and it
    avoids writing a temp file per click.
    """
    from fastapi.responses import Response

    from ... import audio as audio_mod
    from ...config import AudioConfig

    job = get_job(request, job_id)
    src = Path(job.source)
    if not src.is_file():
        raise HTTPException(410, "source audio is no longer on disk")
    cfg = AudioConfig()
    try:
        arr = audio_mod.decode(src, cfg, start, start + seconds)
    except audio_mod.AudioError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response(content=_wav_bytes(arr, cfg.sample_rate), media_type="audio/wav")


def _wav_bytes(arr, sample_rate: int) -> bytes:
    import io
    import wave

    import numpy as np

    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(pcm.tobytes())
    return buf.getvalue()
