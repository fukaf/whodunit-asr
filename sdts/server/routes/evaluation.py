"""`/api/eval`: DER/WER against a human-corrected reference."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from ...evaluate import evaluate
from ...labels import load_result
from ..deps import get_job, manager

router = APIRouter()


@router.post("")
def evaluate_job(request: Request, body: dict[str, Any] = Body(...)):
    job_id = body.get("job_id")
    gold = body.get("gold")
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(400, "`job_id` is required")
    if not isinstance(gold, dict) or not isinstance(gold.get("utterances"), list):
        raise HTTPException(400, "`gold` must be a result-shaped JSON object")

    job = get_job(request, job_id)
    custom = body.get("result_path")
    if custom:
        root = manager(request).data_dir.resolve()
        path = Path(custom).expanduser().resolve()
        if path != root and root not in path.parents:
            raise HTTPException(400, "result_path escapes the workbench directory")
        if not path.is_file():
            raise HTTPException(404, "result file not found")
    else:
        if job.status != "done" or not job.result_path:
            raise HTTPException(409, "job has no result yet")
        path = Path(job.result_path)

    try:
        hypothesis = load_result(path)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(422, f"could not read the hypothesis: {exc}") from None
    language = body.get("language") or hypothesis.get("language")
    report = evaluate(gold, hypothesis, language=language)
    report.update({
        "job_id": job.id,
        "result_source": str(path),
        "language": language,
    })
    return report


@router.get("/gold-template")
def gold_template():
    """A minimal gold file the UI can offer as a download."""
    return {
        "language": "ja",
        "utterances": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.5,
             "text": "ここに正しい書き起こしを入力"},
            {"speaker": "SPEAKER_01", "start": 2.5, "end": 4.0, "text": "次の発話"},
        ],
    }
