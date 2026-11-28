"""`/api/registry`: the local voice gallery, through the browser.

Mirrors `sdts registry ...` one for one. Stored vectors never leave this
process: the API returns names, counts and timestamps only. Enrolment is the
single write that adds a voice, and it is a POST the page makes after the user
ticks the box - there is no implicit enrolment path anywhere in the server.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from ...registry import SpeakerRegistry
from ..deps import settings

router = APIRouter()


def _registry(request: Request) -> SpeakerRegistry:
    return SpeakerRegistry(settings(request).registry_path)


def _public_speaker(row: dict[str, Any], centroids: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "created_at": row.get("created_at"),
        "consent_scope": row.get("consent_scope"),
        "consented_at": row.get("consented_at"),
        "revoked_at": row.get("revoked_at"),
        "centroids": [
            {"id": c["id"], "status": c["status"], "created_at": c.get("created_at"),
             "source_file": c.get("source_file"), "score": c.get("score")}
            for c in centroids
        ],
    }


@router.get("")
def list_speakers(request: Request, include_revoked: bool = False):
    reg = _registry(request)
    try:
        out = []
        for row in reg.list_speakers(include_revoked):
            centroids = reg.list_centroids(row["id"], "active")
            pending = reg.list_centroids(row["id"], "pending")
            out.append(_public_speaker(row, centroids + pending))
        return {"speakers": out, "path": str(reg.path)}
    finally:
        reg.close()


@router.post("")
def enrol(request: Request, body: dict[str, Any] = Body(...)):
    """Enrol a voice from a file on this machine.

    Segmentation is out of scope here: the sample must already be one person
    speaking. The CLI's `enroll` has the same contract; the UI says so next to
    the input rather than guessing.
    """
    name = (body.get("name") or "").strip()
    audio = (body.get("audio") or "").strip()
    start = float(body.get("start") or 0.0)
    seconds = float(body.get("seconds") or 0.0)
    if not name:
        raise HTTPException(400, "`name` is required")
    if not audio:
        raise HTTPException(400, "`audio` must be a path to a sample file")
    path = Path(audio).expanduser()
    if not path.is_file():
        raise HTTPException(404, f"no such file: {path}")

    from ... import audio as audio_mod
    from ...config import AudioConfig, PipelineConfig
    from ...embedding import SpeakerEmbedder

    st = settings(request)
    cfg = PipelineConfig()
    try:
        arr = audio_mod.decode(path, AudioConfig(), start or None,
                               (start + seconds) if seconds else None)
    except audio_mod.AudioError as exc:
        raise HTTPException(422, str(exc)) from None
    vec = SpeakerEmbedder(cfg.diarization.device, _token_or_none(cfg)).embed_audio(
        arr, AudioConfig().sample_rate)
    if vec is None:
        raise HTTPException(422, "could not compute an embedding for this sample "
                                 "(embedding model unavailable, or the clip is silent)")

    reg = _registry(request)
    try:
        sid = reg.add_speaker(name, vec, consent_scope=body.get("consent") or "persistent",
                              source_file=str(path))
        row = reg.get_speaker(sid)
        return {"speaker": _public_speaker(row, reg.list_centroids(sid, "active")),
                "registry": st.registry_path}
    finally:
        reg.close()


def _token_or_none(cfg) -> str | None:
    """Enrolment needs the gated embedding model; report its absence as 422."""
    try:
        return cfg.resolve_hf_token()
    except RuntimeError:
        return None


@router.post("/{speaker_id}/rename")
def rename(request: Request, speaker_id: str, body: dict[str, Any] = Body(...)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "`name` is required")
    reg = _registry(request)
    try:
        if not reg.get_speaker(speaker_id):
            raise HTTPException(404, f"unknown speaker: {speaker_id}")
        reg.rename_speaker(speaker_id, name)
        return {"speaker": _public_speaker(reg.get_speaker(speaker_id),
                                           reg.list_centroids(speaker_id, "active"))}
    finally:
        reg.close()


@router.post("/{speaker_id}/revoke")
def revoke(request: Request, speaker_id: str):
    """Stop matching this voice without destroying the record."""
    reg = _registry(request)
    try:
        if not reg.get_speaker(speaker_id):
            raise HTTPException(404, f"unknown speaker: {speaker_id}")
        reg.revoke_speaker(speaker_id)
        return {"revoked": speaker_id}
    finally:
        reg.close()


@router.delete("/{speaker_id}")
def delete(request: Request, speaker_id: str, confirm: bool = False):
    """Hard delete. Requires `?confirm=true` - this is biometric data."""
    if not confirm:
        raise HTTPException(400, "pass confirm=true; this deletes a stored voice print")
    reg = _registry(request)
    try:
        if not reg.get_speaker(speaker_id):
            raise HTTPException(404, f"unknown speaker: {speaker_id}")
        reg.delete_speaker(speaker_id)
        return {"deleted": speaker_id}
    finally:
        reg.close()


@router.get("/{speaker_id}/sample")
def sample(request: Request, speaker_id: str):
    """Not available: samples were never stored, only vectors.

    Returned as an explicit 404 with an explanatory body rather than silently
    empty audio, so the page can tell the user why there is no play button on
    a registry entry that durably holds only a 256-float vector.
    """
    reg = _registry(request)
    try:
        row = reg.get_speaker(speaker_id)
    finally:
        reg.close()
    if not row:
        raise HTTPException(404, f"unknown speaker: {speaker_id}")
    return Response(status_code=404, content=(
        "This registry stores embeddings only, not audio. "
        "Re-listen through a transcription that contains this speaker."))
